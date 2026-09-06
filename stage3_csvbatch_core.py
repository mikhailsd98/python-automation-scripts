# -*- coding: utf-8 -*-
"""Stage 3 core wrapper.

This module re-exports and reuses the *exact* Tab3 (csvbatch) logic from
`app_parallel_overlay_streamlit.py`, without inventing new behavior.

Why this exists:
- `app_parallel_overlay_streamlit.py` is now safe to import (UI is in main()).
- We want to render only the Tab3 UI inside the unified app.

The functions here are intentionally thin wrappers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

import app_parallel_overlay_streamlit as src

DEFAULT_STAGE3_SITE_ORDER = [
    "SpaceOfMuse.com",
    "NestingMuse.com",
    "glowuproutine.com",
    "sweethomecookery.com",
]

# ---- Download bytes session cache (unified app) ----
#
# Streamlit reruns on any widget interaction, including download buttons.
# Even if `@st.cache_data` is used, repeatedly computing cache keys and
# calling cached functions for every image can still cause a visible "flash"
# (and on some machines can race with browser download).
#
# Here we add an additional ultra-fast `st.session_state` cache layer for:
# - per-image PNG bytes
# - "download all" ZIP bytes
#
# This keeps post-load download clicks effectively constant-time.

_PNG_BYTES_SS_CACHE_KEY = "_unified_stage3_png_bytes_cache"
_ZIP_BYTES_SS_CACHE_KEY = "_unified_stage3_zip_bytes_cache"

# The normal Streamlit button protocol can only deliver a click after the
# active Python render completes.  Batch results can be large, so deletion uses
# this small localhost-only endpoint instead: its HTML button works immediately
# just like the download link and never requests a Streamlit rerun.
_RESULT_DELETE_SERVER_LOCK = threading.Lock()
_RESULT_DELETE_SERVER: ThreadingHTTPServer | None = None
_RESULT_DELETE_SERVER_THREAD: threading.Thread | None = None
_RESULT_DELETE_SERVER_TOKEN: str | None = None
_RESULT_DELETE_ALLOWED_ROOTS: set[str] = set()


class _ResultDeleteRequestHandler(BaseHTTPRequestHandler):
    """Delete only registered image files under a selected Batch results dir."""

    server_version = "Stage3Delete/1.0"

    def log_message(self, _format: str, *_args) -> None:
        # Do not pollute the Streamlit terminal for ordinary delete clicks.
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # The button lives in a Streamlit component iframe on another origin.
        # The unguessable per-process token below prevents arbitrary local pages
        # from using this endpoint.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802 - method name required by BaseHTTPRequestHandler
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - method name required by BaseHTTPRequestHandler
        if self.path.split("?", 1)[0] != "/delete-result-image":
            self._send_json(404, {"ok": False, "error": "Unknown endpoint"})
            return

        try:
            content_length = int(self.headers.get("Content-Length") or 0)
            if content_length <= 0 or content_length > 16_384:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Invalid request")
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"Invalid request: {e}"})
            return

        token = str(payload.get("token") or "")
        target_raw = str(payload.get("path") or "")
        expected_token = _RESULT_DELETE_SERVER_TOKEN or ""
        if not expected_token or not hmac.compare_digest(token, expected_token):
            self._send_json(403, {"ok": False, "error": "Not authorized"})
            return

        try:
            target = Path(target_raw).expanduser().resolve()
            if target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise ValueError("Only result image files can be deleted")

            with _RESULT_DELETE_SERVER_LOCK:
                allowed_roots = [Path(p) for p in _RESULT_DELETE_ALLOWED_ROOTS]

            if not any(_is_path_inside(target, root) for root in allowed_roots):
                raise PermissionError("File is outside the selected results folder")
            if not target.is_file():
                raise FileNotFoundError(str(target))

            target.unlink()
            self._send_json(200, {"ok": True, "name": target.name})
        except PermissionError as e:
            self._send_json(403, {"ok": False, "error": str(e)})
        except FileNotFoundError:
            self._send_json(404, {"ok": False, "error": "File was already removed"})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})


def _is_path_inside(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _ensure_result_delete_endpoint(result_root: str) -> tuple[str, str]:
    """Start the localhost action server once and register one safe root."""

    global _RESULT_DELETE_SERVER, _RESULT_DELETE_SERVER_THREAD, _RESULT_DELETE_SERVER_TOKEN

    root = Path(str(result_root)).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("Current results folder does not exist")

    with _RESULT_DELETE_SERVER_LOCK:
        _RESULT_DELETE_ALLOWED_ROOTS.add(os.path.normcase(str(root)))
        if _RESULT_DELETE_SERVER is None:
            import secrets

            _RESULT_DELETE_SERVER_TOKEN = secrets.token_urlsafe(32)
            _RESULT_DELETE_SERVER = ThreadingHTTPServer(
                ("127.0.0.1", 0), _ResultDeleteRequestHandler
            )
            _RESULT_DELETE_SERVER.daemon_threads = True
            _RESULT_DELETE_SERVER_THREAD = threading.Thread(
                target=_RESULT_DELETE_SERVER.serve_forever,
                name="stage3-result-delete",
                daemon=True,
            )
            _RESULT_DELETE_SERVER_THREAD.start()

        host, port = _RESULT_DELETE_SERVER.server_address[:2]
        return f"http://{host}:{port}/delete-result-image", str(_RESULT_DELETE_SERVER_TOKEN or "")


def _render_result_delete_button(*, result_root: str, image_path: str) -> None:
    """Render a no-rerun delete control beside an HTML download link."""

    endpoint, token = _ensure_result_delete_endpoint(result_root)
    js_payload = json.dumps(
        {"endpoint": endpoint, "token": token, "path": str(image_path)},
        ensure_ascii=False,
    ).replace("</", "<\\/")
    components.html(
        f"""
        <style>
          button {{
            width: 100%; box-sizing: border-box; padding: 0.45rem 0.35rem;
            border-radius: 0.5rem; border: 1px solid rgba(180, 35, 24, 0.45);
            background: #fff5f4; color: #a61b12; font-weight: 600; cursor: pointer;
          }}
          button:disabled {{ opacity: 0.72; cursor: default; }}
        </style>
        <button id="delete-result-image" type="button">🗑️ Удалить</button>
        <script>
          (() => {{
            const cfg = {js_payload};
            const button = document.getElementById('delete-result-image');
            button.addEventListener('click', async () => {{
              if (button.disabled) return;
              button.disabled = true;
              button.textContent = 'Удаляю…';
              try {{
                const response = await fetch(cfg.endpoint, {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ token: cfg.token, path: cfg.path }}),
                }});
                const body = await response.json().catch(() => ({{}}));
                if (!response.ok || !body.ok) throw new Error(body.error || 'Delete failed');
                button.textContent = 'Удалено';
                button.title = 'Файл удалён. Карточка исчезнет при следующем обычном обновлении результатов.';
              }} catch (error) {{
                button.disabled = false;
                button.textContent = 'Ошибка удаления';
                button.title = String(error && error.message ? error.message : error);
              }}
            }});
          }})();
        </script>
        """,
        height=52,
    )


def _get_ss_dict(key: str) -> dict:
    d = st.session_state.get(key)
    if not isinstance(d, dict):
        d = {}
        st.session_state[key] = d
    return d


# Patch `src` caching helpers with an additional session_state layer.
# Safe: only affects the current Python process (i.e., the unified app run).
try:
    _ORIG_READ_AS_PNG_BYTES_CACHED = src._read_as_png_bytes_cached
    _ORIG_BUILD_ZIP_CACHED = src._build_zip_cached

    def _read_as_png_bytes_session_cached(path: str, mtime: float) -> bytes:
        # Normalize mtime to milliseconds to avoid float representation jitter.
        try:
            mt_ms = int(float(mtime) * 1000)
        except Exception:
            mt_ms = 0
        cache = _get_ss_dict(_PNG_BYTES_SS_CACHE_KEY)
        k = f"{path}::{mt_ms}"
        got = cache.get(k)
        if isinstance(got, (bytes, bytearray)):
            return bytes(got)

        data = _ORIG_READ_AS_PNG_BYTES_CACHED(path, float(mtime))
        # Best-effort size guard: keep cache bounded.
        try:
            if len(cache) > 250:
                cache.clear()
        except Exception:
            pass
        cache[k] = data
        return data

    def _build_zip_session_cached(files: tuple[tuple[str, float], ...]) -> bytes:
        # Keep only the last built zip in memory (ZIPs can be large).
        try:
            sig = hash(files)
        except Exception:
            sig = None

        st0 = st.session_state.get(_ZIP_BYTES_SS_CACHE_KEY)
        if isinstance(st0, dict) and st0.get("sig") == sig and isinstance(st0.get("zip"), (bytes, bytearray)):
            return bytes(st0["zip"])  # type: ignore[index]

        zip_bytes = _ORIG_BUILD_ZIP_CACHED(files)
        st.session_state[_ZIP_BYTES_SS_CACHE_KEY] = {"sig": sig, "zip": zip_bytes}
        return zip_bytes

    # Apply patches
    src._read_as_png_bytes_cached = _read_as_png_bytes_session_cached  # type: ignore[assignment]
    src._build_zip_cached = _build_zip_session_cached  # type: ignore[assignment]
except Exception:
    # If upstream function names change, don't break Stage3 UI.
    pass


def _b64_data_url(data: bytes, mime: str) -> str:
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _download_link_html(*, label: str, data: bytes, file_name: str, mime: str, full_width: bool = True) -> None:
    """Render a download link as HTML (<a download>) so it does NOT trigger Streamlit reruns.

    This is crucial in the unified app: `st.download_button` triggers a rerun and can cause
    image-grid flashing and even broken downloads.

    Note: uses a data: URL (bytes embedded in page). This is fine for typical batches;
    if you attempt to download extremely large ZIPs, consider implementing a file-based
    download endpoint.
    """

    safe_label = html.escape(label or "Download")
    safe_name = html.escape(file_name or "download")
    href = _b64_data_url(data, mime)

    # Keep styling close to Streamlit primary button (best-effort).
    width_css = "width: 100%;" if full_width else ""
    components.html(
        f"""
        <div style="margin: 0.25rem 0; {width_css}">
          <a
            href="{href}"
            download="{safe_name}"
            style="
              display: inline-block;
              {width_css}
              box-sizing: border-box;
              text-align: center;
              padding: 0.45rem 0.75rem;
              border-radius: 0.5rem;
              border: 1px solid rgba(49, 51, 63, 0.2);
              background: rgb(255, 255, 255);
              color: rgb(49, 51, 63);
              text-decoration: none;
              font-weight: 600;
              cursor: pointer;
            "
          >{safe_label}</a>
        </div>
        """,
        height=52,
    )


def _render_results_block_unified(*, state_prefix: str, title: str = "Последние результаты") -> None:
    """Unified (no-rerun) results renderer.

    Strategy:
    - Reuse `src` logic for scanning results dir and for cached bytes builders.
    - Replace `st.download_button` with HTML `<a download>` links to avoid reruns.

    This eliminates the "flash" and makes repeated downloads reliable.
    """

    def _load_persisted_dirs() -> dict:
        try:
            p = Path("last_results_dirs.json")
            if p.exists():
                obj = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    return obj
        except Exception:
            return {}
        return {}

    def _get_persisted_recent(prefix: str) -> list[str]:
        data = _load_persisted_dirs()
        cur = data.get(str(prefix))
        if isinstance(cur, dict):
            rec = cur.get("recent")
            if isinstance(rec, list):
                out: list[str] = []
                for x in rec:
                    if isinstance(x, str) and x.strip():
                        out.append(x.strip())
                return out
        return []

    def _push_recent_dir(prefix: str, p: str) -> None:
        p = str(p or "").strip().strip('"').strip("'")
        if not p:
            return
        try:
            if not Path(p).exists():
                return
        except Exception:
            return

        data = _load_persisted_dirs()
        cur = data.get(str(prefix))
        if not isinstance(cur, dict):
            cur = {}
            data[str(prefix)] = cur

        rec = cur.get("recent")
        if not isinstance(rec, list):
            rec = []

        # normalize + de-dup
        out: list[str] = []
        for x in [p, *rec]:
            if isinstance(x, str) and x.strip() and x.strip() not in out:
                out.append(x.strip())
        cur["recent"] = out[:50]
        cur["selected"] = p

        try:
            Path("last_results_dirs.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _list_result_subdirs(root_dir: Path) -> list[Path]:
        """List immediate subdirectories that contain at least one image file."""
        try:
            if not root_dir.exists() or not root_dir.is_dir():
                return []

            exts = {".png", ".jpg", ".jpeg", ".webp"}

            def _has_images(d: Path) -> bool:
                try:
                    # Fast path: typical layout is images directly in the folder
                    for ext in exts:
                        if any(d.glob(f"*{ext}")):
                            return True
                    # Fallback: search nested, but stop early
                    for fp in d.rglob("*"):
                        try:
                            if fp.is_file() and fp.suffix.lower() in exts:
                                return True
                        except Exception:
                            continue
                except Exception:
                    return False
                return False

            subdirs = [p for p in root_dir.iterdir() if p.is_dir()]
            subdirs = [d for d in subdirs if _has_images(d)]
            subdirs.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
            return subdirs
        except Exception:
            return []

    def _build_results_dir_options(*, prefix: str, current_dir: str) -> tuple[Path | None, list[str]]:
        """Return (root_selected, options) for results dir selection."""
        recent = _get_persisted_recent(prefix)

        userprofile = os.environ.get("USERPROFILE") or ""
        desktop_guess = Path(userprofile) / "Desktop" / "generate automation" / "generated_images"

        roots: list[Path] = []
        # Most common location
        roots.append(desktop_guess)
        # Local workspace fallback
        try:
            roots.append(Path("generated_images").resolve())
        except Exception:
            pass
        # If we already have a directory, include its parent to allow switching between siblings
        try:
            if current_dir:
                roots.append(Path(str(current_dir)).resolve().parent)
        except Exception:
            pass

        root_selected: Path | None = None
        subdir_paths: list[Path] = []
        for r in roots:
            subs = _list_result_subdirs(r)
            if subs:
                root_selected = r
                subdir_paths = subs
                break

        opts: list[str] = []
        for x in [current_dir, *[str(p) for p in subdir_paths], *recent]:
            if isinstance(x, str) and x.strip() and x.strip() not in opts:
                # Only show existing directories
                try:
                    if Path(x.strip()).exists() and Path(x.strip()).is_dir():
                        opts.append(x.strip())
                except Exception:
                    continue

        return root_selected, opts

    show_key = f"{state_prefix}_unified_show_results"

    # Hard reset on first render in this session: do not auto-load images.
    init_key = f"{state_prefix}_unified_show_init_done"
    if not bool(st.session_state.get(init_key)):
        st.session_state[show_key] = False
        st.session_state[init_key] = True

    st.session_state.setdefault(show_key, False)

    out_dir = st.session_state.get(f"{state_prefix}_last_base_dir")

    st.markdown("---")
    st.subheader(title)

    # Show/hide gate: do not load/render images until user explicitly asks.
    col_show, col_hide = st.columns([1, 1])
    if not bool(st.session_state.get(show_key)):
        if col_show.button("Показать результаты", key=f"{show_key}_btn_show"):
            st.session_state[show_key] = True
            try:
                st.rerun()
            except Exception:
                pass
        st.caption("Результаты скрыты: картинки и ZIP не загружаются до нажатия 'Показать результаты'.")
    else:
        if col_hide.button("Скрыть результаты", key=f"{show_key}_btn_hide"):
            st.session_state[show_key] = False
            try:
                st.rerun()
            except Exception:
                pass

    # Always show the currently selected output dir (if any).
    if out_dir:
        st.code(str(out_dir))
    else:
        st.caption("Папка результатов пока не выбрана.")

    # --- Folder switching (safe; rerun only on apply) ---
    with st.expander("Сменить папку результатов", expanded=False):
        # If `out_dir` is not set yet, fallback to the most recent dir (if any)
        # so the UI doesn't show "None".
        cur_dir = str(out_dir) if out_dir else ""

        root_selected, opts = _build_results_dir_options(prefix=str(state_prefix), current_dir=str(cur_dir))

        if not opts:
            st.info(
                "Не найдено папок с результатами для выбора. "
                "Проверьте, что есть подпапки с картинками в generated_images, "
                "или вставьте путь вручную."
            )

        pick_key = f"{state_prefix}_unified_pick_results_dir"
        manual_key = f"{state_prefix}_unified_results_dir_manual"

        # Streamlit widget nuance: when a widget has a `key=...`, its displayed value
        # is controlled by `st.session_state[key]` after the first render.
        # So if we want the selectbox choice to actually affect the path input,
        # we must explicitly sync it on selectbox changes.
        if manual_key not in st.session_state:
            # Initialize manual input from current dir; will be synced to selectbox below.
            st.session_state[manual_key] = str(cur_dir)

        def _sync_manual_to_picked() -> None:
            try:
                st.session_state[manual_key] = str(st.session_state.get(pick_key) or "")
            except Exception:
                pass

        picked = ""
        if opts:
            # Ensure current selectbox value is valid after options recompute
            try:
                cur_pick = st.session_state.get(pick_key)
                if cur_pick and cur_pick not in opts:
                    st.session_state[pick_key] = opts[0]
            except Exception:
                pass

            picked = st.selectbox(
                "Папка результатов" + (f" (из {root_selected})" if root_selected else ""),
                options=opts,
                index=0,
                key=pick_key,
                on_change=_sync_manual_to_picked,
            )

            # First render helper: if user hasn't typed anything yet, prefer picked value.
            try:
                if st.session_state.get(manual_key) == str(cur_dir) and picked:
                    st.session_state[manual_key] = str(picked)
            except Exception:
                pass
        else:
            st.caption("Список папок пуст — используйте ручной путь ниже.")

        # NOTE: value is controlled via `manual_key` in session_state.
        new_dir2 = st.text_input(
            "Или вставьте путь вручную",
            key=manual_key,
        )

        if st.button("Применить", key=f"{state_prefix}_unified_apply_results_dir"):
            # Prefer manual input if user edited it; otherwise allow using the selectbox choice.
            nd = (new_dir2 or "").strip().strip('"').strip("'")
            if not nd:
                nd = str(picked or "").strip().strip('"').strip("'")
            if nd and Path(nd).exists() and Path(nd).is_dir():
                prev_dir = str(st.session_state.get(f"{state_prefix}_last_base_dir") or "")
                st.session_state[f"{state_prefix}_last_base_dir"] = nd
                st.session_state[f"{state_prefix}_last_items"] = []
                st.session_state[f"{state_prefix}_last_errors"] = []

                # Persist in recent list for the next session.
                _push_recent_dir(str(state_prefix), nd)

                # If directory changed, require explicit "Показать результаты" again.
                if prev_dir.strip() != str(nd).strip():
                    st.session_state[show_key] = False

                try:
                    st.rerun()
                except Exception:
                    pass
            else:
                st.error("Папка не найдена или путь некорректен")

    # Do not load/scan anything until user clicks "Показать результаты".
    if not bool(st.session_state.get(show_key)):
        return

    if not out_dir:
        st.warning("Сначала выберите/получите папку результатов (out_dir пустой).")
        return

    # Items are continuously synced into session_state by the Stage3 UI.
    # If empty, rescan the directory once (best effort) so results survive refresh.
    items = st.session_state.get(f"{state_prefix}_last_items") or []

    # A finished background run can retain an in-memory reference to a file
    # that the user has just deleted.  Never render such stale entries again;
    # the disk remains the source of truth for Batch results.
    try:
        existing_items = [
            item
            for item in items
            if len(item) >= 3 and Path(str(item[2])).expanduser().is_file()
        ]
        if len(existing_items) != len(items):
            items = existing_items
            st.session_state[f"{state_prefix}_last_items"] = items
    except Exception:
        pass

    if not items:
        try:
            exts = {".png", ".jpg", ".jpeg", ".webp"}
            pp = Path(str(out_dir))
            if pp.exists() and pp.is_dir():
                files = [p for p in pp.rglob("*") if p.is_file() and p.suffix.lower() in exts]
                files.sort(key=lambda p: (p.stat().st_mtime if p.exists() else 0, p.name))
                if files:
                    items = [("file", "", str(p)) for p in files]
                    st.session_state[f"{state_prefix}_last_items"] = items
        except Exception:
            pass

    if not items:
        if state_prefix == "csvbatch" and (st.session_state.get("csvbatch_running") is True):
            st.info("Пока не собраны картинки (в процессе или ещё пишутся).")
        else:
            st.warning("Нет сохранённых картинок для отображения")
        return

    # 1) Download all ZIP
    try:
        files_for_zip: list[tuple[str, float]] = []
        for _pfx, _sw, p in items:
            try:
                mt = float(src.os.path.getmtime(p))
            except Exception:
                mt = 0.0
            files_for_zip.append((str(p), mt))

        zip_bytes = src._build_zip_cached(tuple(files_for_zip))
        zip_name = src._safe_filename(f"{Path(str(out_dir)).name}.zip")
        if not zip_name.lower().endswith(".zip"):
            zip_name = zip_name + ".zip"

        _download_link_html(
            label="Скачать всё (ZIP)",
            data=zip_bytes,
            file_name=zip_name,
            mime="application/zip",
            full_width=True,
        )
    except Exception as e:
        st.caption(f"ZIP build error: {e}")

    # 2) Grid + per-image download links
    grid_cols = 4
    cols = st.columns(grid_cols)
    for i, (pfx, sw, p) in enumerate(items):
        with cols[i % grid_cols]:
            extra = f" | suffix={sw}" if sw else ""
            st.image(p, caption=f"{pfx}{extra} | {src.os.path.basename(p)}", use_container_width=True)

            download_col, delete_col = st.columns(2)

            try:
                # Match original naming behavior
                _, ext0 = src.os.path.splitext(p)
                ext = (ext0 or "").lstrip(".").lower()
                try:
                    mt = float(src.os.path.getmtime(p))
                except Exception:
                    mt = 0.0

                if ext == "png":
                    fname = src.os.path.basename(p)
                    mime = "image/png"
                    data = src._read_file_bytes_cached(str(p), mt)
                else:
                    stem = src.os.path.splitext(src.os.path.basename(p))[0]
                    fname = src._safe_filename(f"{stem}.png")
                    mime = "image/png"
                    data = src._read_as_png_bytes_cached(str(p), mt)

                with download_col:
                    _download_link_html(
                        label="Скачать",
                        data=data,
                        file_name=fname,
                        mime=mime,
                        full_width=True,
                    )
            except Exception as e:
                st.caption(f"Download error: {e}")

            with delete_col:
                # This deliberately is not an st.button: Streamlit sends a
                # widget click to Python only after the current script run has
                # finished.  The lightweight local endpoint gives this button
                # the same immediate behaviour as the download link above.
                _render_result_delete_button(
                    result_root=str(out_dir),
                    image_path=str(p),
                )


def _render_results_block_scoped(*, state_prefix: str, title: str = "Последние результаты") -> None:
    """Render results block with best-effort rerun scoping.

    In the unified multi-tab app, clicking `st.download_button` inside the results grid
    triggers a rerun. On some Streamlit versions this causes a noticeable UI "flash" and
    can even break the download if the app re-renders again while the browser is still
    fetching bytes.

    Prefer the unified no-rerun renderer when possible.
    """

    _render_results_block_unified(state_prefix=state_prefix, title=title)


# Wrap into a fragment when available (older Streamlit versions don't have it).
try:
    if hasattr(st, "fragment"):
        _render_results_block_scoped = st.fragment(_render_results_block_scoped)  # type: ignore[assignment]
except Exception:
    pass


def start_batch_run(*, run_id: str, jobs: list[dict], cfg: dict) -> None:
    """Programmatic start (exactly calls original `_csvbatch_start`)."""
    src._csvbatch_start(run_id=run_id, jobs=jobs, cfg=cfg)


def _site_default_from_state() -> str:
    return str(
        (st.session_state.get("csvbatch_default_site_name") or "").strip()
        or getattr(src, "DEFAULT_SITE_NAME", "SpaceOfMuse.com")
    )


def _default_site_for_autosave_index(idx: int) -> str:
    if 0 <= int(idx) < len(DEFAULT_STAGE3_SITE_ORDER):
        return DEFAULT_STAGE3_SITE_ORDER[int(idx)]
    return _site_default_from_state()


def _site_map_from_state() -> dict[str, str]:
    m = st.session_state.get("csvbatch_site_name_by_autosave")
    if not isinstance(m, dict):
        m = {}
        st.session_state["csvbatch_site_name_by_autosave"] = m
    # Normalize keys/values to strings
    out: dict[str, str] = {}
    for k, v in m.items():
        if not isinstance(k, str):
            continue
        if not isinstance(v, str):
            v = str(v)
        out[k] = v
    # Keep normalized dict in session_state
    st.session_state["csvbatch_site_name_by_autosave"] = out
    return out


def _widget_key_for_path(prefix: str, pth: str) -> str:
    # Use stable hash so keys are safe for Streamlit and consistent across reruns.
    h = hashlib.sha1(str(pth).encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"{prefix}_{h}"


def build_jobs_from_state() -> list[dict]:
    """Build `jobs` exactly like Tab3 does, but from current `st.session_state`.

    This is used by the unified pipeline button.

    Important nuance (why Stage3 sometimes "skips"):
    Streamlit's `st.data_editor(..., key=...)` stores its state differently across
    Streamlit versions. Sometimes `st.session_state[key]` is the full edited table
    (list[dict]), but often it is a *delta* structure like::

        {
          'edited_rows': {0: {'overlay_text': '...'}},
          'added_rows': [],
          'deleted_rows': []
        }

    If we only handle the "full table" case, we can incorrectly think there are no
    overlay texts and skip Stage3.
    """

    autosaves = st.session_state.get("csvbatch_autosave_multi") or []
    default_site = _site_default_from_state()
    site_map = _site_map_from_state()

    merged_results: list[dict] = []
    for pth in autosaves:
        try:
            res_list, _payload = src._load_unif_text_results(str(pth))
            # Resolve site name for this autosave
            site_name = str((site_map.get(str(pth)) or "").strip() or default_site)
            for rr in res_list:
                if isinstance(rr, dict) and not rr.get("error"):
                    rr2 = dict(rr)
                    rr2["_autosave_path"] = str(pth)
                    rr2["_site_name"] = site_name
                    merged_results.append(rr2)
        except Exception:
            continue

    limit_posts = int(st.session_state.get("csvbatch_limit_posts") or 0)
    if limit_posts > 0:
        merged_results = merged_results[:limit_posts]

    posts: list[dict] = []
    for rr in merged_results:
        posts.append(
            {
                "_rr": rr,
                "post_text": rr.get("text") or "",
                "title": (rr.get("title") or "").strip(),
                "site_name": (rr.get("_site_name") or "").strip() or default_site,
            }
        )

    default_portions = int(st.session_state.get("csvbatch_default_portions") or 1)

    def _compute_editor_rows_base() -> list[dict]:
        rows0: list[dict] = []
        for idx, p in enumerate(posts):
            title = (p.get("title") or "").strip()
            if not title:
                # Keep the same title-guessing behavior as the original tab.
                title = src._guess_post_title(str(p.get("post_text") or ""))
            rows0.append(
                {
                    "idx": idx,
                    "title": title,
                    "overlay_text": "",
                    "portions": int(default_portions),
                    "post_link": "",
                }
            )
        return rows0

    edited = st.session_state.get("csvbatch_editor")

    # Normalize edited table to list[dict]
    rows: list[dict] = []
    if edited is None:
        rows = []
    elif isinstance(edited, list):
        rows = [r for r in edited if isinstance(r, dict)]
    elif hasattr(edited, "to_dict"):
        try:
            rows = list(edited.to_dict("records"))
        except Exception:
            rows = []
    elif isinstance(edited, dict):
        # Some Streamlit versions store data_editor state as a dict.
        if isinstance(edited.get("data"), list):
            rows = [r for r in edited.get("data") if isinstance(r, dict)]
        elif isinstance(edited.get("records"), list):
            rows = [r for r in edited.get("records") if isinstance(r, dict)]
        elif isinstance(edited.get("edited_rows"), dict):
            # Delta format: apply edits on top of a base table.
            base_rows = st.session_state.get("csvbatch_editor_base_rows")
            # `csvbatch_editor_base_rows` can be stale if the user changed autosave selection,
            # limit_posts, or Streamlit kept the old editor state across reruns.
            # If base_rows doesn't match the *current* posts table, recompute it.
            base_ok = isinstance(base_rows, list) and all(isinstance(r, dict) for r in base_rows)
            if base_ok:
                try:
                    if len(base_rows) != len(posts):
                        base_ok = False
                    else:
                        # Ensure idx column is consistent (0..N-1)
                        for pos, rr0 in enumerate(base_rows):
                            try:
                                if int(rr0.get("idx", pos)) != pos:
                                    base_ok = False
                                    break
                            except Exception:
                                base_ok = False
                                break
                except Exception:
                    base_ok = False

            if not base_ok:
                base_rows = _compute_editor_rows_base()

            # Copy, then apply: deleted_rows, edited_rows, added_rows.
            rows = [dict(r) for r in base_rows]

            deleted = edited.get("deleted_rows")
            if isinstance(deleted, list):
                for ridx in sorted([x for x in deleted if isinstance(x, int)], reverse=True):
                    if 0 <= ridx < len(rows):
                        rows.pop(ridx)

            edited_rows = edited.get("edited_rows") or {}
            if isinstance(edited_rows, dict):
                for ridx, changes in edited_rows.items():
                    try:
                        irow = int(ridx)
                    except Exception:
                        continue
                    if not (0 <= irow < len(rows)):
                        continue
                    if isinstance(changes, dict):
                        rows[irow].update(changes)

            added = edited.get("added_rows")
            if isinstance(added, list):
                for r in added:
                    if isinstance(r, dict):
                        rows.append(r)
        else:
            rows = []
    else:
        rows = []

    ctx_chars = int(st.session_state.get("csvbatch_ctx_chars") or 1200)

    jobs: list[dict] = []
    for row in rows:
        try:
            i = int(row.get("idx", 0))
            overlay_text = (row.get("overlay_text") or "").strip()
            portions = int(row.get("portions") or 1)
        except Exception:
            continue

        if not overlay_text:
            continue
        if i < 0 or i >= len(posts):
            continue

        rr = posts[i].get("_rr")
        if isinstance(rr, dict):
            ctx = src._unif_text_item_to_context(rr, target_chars=ctx_chars)
        else:
            ctx = src._extract_post_context(posts[i].get("post_text") or "", target_chars=ctx_chars)

        jobs.append(
            {
                "post_idx": i,
                "title": row.get("title") or f"post{i+1}",
                "overlay_text": overlay_text,
                "portions": max(1, portions),
                "context": ctx,
                "site_name": posts[i].get("site_name") or default_site,
            }
        )

    return jobs


def request_stop(*, run_id: str) -> None:
    src._csvbatch_request_stop(str(run_id))


def get_state(*, run_id: str) -> dict:
    return src._csvbatch_get_state(str(run_id))


def _maybe_prebuild_download_cache_for_csvbatch() -> None:
    """Best-effort prebuild ZIP bytes once results are visible.

    This prevents even the first click on "Download all (ZIP)" from triggering
    a noticeable recompute.

    Safe: if anything fails, it's silently ignored.
    """

    try:
        # Respect the "show results" gate: do not prebuild anything until user asked.
        if not bool(st.session_state.get("csvbatch_unified_show_results")):
            return

        items = st.session_state.get("csvbatch_last_items") or []
        out_dir = st.session_state.get("csvbatch_last_base_dir")
        if not items or not out_dir:
            return

        files_for_zip: list[tuple[str, float]] = []
        for _pfx, _sw, p in items:
            try:
                mt = float(src.os.path.getmtime(p))
            except Exception:
                mt = 0.0
            files_for_zip.append((str(p), mt))

        # This will hit the session cache if already built.
        src._build_zip_cached(tuple(files_for_zip))
    except Exception:
        return


def render_stage3_tab() -> None:
    """Render the original Tab3 UI.

    Implementation approach:
    - We call the original code path by recreating *only* the Tab3 part.
    - To avoid rewriting complex logic, we keep the same helper functions and state keys.

    NOTE: This function is not called by the original script; it is for the unified app.
    """

    # We reuse the same code style/keys as original.
    # The simplest safe way: execute the original Tab3 block by calling a helper we add below.
    _render_tab3_block_exact()

    # After rendering, if results are visible in session_state, prebuild download bytes.
    _maybe_prebuild_download_cache_for_csvbatch()


def _render_tab3_block_exact() -> None:
    """Exact Tab3 block extracted as a callable.

    This is a *verbatim* copy of the code inside `with tab3:` in the original file,
    with only one change: indentation.
    """

    # Settings (were in sidebar in the original app). We expose them here so user can edit.
    with st.sidebar:
        st.markdown("### Stage 3 settings")
        _aistudio_new_chat = "https://aistudio.google.com/prompts/new_chat?model=gemini-2.5-flash-image"
        _default_url_idx = 0
        try:
            if _aistudio_new_chat in (src.DEFAULT_URLS or []):
                _default_url_idx = src.DEFAULT_URLS.index(_aistudio_new_chat)
        except Exception:
            _default_url_idx = 0
        st.selectbox("URL интерфейса", src.DEFAULT_URLS, index=_default_url_idx, key="csvbatch_url")
        st.selectbox("Модель", ["Быстрая", "Думающая"], index=0, key="csvbatch_model")
        st.checkbox("Headless", value=False, key="csvbatch_headless")
        st.number_input("Параллельно окон", min_value=1, max_value=24, value=int(st.session_state.get("csvbatch_num_windows", 8)), step=1, key="csvbatch_num_windows")
        st.text_input("База профиля (user-data-dir)", value=str(st.session_state.get("csvbatch_user_data_dir", src.os.path.abspath(".chrome_automation_profile"))), key="csvbatch_user_data_dir")
        st.text_input("Номера профилей", value=str(st.session_state.get("csvbatch_profile_numbers", "21,22,23,24,25,26,27,28")), key="csvbatch_profile_numbers")
        st.text_input("Путь к chrome.exe", value=str(st.session_state.get("csvbatch_executable_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")), key="csvbatch_executable_path")
        st.number_input("Пауза между окнами (сек)", min_value=0, max_value=60, value=int(st.session_state.get("csvbatch_launch_stagger_s", 1)), step=1, key="csvbatch_launch_stagger_s")
        st.number_input("Page default timeout (ms)", min_value=1000, max_value=300000, value=int(st.session_state.get("csvbatch_page_default_timeout_ms", 30000)), step=1000, key="csvbatch_page_default_timeout_ms")
        st.number_input("Input ready timeout (ms)", min_value=1000, max_value=300000, value=int(st.session_state.get("csvbatch_input_ready_timeout_ms", 60000)), step=1000, key="csvbatch_input_ready_timeout_ms")
        st.number_input("Attach timeout (ms)", min_value=1000, max_value=300000, value=int(st.session_state.get("csvbatch_attach_timeout_ms", 5000)), step=1000, key="csvbatch_attach_timeout_ms")
        st.number_input("Gen timeout (s)", min_value=10, max_value=3600, value=int(st.session_state.get("csvbatch_gen_timeout_s", 100)), step=10, key="csvbatch_gen_timeout_s")
        st.number_input("Gen retry timeout (s)", min_value=0, max_value=3600, value=int(st.session_state.get("csvbatch_gen_retry_timeout_s", 45)), step=5, key="csvbatch_gen_retry_timeout_s")
        st.number_input(
            "Offline wait (s)",
            min_value=0,
            max_value=3600,
            value=int(st.session_state.get("csvbatch_offline_wait_timeout_s", 90)),
            step=10,
            key="csvbatch_offline_wait_timeout_s",
            help="Если интернет пропал в момент открытия окна/навигации к Gemini — не закрывать окно сразу, а подождать пока интернет появится.",
        )
        st.number_input("Max session attempts", min_value=1, max_value=10, value=int(st.session_state.get("csvbatch_max_session_attempts", 2)), step=1, key="csvbatch_max_session_attempts")

    # BEGIN copy from app_parallel_overlay_streamlit.py (Tab3)
    st.subheader("Batch: take context from Autosave JSON (Tab0), enter overlay texts here")
    st.caption(
        "Источник - autosave JSON файлы из app_unified_streamlit (Tab0: Article Texts). "
        "Вы выбираете пост и вводите 'текст оверлея' и число порций. "
        "Порции = сколько раз параллельной генерации (то же что и в сайдбаре)."
    )

    base_image_path = ""

    st.markdown("#### Источник: Autosave JSON (Tab0 из app_unified_streamlit)")

    autosave_files = src._list_unif_text_autosaves()

    # Defaults (only on first app open): pick first autosaves + set site names.
    if autosave_files and "csvbatch_autosave_multi" not in st.session_state:
        picked_init = list(autosave_files[: len(DEFAULT_STAGE3_SITE_ORDER)])
        st.session_state["csvbatch_autosave_multi"] = picked_init
        st.session_state["csvbatch_autosave_main"] = picked_init[0] if picked_init else None
        st.session_state.setdefault("csvbatch_default_site_name", "SpaceOfMuse.com")
        if "csvbatch_site_name_by_autosave" not in st.session_state:
            m0: dict[str, str] = {}
            for idx, pth in enumerate(picked_init):
                m0[str(pth)] = _default_site_for_autosave_index(idx)
            st.session_state["csvbatch_site_name_by_autosave"] = m0

    last_ptr = src.Path("autosaves") / "app_unified_streamlit" / "_last_tab0_article_texts_autosave.json"
    last_path = None
    try:
        if last_ptr.exists():
            last_obj = src.json.loads(last_ptr.read_text(encoding="utf-8"))
            last_path = (last_obj or {}).get("path") or (last_obj or {}).get("last_autosave")
    except Exception:
        last_path = None

    cols_src = st.columns([1, 3, 1])
    with cols_src[0]:
        if st.button("Load LAST", key="csvbatch_load_last_autosave"):
            if last_path and src.Path(last_path).exists():
                st.session_state["csvbatch_autosave_main"] = str(last_path)
                st.session_state["csvbatch_autosave_multi"] = [str(last_path)]
                src._ui_rerun()
            else:
                st.warning("No last autosave pointer found.")

    with cols_src[1]:
        if autosave_files:
            default_idx = 0
            if last_path and last_path in autosave_files:
                default_idx = autosave_files.index(last_path)
            st.selectbox(
                "Основной autosave (для превью/по умолчанию)",
                options=autosave_files,
                index=default_idx,
                key="csvbatch_autosave_main",
            )
        else:
            st.caption("Autosave файлы не найдены: autosaves/app_unified_streamlit/tab0_article_texts")

    with cols_src[2]:
        if st.button("Обновить список", key="csvbatch_refresh_autosaves"):
            src._ui_rerun()

    picked_multi = st.multiselect(
        "Выберите autosave JSON файлы (можно несколько - будут объединены)",
        options=autosave_files,
        default=list(
            st.session_state.get("csvbatch_autosave_multi")
            or ([st.session_state.get("csvbatch_autosave_main")] if st.session_state.get("csvbatch_autosave_main") else [])
            or []
        ),
        key="csvbatch_autosave_multi",
    )

    # --- Per-autosave site name mapping ---
    st.markdown("#### Site name for each autosave")
    st.caption(
        "Use this when you selected multiple autosave JSON files from different sites. "
        "The value is inserted into the prompt in the phrase 'I have a ... website called ...'."
    )

    default_site = st.text_input(
        "Default site name (если для файла не задано)",
        value=_site_default_from_state(),
        key="csvbatch_default_site_name",
    )

    site_map = _site_map_from_state()
    for idx, pth in enumerate(list(picked_multi or [])):
        key0 = _widget_key_for_path("csvbatch_site", str(pth))
        if not str(site_map.get(str(pth)) or "").strip():
            site_map[str(pth)] = _default_site_for_autosave_index(idx)
        cur_val = str(site_map.get(str(pth)) or "").strip() or str(default_site)
        new_val = st.text_input(
            f"Site for: {Path(str(pth)).name}",
            value=cur_val,
            key=key0,
        )
        site_map[str(pth)] = str(new_val or "").strip() or str(default_site)
    st.session_state["csvbatch_site_name_by_autosave"] = site_map

    col_a, col_b = st.columns([2, 1])
    with col_a:
        limit_posts = st.number_input(
            "Сколько постов взять (0 = все)",
            min_value=0,
            value=0,
            step=1,
            key="csvbatch_limit_posts",
        )
    with col_b:
        ctx_chars = st.number_input(
            "Длина контекста (пример: 1200)",
            min_value=200,
            max_value=5000,
            value=1200,
            step=50,
            key="csvbatch_ctx_chars",
        )

    default_portions = st.number_input(
        "Порций на пост (по умолчанию)",
        min_value=1,
        max_value=200,
        value=int(st.session_state.get("csvbatch_default_portions", 1) or 1),
        step=1,
        key="csvbatch_default_portions",
    )

    posts: list[dict] = []
    posts_err: str | None = None

    paths = list(picked_multi or [])
    if not paths:
        main = st.session_state.get("csvbatch_autosave_main")
        if main:
            paths = [str(main)]

    merged_results: list[dict] = []
    try:
        for pth in paths:
            res_list, payload = src._load_unif_text_results(pth)
            # Resolve site for this autosave
            site_name = str((site_map.get(str(pth)) or "").strip() or str(default_site))
            for rr in res_list:
                if not isinstance(rr, dict):
                    continue
                if rr.get("error"):
                    continue
                rr2 = dict(rr)
                rr2["_autosave_path"] = str(pth)
                rr2["_site_name"] = site_name
                merged_results.append(rr2)
    except Exception as e:
        posts_err = str(e)

    if posts_err:
        st.error(f"Autosave read error: {posts_err}")
        merged_results = []

    if int(limit_posts) > 0:
        merged_results = merged_results[: int(limit_posts)]

    for rr in merged_results:
        title = (rr.get("title") or "").strip()
        posts.append(
            {
                "post_text": rr.get("text") or "",
                "title": title,
                "pinterest_title": (rr.get("pinterest_title") or "").strip(),
                "post_link": "",
                "_rr": rr,
                "site_name": (rr.get("_site_name") or "").strip() or str(default_site),
            }
        )

    editor_rows: list[dict] = []
    for idx, r in enumerate(posts):
        title = (r.get("title") or "").strip()
        if not title:
            title = src._guess_post_title(str(r.get("post_text") or ""))

        # Prefer pre-extracted pinterest_title if present (saved by app_unified_streamlit).
        ptitle = (r.get("pinterest_title") or "").strip()
        editor_rows.append(
            {
                "idx": idx,
                "title": title,
                # Default overlay text to pinterest_title so the table is not empty by default.
                "overlay_text": ptitle,
                "portions": int(default_portions),
                "post_link": r.get("post_link") or "",
            }
        )

    # Keep a stable "base table" snapshot for Streamlit versions where data_editor
    # stores only deltas (edited_rows/added_rows/deleted_rows) in session_state.
    # This makes programmatic Stage3 runs (RUN FULL PIPELINE) able to reconstruct
    # the final edited table reliably.
    st.session_state["csvbatch_editor_base_rows"] = [dict(r) for r in editor_rows]

    st.markdown("#### Настройки для каждого поста")
    if not editor_rows:
        st.info("Пока нет постов (проверьте autosave).")
        edited: Any = []
    else:
        edited = st.data_editor(
            editor_rows,
            use_container_width=True,
            hide_index=True,
            column_config={
                "idx": st.column_config.NumberColumn("#", disabled=True),
                "title": st.column_config.TextColumn("Пост (превью)", disabled=True),
                "overlay_text": st.column_config.TextColumn("Текст оверлея"),
                "portions": st.column_config.NumberColumn("Порции", min_value=1, max_value=200, step=1),
                "post_link": st.column_config.TextColumn("Link", disabled=True),
            },
            key="csvbatch_editor",
        )

    col_batch_a, col_batch_b = st.columns([1, 1])
    with col_batch_a:
        run_btn3 = st.button("Generate ALL (batch)", type="primary", key="csvbatch_run")
    with col_batch_b:
        stop_btn3 = st.button("STOP", type="secondary", key="csvbatch_stop")

    if "csvbatch_last_base_dir" not in st.session_state:
        st.session_state["csvbatch_last_base_dir"] = None
    if "csvbatch_last_items" not in st.session_state:
        st.session_state["csvbatch_last_items"] = []
    if "csvbatch_last_errors" not in st.session_state:
        st.session_state["csvbatch_last_errors"] = []

    if "csvbatch_run_id" not in st.session_state:
        st.session_state["csvbatch_run_id"] = None
    if "csvbatch_last_run_id" not in st.session_state:
        st.session_state["csvbatch_last_run_id"] = None

    # --- Batch run controls/state ---
    if stop_btn3:
        rid = st.session_state.get("csvbatch_run_id")
        out_dir_hint = st.session_state.get("csvbatch_last_base_dir")

        try:
            if rid:
                sp = src.Path(f"tmp_rovodev_csvbatch_stop_{rid}").resolve()
                sp.write_text("stop requested (ui)\n", encoding="utf-8")
                st.caption(f"STOP-file создан: {sp} (exists={sp.exists()})")
        except Exception as e:
            st.error(f"Не удалось создать STOP-file: {e}")

        if rid:
            src._csvbatch_request_stop(str(rid))
            st.session_state["csvbatch_running"] = False
            st.warning("STOP запрошен.")
            try:
                if out_dir_hint:
                    st.session_state["csvbatch_last_base_dir"] = out_dir_hint
            except Exception:
                pass
            src._ui_rerun()
        else:
            st.info("Сейчас нет активного запуска")

    if run_btn3:
        if not edited:
            st.error("Нет данных для генерации (пустой список постов)")
            st.stop()

        jobs: list[dict] = []
        skipped_empty_overlay = 0
        for row in edited:
            i = int(row.get("idx", 0))
            overlay_text = (row.get("overlay_text") or "").strip()
            portions = int(row.get("portions") or 0)
            if portions <= 0:
                portions = 1
            if not overlay_text:
                skipped_empty_overlay += 1
                continue
            if i < 0 or i >= len(posts):
                continue

            src_post = posts[i]
            rr = src_post.get("_rr") if isinstance(src_post, dict) else None
            if isinstance(rr, dict):
                ctx = src._unif_text_item_to_context(rr, target_chars=int(ctx_chars))
            else:
                ctx = src._extract_post_context(src_post.get("post_text") or "", target_chars=int(ctx_chars))
            jobs.append(
                {
                    "post_idx": i,
                    "title": row.get("title") or f"post{i+1}",
                    "overlay_text": overlay_text,
                    "portions": max(1, portions),
                    "context": ctx,
                    "site_name": (posts[i].get("site_name") if i < len(posts) else None)
                    or _site_default_from_state(),
                }
            )

        if skipped_empty_overlay:
            st.info(f"Пропущено постов с пустым 'Текст оверлея': {skipped_empty_overlay}")

        if not jobs:
            st.error("Нечего генерировать: заполните 'Текст оверлея'")
            st.stop()

        total_runs = sum(int(j["portions"]) for j in jobs)
        base_dir3 = src._get_run_base_dir("csv_batch_pin_parallel")

        # Use microseconds to avoid run_id collisions (can otherwise reuse stale STOP-file).
        run_id = datetime.now().strftime("csvbatch_%Y%m%d_%H%M%S_%f")
        st.session_state["csvbatch_run_id"] = run_id
        st.session_state["csvbatch_last_run_id"] = run_id
        st.session_state["csvbatch_last_base_dir"] = base_dir3
        st.session_state["csvbatch_download_base_name"] = "batch"
        st.session_state["csvbatch_last_items"] = []
        st.session_state["csvbatch_last_errors"] = []

        cfg = {
            "url": st.session_state.get("csvbatch_url", DEFAULT_URLS[0]),
            "model_choice": st.session_state.get("csvbatch_model", "Быстрая"),
            "headless": bool(st.session_state.get("csvbatch_headless", False)),
            "user_data_dir": st.session_state.get("csvbatch_user_data_dir", src.os.path.abspath(".chrome_automation_profile")),
            "profile_numbers": src._parse_profile_numbers(st.session_state.get("csvbatch_profile_numbers", "")),
            "executable_path": st.session_state.get("csvbatch_executable_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"),
            "launch_stagger_s": int(st.session_state.get("csvbatch_launch_stagger_s", 5)),
            "base_image_path": base_image_path,
            "base_dir": base_dir3,
            "num_windows": int(st.session_state.get("csvbatch_num_windows", 8)),
            "total_runs": int(total_runs),
            "page_default_timeout_ms": int(st.session_state.get("csvbatch_page_default_timeout_ms", 30000)),
            "input_ready_timeout_ms": int(st.session_state.get("csvbatch_input_ready_timeout_ms", 60000)),
            "attach_timeout_ms": int(st.session_state.get("csvbatch_attach_timeout_ms", 5000)),
            "offline_wait_timeout_s": int(st.session_state.get("csvbatch_offline_wait_timeout_s", 90)),
            "gen_timeout_s": int(st.session_state.get("csvbatch_gen_timeout_s", 100)),
            "gen_retry_timeout_s": int(st.session_state.get("csvbatch_gen_retry_timeout_s", 45)),
            "max_session_attempts": int(st.session_state.get("csvbatch_max_session_attempts", 2)),
        }

        src._csvbatch_start(run_id=run_id, jobs=jobs, cfg=cfg)
        st.info(f"Стартовали batch в фоне. Run ID: {run_id}")
        st.info(f"Выходная папка: {base_dir3}")
        src._ui_rerun()

    rid = st.session_state.get("csvbatch_run_id") or st.session_state.get("csvbatch_last_run_id")
    run_state = src._csvbatch_get_state(str(rid)) if rid else {}

    if run_state:
        running = bool(run_state.get("running"))
        st.session_state["csvbatch_running"] = running
        try:
            if run_state.get("base_dir"):
                st.session_state["csvbatch_last_base_dir"] = run_state.get("base_dir")
            st.session_state["csvbatch_last_items"] = list(run_state.get("items") or [])
            st.session_state["csvbatch_last_errors"] = list(run_state.get("errors") or [])
        except Exception:
            pass

        done_runs = int(run_state.get("done_runs") or 0)
        total_runs = int(run_state.get("total_runs") or 0)
        st.progress(0.0 if total_runs <= 0 else min(1.0, done_runs / max(1, total_runs)))
        st.write(f"Прогресс: {done_runs}/{total_runs}")

        if running:
            # Auto-refresh UI while running (best-effort)
            did_autorefresh = False
            try:
                if hasattr(st, "autorefresh"):
                    st.autorefresh(interval=2000, key="csvbatch_autorefresh")
                    did_autorefresh = True
            except Exception:
                did_autorefresh = False

            # Fallback: optional external package (same mechanism as streamlit-autorefresh)
            if not did_autorefresh:
                try:
                    from streamlit_autorefresh import st_autorefresh  # type: ignore

                    st_autorefresh(interval=2000, key="csvbatch_autorefresh")
                    did_autorefresh = True
                except Exception:
                    did_autorefresh = False

            # Last-resort fallback: polling rerun (keeps UI + Stage4 orchestration alive)
            if not did_autorefresh:
                try:
                    import time as _time

                    _time.sleep(2.0)
                    src._ui_rerun()
                except Exception:
                    # Manual fallback if everything else is unavailable.
                    if st.button("Обновить статус", key="csvbatch_manual_refresh"):
                        src._ui_rerun()

    _render_results_block_scoped(state_prefix="csvbatch", title="Batch results")
    # END copy
