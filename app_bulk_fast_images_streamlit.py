# Bulk fast image generation UI (Streamlit + Playwright Gemini UI)
# Usage:
#   streamlit run app_bulk_fast_images_streamlit.py -- --load_file <path_to_payload_json>
# Payload schema:
# {
#   "base_root": "generate automation",
#   "articles": [
#     {"idx": 1, "title": "...", "prompts": ["...", ...]}
#   ]
# }

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import queue
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

# Shared post-processing pipeline (watermark -> normalize -> webp)
from image_postprocess_pipeline import (
    NormalizeOptions,
    PhotoshopWatermarkOptions,
    WebpOptions,
    convert_images_to_webp_dir,
    normalize_images_to_dir,
    remove_watermark_photoshop_batch,
)

# We reuse the proven low-level Playwright/Gemini functions.
from gemini_playwright_streamlit import (
    BASE_IMAGE_PATH,
    DEFAULT_URLS,
    _attach_image,
    _click_send,
    _dismiss_overlays,
    _regenerate_prompt,
    _start_new_chat,
    _type_prompt,
    _wait_and_download_generated_images,
    _wait_image_attached,
    _wait_input_ready,
)

import gemini_pw_helpers as gph

# Tongyi HF Space generator (prompts-only)
# Import can fail if Playwright/requests deps are missing in the current environment.
TONGYI_DEFAULT_SPACE_URL = "https://tongyi-mai-z-image-turbo.hf.space/"
TONGYI_DEFAULT_SIZE_TEXT = "832x1248 ( 2:3 )"
_TONGYI_IMPORT_ERROR: str | None = None

try:
    from generate_tongyi_images import DEFAULT_SPACE_URL as TONGYI_DEFAULT_SPACE_URL  # type: ignore
    from generate_tongyi_images import TARGET_SIZE_TEXT as TONGYI_DEFAULT_SIZE_TEXT  # type: ignore
    from generate_tongyi_images import generate_batch_detailed as tongyi_generate_batch_detailed  # type: ignore
except Exception as _e:
    tongyi_generate_batch_detailed = None
    _TONGYI_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

from playwright.sync_api import sync_playwright


@dataclass
class ProTask:
    task_idx: int
    article_idx: int
    article_title: str
    prompt_idx: int
    prompt: str
    out_dir: str


def _build_pro_tasks_from_articles(articles: list[dict[str, Any]], run_base_dir: str, *, max_prompts_per_article: int = 4) -> list[ProTask]:
    tasks: list[ProTask] = []
    global_idx = 0
    for a in (articles or []):
        if not isinstance(a, dict):
            continue
        aidx = int(a.get("idx") or 0)
        title = str(a.get("title") or "").strip()
        prompts0 = a.get("prompts") or []
        if not isinstance(prompts0, list):
            continue
        prompts = [str(p).strip() for p in prompts0 if str(p).strip()]
        if not prompts:
            continue
        art_dir = Path(run_base_dir) / f"article_{aidx:02d}_{_sanitize_dir_name(title)}"
        # IMPORTANT: do NOT create directories here.
        # This function is used during Streamlit render / payload preview and must be side-effect free.
        for i, p in enumerate(prompts[: max(1, int(max_prompts_per_article))], 1):
            global_idx += 1
            tasks.append(
                ProTask(
                    task_idx=global_idx,
                    article_idx=aidx,
                    article_title=title,
                    prompt_idx=i,
                    prompt=p,
                    out_dir=str(art_dir.resolve()),
                )
            )
    return tasks


def _select_pro_articles_and_prompt_count(payload: dict[str, Any] | None) -> tuple[list[dict[str, Any]], int]:
    """Return the same article source/count used by the Pro tab."""

    if not isinstance(payload, dict):
        return [], 4

    has_pro_articles = ("pro_articles" in payload) and (payload.get("pro_articles") is not None)
    articles0 = (payload.get("pro_articles") or []) if has_pro_articles else (payload.get("articles") or [])
    if not isinstance(articles0, list):
        articles0 = []

    try:
        pro_prompt_count = int(payload.get("pro_prompt_count") or 4)
    except Exception:
        pro_prompt_count = 4
    pro_prompt_count = max(1, min(12, int(pro_prompt_count or 4)))

    return [a for a in articles0 if isinstance(a, dict)], pro_prompt_count


def _build_pro_tasks_from_payload(payload: dict[str, Any] | None, run_base_dir: str) -> list[ProTask]:
    articles0, pro_prompt_count = _select_pro_articles_and_prompt_count(payload)
    return _build_pro_tasks_from_articles(
        list(articles0 or []),
        str(run_base_dir),
        max_prompts_per_article=int(pro_prompt_count),
    )


# ------------------------ Small shared helpers ------------------------

# Keep Nano Banana Pro prompt wrapper consistent across automation + manual copy
NBP_PROMPT_PRE = "Generate one image in a 10:16 vertical aspect ratio using this prompt: "
NBP_PROMPT_POST = " Fill the entire frame; do not leave blank white space."


def _sanitize_prompt(text: str) -> str:
    """Conservative cleanup of known garbage tails that sometimes get appended to prompts."""
    import re as _re

    s = "" if text is None else str(text)

    markers = [
        r"\bfrom\s+Amazon\s+based\s+on\s+these\b",
        r"\bComparative\s+and\s+Opinion\-?Ba\b",
        r"\bphrafor\b",
    ]

    cut_at: int | None = None
    for pat in markers:
        m = _re.search(pat, s, flags=_re.IGNORECASE)
        if m:
            cut_at = m.start() if cut_at is None else min(cut_at, m.start())
    if cut_at is not None:
        s = s[:cut_at]

    return s


def _wrap_prompt_for_nbp_fast(prompt_raw: str) -> str:
    """Build the text prompt used for Nano Banana Pro Fast-side generation."""

    p = _sanitize_prompt(prompt_raw or "").strip()
    if not p:
        return ""

    low = p.lower()
    pre = str(NBP_PROMPT_PRE or "").strip()
    post = str(NBP_PROMPT_POST or "").strip()

    if pre and low.startswith(pre.lower()):
        if post and post.lower() not in low:
            return f"{p} {post}".strip()
        return p

    if "10:16 vertical aspect ratio" in low and (not post or post.lower() in low):
        return p

    return f"{NBP_PROMPT_PRE}{p}{NBP_PROMPT_POST}".strip()


def _apply_regen_prefix_variation(prompt_text: str, *, key: str) -> str:
    """Prepend a rotating neutral prefix to avoid sending identical prompts on regeneration.

    Mirrors Tab1 logic in app_unified_streamlit.py.
    Rotation is tracked in st.session_state["bulk_regen_prefix_counts"][key].
    """
    try:
        prefixes = ["будь добр", "пожалуйста", "плиз", "please"]
        txt = (prompt_text or "").strip()
        if not txt:
            return txt

        # remove existing prefix if present
        low = txt.lower()
        for pref in prefixes:
            if low.startswith(pref + " ") or low == pref:
                txt = txt[len(pref) :].lstrip()
                low = txt.lower()
                break

        counts = st.session_state.get("bulk_regen_prefix_counts", {})
        c = int(counts.get(str(key), 0) or 0)
        pref = prefixes[c % len(prefixes)]
        counts[str(key)] = c + 1
        st.session_state["bulk_regen_prefix_counts"] = counts
        return f"{pref} {txt}".strip()
    except Exception:
        return (prompt_text or "").strip()


# ---------------- Clipboard helpers (manual mode convenience) ----------------
# Streamlit doesn't provide a native clipboard API.
# We render a small HTML/JS button (inside a Streamlit component iframe) that copies text.
import json as _json


def _clipboard_copy_text_button(*, label: str, text: str, key: str, help_text: str | None = None) -> None:
    payload = _json.dumps(text or "")
    dom_id = f"clip_txt_{key}".replace(" ", "_")
    safe_label = (label or "Copy").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""
    <div style=\"display:flex;align-items:center;flex-wrap:nowrap;overflow:visible\">
      <button id=\"{dom_id}\" style=\"display:inline-flex;align-items:center;justify-content:center;height:32px;padding:0 12px;margin:0;border:1px solid #999;border-radius:6px;background:#f8f8f8;cursor:pointer;white-space:nowrap;line-height:1;font-size:13px\">{safe_label}</button>
    </div>
    <script>
      const text = {payload};
      const btn = document.getElementById({ _json.dumps(dom_id) });
      const orig = btn ? btn.textContent : '';
      btn?.addEventListener('click', async () => {{
        try {{
          await navigator.clipboard.writeText(text);
          if (btn) btn.textContent = 'Copied';
          setTimeout(() => {{ if (btn) btn.textContent = orig; }}, 900);
        }} catch (e) {{
          if (btn) btn.textContent = 'Failed';
          setTimeout(() => {{ if (btn) btn.textContent = orig; }}, 900);
        }}
      }});
    </script>
    """
    components.html(html, height=52)


def _clipboard_copy_text_button_with_random_prefix(*, label: str, text: str, key: str, prefixes: list[str]) -> None:
    """Copy `text` to clipboard, but prepend a neutral prefix sequentially (round-robin).

    Each click uses the next prefix from `prefixes`; when the list ends, it starts again from the beginning.
    Index is stored in browser `localStorage` so it survives Streamlit rerenders.

    NOTE: This affects only clipboard copy, NOT the actual generation logic.
    """
    payload_text = _json.dumps(text or "")
    payload_prefixes = _json.dumps([str(p) for p in (prefixes or []) if str(p).strip()])
    dom_id = f"clip_txt_rand_{key}".replace(" ", "_")
    safe_label = (label or "Copy").replace("<", "&lt;").replace(">", "&gt;")

    html = f"""
    <div style=\"display:flex;align-items:center;flex-wrap:nowrap;overflow:visible\">
      <button id=\"{dom_id}\" style=\"display:inline-flex;align-items:center;justify-content:center;height:32px;padding:0 12px;margin:0;border:1px solid #999;border-radius:6px;background:#f8f8f8;cursor:pointer;white-space:nowrap;line-height:1;font-size:13px\">{safe_label}</button>
    </div>
    <script>
      const baseText = {payload_text};
      const prefixes = {payload_prefixes};
      const btn = document.getElementById({ _json.dumps(dom_id) });
      const orig = btn ? btn.textContent : '';

      function pickPrefixSequential() {{
        if (!prefixes || prefixes.length === 0) return '';
        const storageKey = 'bulkpro_prefix_idx_' + { _json.dumps(dom_id) };
        let idx = 0;
        try {{
          const raw = window.localStorage.getItem(storageKey);
          if (raw !== null) {{
            const n = parseInt(raw, 10);
            if (!Number.isNaN(n) && n >= 0) idx = n;
          }}
        }} catch (e) {{
          idx = 0;
        }}

        const pref = String(prefixes[idx % prefixes.length] || '').trim();
        try {{
          window.localStorage.setItem(storageKey, String((idx + 1) % prefixes.length));
        }} catch (e) {{
          // ignore
        }}
        return pref;
      }}

      btn?.addEventListener('click', async () => {{
        try {{
          const pref = pickPrefixSequential();
          const finalText = pref ? (pref + ' ' + baseText) : baseText;
          await navigator.clipboard.writeText(finalText);
          if (btn) btn.textContent = 'Copied';
          setTimeout(() => {{ if (btn) btn.textContent = orig; }}, 900);
        }} catch (e) {{
          if (btn) btn.textContent = 'Failed';
          setTimeout(() => {{ if (btn) btn.textContent = orig; }}, 900);
        }}
      }});
    </script>
    """
    components.html(html, height=52)


def _copy_image_to_windows_clipboard_via_powershell(image_path: str) -> bool:
    """Copy image file to Windows clipboard via PowerShell (STA)."""
    try:
        if os.name != "nt":
            return False
        p = str(Path(image_path).expanduser().resolve())
        if not p or not os.path.exists(p):
            return False
        p_ps = p.replace("'", "''")
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            f"$img=[System.Drawing.Image]::FromFile('{p_ps}');"
            "[System.Windows.Forms.Clipboard]::SetImage($img);"
            "$img.Dispose();"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True,
            text=True,
        )
        return True
    except Exception:
        return False


def _set_chrome_profile_download_dir(user_data_dir: str, download_dir: str) -> bool:
    """Set Chrome profile download directory by editing Preferences JSON.

    Works for persistent profiles (user-data-dir). We write to Default/Preferences.
    Returns True if write succeeded.
    """
    try:
        udir = _normalize_user_data_dir(user_data_dir)
        if not udir:
            return False
        dd = str(Path(download_dir).expanduser())
        if not dd:
            return False
        Path(dd).mkdir(parents=True, exist_ok=True)

        pref_dir = Path(udir) / "Default"
        pref_dir.mkdir(parents=True, exist_ok=True)
        pref_path = pref_dir / "Preferences"

        data: dict[str, Any] = {}
        if pref_path.exists():
            try:
                raw = pref_path.read_text(encoding="utf-8", errors="ignore")
                obj = json.loads(raw or "{}")
                if isinstance(obj, dict):
                    data = obj
            except Exception:
                data = {}

        download = data.get("download")
        if not isinstance(download, dict):
            download = {}
        download["default_directory"] = dd
        download["prompt_for_download"] = False
        download["directory_upgrade"] = True
        data["download"] = download

        pref_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _get_or_create_manual_pro_profile_dir(*, base_user_data_dir: str, run_base_dir: str, task_key: str) -> str:
    """Create (once) a cloned Chrome profile for manual Pro mode.

    We must not reuse the same user_data_dir for multiple parallel windows because changing
    download.default_directory in Preferences is per-profile and will race.

    The clone is stored under:
        <run_base_dir>/_manual_pro_profiles/task_<task_key>
    """
    base_norm = _normalize_user_data_dir(base_user_data_dir) or base_user_data_dir
    tkey = str(task_key or "").strip() or "task"
    # include base profile hash so changing base_user_data_dir in UI produces a different cloned profile
    try:
        h = hashlib.md5(str(base_norm).encode("utf-8", errors="ignore")).hexdigest()[:8]
    except Exception:
        h = "base"
    root = Path(run_base_dir).expanduser() / "_manual_pro_profiles" / f"task_{tkey}_{h}"
    dst = str(root.resolve())

    try:
        if Path(dst).exists():
            return dst
    except Exception:
        pass

    try:
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    try:
        _clone_profile_dir(base_norm, dst)
    except Exception:
        # last resort: use base (may conflict, but avoids crash)
        return base_norm

    return dst


def _open_chrome_window_with_profile(*, executable_path: str | None, user_data_dir: str | None, url: str | None = None, download_dir: str | None = None) -> None:
    """Open a normal Chrome window using the given user-data-dir (manual fallback)."""
    try:
        exe = (executable_path or "").strip() or None
        udir = _normalize_user_data_dir(user_data_dir) if user_data_dir else None
        if not exe or not udir:
            return
        try:
            os.makedirs(udir, exist_ok=True)
        except Exception:
            pass

        # Optional: make this profile download into a task-specific directory to avoid mixing parallel downloads
        try:
            if download_dir:
                _set_chrome_profile_download_dir(udir, str(download_dir))
        except Exception:
            pass

        cmd = [
            exe,
            "--new-instance",
            f"--user-data-dir={udir}",
            "--lang=ru-RU",
            "--new-window",
        ]
        if url:
            cmd.append(str(url))
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ---------------- Native Chrome download isolation (Stage1) ----------------

def _stage1_native_download_watch_dirs(profile_dir: str | None) -> list[str]:
    """Return every location where Chrome can place a native Stage1 download.

    Gemini's Download action may ignore either Playwright's ``downloads_path``
    or the persistent profile preference. The Windows Downloads directory is
    retained as a fallback: a confirmed file there is immediately moved into
    the worker-private directory by the common downloader.
    """

    candidates: list[str] = []
    try:
        root = Path(profile_dir).resolve() if profile_dir else None
        if root:
            for pref_path in (root / "Default" / "Preferences", root / "Preferences"):
                if not pref_path.is_file():
                    continue
                try:
                    prefs = json.loads(pref_path.read_text(encoding="utf-8", errors="ignore"))
                    for section, key in (("download", "default_directory"), ("savefile", "default_directory")):
                        value = str((prefs.get(section) or {}).get(key) or "").strip()
                        if value:
                            candidates.append(value)
                except Exception:
                    pass
                break
    except Exception:
        pass

    try:
        if os.name == "nt":
            fallback = Path(os.environ.get("USERPROFILE") or Path.home()) / "Downloads"
        else:
            fallback = Path.home() / "Downloads"
        candidates.append(str(fallback))
    except Exception:
        pass

    unique: list[str] = []
    for candidate in candidates:
        try:
            resolved = str(Path(candidate).expanduser().resolve())
            if Path(resolved).is_dir() and resolved not in unique:
                unique.append(resolved)
        except Exception:
            continue
    return unique


def _set_stage1_profile_download_directory_temporarily(
    profile_dir: str | None, download_dir: str
) -> tuple[list[tuple[Path, bytes]], list[str]]:
    """Temporarily point one Stage1 profile at its private download directory.

    The raw Preferences bytes are restored after the persistent context closes.
    Stage1 already locks a profile while a task is using it, so this cannot race
    another Stage1 task that happens to reuse that profile.
    """

    backups: list[tuple[Path, bytes]] = []
    notes: list[str] = []
    if not profile_dir:
        return backups, ["profile preference: skipped (no profile directory)"]

    try:
        target = str(Path(download_dir).resolve())
        root = Path(profile_dir).resolve()
    except Exception as e:
        return backups, [f"profile preference: path resolution failed: {e}"]

    for pref_path in (root / "Default" / "Preferences", root / "Preferences"):
        if not pref_path.is_file():
            continue
        try:
            original = pref_path.read_bytes()
            prefs = json.loads(original.decode("utf-8"))
            download = prefs.get("download")
            if not isinstance(download, dict):
                download = {}
                prefs["download"] = download
            download["default_directory"] = target
            download["prompt_for_download"] = False
            download["directory_upgrade"] = True

            tmp_path = pref_path.with_name(f"{pref_path.name}.stage1-download-tmp")
            tmp_path.write_text(
                json.dumps(prefs, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(str(tmp_path), str(pref_path))
            backups.append((pref_path, original))
            notes.append(f"profile preference pinned: {pref_path}")
        except Exception as e:
            notes.append(f"profile preference failed: {pref_path}: {e}")

    if not backups:
        notes.append("profile preference: no editable Preferences file found")
    return backups, notes


def _restore_stage1_profile_download_directory(backups: list[tuple[Path, bytes]]) -> list[str]:
    """Restore the exact Chrome Preferences bytes saved for a Stage1 task."""

    notes: list[str] = []
    for pref_path, original in backups or []:
        try:
            tmp_path = pref_path.with_name(f"{pref_path.name}.stage1-restore-tmp")
            tmp_path.write_bytes(original)
            os.replace(str(tmp_path), str(pref_path))
            notes.append(f"profile preference restored: {pref_path}")
        except Exception as e:
            notes.append(f"profile preference restore failed: {pref_path}: {e}")
    return notes


def _pin_stage1_chrome_download_directory(page, download_dir: str) -> str:
    """Set Chrome's live download destination through CDP when available."""

    try:
        target = str(Path(download_dir).resolve())
        session = page.context.new_cdp_session(page)
    except Exception as e:
        return f"CDP download path unavailable: {e}"

    try:
        for method in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
            try:
                session.send(
                    method,
                    {
                        "behavior": "allow",
                        "downloadPath": target,
                        "eventsEnabled": True,
                    },
                )
                return f"native Chrome path pinned via {method}: {target}"
            except Exception:
                continue
        return "CDP download path was rejected by this Chrome build"
    finally:
        try:
            session.detach()
        except Exception:
            pass


# ---------------- Manual-download helper: rename newest downloaded image ----------------

def _default_downloads_dir() -> str:
    """Best-effort default Downloads folder."""
    try:
        if os.name == "nt":
            base = os.environ.get("USERPROFILE") or str(Path.home())
            return str(Path(base) / "Downloads")
        return str(Path.home() / "Downloads")
    except Exception:
        return ""


def _is_temp_download_name(name: str) -> bool:
    n = (name or "").lower()
    return n.endswith(".crdownload") or n.endswith(".tmp") or n.endswith(".download")


def _looks_like_image_file(path: Path) -> bool:
    try:
        if not path or not path.is_file():
            return False
        ext = path.suffix.lower()
        return ext in {".png", ".jpg", ".jpeg", ".webp"}
    except Exception:
        return False


def _wait_file_stable(path: Path, *, stable_for_s: float = 1.2, timeout_s: float = 30.0) -> bool:
    """Wait until file size stops changing (download completed)."""
    deadline = time.time() + float(timeout_s)
    last_size = -1
    last_change = time.time()
    while time.time() < deadline:
        try:
            if not path.exists() or not path.is_file():
                time.sleep(0.2)
                continue
            size = int(path.stat().st_size)
            if size != last_size:
                last_size = size
                last_change = time.time()
            else:
                if (time.time() - last_change) >= float(stable_for_s) and size > 0:
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _rename_with_collision(dst: Path) -> Path:
    """If dst exists, append _dupNN before extension."""
    if not dst.exists():
        return dst
    stem = dst.stem
    ext = dst.suffix
    for i in range(1, 1000):
        cand = dst.with_name(f"{stem}_dup{i:02d}{ext}")
        if not cand.exists():
            return cand
    return dst


_BULKPRO_DL_DISPATCHER_LOCK = threading.Lock()


def _bulkpro_dl_apply_file(
    *,
    picked: Path,
    watch_dir: Path,
    expected_basename: str,
    task_key: str,
    dest_dir: str | None,
) -> None:
    """Rename picked file to expected_basename and optionally move to dest_dir."""
    try:
        expected_basename = str(expected_basename or "").strip()
        if not expected_basename:
            raise ValueError("expected_basename is empty")

        dst = watch_dir / expected_basename
        dst = _rename_with_collision(dst)
        try:
            picked.rename(dst)
        except Exception:
            # Fallback: copy+remove
            shutil.copy2(str(picked), str(dst))
            try:
                picked.unlink(missing_ok=True)
            except Exception:
                pass

        final_path = dst
        if dest_dir:
            try:
                dd = Path(dest_dir).expanduser()
                dd.mkdir(parents=True, exist_ok=True)
                target = _rename_with_collision(dd / dst.name)
                try:
                    shutil.move(str(dst), str(target))
                    final_path = target
                except Exception:
                    # cross-device / busy file fallback
                    shutil.copy2(str(dst), str(target))
                    try:
                        Path(dst).unlink(missing_ok=True)
                    except Exception:
                        pass
                    final_path = target
            except Exception:
                # keep in downloads
                final_path = dst

        st.session_state.setdefault("bulkpro_dl_watch_status", {})[str(task_key)] = {
            "ok": True,
            "from": str(picked),
            "to": str(final_path),
        }
    except Exception as e:
        st.session_state.setdefault("bulkpro_dl_watch_status", {})[str(task_key)] = {
            "ok": False,
            "error": str(e),
        }


def _start_download_rename_watcher_direct(*, watch_dir: str, expected_basename: str, task_key: str, dest_dir: str | None = None, timeout_s: int = 180) -> None:
    """Start a dedicated watcher for a unique download folder.

    Use this when each Chrome window/profile downloads into its own folder (task-specific).
    Then per-task watcher is reliable and supports parallel windows.
    """

    def _worker(start_ts: float, watch_dir0: str, expected0: str, tkey: str, dest_dir0: str | None, timeout0: int):
        try:
            wd = Path(watch_dir0).expanduser()
            try:
                wd.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            if not wd.exists() or not wd.is_dir():
                st.session_state.setdefault("bulkpro_dl_watch_status", {})[tkey] = {
                    "ok": False,
                    "error": f"Downloads dir not found: {wd}",
                }
                return

            deadline = time.time() + float(timeout0)
            picked: Path | None = None

            while time.time() < deadline:
                try:
                    candidates: list[Path] = []
                    for p in wd.iterdir():
                        try:
                            if not p.is_file():
                                continue
                            if _is_temp_download_name(p.name):
                                continue
                            if not _looks_like_image_file(p):
                                continue
                            # Be tolerant: sometimes mtime may be very close to thread start.
                            # We still only want "fresh" downloads, but avoid missing the file due to clock jitter.
                            if p.stat().st_mtime < (start_ts - 5.0):
                                continue
                            candidates.append(p)
                        except Exception:
                            continue

                    if candidates:
                        candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                        picked = candidates[0]
                        break
                except Exception:
                    pass

                time.sleep(0.25)

            if not picked:
                # Provide extra debug info (list current files) to make failures understandable.
                try:
                    cur_files = sorted([p.name for p in wd.iterdir() if p.is_file()])
                except Exception:
                    cur_files = []
                st.session_state.setdefault("bulkpro_dl_watch_status", {})[tkey] = {
                    "ok": False,
                    "error": f"Timeout: no image download found in {wd}. Files now: {cur_files[:15]}",
                }
                return

            if not _wait_file_stable(picked, stable_for_s=1.2, timeout_s=45.0):
                st.session_state.setdefault("bulkpro_dl_watch_status", {})[tkey] = {
                    "ok": False,
                    "error": f"File did not become stable: {picked.name}",
                }
                return

            _bulkpro_dl_apply_file(
                picked=picked,
                watch_dir=wd,
                expected_basename=str(expected0),
                task_key=str(tkey),
                dest_dir=str(dest_dir0) if dest_dir0 else None,
            )
        except Exception as e:
            st.session_state.setdefault("bulkpro_dl_watch_status", {})[tkey] = {
                "ok": False,
                "error": str(e),
            }

    try:
        st.session_state.setdefault("bulkpro_dl_watch_status", {})[str(task_key)] = {"ok": None, "error": None}
        t = threading.Thread(
            target=_worker,
            args=(time.time(), str(watch_dir or ""), str(expected_basename or ""), str(task_key or ""), str(dest_dir) if dest_dir else None, int(timeout_s)),
            daemon=True,
        )
        t.start()
        # keep reference
        st.session_state.setdefault("bulkpro_dl_direct_threads", {})[str(task_key)] = t
    except Exception:
        pass


def _bulkpro_dl_ensure_dispatcher_running(watch_dir: str) -> None:
    # Dispatcher disabled (we use per-task unique download folders now).
    return


def _start_download_rename_watcher(*, watch_dir: str, expected_basename: str, task_key: str, dest_dir: str | None = None, timeout_s: int = 180) -> None:
    """Start per-task watcher for a *task-specific* download folder."""
    _start_download_rename_watcher_direct(
        watch_dir=str(watch_dir),
        expected_basename=str(expected_basename),
        task_key=str(task_key),
        dest_dir=str(dest_dir) if dest_dir else None,
        timeout_s=int(timeout_s),
    )


def _limit_slug_for_full_path(directory: str | None, prefix: str, slug: str, suffix: str) -> str:
    """Trim only when the full Windows path would be too long."""

    if not directory:
        return slug

    max_full = 255
    try:
        max_full = int(os.environ.get("BULK_IMAGE_MAX_FULL_PATH") or max_full)
    except Exception:
        max_full = 255

    try:
        dir_len = len(str(Path(str(directory)).expanduser().resolve()))
    except Exception:
        dir_len = len(str(directory or ""))

    allowed = int(max_full) - dir_len - 1 - len(prefix) - len(suffix)
    if allowed >= len(slug):
        return slug
    return (slug[: max(1, allowed)].rstrip("_-") or "prompt")


def _predict_nbp_image_basename(
    task_idx: int,
    prompt_raw: str | None,
    *,
    j: int = 1,
    ext: str = "png",
    out_dir: str | None = None,
) -> str:
    """Predict NBP-Pro filename (basename only) using the same slug rules as the saver."""
    import re as _re

    idx = int(task_idx)
    prompt_raw = (prompt_raw or "").strip()

    base_slug = _re.sub(r"[^a-zA-Z0-9_-]+", "_", prompt_raw)
    base_slug = _re.sub(r"_+", "_", base_slug).strip("_")
    if not base_slug:
        base_slug = f"prompt_{idx}"

    prefix = f"{idx}_pro_"
    prefix_len = len(prefix)
    suffix = f"_{int(j):02d}.{ext}"
    suffix_len = len(suffix)

    MAX_BASENAME = 110
    allowed_slug_len = max(1, MAX_BASENAME - prefix_len - suffix_len)
    slug = base_slug[:allowed_slug_len]
    slug = _limit_slug_for_full_path(out_dir, prefix, slug, suffix)

    return f"{idx}_pro_{slug}_{int(j):02d}.{ext}"


def _move_image_to_pro_named_file(*, src_path: str, out_dir: str, task_idx: int, prompt_raw: str | None) -> str:
    """Move/rename an existing image to the canonical Pro filename in out_dir.

    Used for Tongyi automation so results match manual/expected naming: <task_idx>_pro_<slug>_01.png.
    If the target exists, increments NN (01..99).

    IMPORTANT: Tongyi downloads may still be writing to disk when we get the path.
    So we wait for the file to become stable and retry move/copy a few times.

    Returns the final path (string). If move fails, returns original src_path.
    """
    try:
        sp = Path(str(src_path)).expanduser()
        if not sp.exists() or not sp.is_file():
            # generate_tongyi_images may return a basename/relative path.
            # Try to resolve relative to out_dir.
            try:
                od0 = Path(str(out_dir)).expanduser()
                cand = (od0 / str(src_path)).expanduser()
                if cand.exists() and cand.is_file():
                    sp = cand
                else:
                    # fallback: pick newest likely tongyi output in out_dir
                    exts = ["*.png", "*.jpg", "*.jpeg", "*.webp"]
                    newest: Path | None = None
                    newest_m = 0.0
                    for pat in exts:
                        for fp in od0.glob(pat):
                            try:
                                m = fp.stat().st_mtime
                                if m > newest_m:
                                    newest_m = m
                                    newest = fp
                            except Exception:
                                continue
                    if newest is None:
                        return str(src_path)
                    # accept newest only if it looks like Tongyi naming (001-...) or is very recent
                    import re
                    bn = newest.name
                    age = time.time() - newest_m
                    if re.match(r"^\d{3}-", bn) or age < 180:
                        sp = newest
                    else:
                        return str(src_path)
            except Exception:
                return str(src_path)

        # Wait until download finishes (size stops changing)
        try:
            _wait_file_stable(sp, stable_for_s=1.2, timeout_s=45.0)
            try:
                if sp.stat().st_size < 8 * 1024:
                    # Do not rename empty/placeholder files
                    return str(sp)
            except Exception:
                return str(sp)
        except Exception:
            pass

        od = Path(str(out_dir)).expanduser()
        od.mkdir(parents=True, exist_ok=True)

        ext = (sp.suffix or ".png").lstrip(".").lower() or "png"

        # find free name
        for j in range(1, 100):
            bn = _predict_nbp_image_basename(int(task_idx), (prompt_raw or ""), j=j, ext=ext, out_dir=str(od))
            dst = od / bn
            if dst.exists():
                continue

            # retry move/copy, because file can be temporarily locked
            for attempt in range(1, 6):
                try:
                    shutil.move(str(sp), str(dst))
                    return str(dst)
                except Exception:
                    try:
                        shutil.copy2(str(sp), str(dst))
                        try:
                            sp.unlink(missing_ok=True)
                        except Exception:
                            pass
                        return str(dst)
                    except Exception:
                        time.sleep(0.35 * attempt)
                        continue

            # if we couldn't place to this dst, try next j

        # if all occupied, just do collision rename
        dst2 = _rename_with_collision(od / _predict_nbp_image_basename(int(task_idx), (prompt_raw or ""), j=1, ext=ext, out_dir=str(od)))
        for attempt in range(1, 6):
            try:
                shutil.move(str(sp), str(dst2))
                return str(dst2)
            except Exception:
                try:
                    shutil.copy2(str(sp), str(dst2))
                    try:
                        sp.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return str(dst2)
                except Exception:
                    time.sleep(0.35 * attempt)
                    continue

        return str(src_path)
    except Exception:
        return str(src_path)


from typing import Tuple, List


def _has_generated_images_simple(page) -> bool:
    try:
        last_loc = page.locator(
            ".presented-response-container, .response-container-content, structured-content-container, .response-container, div[class*='response']"
        ).last
        if not last_loc or last_loc.count() == 0:
            return False
        for sel in [
            '.attachment-container.generated-images',
            'single-image',
            'img.image.animate.loaded',
            'img.image',
            'canvas',
        ]:
            try:
                if last_loc.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return False


def _filter_imgs_min_bytes(imgs: List[Tuple[str, bytes]] | None, *, min_bytes: int = 25000) -> List[Tuple[str, bytes]]:
    out: List[Tuple[str, bytes]] = []
    for mime, blob in (imgs or []):
        try:
            if blob and len(blob) >= int(min_bytes):
                out.append((mime, blob))
        except Exception:
            continue
    return out


def _wait_and_collect_images_simple(page, timeout_s: int = 90, max_images: int = 6) -> List[Tuple[str, bytes]]:
    deadline = time.time() + float(timeout_s)
    while time.time() < deadline:
        if _has_generated_images_simple(page):
            break
        time.sleep(0.4)
    try:
        containers = []
        for sel in [
            ".presented-response-container",
            ".response-container-content",
            "structured-content-container",
            ".attachment-container.generated-images",
            "single-image",
            ".response-container",
            "div[class*='response']",
        ]:
            containers.extend(page.query_selector_all(sel))
        if not containers:
            return []
        last = containers[-1]
        probes = [
            "single-image img.image.animate.loaded",
            "single-image img.image",
            ".attachment-container.generated-images img.image.loaded",
            ".attachment-container.generated-images img",
        ]
        results: List[Tuple[str, bytes]] = []
        seen = set()
        for psel in probes:
            if len(results) >= int(max_images):
                break
            try:
                ims = last.query_selector_all(psel)
            except Exception:
                ims = []
            for im in ims:
                if len(results) >= int(max_images):
                    break
                try:
                    src = im.get_attribute("src") or ""
                except Exception:
                    src = ""
                if src and src in seen:
                    continue
                if src:
                    seen.add(src)
                try:
                    if src.startswith("data:image/"):
                        header, b64 = src.split(",", 1)
                        mime = header.split(":", 1)[1].split(";")[0]
                        import base64 as _b64

                        data = _b64.b64decode(b64)
                        results.append((mime, data))
                        continue
                except Exception:
                    pass
                try:
                    data = im.screenshot(type="png")
                    if data:
                        results.append(("image/png", data))
                except Exception:
                    pass
        return results
    except Exception:
        return []


def _get_latest_pro_saved_image_path(
    task_idx: int,
    *,
    out_dir: str | None = None,
    run_base_dir: str | None = None,
) -> str | None:
    """Return latest saved image path for a given pro task.

    Primary source: st.session_state['bulkpro_saved_paths'] (from current session).
    Fallback: scan `out_dir` on disk (useful after Streamlit restart / when selecting an old run dir).
    """
    try:
        if not task_idx:
            return None

        prefix1 = f"{int(task_idx)}_pro_"
        prefix2 = f"{int(task_idx):02d}_pro_"

        # 1) session_state cache
        saved = list(st.session_state.get("bulkpro_saved_paths") or [])
        cand: list[Path] = []
        for p in saved:
            try:
                pp = Path(p)
                name = pp.name
                if not (name.startswith(prefix1) or name.startswith(prefix2)):
                    continue
                if pp.exists() and pp.is_file():
                    cand.append(pp)
            except Exception:
                continue
        if cand:
            cand.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
            return str(cand[0])

        # 2) disk scan fallback (out_dir)
        if out_dir:
            try:
                d = Path(out_dir).expanduser()
                if d.exists() and d.is_dir():
                    disk_cand: list[Path] = []
                    any_imgs: list[Path] = []
                    for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
                        for fp in d.glob(ext):
                            try:
                                if not fp.is_file():
                                    continue
                                any_imgs.append(fp)
                                n = fp.name
                                if n.startswith(prefix1) or n.startswith(prefix2):
                                    disk_cand.append(fp)
                            except Exception:
                                continue
                    if disk_cand:
                        disk_cand.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
                        return str(disk_cand[0])
                    # Important fallback: after Streamlit restart task_idx numbering may change (pro_prompt_count etc.)
                    # In that case, at least show the newest image from the folder.
                    if any_imgs:
                        any_imgs.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
                        return str(any_imgs[0])
            except Exception:
                pass

        # 3) disk scan fallback (recursive over run_base_dir)
        if run_base_dir:
            try:
                rb = Path(run_base_dir).expanduser()
                if rb.exists() and rb.is_dir():
                    disk_cand2: list[Path] = []
                    # rglob can be heavy; but run folders are typically moderate.
                    for fp in rb.rglob("*"):
                        try:
                            if not fp.is_file():
                                continue
                            if fp.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                                continue
                            n = fp.name
                            if n.startswith(prefix1) or n.startswith(prefix2):
                                disk_cand2.append(fp)
                        except Exception:
                            continue
                    if disk_cand2:
                        disk_cand2.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
                        return str(disk_cand2[0])
            except Exception:
                pass

        return None
    except Exception:
        return None


def _fix_common_profile_path_typos(path_str: str) -> str:
    try:
        s = (path_str or "").strip().strip('"').strip("'")
        # If the dir exists, do not touch.
        try:
            if s and Path(os.path.expanduser(s)).exists():
                return s
        except Exception:
            pass

        keys = [".chrome_automation_profile", "chrome_automation_profile"]
        for key in keys:
            i = s.find(key)
            if i > 0:
                prev = s[i - 1]
                if prev not in {"/", "\\", os.sep}:
                    sep = "\\" if os.name == "nt" else os.sep
                    candidate = s[:i] + sep + s[i:]
                    try:
                        if Path(os.path.expanduser(candidate)).exists() or not Path(os.path.expanduser(s)).exists():
                            s = candidate
                            break
                    except Exception:
                        s = candidate
                        break
        return s
    except Exception:
        return path_str


def _normalize_user_data_dir(path_str: str | None) -> str | None:
    if not path_str:
        return None
    s = _fix_common_profile_path_typos(path_str)
    try:
        return str(Path(os.path.expanduser(s)).resolve())
    except Exception:
        try:
            return os.path.abspath(os.path.expanduser(s))
        except Exception:
            return s


def _chrome_launch_args(extra: list[str] | None = None) -> list[str]:
    base = [
        "--lang=ru-RU",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-gpu-compositing",
    ]
    if extra:
        base.extend(extra)
    return base


def _launch_persistent_ctx_with_retries(
    pw,
    *,
    user_data_dir: str | None,
    headless: bool,
    executable_path: str | None,
    downloads_path: str | None = None,
    extra_args: list[str] | None = None,
    attempts: int = 6,
):
    norm_udir = _normalize_user_data_dir(user_data_dir) if user_data_dir else None
    norm_downloads = os.path.abspath(downloads_path) if downloads_path else None
    if norm_udir:
        try:
            os.makedirs(norm_udir, exist_ok=True)
        except Exception:
            pass

    last_err: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=norm_udir,
                headless=headless,
                downloads_path=norm_downloads,
                channel="chrome",
                executable_path=executable_path or None,
                # Reduce obvious automation fingerprints (can affect Google AI Studio permissions).
                ignore_default_args=["--enable-automation"],
                args=_chrome_launch_args((extra_args or []) + ["--disable-blink-features=AutomationControlled"]),
            )
            try:
                ctx.add_init_script(
                    """
                    // Hide Playwright/WebDriver automation flag.
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    """
                )
            except Exception:
                pass
            return ctx
        except Exception as e:
            last_err = e
            time.sleep(0.6 + (0.7 * attempt))
    raise last_err or RuntimeError("Failed to launch persistent context")


def _clone_profile_dir(src_dir: str, dst_dir: str) -> None:
    def _ignore_profile(_dirpath: str, names: list[str]):
        skip_exact = {
            "Cache",
            "Code Cache",
            "GPUCache",
            "GrShaderCache",
            "ShaderCache",
            "DawnGraphiteCache",
            "DawnWebGPUCache",
            "GraphiteDawnCache",
            "Crashpad",
            "Crash Reports",
        }
        skip_prefix = ("Singleton",)
        ignored: list[str] = []
        for n in names:
            if n in skip_exact or any(n.startswith(p) for p in skip_prefix):
                ignored.append(n)
                continue
            if n.upper() == "LOCK" or n.lower() in {"lockfile", "devtoolsactiveport"}:
                ignored.append(n)
                continue
        return ignored

    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=False, ignore=_ignore_profile)


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _compute_new_run_dir_path(base_root: str) -> str:
    """Compute a new run dir path under `base_root` (without creating it)."""
    root = Path(base_root)
    date_str = datetime.now().strftime("%Y-%m-%d")
    base = root / date_str
    cand = base
    if cand.exists():
        i = 1
        while True:
            cand = Path(f"{base}_{i}")
            if not cand.exists():
                break
            i += 1
    try:
        return str(cand.resolve())
    except Exception:
        return str(cand)


def _ensure_run_base_dir(base_root: str) -> str:
    """Create a new run directory under `base_root` using today's date with _N suffix."""
    cand = Path(_compute_new_run_dir_path(base_root))
    cand.mkdir(parents=True, exist_ok=True)
    return str(cand.resolve())


_LAST_RUN_MAP_PATH = Path("bulk_last_run_dirs.json")


def _load_last_run_map() -> dict[str, str]:
    try:
        if not _LAST_RUN_MAP_PATH.exists():
            return {}
        raw = _LAST_RUN_MAP_PATH.read_text(encoding="utf-8", errors="ignore")
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in data.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            out[k] = v
        return out
    except Exception:
        return {}


def _save_last_run_map(m: dict[str, str]) -> None:
    try:
        _LAST_RUN_MAP_PATH.write_text(json.dumps(m or {}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _payload_key(payload_path: str) -> str:
    """Stable key for mapping (normalized absolute path)."""
    try:
        p = str(Path(payload_path).expanduser().resolve())
        return p
    except Exception:
        return str(payload_path or "").strip()


def _list_existing_run_dirs(base_root: str) -> list[str]:
    """Return run dirs under base_root (YYYY-MM-DD, YYYY-MM-DD_N) sorted by mtime desc."""
    try:
        root = Path(base_root).expanduser()
        if not root.exists() or not root.is_dir():
            return []
        dirs: list[Path] = []
        for p in root.iterdir():
            try:
                if not p.is_dir():
                    continue
                name = p.name
                # very lax check: starts with 4-digit year
                if len(name) < 10 or not name[:4].isdigit() or name[4] != "-":
                    continue
                dirs.append(p)
            except Exception:
                continue
        dirs.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
        return [str(d.resolve()) for d in dirs]
    except Exception:
        return []


def _suggest_run_dir(base_root: str, *, payload_path: str = "") -> str:
    """Best-effort restore of previous run dir.

    IMPORTANT: this function must be side-effect free (no mkdir).
    Otherwise simply switching payloads in Streamlit can create empty run folders.

    Priority:
      1) last saved mapping for this payload_path (if exists and still exists on disk)
      2) newest existing run dir under base_root
      3) propose a new run dir path (NOT created yet)
    """
    try:
        key = _payload_key(payload_path)
        mp = _load_last_run_map()
        cand = (mp.get(key) or "").strip()
        if cand:
            try:
                if Path(cand).expanduser().exists():
                    return str(Path(cand).expanduser().resolve())
            except Exception:
                pass
        existing = _list_existing_run_dirs(base_root)
        if existing:
            return existing[0]
    except Exception:
        pass
    return _compute_new_run_dir_path(base_root)


def _remember_run_dir(payload_path: str, run_dir: str) -> None:
    """Remember last used run dir for a payload.

    IMPORTANT: must not create directories.
    We only persist mapping if the directory already exists.
    """
    try:
        key = _payload_key(payload_path)
        run_dir = str(run_dir or "").strip()
        if not key or not run_dir:
            return
        try:
            p = Path(run_dir).expanduser()
            if not p.exists() or not p.is_dir():
                return
        except Exception:
            return

        mp = _load_last_run_map()
        mp[key] = str(Path(run_dir).expanduser().resolve())
        _save_last_run_map(mp)
    except Exception:
        pass


# ------------------------ Concurrency safety ------------------------

_PROFILE_LOCKS: dict[str, threading.Lock] = {}
_PROFILE_LOCKS_GUARD = threading.Lock()


def _get_profile_lock(profile_dir: str | None) -> threading.Lock:
    key = str(profile_dir or "").strip()
    with _PROFILE_LOCKS_GUARD:
        lk = _PROFILE_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _PROFILE_LOCKS[key] = lk
        return lk


@dataclass
class BulkTask:
    article_idx: int
    article_title: str
    prompt_idx: int
    prompt: str
    out_dir: str


def _load_payload(path_str: str) -> dict[str, Any]:
    path_str = str(path_str or "").strip().strip('"').strip("'")
    if not path_str:
        raise ValueError("Payload path is empty")

    p = Path(path_str).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"Payload file not found: {p}")
    if p.is_dir():
        raise ValueError(f"Payload path is a directory, expected .json file: {p}")

    raw = p.read_text(encoding="utf-8", errors="ignore")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Payload must be a JSON object")
    return data


def _sanitize_dir_name(s: str, *, max_len: int = 80) -> str:
    import re

    s = (s or "").strip()
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        s = "article"
    return s[:max_len]


def _scan_fast_saved_items_from_disk(run_base_dir: str) -> list[dict[str, Any]]:
    """Reconstruct `bulk_saved_items` from an existing run folder on disk.

    Fast images are saved by gemini_playwright_streamlit._regenerate_prompt() as:
        {prompt_idx}_{slug}_{j:02d}.{ext}

    They are stored inside:
        <run_base_dir>/article_{article_idx:02d}_<slug>/

    After Streamlit restart, `st.session_state.bulk_saved_items` is empty, so the gallery shows
    'Не сгенерировано'. This disk scan restores preview+regen mapping.
    """
    out: list[dict[str, Any]] = []
    try:
        rb = Path(run_base_dir).expanduser()
        if not rb.exists() or not rb.is_dir():
            return []

        # Scan article_* directories only (avoid picking up unrelated folders)
        for art in rb.iterdir():
            try:
                if not art.is_dir():
                    continue
                nm = art.name
                if not nm.startswith("article_"):
                    continue
                # article_02_title -> 2
                aidx = 0
                try:
                    parts = nm.split("_", 2)
                    if len(parts) >= 2:
                        aidx = int(parts[1])
                except Exception:
                    aidx = 0

                # pick latest image per prompt_idx
                best: dict[int, Path] = {}
                best_m: dict[int, float] = {}
                for fp in art.iterdir():
                    try:
                        if not fp.is_file():
                            continue
                        # A .bin result is an unsuccessful/unknown download
                        # artifact, not a generated image.  Do not restore it
                        # into the saved-items map: otherwise the "generate all
                        # missing" check incorrectly treats that prompt as done.
                        if fp.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                            continue
                        # expect: <prompt_idx>_<slug>_<NN>.<ext>
                        base = fp.name
                        # Nano Banana Pro files should be skipped
                        if "_pro_" in base.lower():
                            continue
                        p0 = base.split("_", 1)[0]
                        if not p0.isdigit():
                            continue
                        pidx = int(p0)
                        mt = float(fp.stat().st_mtime)
                        if (pidx not in best_m) or (mt > best_m[pidx]):
                            best[pidx] = fp
                            best_m[pidx] = mt
                    except Exception:
                        continue

                for pidx, fp in best.items():
                    out.append(
                        {
                            "path": str(fp.resolve()),
                            "out_dir": str(art.resolve()),
                            "article_idx": int(aidx or 0),
                            "prompt_idx": int(pidx or 0),
                        }
                    )
            except Exception:
                continue

        # stable order: by article_idx, prompt_idx
        out.sort(key=lambda it: (int(it.get("article_idx") or 0), int(it.get("prompt_idx") or 0)))
        return out
    except Exception:
        return []


def _scan_pro_saved_items_from_disk(run_base_dir: str) -> dict[int, dict[str, Any]]:
    """Scan run folder and return latest PRO image per pro task_idx.

    PRO files are named as:
        <task_idx>_pro_<slug>_<NN>.<ext>

    Returns mapping:
        task_idx -> {path, out_dir}

    This is used to show PRO images inside Fast results (Stage1) when using Unified mode
    (or after Tongyi), without mixing them into bulk_saved_items.
    """

    import re

    best: dict[int, dict[str, Any]] = {}
    try:
        rb = Path(run_base_dir).expanduser()
        if not rb.exists() or not rb.is_dir():
            return {}

        # Scan article_* dirs
        for art in rb.iterdir():
            try:
                if not art.is_dir():
                    continue
                if not art.name.startswith("article_"):
                    continue

                for fp in art.iterdir():
                    try:
                        if not fp.is_file():
                            continue
                        if fp.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                            continue

                        m = re.match(r"^(\d+)_pro_", fp.name, flags=re.IGNORECASE)
                        if not m:
                            continue
                        tid = int(m.group(1) or 0)
                        if tid <= 0:
                            continue

                        mt = float(fp.stat().st_mtime)
                        cur = best.get(tid)
                        if (cur is None) or (float(cur.get("mtime") or 0) < mt):
                            best[tid] = {
                                "path": str(fp.resolve()),
                                "out_dir": str(art.resolve()),
                                "mtime": mt,
                            }
                    except Exception:
                        continue
            except Exception:
                continue

        # drop helper mtime
        for k in list(best.keys()):
            try:
                best[k].pop("mtime", None)
            except Exception:
                pass

        return best
    except Exception:
        return {}


def _build_tasks(articles: list[dict[str, Any]], run_base_dir: str) -> list[BulkTask]:
    tasks: list[BulkTask] = []
    for a in (articles or []):
        if not isinstance(a, dict):
            continue
        aidx = int(a.get("idx") or 0)
        title = str(a.get("title") or "").strip()
        prompts = a.get("prompts") or []
        if not isinstance(prompts, list):
            continue
        prompts = [str(p).strip() for p in prompts if str(p).strip()]
        if not prompts:
            continue
        art_dir = Path(run_base_dir) / f"article_{aidx:02d}_{_sanitize_dir_name(title)}"
        # NOTE: Do NOT create directories here. Building task lists runs during UI rerenders
        # (e.g. when switching payloads), and creating folders would leave empty artifacts on disk.
        # Directories are created lazily at generation/save time.
        for i, p in enumerate(prompts, 1):
            tasks.append(
                BulkTask(
                    article_idx=aidx,
                    article_title=title,
                    prompt_idx=i,
                    prompt=p,
                    out_dir=str(art_dir.resolve()),
                )
            )
    return tasks


def _prepare_profile_pool(
    base_profile: str,
    *,
    max_workers: int,
    start_profile_num: int = 1,
) -> tuple[queue.Queue, list[str]]:
    """Return (profile_pool, tmp_profiles_to_cleanup).

    If numbered profiles exist, uses `<base>_<start_profile_num>`, `<base>_<start_profile_num+1>`, ...
    """
    profile_pool: queue.Queue = queue.Queue()
    tmp_profiles: list[str] = []

    base_profile_norm = _normalize_user_data_dir(base_profile) or base_profile

    start_profile_num = max(1, int(start_profile_num or 1))

    def _prepare_slot(slot_idx: int) -> dict[str, Any]:
        # Prefer numbered profile
        prof_num = start_profile_num + (int(slot_idx) - 1)
        cand = f"{base_profile_norm}_{prof_num}"
        cand_norm = _normalize_user_data_dir(cand) or cand
        try:
            if Path(cand_norm).exists():
                return {"dir": cand_norm, "is_temp": False}
        except Exception:
            pass

        # Fallback: clone
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp_dir = str(Path(f"tmp_rovodev_bulk_fast_profile_{ts}_{slot_idx}").resolve())
        try:
            _clone_profile_dir(base_profile_norm, tmp_dir)
            tmp_profiles.append(tmp_dir)
            return {"dir": tmp_dir, "is_temp": True}
        except Exception:
            # last resort: base (WARNING: this will serialize parallelism due to profile locks)
            return {"dir": base_profile_norm, "is_temp": False}

    dirs: list[str] = []
    for i in range(1, max_workers + 1):
        slot = _prepare_slot(i)
        profile_pool.put(slot)
        try:
            dirs.append(str(slot.get("dir") or ""))
        except Exception:
            pass

    # Warn if we ended up with duplicate profile dirs (parallel windows will serialize)
    try:
        uniq = {d for d in dirs if d}
        if len(uniq) < len([d for d in dirs if d]):
            # We can't call st.warning here (helper used outside Streamlit sometimes), so caller may display.
            pass
    except Exception:
        pass

    return profile_pool, tmp_profiles


def _run_one_task(
    task: BulkTask,
    *,
    url: str,
    headless: bool,
    executable_path: str | None,
    model_choice: str | None,
    timeout_s: int,
    profile_pool: queue.Queue,
) -> dict[str, Any]:
    """Run one prompt in one Chrome profile.

    Tab1-like resilience:
    - if the user closes the window/tab, Playwright often raises "Target ... has been closed".
      We retry by reopening a fresh persistent context on the SAME profile (limited attempts).
    - if generation returns no saved files, we also retry once (common flaky/closed case).
    """

    def _is_retryable_error(msg: str) -> bool:
        m = (msg or "").lower()
        markers = [
            "target closed",
            "browser closed",
            "page closed",
            "has been closed",
            "context closed",
            "connection closed",
            "timeout",
        ]
        return any(x in m for x in markers)

    slot = profile_pool.get()
    try:
        profile_dir = slot.get("dir")
        lock = _get_profile_lock(profile_dir)
        with lock:
            max_session_attempts = 2
            last_err: Exception | None = None

            for session_attempt in range(1, max_session_attempts + 1):
                profile_pref_backups: list[tuple[Path, bytes]] = []
                browser_download_dir: Path | None = None
                saved_successfully = False
                try:
                    # The final image is always written into task.out_dir. This
                    # separate directory exists only while Chrome is confirming
                    # its native download, so Chrome never has to use the shared
                    # user Downloads folder for a Stage1 task.
                    try:
                        run_root = Path(task.out_dir).resolve().parent
                    except Exception:
                        run_root = Path(task.out_dir).parent
                    trace_path = (
                        run_root
                        / "_download_debug"
                        / f"stage1_a{int(task.article_idx):02d}_p{int(task.prompt_idx)}_attempt{session_attempt}.log"
                    )

                    def _download_trace(message: str) -> None:
                        try:
                            trace_path.parent.mkdir(parents=True, exist_ok=True)
                            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                            with open(trace_path, "a", encoding="utf-8") as trace_file:
                                trace_file.write(f"{stamp} {message}\n")
                        except Exception:
                            pass

                    browser_download_dir = (
                        run_root
                        / "_browser_download_tmp"
                        / (
                            f"stage1_a{int(task.article_idx):02d}_p{int(task.prompt_idx)}"
                            f"_attempt{session_attempt}_{time.time_ns()}"
                        )
                    )
                    browser_download_dir.mkdir(parents=True, exist_ok=True)
                    _download_trace(f"worker download directory: {browser_download_dir}")

                    # Match Tab3's three layers of isolation: profile
                    # preference before launch, Playwright download path during
                    # launch, and CDP after the page is available.
                    profile_pref_backups, pref_notes = _set_stage1_profile_download_directory_temporarily(
                        profile_dir,
                        str(browser_download_dir),
                    )
                    for pref_note in pref_notes:
                        _download_trace(pref_note)
                    native_watch_dirs = [str(browser_download_dir), *_stage1_native_download_watch_dirs(profile_dir)]
                    _download_trace(f"native watch directories: {native_watch_dirs}")

                    with sync_playwright() as pw:
                        ctx = _launch_persistent_ctx_with_retries(
                            pw,
                            user_data_dir=profile_dir,
                            headless=headless,
                            executable_path=executable_path,
                            downloads_path=str(browser_download_dir),
                            attempts=6,
                        )
                        try:
                            page = ctx.pages[0] if ctx.pages else ctx.new_page()
                            _download_trace(_pin_stage1_chrome_download_directory(page, str(browser_download_dir)))
                            page.set_default_timeout(30000)

                            # Ensure Gemini page
                            try:
                                cur_url = page.url or ""
                            except Exception:
                                cur_url = ""
                            if ("gemini.google.com" not in cur_url) and ("aistudio.google.com" not in cur_url):
                                try:
                                    page.goto(url, wait_until="load")
                                except Exception:
                                    pass

                            try:
                                _start_new_chat(page)
                            except Exception:
                                pass

                            _wait_input_ready(page, timeout_ms=30000)
                            _dismiss_overlays(page)

                            _, saved = _regenerate_prompt(
                                page,
                                ctx,
                                task.prompt,
                                task.prompt_idx,
                                task.out_dir,
                                max_images=1,
                                attach_base=False,
                                base_image_path=None,
                                model_choice=model_choice,
                                # Stage1 now uses the same strict native-download
                                # confirmation as Tab3. The global helper lock
                                # serializes only Download -> file confirmation,
                                # while Gemini image generation stays parallel.
                                require_browser_download=True,
                                browser_download_dir=str(browser_download_dir),
                                native_download_dirs=native_watch_dirs,
                                serialize_native_download_click=True,
                                download_debug_hook=_download_trace,
                                native_download_confirmation_timeout_s=20.0,
                            )

                            saved = saved or []
                            if saved:
                                saved_successfully = True
                                _download_trace(f"final result saved: {saved}")
                                return {
                                    "ok": True,
                                    "article_idx": task.article_idx,
                                    "article_title": task.article_title,
                                    "prompt_idx": task.prompt_idx,
                                    "out_dir": task.out_dir,
                                    "saved": saved,
                                    "error": None,
                                    "session_attempt": session_attempt,
                                    "session_attempts_total": max_session_attempts,
                                }

                            # No output: retry once by reopening context
                            if session_attempt < max_session_attempts:
                                time.sleep(0.7 * session_attempt)
                                continue

                            return {
                                "ok": False,
                                "article_idx": task.article_idx,
                                "article_title": task.article_title,
                                "prompt_idx": task.prompt_idx,
                                "out_dir": task.out_dir,
                                "saved": [],
                                "error": "No confirmed Chrome download / no images saved",
                                "session_attempt": session_attempt,
                                "session_attempts_total": max_session_attempts,
                            }
                        finally:
                            try:
                                ctx.close()
                            except Exception:
                                pass
                except Exception as e:
                    last_err = e
                    msg = str(e)
                    if session_attempt < max_session_attempts and _is_retryable_error(msg):
                        time.sleep(0.8 * session_attempt)
                        continue
                    raise
                finally:
                    for restore_note in _restore_stage1_profile_download_directory(profile_pref_backups):
                        try:
                            _download_trace(restore_note)
                        except Exception:
                            pass
                    if browser_download_dir and saved_successfully:
                        try:
                            shutil.rmtree(browser_download_dir, ignore_errors=True)
                            _download_trace("private download directory removed after confirmed save")
                        except Exception:
                            pass

            raise last_err or RuntimeError("Generation failed")
    except Exception as e:
        return {
            "ok": False,
            "article_idx": task.article_idx,
            "article_title": task.article_title,
            "prompt_idx": task.prompt_idx,
            "out_dir": task.out_dir,
            "saved": [],
            "error": str(e),
        }
    finally:
        profile_pool.put(slot)


# ---------------- Public helpers for unified pipeline (no logic changes) ----------------

def run_bulk_fast_generation(
    *,
    payload: dict[str, Any],
    run_base_dir: str,
    url: str,
    model_choice: str,
    headless: bool,
    executable_path: str | None,
    user_data_dir: str,
    parallelism: int,
    start_profile_num: int,
    timeout_s: int,
) -> dict[str, Any]:
    """Run Stage1 fast generation programmatically.

    This is the same logic as the "Сгенерировать ВСЕ картинки (fast)" button.
    Returns a small summary dict.
    """

    articles = payload.get("articles") or []
    tasks = _build_tasks(articles, run_base_dir)
    if not tasks:
        return {"ok": False, "error": "No tasks"}

    max_workers = max(1, min(12, int(parallelism), len(tasks)))
    profile_pool, tmp_profiles = _prepare_profile_pool(
        user_data_dir,
        max_workers=max_workers,
        start_profile_num=int(start_profile_num),
    )

    # Reset UI state exactly like button handler
    st.session_state.bulk_errors = []
    st.session_state.bulk_saved = {}
    st.session_state.bulk_saved_items = []
    st.session_state.bulk_regen_jobs = {}
    st.session_state.bulk_regen_dirty_counter = 0

    progress = st.progress(0)
    status = st.empty()

    done = 0
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [
            ex.submit(
                _run_one_task,
                t,
                url=url,
                headless=bool(headless),
                executable_path=executable_path,
                model_choice=model_choice,
                timeout_s=int(timeout_s),
                profile_pool=profile_pool,
            )
            for t in tasks
        ]
        for fut in concurrent.futures.as_completed(futs):
            res = fut.result() or {}
            done += 1
            if res.get("ok"):
                out_dir = str(res.get("out_dir") or "")
                saved_list = list(res.get("saved") or [])
                if saved_list:
                    st.session_state.bulk_saved.setdefault(out_dir, []).extend(saved_list)
                    for p in saved_list:
                        st.session_state.bulk_saved_items.append(
                            {
                                "path": p,
                                "out_dir": out_dir,
                                "article_idx": res.get("article_idx"),
                                "prompt_idx": res.get("prompt_idx"),
                            }
                        )
            else:
                st.session_state.bulk_errors.append(res)

            progress.progress(int(done / max(1, len(tasks)) * 100))
            try:
                status.write(f"{done}/{len(tasks)} done")
            except Exception:
                pass

    # Cleanup temp profiles (same pattern as UI)
    try:
        for p in tmp_profiles or []:
            try:
                shutil.rmtree(str(p), ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass

    return {
        "ok": True,
        "total": len(tasks),
        "saved": sum(len(v) for v in (st.session_state.bulk_saved or {}).values()),
        "errors": len(st.session_state.bulk_errors or []),
        "run_base_dir": run_base_dir,
    }


def run_bulk_fast_generation_subset(
    *,
    tasks: list[BulkTask],
    run_base_dir: str,
    url: str,
    model_choice: str,
    headless: bool,
    executable_path: str | None,
    user_data_dir: str,
    parallelism: int,
    start_profile_num: int,
    timeout_s: int,
) -> dict[str, Any]:
    """Run Stage1 fast generation for a selected task subset.

    Unlike `run_bulk_fast_generation`, this preserves already generated images
    and appends only newly saved files to the current gallery state.
    """

    tasks = [t for t in list(tasks or []) if t is not None]
    if not tasks:
        return {"ok": True, "total": 0, "saved": 0, "errors": 0, "run_base_dir": run_base_dir}

    max_workers = max(1, min(12, int(parallelism), len(tasks)))
    profile_pool, tmp_profiles = _prepare_profile_pool(
        user_data_dir,
        max_workers=max_workers,
        start_profile_num=int(start_profile_num),
    )

    st.session_state.bulk_errors = []
    st.session_state.setdefault("bulk_saved", {})
    st.session_state.setdefault("bulk_saved_items", [])

    progress = st.progress(0)
    status = st.empty()

    done = 0
    saved_count = 0
    errors: list[dict[str, Any]] = []

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = [
                ex.submit(
                    _run_one_task,
                    t,
                    url=url,
                    headless=bool(headless),
                    executable_path=executable_path,
                    model_choice=model_choice,
                    timeout_s=int(timeout_s),
                    profile_pool=profile_pool,
                )
                for t in tasks
            ]

            for fut in concurrent.futures.as_completed(futs):
                res = fut.result() or {}
                done += 1

                if res.get("ok"):
                    out_dir = str(res.get("out_dir") or "")
                    saved_list = [str(p) for p in list(res.get("saved") or []) if str(p or "").strip()]
                    if saved_list:
                        st.session_state.bulk_saved.setdefault(out_dir, [])
                        st.session_state.bulk_saved[out_dir].extend(saved_list)
                        st.session_state.bulk_saved[out_dir] = list(dict.fromkeys(st.session_state.bulk_saved[out_dir]))

                        for p in saved_list:
                            st.session_state.bulk_saved_items.append(
                                {
                                    "path": p,
                                    "out_dir": out_dir,
                                    "article_idx": res.get("article_idx"),
                                    "prompt_idx": res.get("prompt_idx"),
                                }
                            )
                        saved_count += len(saved_list)
                else:
                    errors.append(res)

                progress.progress(int(done / max(1, len(tasks)) * 100))
                try:
                    status.write(f"{done}/{len(tasks)} done")
                except Exception:
                    pass
    finally:
        try:
            for p in tmp_profiles or []:
                try:
                    shutil.rmtree(str(p), ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            pass

    if errors:
        st.session_state.bulk_errors.extend(errors)

    # De-dup gallery items by path after appending new subset results.
    try:
        seen_paths: set[str] = set()
        dedup: list[dict[str, Any]] = []
        for it in list(st.session_state.get("bulk_saved_items") or []):
            pth = str((it or {}).get("path") or "")
            if not pth or pth in seen_paths:
                continue
            seen_paths.add(pth)
            dedup.append(it)
        st.session_state.bulk_saved_items = dedup
    except Exception:
        pass

    return {
        "ok": True,
        "total": len(tasks),
        "saved": int(saved_count),
        "errors": len(errors),
        "run_base_dir": run_base_dir,
    }


def _move_fast_result_to_pro_name_with_retries(
    *,
    src_path: str,
    out_dir: str,
    task_idx: int,
    prompt_raw: str | None,
) -> str:
    final_p = str(src_path)

    for attempt in range(1, 4):
        try:
            final_p = _move_image_to_pro_named_file(
                src_path=str(src_path),
                out_dir=str(out_dir),
                task_idx=int(task_idx),
                prompt_raw=str(prompt_raw or ""),
            )
        except Exception:
            final_p = str(src_path)

        try:
            if final_p and "_pro_" in Path(str(final_p)).name.lower():
                return str(final_p)
        except Exception:
            pass

        time.sleep(0.6 * attempt)

    # Last-resort direct rename, mirroring the per-image PRO regen fallback.
    try:
        sp = Path(str(src_path)).expanduser()
        if sp.exists() and sp.is_file():
            ext = (sp.suffix or ".png").lstrip(".").lower() or "png"
            dst = Path(str(out_dir)).expanduser() / _predict_nbp_image_basename(
                int(task_idx),
                str(prompt_raw or ""),
                j=1,
                ext=str(ext),
                out_dir=str(out_dir),
            )
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst = _rename_with_collision(dst)
            for attempt in range(1, 6):
                try:
                    os.replace(str(sp), str(dst))
                    return str(dst)
                except Exception:
                    time.sleep(0.35 * attempt)
    except Exception:
        pass

    return str(final_p)


def run_bulk_fast_pro_generation_subset(
    *,
    tasks: list[ProTask],
    run_base_dir: str,
    url: str,
    headless: bool,
    executable_path: str | None,
    user_data_dir: str,
    parallelism: int,
    start_profile_num: int,
    timeout_s: int,
    model_choice: str = "Nano Banana Pro",
) -> dict[str, Any]:
    """Generate selected missing PRO/NBP images via the Fast browser runner.

    Results are moved to the canonical `<task_idx>_pro_<slug>_NN.ext` names,
    matching the Pro tab and the per-image PRO regeneration in Fast results.
    """

    tasks = [t for t in list(tasks or []) if t is not None]
    if not tasks:
        return {"ok": True, "total": 0, "saved": 0, "errors": 0, "run_base_dir": run_base_dir}

    st.session_state.setdefault("bulkpro_saved_paths", [])
    st.session_state.setdefault("bulkpro_errors", [])

    progress = st.progress(0)
    status = st.empty()

    done = 0
    saved_count = 0
    errors: list[dict[str, Any]] = []

    task_items: list[dict[str, Any]] = []
    for t in tasks:
        try:
            pkey = f"bulkpro_prompt_{int(t.article_idx)}_{int(t.prompt_idx)}"
            prompt_raw = (st.session_state.get(pkey) or str(t.prompt) or "").strip()
            prompt_clean = _sanitize_prompt(prompt_raw or "").strip()
            final_prompt = _wrap_prompt_for_nbp_fast(prompt_clean)
            if not final_prompt:
                errors.append(
                    {
                        "ok": False,
                        "task_idx": int(getattr(t, "task_idx", 0) or 0),
                        "article_idx": int(getattr(t, "article_idx", 0) or 0),
                        "prompt_idx": int(getattr(t, "prompt_idx", 0) or 0),
                        "out_dir": str(getattr(t, "out_dir", run_base_dir) or run_base_dir),
                        "error": "Empty PRO prompt",
                    }
                )
                continue
            task_items.append({"task": t, "prompt_clean": prompt_clean, "final_prompt": final_prompt})
        except Exception as e:
            errors.append(
                {
                    "ok": False,
                    "task_idx": int(getattr(t, "task_idx", 0) or 0),
                    "article_idx": int(getattr(t, "article_idx", 0) or 0),
                    "prompt_idx": int(getattr(t, "prompt_idx", 0) or 0),
                    "out_dir": str(getattr(t, "out_dir", run_base_dir) or run_base_dir),
                    "error": str(e),
                }
            )

    if not task_items:
        if errors:
            try:
                st.session_state.bulkpro_errors.extend(errors)
            except Exception:
                st.session_state["bulkpro_errors"] = list(errors)
        return {
            "ok": True,
            "total": len(tasks),
            "saved": 0,
            "errors": len(errors),
            "run_base_dir": run_base_dir,
        }

    max_workers = max(1, min(12, int(parallelism), len(task_items) or 1))
    profile_pool, tmp_profiles = _prepare_profile_pool(
        user_data_dir,
        max_workers=max_workers,
        start_profile_num=int(start_profile_num),
    )

    def _run_one_pro_missing(item: dict[str, Any]) -> dict[str, Any]:
        t = item["task"]
        prompt_clean = str(item.get("prompt_clean") or "")
        final_prompt = str(item.get("final_prompt") or "")
        try:
            pro_prompt_idx = int(1000 + int(t.task_idx))
            bt = BulkTask(
                article_idx=int(t.article_idx),
                article_title=str(t.article_title),
                prompt_idx=int(pro_prompt_idx),
                prompt=str(final_prompt),
                out_dir=str(t.out_dir),
            )

            res = _run_one_task(
                bt,
                url=str(url),
                headless=bool(headless),
                executable_path=executable_path,
                model_choice=str(model_choice or "Nano Banana Pro"),
                timeout_s=int(timeout_s),
                profile_pool=profile_pool,
            )
            return {"ok": True, "task": t, "prompt_clean": prompt_clean, "result": res}
        except Exception as e:
            return {
                "ok": False,
                "task": t,
                "prompt_clean": str(getattr(t, "prompt", "") or ""),
                "result": {"ok": False, "error": str(e)},
            }

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = [ex.submit(_run_one_pro_missing, item) for item in task_items]

            for fut in concurrent.futures.as_completed(futs):
                item = fut.result() or {}
                done += 1

                t = item.get("task")
                res = item.get("result") or {}
                prompt_clean = str(item.get("prompt_clean") or "")

                if bool(res.get("ok")):
                    saved_list = [str(p) for p in list(res.get("saved") or []) if str(p or "").strip()]
                    if saved_list and t is not None:
                        for sp in saved_list:
                            final_p = _move_fast_result_to_pro_name_with_retries(
                                src_path=str(sp),
                                out_dir=str(getattr(t, "out_dir", run_base_dir) or run_base_dir),
                                task_idx=int(getattr(t, "task_idx", 0) or 0),
                                prompt_raw=str(prompt_clean or ""),
                            )
                            st.session_state.bulkpro_saved_paths.append(str(final_p))
                            saved_count += 1
                    else:
                        errors.append(
                            {
                                "ok": False,
                                "task_idx": int(getattr(t, "task_idx", 0) or 0) if t is not None else None,
                                "error": "No PRO images saved",
                            }
                        )
                else:
                    errors.append(
                        {
                            "ok": False,
                            "task_idx": int(getattr(t, "task_idx", 0) or 0) if t is not None else None,
                            "article_idx": int(getattr(t, "article_idx", 0) or 0) if t is not None else None,
                            "prompt_idx": int(getattr(t, "prompt_idx", 0) or 0) if t is not None else None,
                            "out_dir": str(getattr(t, "out_dir", run_base_dir) or run_base_dir) if t is not None else str(run_base_dir),
                            "error": str(res.get("error") or "Unknown"),
                        }
                    )

                progress.progress(int(done / max(1, len(task_items)) * 100))
                try:
                    status.write(f"PRO/NBP missing: {done}/{len(task_items)} done")
                except Exception:
                    pass
    finally:
        try:
            for p in tmp_profiles or []:
                try:
                    shutil.rmtree(str(p), ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            pass

    if errors:
        try:
            st.session_state.bulkpro_errors.extend(errors)
        except Exception:
            st.session_state["bulkpro_errors"] = list(errors)

    try:
        st.session_state["bulkpro_saved_paths"] = list(dict.fromkeys(st.session_state.get("bulkpro_saved_paths") or []))
    except Exception:
        pass

    return {
        "ok": True,
        "total": len(tasks),
        "saved": int(saved_count),
        "errors": len(errors),
        "run_base_dir": run_base_dir,
    }


def run_bulk_tongyi_generation(
    *,
    tasks: list[ProTask],
    tasks_snapshot: list[dict[str, Any]],
    run_base_dir: str,
    tongyi_space_url: str,
    tongyi_headless: bool,
    tongyi_timeout_s: int,
    tongyi_size_text: str,
    tongyi_start_from: int = 1,
) -> dict[str, Any]:
    """Run Tongyi stage programmatically.

    Mirrors the Tongyi branch inside bulkpro_run button.
    """

    if tongyi_generate_batch_detailed is None:
        err = _TONGYI_IMPORT_ERROR or "Unknown import error"
        return {"ok": False, "error": err}

    if bool(st.session_state.get("bulkpro_tongyi_running")):
        return {"ok": False, "error": "Tongyi already running"}

    st.session_state["bulkpro_tongyi_running"] = True
    st.session_state["bulkpro_tongyi_cancel"] = False

    # Start from N-th prompt
    start_from = max(1, int(tongyi_start_from or 1))
    if start_from > 1:
        tasks_snapshot = tasks_snapshot[(start_from - 1) :]
        if not tasks_snapshot:
            st.session_state["bulkpro_tongyi_running"] = False
            return {"ok": False, "error": "Nothing to run after start_from"}

    # group tasks by out_dir
    grouped: dict[str, list[dict[str, Any]]] = {}
    for it in tasks_snapshot:
        task = it["task"]
        out_dir = str(getattr(task, "out_dir", "") or "").strip() or str(run_base_dir)
        grouped.setdefault(out_dir, []).append({"task": task, "prompt": it["prompt"]})

    total = sum(len(v) for v in grouped.values())
    progress = st.progress(0)
    done = 0

    def _cb(i, n, prompt, item):
        nonlocal done
        done += 1
        progress.progress(int(done / max(1, total) * 100))

    # local log sink (same as UI tab)
    st.session_state.setdefault("bulkpro_tongyi_log_lines", [])

    def _tongyi_log2(line: str) -> None:
        try:
            lines = st.session_state.get("bulkpro_tongyi_log_lines") or []
            lines.append(str(line))
            if len(lines) > 2000:
                lines = lines[-2000:]
            st.session_state["bulkpro_tongyi_log_lines"] = lines
        except Exception:
            pass

    try:
        with st.status("Запуск Tongyi (sequential)...", expanded=True) as status:
            for out_dir, items in grouped.items():
                # Ensure output folder exists ONLY when we actually run Tongyi.
                # (Avoid side effects during payload preview/render.)
                try:
                    Path(str(out_dir)).expanduser().mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass

                prompts = [x["prompt"] for x in items]

                def _on_saved(ii: int, _tt: int, saved_path: str) -> str:
                    idx0 = max(0, int(ii) - 1)
                    if idx0 >= len(items):
                        return saved_path
                    task0 = items[idx0].get("task")
                    prompt0 = str(items[idx0].get("prompt") or "")
                    final_p = _move_image_to_pro_named_file(
                        src_path=str(saved_path),
                        out_dir=str(getattr(task0, "out_dir", out_dir) or out_dir),
                        task_idx=int(getattr(task0, "task_idx", 0) or 0),
                        prompt_raw=prompt0,
                    )
                    return str(final_p)

                tongyi_generate_batch_detailed(
                    space_url=str(tongyi_space_url or TONGYI_DEFAULT_SPACE_URL),
                    prompts=list(prompts),
                    out_dir=str(out_dir),
                    headless=bool(tongyi_headless),
                    hosts_start_pos=None,
                    cancel_check=lambda: bool(st.session_state.get("bulkpro_tongyi_cancel")),
                    max_wait_sec=int(tongyi_timeout_s),
                    size_text=str(tongyi_size_text or TONGYI_DEFAULT_SIZE_TEXT),
                    progress_callback=_cb,
                    log_callback=_tongyi_log2,
                    on_image_saved=_on_saved,
                )
                try:
                    status.write(f"Done group: {out_dir}")
                except Exception:
                    pass
    finally:
        st.session_state["bulkpro_tongyi_running"] = False

    return {"ok": True, "total": total, "run_base_dir": run_base_dir}


def _render_fast_tab() -> None:
    # Inputs
    # NOTE: always pass explicit `key=` to avoid StreamlitDuplicateElementId on first render
    # when this app is opened in parallel / embedded workflows.
    url = st.selectbox("URL интерфейса", DEFAULT_URLS, index=0, key="bulkfast_url")
    model_choice = st.selectbox("Модель Gemini", ["Быстрая", "Думающая"], index=0, key="bulkfast_model")
    headless = st.checkbox("Headless", value=False, key="bulkfast_headless")
    executable_path = st.text_input(
        "Путь к chrome.exe",
        value=r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        key="bulkfast_exe_path",
    ).strip() or None

    user_data_dir = st.text_input(
        "Базовый user-data-dir (будут использованы _1.._N если существуют, иначе будут создаваться временные копии)",
        value=os.path.abspath(".chrome_automation_profile"),
        key="bulkfast_user_data_dir",
    )
    user_data_dir = _normalize_user_data_dir(user_data_dir) or user_data_dir
    st.caption(f"Реально используется: {user_data_dir}")

    if not bool(st.session_state.get("bulkfast_defaults_20260625_migrated")):
        try:
            if int(st.session_state.get("bulkfast_parallelism", 7) or 7) in (3, 4):
                st.session_state["bulkfast_parallelism"] = 7
        except Exception:
            st.session_state["bulkfast_parallelism"] = 7
        try:
            if int(st.session_state.get("bulkfast_timeout", 100) or 100) == 90:
                st.session_state["bulkfast_timeout"] = 100
        except Exception:
            st.session_state["bulkfast_timeout"] = 100
        st.session_state["bulkfast_defaults_20260625_migrated"] = True

    parallelism = st.number_input("Параллельно окон", min_value=1, max_value=12, value=7, key="bulkfast_parallelism")
    start_profile_num = st.number_input(
        "Стартовый номер профиля (если 6 → будут использованы _6,_7,_8,...) ",
        min_value=1,
        max_value=999,
        value=int(st.session_state.get("bulkfast_start_profile_num", 1) or 1),
        step=1,
        key="bulkfast_start_profile_num",
    )
    timeout_s = st.number_input(
        "Timeout (сек)",
        min_value=30,
        max_value=300,
        value=int(st.session_state.get("bulkfast_timeout") or 100),
        key="bulkfast_timeout",
    )

    st.markdown("---")

    # Load payload
    # If user loaded payload in Pro tab, mirror it here automatically.
    try:
        bp = str(st.session_state.get("bulkpro_payload_path") or "").strip()
        bf = str(st.session_state.get("bulkfast_payload_path") or "").strip()
        if bp and not bf:
            st.session_state["bulkfast_payload_path"] = bp
    except Exception:
        pass

    load_file = st.query_params.get("load_file") if hasattr(st, "query_params") else None
    if isinstance(load_file, list):
        load_file = load_file[0] if load_file else None
    load_file = str(load_file).strip() if load_file else ""

    payload_path = st.text_input(
        "Payload JSON path",
        value=load_file or "",
        key="bulkfast_payload_path",
    )

    if "bulk_payload" not in st.session_state:
        st.session_state.bulk_payload = None

    col_load1, col_load2 = st.columns([1, 4])
    with col_load1:
        if st.button("Загрузить payload", type="primary"):
            try:
                st.session_state.bulk_payload = _load_payload(payload_path)
                # Sync payload path to Pro tab as well (same payload drives both tabs).
                try:
                    st.session_state["bulkpro_payload_path"] = str(payload_path or "").strip()
                except Exception:
                    pass

                # Reset Pro-tab cached state so it reflects the new payload.
                try:
                    st.session_state["bulkpro_saved_paths"] = []
                    st.session_state["bulkpro_errors"] = []
                    st.session_state["bulkpro_saved_items"] = []
                    st.session_state["bulkpro_saved"] = {}
                except Exception:
                    pass

                # When switching payloads, reset run dir + cached results so UI reflects the new payload.
                try:
                    base_root0 = str((st.session_state.bulk_payload or {}).get("base_root") or "generate automation")
                    sel_dir = _suggest_run_dir(base_root0, payload_path=payload_path)
                    st.session_state.bulk_run_base_dir = sel_dir
                    try:
                        # also reset the text_input bound value (key persists across payload switches)
                        st.session_state["bulkfast_run_base_dir"] = sel_dir
                    except Exception:
                        pass
                except Exception:
                    # keep previous if suggest fails
                    pass
                try:
                    # Reset cached article/prompt editor state so UI does not stick to previous payload
                    st.session_state.bulk_articles = (st.session_state.bulk_payload or {}).get("articles") or []

                    # IMPORTANT: prompt text_inputs are keyed by (article_idx,prompt_idx) only.
                    # When switching payloads, Streamlit will keep old widget values unless we clear them.
                    for _k in list(st.session_state.keys()):
                        if str(_k).startswith("bulk_prompt_"):
                            try:
                                del st.session_state[_k]
                            except Exception:
                                pass
                except Exception:
                    pass

                try:
                    st.session_state["bulk_errors"] = []
                    st.session_state["bulkfast_last_disk_scan_run_dir"] = ""
                    st.session_state["bulkfast_force_disk_rescan"] = True
                    st.session_state["bulk_saved_items"] = []
                    st.session_state["bulk_saved"] = {}
                except Exception:
                    pass

                st.success("Payload загружен")
            except Exception as e:
                st.session_state.bulk_payload = None
                st.error(f"Не удалось загрузить payload: {e}")

    payload = st.session_state.bulk_payload
    if not payload:
        st.info("Укажите путь к payload JSON (его создаёт Tab0 в app_unified_streamlit.py) и нажмите 'Загрузить payload'.")
        return

    base_root = str(payload.get("base_root") or "generate automation")
    articles = payload.get("articles") or []

    # Run directory (restore previous if possible)
    if "bulk_run_base_dir" not in st.session_state:
        try:
            st.session_state.bulk_run_base_dir = _suggest_run_dir(base_root, payload_path=payload_path)
        except Exception:
            st.session_state.bulk_run_base_dir = _suggest_run_dir("generate automation", payload_path=payload_path)

    existing_runs = _list_existing_run_dirs(base_root)
    with st.expander("Run folder (восстановление / выбор)", expanded=False):
        if existing_runs:
            picked = st.selectbox(
                "Existing run dirs",
                options=existing_runs,
                index=0,
                key="bulkfast_existing_run_dirs",
                help="Выбери существующую папку, чтобы подтянуть уже сгенерированные картинки после перезапуска.",
            )
            if st.button("Use selected run dir", key="bulkfast_use_selected_run"):
                sel = str(picked)
                st.session_state.bulk_run_base_dir = sel
                try:
                    st.session_state["bulkfast_run_base_dir"] = sel
                except Exception:
                    pass
                try:
                    st.session_state["bulk_errors"] = []
                    # Force disk-rescan for selected run dir
                    st.session_state["bulkfast_last_disk_scan_run_dir"] = ""
                    st.session_state["bulkfast_force_disk_rescan"] = True
                    st.session_state["bulk_saved_items"] = []
                    st.session_state["bulk_saved"] = {}
                except Exception:
                    pass
                try:
                    st.rerun()
                except Exception:
                    pass
        if st.button("Create NEW run dir", key="bulkfast_create_new_run"):
            newd = _ensure_run_base_dir(base_root)
            st.session_state.bulk_run_base_dir = newd
            try:
                st.session_state["bulkfast_run_base_dir"] = newd
            except Exception:
                pass
            try:
                st.session_state["bulk_errors"] = []
                st.session_state["bulkfast_last_disk_scan_run_dir"] = ""
                st.session_state["bulk_saved_items"] = []
                st.session_state["bulk_saved"] = {}
            except Exception:
                pass
            try:
                st.rerun()
            except Exception:
                pass

    run_base_dir = st.text_input(
        "Run dir (корень сохранения для этого запуска)",
        value=st.session_state.bulk_run_base_dir,
        key="bulkfast_run_base_dir",
    )
    st.session_state.bulk_run_base_dir = run_base_dir
    _remember_run_dir(payload_path, run_base_dir)

    tasks = _build_tasks(articles, run_base_dir)

    st.subheader(f"Задания: {len(tasks)}")
    if not tasks:
        st.warning("В payload нет fast-промптов")
        return

    show_regen_near_prompts = st.checkbox(
        "Показывать кнопку 'Пересоздать' рядом с промптами (обычно удобнее под картинкой)",
        value=False,
        key="bulkfast_show_regen_near_prompts",
    )

    with st.expander("Промпты (по статьям)", expanded=False):
        # Render editable prompts per article
        # Store in session state to allow edits.
        if "bulk_articles" not in st.session_state:
            st.session_state.bulk_articles = articles

        new_articles: list[dict[str, Any]] = []
        for a in (st.session_state.bulk_articles or []):
            if not isinstance(a, dict):
                continue
            aidx = int(a.get("idx") or 0)
            title = str(a.get("title") or "").strip()
            prompts = a.get("prompts") or []
            if not isinstance(prompts, list) or not prompts:
                continue

            st.markdown(f"#### #{aidx}. {title}")
            new_prompts: list[str] = []

            # Prompt editor + per-prompt regenerate button (Tab1-like)
            for i, p in enumerate(prompts, 1):
                c_prompt, c_btn = st.columns([8, 1], gap="small", vertical_alignment="bottom")
                with c_prompt:
                    nv = st.text_input(
                        f"Prompt {aidx}.{i}",
                        value=str(p),
                        key=f"bulk_prompt_{aidx}_{i}",
                    )
                with c_btn:
                    if show_regen_near_prompts:
                        if st.button("🔄 Пересоздать", key=f"bulk_regen_btn_{aidx}_{i}"):
                            # Regenerate just this one prompt synchronously.
                            # We reuse the same machinery as bulk run (Playwright + _regenerate_prompt)
                            try:
                                # Refresh articles from current UI values first
                                st.session_state.bulk_articles = st.session_state.get("bulk_articles") or articles

                                # Rebuild tasks from current edited prompts
                                _tasks_now = _build_tasks(st.session_state.bulk_articles, run_base_dir)
                                task_map = {(t.article_idx, t.prompt_idx): t for t in _tasks_now}
                                t = task_map.get((aidx, i))
                                if not t:
                                    st.error("Не удалось найти задачу для этого промпта")
                                else:
                                    # Use the shared regen profile pool (so regen-near-prompts can also be parallel)
                                    regen_workers = max(1, min(12, int(parallelism)))
                                    sig = (str(user_data_dir or ""), int(regen_workers), int(start_profile_num))
                                    if st.session_state.get("bulk_regen_profile_pool") is None or st.session_state.get("bulk_regen_pool_sig") != sig:
                                        for d in list(st.session_state.get("bulk_regen_tmp_profiles") or []):
                                            try:
                                                shutil.rmtree(d, ignore_errors=True)
                                            except Exception:
                                                pass
                                        pp, tmps = _prepare_profile_pool(
                                            user_data_dir,
                                            max_workers=int(regen_workers),
                                            start_profile_num=int(start_profile_num),
                                        )
                                        st.session_state.bulk_regen_profile_pool = pp
                                        st.session_state.bulk_regen_tmp_profiles = tmps
                                        st.session_state.bulk_regen_pool_sig = sig

                                    shared_pool = st.session_state.bulk_regen_profile_pool
                                    # Determine existing image path for this (article,prompt) so we can delete it after regen.
                                    replace_path = ""
                                    try:
                                        for it0 in reversed(list(st.session_state.get("bulk_saved_items") or [])):
                                            if int(it0.get("article_idx") or 0) == int(aidx) and int(it0.get("prompt_idx") or 0) == int(i):
                                                replace_path = str(it0.get("path") or "")
                                                if replace_path:
                                                    break
                                    except Exception:
                                        replace_path = ""

                                    # Regen variation: rotate a polite prefix to avoid identical outputs
                                    try:
                                        prompt_var = _apply_regen_prefix_variation(t.prompt, key=f"{aidx}.{i}")
                                    except Exception:
                                        prompt_var = t.prompt

                                    t2 = BulkTask(
                                        article_idx=t.article_idx,
                                        article_title=t.article_title,
                                        prompt_idx=t.prompt_idx,
                                        prompt=str(prompt_var or t.prompt),
                                        out_dir=t.out_dir,
                                    )

                                    res = _run_one_task(
                                        t2,
                                        url=url,
                                        headless=bool(headless),
                                        executable_path=executable_path,
                                        model_choice=model_choice,
                                        timeout_s=int(timeout_s),
                                        profile_pool=shared_pool,
                                    )
                                    if res.get("ok"):
                                        out_dir = str(res.get("out_dir") or "")
                                        new_saved = list(res.get("saved") or [])
                                        if new_saved:
                                            st.session_state.bulk_saved.setdefault(out_dir, [])
                                            st.session_state.bulk_saved[out_dir].extend(new_saved)
                                            # de-dup while preserving order
                                            st.session_state.bulk_saved[out_dir] = list(dict.fromkeys(st.session_state.bulk_saved[out_dir]))

                                            # Best-effort: remove old file on disk if we replaced it.
                                            try:
                                                if replace_path and new_saved and os.path.normcase(os.path.abspath(replace_path)) != os.path.normcase(os.path.abspath(new_saved[0])):
                                                    if os.path.exists(replace_path):
                                                        os.remove(replace_path)
                                            except Exception:
                                                pass

                                            for pth in new_saved:
                                                st.session_state.bulk_saved_items.append(
                                                    {
                                                        "path": pth,
                                                        "out_dir": out_dir,
                                                        "article_idx": int(res.get("article_idx") or aidx),
                                                        "prompt_idx": int(res.get("prompt_idx") or i),
                                                    }
                                                )

                                            st.success(f"Готово: сохранено {len(new_saved)}")
                                        else:
                                            st.warning("Пересоздание выполнено, но файлы не сохранены")
                                    else:
                                        st.error(str(res.get("error") or "Ошибка пересоздания"))

                                    # No explicit st.rerun(): the button click already triggered a rerun.
                                    # Mark UI as dirty so autorefresh repaints the updated image.
                                    st.session_state["bulk_regen_dirty_counter"] = max(
                                        3,
                                        int(st.session_state.get("bulk_regen_dirty_counter") or 0),
                                    )
                            except Exception as e:
                                st.error(f"Ошибка пересоздания: {e}")

                new_prompts.append(nv)

            new_articles.append({"idx": aidx, "title": title, "prompts": new_prompts})

        st.session_state.bulk_articles = new_articles
        # Refresh tasks from edited prompts
        tasks = _build_tasks(st.session_state.bulk_articles, run_base_dir)

    # Storage for results
    # bulk_saved: legacy map for quick folder grouping
    if "bulk_saved" not in st.session_state:
        st.session_state.bulk_saved = {}  # out_dir -> list[path]
    # bulk_saved_items: rich list for per-image actions (regen)
    if "bulk_saved_items" not in st.session_state:
        st.session_state.bulk_saved_items = []  # list[dict(path,out_dir,article_idx,prompt_idx)]
    if "bulk_errors" not in st.session_state:
        st.session_state.bulk_errors = []

    # Background regen (Tab1-like: click multiple times -> parallel in several windows)
    if "bulk_regen_jobs" not in st.session_state:
        # job_id -> {status,article_idx,prompt_idx,created_at,replace_path,started_at,ended_at,error,future}
        st.session_state.bulk_regen_jobs = {}
    # Thread-safe queue with completed job results (avoid fragile Future polling in Streamlit)
    if "bulk_regen_result_queue" not in st.session_state:
        st.session_state.bulk_regen_result_queue = queue.Queue()
    if "bulk_regen_executor" not in st.session_state:
        st.session_state.bulk_regen_executor = None
    if "bulk_regen_job_seq" not in st.session_state:
        st.session_state.bulk_regen_job_seq = 0
    if "bulk_regen_lock" not in st.session_state:
        st.session_state.bulk_regen_lock = threading.Lock()
    if "bulk_regen_dirty_counter" not in st.session_state:
        st.session_state.bulk_regen_dirty_counter = 0

    # Shared profile pool for regen (critical for real parallel windows)
    if "bulk_regen_profile_pool" not in st.session_state:
        st.session_state.bulk_regen_profile_pool = None
    if "bulk_regen_tmp_profiles" not in st.session_state:
        st.session_state.bulk_regen_tmp_profiles = []
    if "bulk_regen_pool_sig" not in st.session_state:
        # (user_data_dir, workers)
        st.session_state.bulk_regen_pool_sig = None

    if st.button("Сгенерировать ВСЕ картинки (fast)", type="primary"):
        run_bulk_fast_generation(
            payload=payload,
            run_base_dir=str(run_base_dir),
            url=str(url),
            model_choice=str(model_choice),
            headless=bool(headless),
            executable_path=executable_path,
            user_data_dir=str(user_data_dir),
            parallelism=int(parallelism),
            start_profile_num=int(start_profile_num),
            timeout_s=int(timeout_s),
        )

    def _apply_regen_result(job_id: str, res: dict[str, Any] | None, err: str | None, ended_at: str | None) -> bool:
        """Apply one regen completion into session_state. Returns True if state changed."""
        jobs = st.session_state.get("bulk_regen_jobs") or {}
        job = jobs.get(job_id) or {}
        changed_local = False

        if err:
            job["status"] = "error"
            job["error"] = str(err)
            job["ended_at"] = ended_at
            jobs[job_id] = job
            st.session_state.bulk_regen_jobs = jobs
            return True

        res = res or {}
        if isinstance(res, dict) and res.get("ok"):
            job["status"] = "done"
            job["ended_at"] = ended_at
            jobs[job_id] = job
            st.session_state.bulk_regen_jobs = jobs

            # PRO jobs need extra post-processing: rename to N_pro_* and update bulkpro_saved_paths.
            if str(job.get("kind") or "") == "pro":
                new_saved = list(res.get("saved") or [])
                replace_path = str(job.get("replace_path") or "")
                pro_task_idx = int(job.get("pro_task_idx") or 0)
                pro_prompt_raw = str(job.get("pro_prompt_raw") or "")
                pro_out_dir = str(job.get("pro_out_dir") or res.get("out_dir") or "")

                if new_saved:
                    tmp_path = str(new_saved[0])

                    # Fallback: if metadata missing, derive pro_task_idx from our virtual prompt_idx = 1000 + task_idx
                    try:
                        if int(pro_task_idx or 0) <= 0:
                            pidx0 = int(job.get("prompt_idx") or 0)
                            if pidx0 >= 1000:
                                pro_task_idx = int(pidx0 - 1000)
                    except Exception:
                        pass

                    # Fallback: out_dir from tmp_path
                    try:
                        if not str(pro_out_dir or "").strip():
                            pro_out_dir = str(Path(tmp_path).expanduser().resolve().parent)
                    except Exception:
                        pass

                    final_p = tmp_path
                    # Try multiple times: downloads/files can be locked for a short time.
                    for _attempt in range(1, 4):
                        try:
                            final_p = _move_image_to_pro_named_file(
                                src_path=str(tmp_path),
                                out_dir=str(pro_out_dir),
                                task_idx=int(pro_task_idx),
                                prompt_raw=str(pro_prompt_raw),
                            )
                        except Exception:
                            final_p = tmp_path

                        try:
                            if final_p and "_pro_" in Path(str(final_p)).name.lower():
                                break
                        except Exception:
                            pass

                        time.sleep(0.6 * _attempt)

                    # Last resort: try direct rename into canonical name
                    try:
                        if final_p and "_pro_" not in Path(str(final_p)).name.lower():
                            sp = Path(tmp_path).expanduser()
                            ext = (sp.suffix or ".png").lstrip(".").lower() or "png"
                            bn = _predict_nbp_image_basename(int(pro_task_idx), str(pro_prompt_raw), j=1, ext=str(ext), out_dir=str(pro_out_dir))
                            dst = Path(str(pro_out_dir)).expanduser() / bn
                            dst = _rename_with_collision(dst)
                            for _a in range(1, 6):
                                try:
                                    os.replace(str(sp), str(dst))
                                    final_p = str(dst)
                                    break
                                except Exception:
                                    time.sleep(0.35 * _a)
                    except Exception:
                        pass

                    # Best-effort: remove old file on disk if we replaced it.
                    try:
                        if replace_path and final_p and os.path.normcase(os.path.abspath(replace_path)) != os.path.normcase(os.path.abspath(final_p)):
                            if os.path.exists(replace_path):
                                os.remove(replace_path)
                    except Exception:
                        pass

                    st.session_state.setdefault("bulkpro_saved_paths", []).append(str(final_p))
                    try:
                        st.session_state["bulkpro_saved_paths"] = list(dict.fromkeys(st.session_state.get("bulkpro_saved_paths") or []))
                    except Exception:
                        pass

                    job["final_path"] = str(final_p)
                    jobs[job_id] = job
                    st.session_state.bulk_regen_jobs = jobs

                changed_local = True
                return changed_local

            # Normal (non-pro) jobs: update bulk_saved_items for Fast gallery.
            out_dir2 = str(res.get("out_dir") or "")
            new_saved = list(res.get("saved") or [])
            replace_path = str(job.get("replace_path") or "")

            if new_saved:
                new_path = str(new_saved[0])

                if out_dir2:
                    lst = list(st.session_state.bulk_saved.get(out_dir2) or [])
                    if replace_path and replace_path in lst:
                        lst = [x for x in lst if x != replace_path]
                    lst.append(new_path)

                # Best-effort: remove old file on disk if we replaced it.
                try:
                    if replace_path and new_path and os.path.normcase(os.path.abspath(replace_path)) != os.path.normcase(os.path.abspath(new_path)):
                        if os.path.exists(replace_path):
                            os.remove(replace_path)
                except Exception:
                    pass

                try:
                    st.session_state.bulk_saved[out_dir2] = list(dict.fromkeys(lst))
                except Exception:
                    pass

                items0 = list(st.session_state.get("bulk_saved_items") or [])
                replaced = False
                for it in items0:
                    if str(it.get("path") or "") == replace_path:
                        it["path"] = new_path
                        it["out_dir"] = out_dir2
                        it["article_idx"] = int(res.get("article_idx") or it.get("article_idx") or 0)
                        it["prompt_idx"] = int(res.get("prompt_idx") or it.get("prompt_idx") or 0)
                        replaced = True
                        break

                if not replaced:
                    items0.append(
                        {
                            "path": new_path,
                            "out_dir": out_dir2,
                            "article_idx": int(res.get("article_idx") or 0),
                            "prompt_idx": int(res.get("prompt_idx") or 0),
                        }
                    )

                st.session_state.bulk_saved_items = items0
            changed_local = True
        else:
            job["status"] = "error"
            job["error"] = str((res or {}).get("error") or "Unknown error")
            job["ended_at"] = ended_at
            jobs[job_id] = job
            st.session_state.bulk_regen_jobs = jobs
            changed_local = True

        return changed_local

    def _poll_bulk_regen_jobs() -> None:
        jobs = st.session_state.get("bulk_regen_jobs") or {}
        q: queue.Queue = st.session_state.get("bulk_regen_result_queue")
        changed = False

        # 1) Drain completed results produced by background callbacks
        while q is not None:
            try:
                payload = q.get_nowait()
            except Exception:
                break

            try:
                job_id = str(payload.get("job_id") or "")
                res = payload.get("result")
                err = payload.get("error")
                ended_at = payload.get("ended_at")
                if _apply_regen_result(job_id, res if isinstance(res, dict) else None, str(err) if err else None, ended_at):
                    changed = True
            except Exception as e:
                # If draining fails, record a generic error
                try:
                    jid = str(payload.get("job_id") or "")
                    if _apply_regen_result(jid, None, f"Queue drain error: {e}", datetime.now().isoformat(timespec="seconds")):
                        changed = True
                except Exception:
                    changed = True

        # 2) Fallback: mark any running jobs whose future is already done (in case callback didn't fire)
        jobs = st.session_state.get("bulk_regen_jobs") or {}
        for job_id, job in list(jobs.items()):
            if job.get("status") != "running":
                continue
            fut = job.get("future")
            if fut is None:
                continue
            try:
                if fut.done():
                    try:
                        res = fut.result()
                        if _apply_regen_result(job_id, res if isinstance(res, dict) else None, None, datetime.now().isoformat(timespec="seconds")):
                            changed = True
                    except Exception as e:
                        if _apply_regen_result(job_id, None, str(e), datetime.now().isoformat(timespec="seconds")):
                            changed = True
            except Exception:
                continue

        # Mark UI as dirty so autorefresh will repaint a few times.
        # IMPORTANT: do NOT call st.rerun() from polling code.
        # Frequent immediate reruns can swallow button click events (especially when user clicks
        # multiple "Пересоздать" buttons quickly).
        if changed:
            st.session_state["bulk_regen_dirty_counter"] = 3

        # Counter is decremented in the main autorefresh section below.

    # Poll background jobs every run
    _poll_bulk_regen_jobs()

    # Auto-refresh like Tab1 (requires optional dependency `streamlit_autorefresh`)
    jobs_all_now = st.session_state.get("bulk_regen_jobs") or {}
    has_running = any(j.get("status") == "running" for j in jobs_all_now.values())
    dirty_counter = int(st.session_state.get("bulk_regen_dirty_counter") or 0)

    # When everything is finished we still do a few extra refreshes so images repaint
    if (not has_running) and dirty_counter > 0:
        st.session_state["bulk_regen_dirty_counter"] = dirty_counter - 1
        dirty_counter = dirty_counter - 1

    if has_running or dirty_counter > 0:
        try:
            from streamlit_autorefresh import st_autorefresh

            # Match the more stable strategy from app_unified_streamlit.py:
            # poll slower while jobs are running (reduces UI flicker and missed clicks),
            # then faster for a short period after completion.
            interval = 4000 if has_running else 1000

            # Prevent autorefresh from swallowing click events.
            # IMPORTANT: never skip calling st_autorefresh(), иначе UI может перестать обновляться.
            last_click_ts = float(st.session_state.get("bulk_regen_last_click_ts") or 0.0)
            if time.time() - last_click_ts < 1.0:
                interval = max(interval, 2000)

            st_autorefresh(interval=interval, key="bulk_regen_autorefresh")
        except Exception:
            # Fallback: no autorefresh package; user can still trigger reruns manually
            if has_running:
                st.warning("Идёт пересоздание, но пакет streamlit-autorefresh не найден. UI не будет обновляться автоматически. Установи: pip install streamlit-autorefresh")
            pass

    st.markdown("---")
    st.subheader("Результаты")

    # Diagnostics: show actual profile dirs used for parallel regen
    try:
        regen_workers_dbg = max(1, min(12, int(parallelism)))
        sig_dbg = (str(user_data_dir or ""), int(regen_workers_dbg), int(start_profile_num))
        if st.session_state.get("bulk_regen_profile_pool") is None or st.session_state.get("bulk_regen_pool_sig") != sig_dbg:
            # build a temporary view without mutating existing pool
            pp_dbg, tmps_dbg = _prepare_profile_pool(
                user_data_dir,
                max_workers=int(regen_workers_dbg),
                start_profile_num=int(start_profile_num),
            )
            # put back immediately and cleanup clones
            dirs_dbg = []
            while not pp_dbg.empty():
                try:
                    slot = pp_dbg.get_nowait()
                    dirs_dbg.append(str(slot.get("dir") or ""))
                except Exception:
                    break
            for d in tmps_dbg:
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass
        else:
            # peek into existing pool by copying slots out/in
            dirs_dbg = []
            pp0 = st.session_state.get("bulk_regen_profile_pool")
            if pp0 is not None:
                tmp_slots = []
                for _ in range(pp0.qsize()):
                    try:
                        s0 = pp0.get_nowait()
                        tmp_slots.append(s0)
                        dirs_dbg.append(str(s0.get("dir") or ""))
                    except Exception:
                        break
                for s0 in tmp_slots:
                    pp0.put(s0)

        uniq = [d for d in dict.fromkeys([d for d in dirs_dbg if d]).keys()]
        if dirs_dbg:
            with st.expander("Диагностика профилей (для параллельных окон)", expanded=False):
                st.caption(f"Параллельно окон: {int(parallelism)} | старт: {int(start_profile_num)}")
                st.caption(f"Слотов в пуле: {len(dirs_dbg)} | уникальных профилей: {len(uniq)}")
                if len(uniq) < len(dirs_dbg):
                    st.warning("В пуле повторяются одни и те же профили. Тогда окна будут сериализоваться/вести себя странно. Создайте .chrome_automation_profile_N для каждого слота.")
                for d in dirs_dbg:
                    st.code(d)
    except Exception:
        pass

    # Regen status (Tab1-like: immediate background submits)
    jobs_all = st.session_state.get("bulk_regen_jobs") or {}
    running = [j for j in jobs_all.values() if j.get("status") == "running"]
    done = [j for j in jobs_all.values() if j.get("status") == "done"]
    errs = [j for j in jobs_all.values() if j.get("status") == "error"]

    cqa, cqb, cqc, cqd = st.columns([2, 2, 2, 2])
    with cqa:
        st.caption(f"В работе: {len(running)}")
    with cqb:
        st.caption(f"Готово: {len(done)}")
    with cqc:
        st.caption(f"Ошибки: {len(errs)}")
    with cqd:
        if st.button("🧹 Очистить историю пересоздания", key="bulkfast_clear_regen_history"):
            st.session_state.bulk_regen_jobs = {}
            # also shutdown executor and cleanup temp cloned profiles
            ex0 = st.session_state.get("bulk_regen_executor")
            if ex0:
                try:
                    ex0.shutdown(wait=False, cancel_futures=False)
                except Exception:
                    pass
            st.session_state.bulk_regen_executor = None
            st.session_state.bulk_regen_profile_pool = None
            for d in list(st.session_state.get("bulk_regen_tmp_profiles") or []):
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass
            st.session_state.bulk_regen_tmp_profiles = []
            st.session_state.bulk_regen_pool_sig = None
            st.rerun()

    with st.expander("Статус пересоздания", expanded=False):
        if jobs_all:
            tail = list(jobs_all.items())[-30:]
            for job_id, job in tail:
                st.write(
                    f"{job_id}: {job.get('status')} (article {job.get('article_idx')}, prompt {job.get('prompt_idx')})"
                )
                if job.get("status") == "error":
                    st.caption(str(job.get("error") or ""))
        else:
            st.caption("Пока нет задач пересоздания")

    # Tab1-like gallery options
    gallery_cols = st.slider("Колонок в галерее", min_value=1, max_value=6, value=4, key="bulkfast_gallery_cols")

    # ---------------- Bulk post-processing (Photoshop watermark -> normalize -> webp) ----------------
    st.markdown("---")
    with st.expander("🧰 Post-process: watermark → (normalize) → WebP (ALL folders)", expanded=False):
        st.caption(
            "Прогоняет тот же пайплайн, что и вкладки в app_unified_streamlit.py: "
            "1) Photoshop убирает watermark (создаёт *_filled рядом с исходниками), "
            "2) нормализация до нужного размера (crop-to-fit) — опционально, "
            "3) конвертация в WebP."
        )

        saved_map_pp: dict[str, list[str]] = st.session_state.get("bulk_saved") or {}

        def _list_source_images_for_folder(folder: str) -> list[str]:
            """List source images in a folder for post-processing.

            Important: include manually-added images (e.g. *_pro_*.png) and not only those tracked in session_state.
            We keep it conservative to avoid re-processing outputs:
            - only top-level files in `folder`
            - only PNG/JPG/JPEG
            - exclude *_filled.*
            - exclude anything under postproc/
            """
            try:
                out: list[str] = []
                p = Path(folder).expanduser().resolve()
                if not p.exists() or not p.is_dir():
                    return []
                for ext in ("*.png", "*.jpg", "*.jpeg"):
                    for fp in p.glob(ext):
                        try:
                            if not fp.is_file():
                                continue
                            # IMPORTANT: do NOT exclude files that merely contain the word "filled" in the prompt text.
                            # Only exclude real Photoshop outputs where suffix "_filled" is appended right before extension.
                            stem_low = fp.stem.lower()
                            if stem_low.endswith("_filled"):
                                continue
                            out.append(str(fp))
                        except Exception:
                            continue
                # de-dup preserve order
                return list(dict.fromkeys(out))
            except Exception:
                return []

        # Determine folders to process:
        # - those already known from bulk_saved (fast)
        # - plus any article_* dirs under selected run dir(s) (so manually-added Pro images are included)
        #
        # User request: allow selecting MULTIPLE run dirs under base_root, e.g.
        #   ...\generate automation\generate automation\2026-02-26_24
        #   ...\generate automation\generate automation\2026-02-25_19
        # and process them sequentially.

        run_dir_candidates = []
        try:
            run_dir_candidates = list(_list_existing_run_dirs(base_root) or [])
        except Exception:
            run_dir_candidates = []

        # Backward compatible default: current run_base_dir
        default_run_dirs: list[str] = []
        try:
            rbd = str(Path(run_base_dir).expanduser().resolve())
            if rbd:
                default_run_dirs = [rbd]
        except Exception:
            default_run_dirs = [str(run_base_dir or "")]

        picked_run_dirs = st.multiselect(
            "Run-папки для обработки (можно несколько)",
            options=run_dir_candidates or default_run_dirs,
            default=[d for d in default_run_dirs if d] if default_run_dirs else (run_dir_candidates[:1] if run_dir_candidates else []),
            key="bulk_pp_picked_run_dirs",
            help="Это папки вида YYYY-MM-DD_NN внутри base_root. Внутри каждой будут обработаны все article_*.",
        )

        def _article_dirs_in_selected_runs(run_dirs: list[str]) -> list[str]:
            """Discover direct article_* children of the currently selected run dirs."""
            found: list[str] = []
            for run_dir in run_dirs or []:
                try:
                    rb = Path(run_dir).expanduser().resolve()
                    if rb.exists() and rb.is_dir():
                        for d in sorted(rb.glob("article_*")):
                            if d.is_dir():
                                found.append(str(d.resolve()))
                except Exception:
                    continue
            return list(dict.fromkeys(found))

        def _is_in_selected_run(folder: str, run_dirs: list[str]) -> bool:
            """True only when `folder` belongs to one of the selected run directories."""
            try:
                fp = Path(folder).expanduser().resolve()
                for run_dir in run_dirs or []:
                    try:
                        fp.relative_to(Path(run_dir).expanduser().resolve())
                        return True
                    except ValueError:
                        continue
            except Exception:
                pass
            return False

        discovered_dirs = _article_dirs_in_selected_runs(list(picked_run_dirs or []))

        # bulk_saved is session-wide and may still contain folders from an older run.
        # Never let those folders leak into the current run-folder selection.
        saved_dirs_in_selected_runs = {
            str(Path(k).expanduser().resolve())
            for k in (saved_map_pp.keys() or [])
            if k and _is_in_selected_run(str(k), list(picked_run_dirs or []))
        }
        folder_dirs = sorted(saved_dirs_in_selected_runs | set(discovered_dirs))
        folders_with_inputs: list[tuple[str, list[str]]] = []
        for d in folder_dirs:
            imgs = _list_source_images_for_folder(d)
            if imgs:
                folders_with_inputs.append((d, imgs))

        # Let user pick multiple folders (queue) instead of only "current" one.
        folder_options = [d for d, _ in folders_with_inputs]

        # A Streamlit widget keeps its value by key.  Use a key derived from the
        # selected run dirs so this dependent multiselect is recreated whenever that
        # selection changes; its default then selects all newly discovered folders.
        # With the same run dirs the key stays stable, so manual article choices stay.
        run_signature = "\n".join(sorted(str(Path(d).expanduser().resolve()) for d in (picked_run_dirs or [])))
        article_picker_key = "bulk_pp_picked_folders_" + hashlib.md5(
            run_signature.encode("utf-8", errors="ignore")
        ).hexdigest()[:16]

        picked_folders = st.multiselect(
            "Article-папки для обработки (внутри выбранных run-папок)",
            options=folder_options,
            default=folder_options,
            key=article_picker_key,
            help="Выберите папки. Обработка пойдёт последовательно: папка за папкой.",
        )

        # Keep original order but filter by user selection
        folders_picked: list[tuple[str, list[str]]] = [
            (d, imgs) for (d, imgs) in folders_with_inputs if d in set(picked_folders)
        ]

        total_imgs = sum(len(v) for _, v in folders_picked) if folders_picked else 0
        st.write(f"Папок: {len(folders_picked)} из {len(folders_with_inputs)} | Картинок: {total_imgs}")

        # One-time migration for browser sessions that still hold the old 75/75 default.
        if not bool(st.session_state.get("bulk_pp_ps_margin1_default_82_migrated")):
            for _k in ("bulk_pp_ps_ml_1", "bulk_pp_ps_mb_1"):
                try:
                    if int(st.session_state.get(_k, 75) or 75) == 75:
                        st.session_state[_k] = 82
                except Exception:
                    st.session_state[_k] = 82
            st.session_state["bulk_pp_ps_margin1_default_82_migrated"] = True

        colA, colB, colC = st.columns([1, 1, 1])
        with colA:
            pp_skip_watermark = st.checkbox(
                "Пропустить watermark (только WebP)",
                value=False,
                key="bulk_pp_skip_watermark",
                help=(
                    "Не запускать Photoshop и также пропустить нормализацию. "
                    "Исходные картинки будут сразу сконвертированы в WebP "
                    "в postproc/webp_no_normalize."
                ),
            )
            pp_enable_normalize = st.checkbox(
                "Нормализация (этап 2: crop/resize)",
                value=False,
                key="bulk_pp_enable_normalize",
                help="Если выключено — после Photoshop (watermark) сразу конвертация в WebP, без изменения размеров.",
                disabled=bool(pp_skip_watermark),
            )
            pp_target_w = st.number_input(
                "Ширина (normalize)",
                min_value=64,
                max_value=4096,
                value=640,
                step=1,
                key="bulk_pp_w",
                disabled=(not bool(pp_enable_normalize)) or bool(pp_skip_watermark),
            )
            pp_target_h = st.number_input(
                "Высота (normalize)",
                min_value=64,
                max_value=4096,
                value=1024,
                step=1,
                key="bulk_pp_h",
                disabled=(not bool(pp_enable_normalize)) or bool(pp_skip_watermark),
            )
            pp_try_match_size = st.checkbox(
                "Стараться сохранить размер файла (JPEG/WebP)",
                value=True,
                key="bulk_pp_try_match_size",
                help="Подбор качества для достижения веса, близкого к исходному (±10%).",
                disabled=(not bool(pp_enable_normalize)) or bool(pp_skip_watermark),
            )

        with colB:
            pp_ps_size_or_scale = st.number_input("Photoshop scale/divider", min_value=1, max_value=999, value=13, step=1, key="bulk_pp_ps_scale")
            pp_ps_out_fmt = st.selectbox("Photoshop output format", options=["PNG", "JPEG", "AUTO"], index=0, key="bulk_pp_ps_fmt")
            pp_ps_margin_left_1 = st.number_input(
                "Photoshop margin left 1",
                min_value=0,
                max_value=999,
                value=82,
                step=1,
                key="bulk_pp_ps_ml_1",
            )
            pp_ps_margin_bottom_1 = st.number_input(
                "Photoshop margin bottom 1",
                min_value=0,
                max_value=999,
                value=82,
                step=1,
                key="bulk_pp_ps_mb_1",
            )
            pp_ps_margin_left_2 = st.number_input(
                "Photoshop margin left 2",
                min_value=0,
                max_value=999,
                value=int(st.session_state.get("bulk_pp_ps_ml", 25) or 25),
                step=1,
                key="bulk_pp_ps_ml_2",
            )
            pp_ps_margin_bottom_2 = st.number_input(
                "Photoshop margin bottom 2",
                min_value=0,
                max_value=999,
                value=int(st.session_state.get("bulk_pp_ps_mb", 25) or 25),
                step=1,
                key="bulk_pp_ps_mb_2",
            )

        with colC:
            pp_webp_lossless = st.checkbox("WebP lossless", value=False, key="bulk_pp_webp_lossless")
            pp_webp_quality = st.slider("WebP quality", min_value=0, max_value=100, value=80, key="bulk_pp_webp_quality")
            pp_webp_overwrite = st.checkbox("Overwrite existing WebP", value=False, key="bulk_pp_webp_overwrite")

        # "Skip watermark" is intentionally an all-or-nothing WebP-only mode.
        # Keep webp_no_normalize as the output name: other tools consume it.
        pp_run_normalize = bool(pp_enable_normalize) and not bool(pp_skip_watermark)

        # Если выключено: файлы, начинающиеся с <число>_pro_ (например 1_pro_, 2_pro_, 3_pro_, 12_pro_),
        # пропускают watermark+normalize, но всё равно пойдут в WebP.
        pp_include_pro_in_watermark_normalize = st.checkbox(
            "Обрабатывать *_pro_* (N_pro_) в watermark + normalize",
            value=True,
            key="bulk_pp_include_pro_wm_norm",
            help="Если выключить — файлы N_pro_* (1_pro_*, 2_pro_*, 3_pro_* и т.д.) не отправляются в Photoshop и не нормализуются, но конвертация в WebP выполняется для всех файлов.",
            disabled=bool(pp_skip_watermark),
        )

        pp_preview = st.checkbox("Показать превью WebP (первые 24) после обработки", value=True, key="bulk_pp_preview")

        def _bulk_pp_path_identity(path_str: str) -> str:
            try:
                return str(Path(str(path_str)).expanduser().resolve()).lower()
            except Exception:
                return str(path_str or "").strip().lower()

        def _bulk_pp_margin1_key(path_str: str) -> str:
            ident = _bulk_pp_path_identity(path_str)
            return "bulk_pp_margin1_" + hashlib.md5(ident.encode("utf-8", errors="ignore")).hexdigest()[:16]

        pp_manual_paths: list[str] = []
        for _out_dir, _paths in (folders_picked or []):
            pp_manual_paths.extend([str(p) for p in (_paths or []) if p])
        pp_manual_paths = list(dict.fromkeys(pp_manual_paths))

        if st.button(
            "🖼️ Показать картинки для выбора Photoshop margin",
            disabled=(not bool(pp_manual_paths)) or bool(pp_skip_watermark),
            key="bulk_pp_show_margin_picker_btn",
            help="Отмеченные картинки обработаются через Photoshop margin 1. Все остальные — через Photoshop margin 2.",
        ):
            st.session_state["bulk_pp_show_margin_picker"] = True

        if (not bool(pp_skip_watermark)) and bool(st.session_state.get("bulk_pp_show_margin_picker")) and pp_manual_paths:
            st.caption(
                "Отметьте картинки для Photoshop margin 1. Неотмеченные картинки автоматически пойдут через Photoshop margin 2."
            )
            cols = st.columns(max(1, int(gallery_cols or 4)))
            for j, img_path in enumerate(pp_manual_paths):
                with cols[j % max(1, int(gallery_cols or 4))]:
                    try:
                        st.image(img_path, caption=os.path.basename(img_path), use_container_width=True)
                    except Exception:
                        st.write(img_path)
                    st.checkbox(
                        "Удалять watermark с margin 1",
                        value=True,
                        key=_bulk_pp_margin1_key(img_path),
                    )

        pp_margin_set1_path_ids = {
            _bulk_pp_path_identity(p)
            for p in pp_manual_paths
            if bool(st.session_state.get(_bulk_pp_margin1_key(p), True))
        }
        if pp_manual_paths:
            st.caption(
                f"Photoshop margin groups: margin 1 = {len(pp_margin_set1_path_ids)} | "
                f"margin 2 = {max(0, len(pp_manual_paths) - len(pp_margin_set1_path_ids))}"
            )

        run_pp_label = (
            "🚀 Запустить только WebP (skip watermark + normalize) для выбранных папок"
            if bool(pp_skip_watermark)
            else
            "🚀 Запустить watermark → normalize → WebP для выбранных папок"
            if bool(pp_run_normalize)
            else "🚀 Запустить watermark → WebP (skip normalize) для выбранных папок"
        )
        run_pp = st.button(
            run_pp_label,
            type="primary",
            disabled=(not folders_picked),
            key="bulk_pp_run_all",
        )

        if run_pp:
            st.session_state["bulk_pp_last_results"] = {}
            all_errors: list[str] = []
            all_webp: list[str] = []

            ps_opts_margin1 = PhotoshopWatermarkOptions(
                size_or_scale=int(pp_ps_size_or_scale),
                out_format=str(pp_ps_out_fmt),
                mode="scale",
                margin_left=int(pp_ps_margin_left_1),
                margin_bottom=int(pp_ps_margin_bottom_1),
            )
            ps_opts_margin2 = PhotoshopWatermarkOptions(
                size_or_scale=int(pp_ps_size_or_scale),
                out_format=str(pp_ps_out_fmt),
                mode="scale",
                margin_left=int(pp_ps_margin_left_2),
                margin_bottom=int(pp_ps_margin_bottom_2),
            )
            # Output naming depends on format, not on margin. Keep a default opts object
            # for existing filename detection helpers.
            ps_opts = ps_opts_margin2
            norm_opts = NormalizeOptions(
                target_w=int(pp_target_w),
                target_h=int(pp_target_h),
                try_match_size=bool(pp_try_match_size),
            )
            webp_opts = WebpOptions(
                quality=int(pp_webp_quality),
                lossless=bool(pp_webp_lossless),
                # WordPress/server-side processors sometimes reject WebP with metadata chunks.
                # Unified app commonly produces compatible files by saving WebP without extra metadata.
                keep_metadata=False,
                method=6,
                overwrite=bool(pp_webp_overwrite),
            )

            # Progress across folders
            folders = list(folders_picked)
            prog = st.progress(0)
            status = st.empty()

            def _is_pro_image_path(p: str) -> bool:
                """True for any image whose filename starts with <digits>_pro_ (e.g. 1_pro_, 02_pro_, 3_pro_...)."""
                try:
                    name_low = Path(str(p)).name.lower()
                    import re

                    return re.match(r"^\d+_pro_", name_low) is not None
                except Exception:
                    return False

            # 1) Photoshop watermark removal (optional; WebP-only mode skips it)
            # IMPORTANT: calling Photoshop with a *very large* list in one batch can look like "Photoshop opened but does nothing"
            # because the generated JSX becomes huge and DoJavaScript can hang/timeout.
            # In bulk mode we therefore chunk the work.
            if bool(pp_skip_watermark):
                status.text("WebP only: watermark and normalize skipped…")
            else:
                status.text("Photoshop: removing watermark…")

            all_src_paths: list[str] = []
            if not bool(pp_skip_watermark):
                for _out_dir, _paths in folders:
                    all_src_paths.extend(list(_paths or []))
            # de-dup preserve order
            all_src_paths = list(dict.fromkeys([p for p in all_src_paths if p]))

            # Optionally exclude 1_pro_/2_pro_ from watermark removal + normalize.
            # (They will still be converted to WebP later.)
            if (not bool(pp_skip_watermark)) and (not pp_include_pro_in_watermark_normalize):
                all_src_paths = [p for p in all_src_paths if not _is_pro_image_path(p)]

            # Diagnostics: ensure paths exist + show quick stats
            exists = 0
            missing = 0
            max_len = 0
            for p in all_src_paths:
                try:
                    pp = str(p)
                    max_len = max(max_len, len(pp))
                    if Path(pp).exists():
                        exists += 1
                    else:
                        missing += 1
                except Exception:
                    missing += 1
            st.write(
                    f"Photoshop input files: total={len(all_src_paths)} | exists={exists} | missing={missing} | max_path_len={max_len}"
            )
            if missing:
                all_errors.append(f"Some input files do not exist: {missing} (Photoshop may appear to do nothing)")

            import tempfile

            # Chunk size (safe default). You can tune if needed.
            # If you pass thousands of paths at once, JSX string can exceed COM/Photoshop limits.
            chunk_size = 80
            if len(all_src_paths) > chunk_size:
                st.info(f"Photoshop will run in chunks: {chunk_size} files per batch (total {len(all_src_paths)}).")

            # If paths are too long (Windows MAX_PATH issues), Photoshop may silently fail to open them.
            # Workaround: copy inputs into a temporary short path folder, run Photoshop there, then copy *_filled back.
            enable_staging_for_long_paths = max_len >= 245  # conservative threshold
            if enable_staging_for_long_paths and all_src_paths:
                st.warning(
                    f"Some file paths are long (max {max_len}). Photoshop may fail with long paths; will stage files into a temp folder with short names."
                )

            def _filled_candidates_for(src: Path) -> list[Path]:
                """Return expected + fallback candidates for *_filled output next to `src`."""
                try:
                    fmt = str(ps_opts.out_format).upper()
                except Exception:
                    fmt = "PNG"
                if fmt == "AUTO":
                    ext = src.suffix.lower()
                    fmt = "JPEG" if ext in (".jpg", ".jpeg") else "PNG"

                cands: list[Path] = []
                if fmt == "PNG":
                    cands.append(src.with_name(src.stem + "_filled.png"))
                elif fmt == "JPEG":
                    cands.append(src.with_name(src.stem + "_filled.jpg"))

                # Fallback scan (handles .jpeg vs .jpg etc.)
                try:
                    for p in sorted(src.parent.glob(f"{src.stem}_filled.*")):
                        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg"):
                            cands.append(p)
                except Exception:
                    pass
                # de-dup preserve order
                return list(dict.fromkeys(cands))

            def _find_existing_filled(src: Path) -> Path | None:
                for cand in _filled_candidates_for(src):
                    try:
                        if cand.exists() and cand.is_file():
                            return cand.resolve()
                    except Exception:
                        continue
                return None

            def _compute_missing(src_list: list[str]) -> list[Path]:
                miss: list[Path] = []
                for sp in (src_list or []):
                    try:
                        src = Path(sp)
                        if not src.exists():
                            continue
                        if _find_existing_filled(src) is None:
                            miss.append(src)
                    except Exception:
                        continue
                return miss

            def _run_photoshop_group(
                src_paths: list[str],
                *,
                opts: PhotoshopWatermarkOptions,
                group_label: str,
            ) -> list[Path]:
                src_paths = list(dict.fromkeys([p for p in (src_paths or []) if p]))
                group_filled_paths: list[Path] = []
                if not src_paths:
                    return []

                if len(src_paths) > chunk_size:
                    st.info(
                        f"Photoshop {group_label} will run in chunks: "
                        f"{chunk_size} files per batch (total {len(src_paths)})."
                    )

                def _copy_stage_outputs_back(mapping: list[tuple[Path, Path]]) -> None:
                    for staged_p, orig_p in mapping:
                        filled_candidates = sorted(staged_p.parent.glob(staged_p.stem + "_filled.*"))
                        if not filled_candidates:
                            continue
                        staged_filled = filled_candidates[0]
                        out_name = orig_p.with_name(orig_p.stem + "_filled" + staged_filled.suffix)
                        shutil.copy2(staged_filled, out_name)
                        group_filled_paths.append(out_name)

                for start in range(0, len(src_paths), chunk_size):
                    batch = src_paths[start : start + chunk_size]
                    batch_no = start // chunk_size + 1
                    batch_total = (len(src_paths) + chunk_size - 1) // chunk_size
                    status.text(f"Photoshop {group_label}: batch {batch_no}/{batch_total} ({len(batch)} files)…")

                    if enable_staging_for_long_paths:
                        stage_dir = Path(tempfile.mkdtemp(prefix="tmp_rovodev_ps_stage_"))
                        try:
                            mapping: list[tuple[Path, Path]] = []
                            for i, orig in enumerate(batch, start=1):
                                orig_p = Path(orig)
                                staged_p = stage_dir / f"img_{i:04d}{orig_p.suffix.lower()}"
                                shutil.copy2(orig_p, staged_p)
                                mapping.append((staged_p, orig_p))

                            f0, ps_err = remove_watermark_photoshop_batch(
                                [str(p[0]) for p in mapping],
                                opts=opts,
                                use_direct_import=True,
                            )
                            if ps_err:
                                all_errors.append(f"Photoshop {group_label} batch {batch_no}: {ps_err}")
                            _copy_stage_outputs_back(mapping)
                        finally:
                            try:
                                shutil.rmtree(stage_dir, ignore_errors=True)
                            except Exception:
                                pass
                    else:
                        f0, ps_err = remove_watermark_photoshop_batch(batch, opts=opts, use_direct_import=True)
                        if ps_err:
                            all_errors.append(f"Photoshop {group_label} batch {batch_no}: {ps_err}")
                        group_filled_paths.extend(list(f0 or []))

                group_filled_paths = list(dict.fromkeys([p for p in group_filled_paths if p]))

                # If Photoshop is still flushing file I/O, some outputs can appear a moment later.
                # We therefore do a tiny wait+rescan and then retry ONLY truly missing sources.
                missing_srcs = _compute_missing(src_paths)
                if missing_srcs:
                    try:
                        time.sleep(1.0)
                    except Exception:
                        pass
                    missing_srcs = _compute_missing([str(p) for p in missing_srcs])

                if missing_srcs:
                    st.warning(
                        f"Photoshop {group_label}: не создано *_filled для {len(missing_srcs)} файлов. "
                        "Ретраим только их…"
                    )
                    retry_paths = [str(p) for p in missing_srcs]
                    retry_chunk_size = 30
                    for rstart in range(0, len(retry_paths), retry_chunk_size):
                        batch2 = retry_paths[rstart : rstart + retry_chunk_size]
                        retry_no = rstart // retry_chunk_size + 1
                        retry_total = (len(retry_paths) + retry_chunk_size - 1) // retry_chunk_size
                        status.text(
                            f"Photoshop {group_label} retry: batch {retry_no}/{retry_total} ({len(batch2)} files)…"
                        )

                        if enable_staging_for_long_paths:
                            stage_dir = Path(tempfile.mkdtemp(prefix="tmp_rovodev_ps_stage_retry_"))
                            try:
                                mapping2: list[tuple[Path, Path]] = []
                                for i, orig in enumerate(batch2, start=1):
                                    orig_p = Path(orig)
                                    staged_p = stage_dir / f"img_{i:04d}{orig_p.suffix.lower()}"
                                    shutil.copy2(orig_p, staged_p)
                                    mapping2.append((staged_p, orig_p))

                                f1, ps_err = remove_watermark_photoshop_batch(
                                    [str(p[0]) for p in mapping2],
                                    opts=opts,
                                    use_direct_import=True,
                                    max_rounds=1,
                                    chunk_size=10,
                                )
                                if ps_err:
                                    all_errors.append(f"Photoshop {group_label} retry batch {retry_no}: {ps_err}")
                                _copy_stage_outputs_back(mapping2)
                            finally:
                                try:
                                    shutil.rmtree(stage_dir, ignore_errors=True)
                                except Exception:
                                    pass
                        else:
                            f1, ps_err = remove_watermark_photoshop_batch(
                                batch2,
                                opts=opts,
                                use_direct_import=True,
                                max_rounds=1,
                                chunk_size=10,
                            )
                            if ps_err:
                                all_errors.append(f"Photoshop {group_label} retry batch {retry_no}: {ps_err}")
                            group_filled_paths.extend(list(f1 or []))

                # Ensure we detect any outputs that were created but not returned (late saves, format variants, etc.)
                for sp in src_paths:
                    try:
                        src = Path(sp)
                        outp = _find_existing_filled(src)
                        if outp is not None:
                            group_filled_paths.append(outp)
                    except Exception:
                        pass

                return list(dict.fromkeys([p for p in group_filled_paths if p and Path(p).exists()]))

            margin1_ids = set(pp_margin_set1_path_ids or set())
            all_src_paths_margin1 = [p for p in all_src_paths if _bulk_pp_path_identity(p) in margin1_ids]
            all_src_paths_margin2 = [p for p in all_src_paths if _bulk_pp_path_identity(p) not in margin1_ids]
            if all_src_paths:
                st.write(
                    f"Photoshop margin groups to process: margin 1 = {len(all_src_paths_margin1)} | "
                    f"margin 2 = {len(all_src_paths_margin2)}"
                )

            filled_paths: list[Path] = []
            if not all_src_paths:
                if bool(pp_skip_watermark):
                    st.info("Photoshop skipped: WebP-only mode uses the original images directly.")
                else:
                    st.info("Photoshop step skipped: no files selected for watermark removal (possibly due to 1_pro_/2_pro_ exclusion).")
            else:
                filled_paths.extend(
                    _run_photoshop_group(
                        all_src_paths_margin1,
                        opts=ps_opts_margin1,
                        group_label="margin 1",
                    )
                )
                filled_paths.extend(
                    _run_photoshop_group(
                        all_src_paths_margin2,
                        opts=ps_opts_margin2,
                        group_label="margin 2",
                    )
                )

            # Final de-dup + exists check
            filled_paths = list(dict.fromkeys([p for p in filled_paths if p and Path(p).exists()]))

            still_missing = _compute_missing(all_src_paths)
            st.write(f"Photoshop outputs detected: {len(filled_paths)}")
            if still_missing:
                st.caption(
                    f"Photoshop: осталось без *_filled после ретраев: {len(still_missing)} (пайплайн продолжит по оригиналам для них)"
                )

            # Map originals to filled by deterministic naming convention (<stem>_filled.<ext>) in same folder
            filled_by_parent_stem: dict[tuple[str, str], str] = {}
            for fp in filled_paths or []:
                try:
                    key = (str(fp.parent.resolve()).lower(), fp.stem.lower())
                    filled_by_parent_stem[key] = str(fp)
                except Exception:
                    continue

            # 2) Normalize (optional) + 3) WebP per folder
            for i, (out_dir, paths) in enumerate(folders, start=1):
                if bool(pp_run_normalize):
                    status.text(f"{i}/{len(folders)}: normalize + webp → {out_dir}")
                elif bool(pp_skip_watermark):
                    status.text(f"{i}/{len(folders)}: webp only (skip watermark + normalize) → {out_dir}")
                else:
                    status.text(f"{i}/{len(folders)}: webp (skip normalize) → {out_dir}")

                try:
                    base_out = Path(out_dir).expanduser().resolve() / "postproc"

                    # Prefer filled versions if they exist
                    folder_inputs: list[str] = []
                    for p in (paths or []):
                        try:
                            pp = Path(p)
                            key = (str(pp.parent.resolve()).lower(), f"{pp.stem}_filled".lower())
                            folder_inputs.append(filled_by_parent_stem.get(key, str(pp)))
                        except Exception:
                            folder_inputs.append(str(p))

                    if bool(pp_run_normalize):
                        norm_dir = base_out / f"normalized_{norm_opts.target_w}x{norm_opts.target_h}"
                        webp_dir = base_out / f"webp_{norm_opts.target_w}x{norm_opts.target_h}"

                        # If pro-images are excluded from watermark+normalize:
                        # - they should NOT be normalized
                        # - but they still must be converted to WebP
                        if pp_include_pro_in_watermark_normalize:
                            normalized, norm_errs = normalize_images_to_dir(folder_inputs, out_dir=norm_dir, opts=norm_opts)
                            webp_inputs = normalized
                        else:
                            pro_inputs = [p for p in folder_inputs if _is_pro_image_path(p)]
                            non_pro_inputs = [p for p in folder_inputs if not _is_pro_image_path(p)]

                            normalized_non_pro, norm_errs = normalize_images_to_dir(
                                non_pro_inputs,
                                out_dir=norm_dir,
                                opts=norm_opts,
                            )
                            # WebP takes both: normalized (non-pro) + originals (pro)
                            webp_inputs = list(dict.fromkeys([*(str(p) for p in (normalized_non_pro or [])), *pro_inputs]))

                            # For reporting/UI we keep "normalized" as whatever we actually normalized.
                            normalized = normalized_non_pro
                    else:
                        # Skip normalize entirely: WebP directly from Photoshop outputs (or originals if no *_filled).
                        norm_errs = []
                        normalized = []
                        webp_inputs = folder_inputs
                        webp_dir = base_out / "webp_no_normalize"

                    webp, webp_errs = convert_images_to_webp_dir(webp_inputs, out_dir=webp_dir, opts=webp_opts)

                    extra_notes: list[str] = []
                    if bool(pp_skip_watermark):
                        extra_notes.append("Watermark and normalize disabled by user (WebP-only mode).")
                    elif not bool(pp_run_normalize):
                        extra_notes.append("Normalize disabled by user (stage 2 skipped).")

                    st.session_state["bulk_pp_last_results"][out_dir] = {
                        "filled": [
                            filled_by_parent_stem.get(
                                (str(Path(p).parent.resolve()).lower(), f"{Path(p).stem}_filled".lower()),
                                "",
                            )
                            for p in (paths or [])
                        ],
                        "normalized": [str(p) for p in (normalized or [])],
                        "webp": [str(p) for p in (webp or [])],
                        "errors": list(extra_notes or []) + list(norm_errs or []) + list(webp_errs or []),
                        "base_out": str(base_out),
                        "normalize_enabled": bool(pp_run_normalize),
                    }

                    all_webp.extend([str(p) for p in (webp or [])])
                    all_errors.extend(list(extra_notes or []))
                    all_errors.extend(list(norm_errs or []))
                    all_errors.extend(list(webp_errs or []))
                except Exception as e:
                    all_errors.append(f"{out_dir}: {e}")

                prog.progress(int(i / max(1, len(folders)) * 100))

            # Persist a flat list for preview
            all_webp = list(dict.fromkeys([p for p in all_webp if p]))
            st.session_state["bulk_pp_last_webp"] = all_webp

            st.success(f"Готово. WebP: {len(all_webp)} | Ошибок: {len(all_errors)}")
            if all_errors:
                st.warning("Есть ошибки/предупреждения. Часто это означает, что Photoshop не создал *_filled и нормализация пошла по оригиналам.")
                st.write("Первые сообщения:")
                for e in all_errors[:5]:
                    st.write("- ", e)
                with st.expander("Ошибки post-process (все)", expanded=False):
                    for e in all_errors[:200]:
                        st.write("- ", e)
                    if len(all_errors) > 200:
                        st.caption(f"…и ещё {len(all_errors) - 200}")

            if pp_preview and all_webp:
                st.markdown("---")
                st.subheader("Preview WebP")
                cols = st.columns(int(gallery_cols))
                for j, p in enumerate(all_webp[:24]):
                    with cols[j % int(gallery_cols)]:
                        try:
                            st.image(p, caption=os.path.basename(p), use_container_width=True)
                        except Exception:
                            st.write(p)

    show_all_prompts_in_results = st.checkbox(
        "Показывать ВСЕ промпты в результатах (включая неуспешные)",
        value=True,
        key="bulkfast_show_all_prompts_in_results",
        help="Если промпт не сгенерировался (ретраи не помогли), будет показано предупреждение и кнопка 'Пересоздать промпт' прямо в галерее.",
    )

    def _is_regen_running_for(article_idx: int, prompt_idx: int, replace_path: str | None = None) -> bool:
        jobs0 = st.session_state.get("bulk_regen_jobs") or {}
        for _job in jobs0.values():
            if int(_job.get("article_idx") or 0) != int(article_idx):
                continue
            if int(_job.get("prompt_idx") or 0) != int(prompt_idx):
                continue
            if replace_path is not None and str(_job.get("replace_path") or "") != str(replace_path or ""):
                continue
            if _job.get("status") == "running":
                return True
        return False

    def _submit_bulk_regen(article_idx: int, prompt_idx: int, *, replace_path: str = "") -> None:
        """Submit background regeneration for a specific (article_idx, prompt_idx).

        Works both for existing images (replace_path set) and missing outputs (replace_path empty).
        """

        st.session_state["bulk_regen_last_click_ts"] = time.time()

        regen_workers = max(1, min(12, int(parallelism)))

        ex0 = st.session_state.get("bulk_regen_executor")
        cur_w = int(getattr(ex0, "_max_workers", 0) or 0) if ex0 else 0
        if (ex0 is None) or (cur_w != regen_workers):
            try:
                if ex0:
                    ex0.shutdown(wait=False, cancel_futures=False)
            except Exception:
                pass
            st.session_state.bulk_regen_executor = concurrent.futures.ThreadPoolExecutor(max_workers=regen_workers)

        executor = st.session_state.bulk_regen_executor

        # Build tasks from current edited prompts
        _tasks_now = _build_tasks(st.session_state.get("bulk_articles") or articles, run_base_dir)
        task_map = {(t.article_idx, t.prompt_idx): t for t in _tasks_now}
        t = task_map.get((int(article_idx), int(prompt_idx)))
        if not t:
            st.error("Не удалось найти задачу для этого промпта")
            return

        with st.session_state["bulk_regen_lock"]:
            st.session_state.bulk_regen_job_seq += 1
            job_id = f"job_{st.session_state.bulk_regen_job_seq}_{article_idx}_{prompt_idx}"

        # Shared profile pool sized by parallelism (ensures different windows/profiles)
        sig = (str(user_data_dir or ""), int(regen_workers), int(start_profile_num))
        if st.session_state.get("bulk_regen_profile_pool") is None or st.session_state.get("bulk_regen_pool_sig") != sig:
            # cleanup old tmp profiles
            for d in list(st.session_state.get("bulk_regen_tmp_profiles") or []):
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass
            pp, tmps = _prepare_profile_pool(
                user_data_dir,
                max_workers=int(regen_workers),
                start_profile_num=int(start_profile_num),
            )
            st.session_state.bulk_regen_profile_pool = pp
            st.session_state.bulk_regen_tmp_profiles = tmps
            st.session_state.bulk_regen_pool_sig = sig

        shared_pool = st.session_state.bulk_regen_profile_pool

        # Regen variation: rotate a polite prefix to avoid identical outputs
        try:
            prompt_var = _apply_regen_prefix_variation(t.prompt, key=f"{int(article_idx)}.{int(prompt_idx)}")
        except Exception:
            prompt_var = t.prompt

        t_var = BulkTask(
            article_idx=t.article_idx,
            article_title=t.article_title,
            prompt_idx=t.prompt_idx,
            prompt=str(prompt_var or t.prompt),
            out_dir=t.out_dir,
        )

        def _job_fn(_t=t_var, _pp=shared_pool):
            return _run_one_task(
                _t,
                url=url,
                headless=bool(headless),
                executable_path=executable_path,
                model_choice=model_choice,
                timeout_s=int(timeout_s),
                profile_pool=_pp,
            )

        fut = executor.submit(_job_fn)

        jobs = st.session_state.get("bulk_regen_jobs") or {}
        jobs[job_id] = {
            "status": "running",
            "article_idx": int(article_idx),
            "prompt_idx": int(prompt_idx),
            "replace_path": str(replace_path or ""),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "future": fut,
        }
        st.session_state.bulk_regen_jobs = jobs

        # Completion callback -> push result into queue (thread-safe)
        q: queue.Queue = st.session_state.get("bulk_regen_result_queue")

        def _done_cb(_fut, _job_id=job_id, _q=q):
            try:
                r = _fut.result()
                _q.put(
                    {
                        "job_id": _job_id,
                        "result": r,
                        "ended_at": datetime.now().isoformat(timespec="seconds"),
                    }
                )
            except Exception as e:
                _q.put(
                    {
                        "job_id": _job_id,
                        "error": str(e),
                        "ended_at": datetime.now().isoformat(timespec="seconds"),
                    }
                )

        try:
            fut.add_done_callback(_done_cb)
        except Exception:
            pass

        # No explicit st.rerun(): the button click already triggers a rerun,
        # and the autorefresh loop will keep the UI updated while the job runs.
        st.session_state["bulk_regen_dirty_counter"] = max(3, int(st.session_state.get("bulk_regen_dirty_counter") or 0))

    # Prefer rich items list (needed for per-image regenerate)
    # After Streamlit restart, this list is empty; reconstruct from disk for the selected run_dir.
    try:
        last_scan = str(st.session_state.get("bulkfast_last_disk_scan_run_dir") or "")
    except Exception:
        last_scan = ""
    try:
        force_rescan = False
        try:
            force_rescan = bool(st.session_state.pop("bulkfast_force_disk_rescan", False))
        except Exception:
            force_rescan = False

        if str(run_base_dir or "") and (force_rescan or str(run_base_dir or "") != last_scan):
            scanned = _scan_fast_saved_items_from_disk(str(run_base_dir))
            if scanned:
                st.session_state.bulk_saved_items = scanned
                # also fill bulk_saved (legacy map)
                m: dict[str, list[str]] = {}
                for it in scanned:
                    od = str(it.get("out_dir") or "")
                    p = str(it.get("path") or "")
                    if od and p:
                        m.setdefault(od, []).append(p)
                st.session_state.bulk_saved = m
            else:
                # keep empty if nothing found
                st.session_state.bulk_saved_items = []
                st.session_state.bulk_saved = {}
            st.session_state["bulkfast_last_disk_scan_run_dir"] = str(run_base_dir)
    except Exception:
        pass

    items: list[dict[str, Any]] = st.session_state.get("bulk_saved_items") or []
    # de-dup items by path (preserve order)
    if items:
        seen_paths: set[str] = set()
        dedup: list[dict[str, Any]] = []
        for it in items:
            pth = str(it.get("path") or "")
            if not pth or pth in seen_paths:
                continue
            try:
                if not os.path.exists(pth):
                    continue
            except Exception:
                continue
            seen_paths.add(pth)
            dedup.append(it)
        st.session_state.bulk_saved_items = dedup
        items = dedup

    # One-click recovery for prompts that are still missing in the all-prompts results view.
    _tasks_for_missing_generation = _build_tasks(st.session_state.get("bulk_articles") or articles, run_base_dir)
    _saved_keys_for_missing: set[tuple[int, int]] = set()
    for it in items:
        try:
            pth = str((it or {}).get("path") or "")
            # Existence alone is not enough: Gemini occasionally leaves a
            # file named *.bin.  It must remain eligible for regeneration.
            if pth and _looks_like_image_file(Path(pth)):
                _saved_keys_for_missing.add((int(it.get("article_idx") or 0), int(it.get("prompt_idx") or 0)))
        except Exception:
            continue

    _missing_tasks_all = [
        t
        for t in _tasks_for_missing_generation
        if (int(t.article_idx), int(t.prompt_idx)) not in _saved_keys_for_missing
    ]
    _missing_tasks_ready = [
        t
        for t in _missing_tasks_all
        if not _is_regen_running_for(int(t.article_idx), int(t.prompt_idx))
    ]

    try:
        _pro_tasks_for_missing_generation = _build_pro_tasks_from_payload(payload, str(run_base_dir))
        _pro_disk_for_missing = _scan_pro_saved_items_from_disk(str(run_base_dir))
    except Exception:
        _pro_tasks_for_missing_generation = []
        _pro_disk_for_missing = {}

    _missing_pro_tasks_all = [
        t
        for t in _pro_tasks_for_missing_generation
        if int(t.task_idx) not in set(int(k) for k in (_pro_disk_for_missing or {}).keys())
    ]
    _missing_pro_tasks_ready = [
        t
        for t in _missing_pro_tasks_all
        if not _is_regen_running_for(int(t.article_idx), int(1000 + int(t.task_idx)))
    ]

    col_missing_btn, col_missing_info = st.columns([1, 3], vertical_alignment="center")
    with col_missing_btn:
        _has_any_regen_running = any(
            (j or {}).get("status") == "running" for j in (st.session_state.get("bulk_regen_jobs") or {}).values()
        )
        if st.button(
            "Догенерировать всё недогенерированное",
            key="bulkfast_generate_all_missing",
            disabled=((not _missing_tasks_ready) and (not _missing_pro_tasks_ready)) or bool(_has_any_regen_running),
            help=(
                "Запускает только prompt-ы без сохраненной картинки. Используются те же настройки, "
                "что и для основной генерации: URL, модель, Headless, Параллельно окон и Стартовый номер профиля."
            ),
        ):
            if _missing_tasks_ready:
                with st.status(f"Догенерация недостающих Fast-картинок: {len(_missing_tasks_ready)}", expanded=True) as s_missing:
                    res_missing = run_bulk_fast_generation_subset(
                        tasks=list(_missing_tasks_ready),
                        run_base_dir=str(run_base_dir),
                        url=str(url),
                        model_choice=str(model_choice),
                        headless=bool(headless),
                        executable_path=executable_path,
                        user_data_dir=str(user_data_dir),
                        parallelism=int(parallelism),
                        start_profile_num=int(start_profile_num),
                        timeout_s=int(timeout_s),
                    )
                    s_missing.write(res_missing)
                    if int(res_missing.get("errors") or 0):
                        s_missing.update(label="Догенерация Fast завершилась с ошибками", state="error")
                    else:
                        s_missing.update(label="Догенерация Fast завершена", state="complete")

            if _missing_pro_tasks_ready:
                with st.status(f"Догенерация недостающих PRO/NBP-картинок: {len(_missing_pro_tasks_ready)}", expanded=True) as s_missing_pro:
                    res_missing_pro = run_bulk_fast_pro_generation_subset(
                        tasks=list(_missing_pro_tasks_ready),
                        run_base_dir=str(run_base_dir),
                        url=str(url),
                        headless=bool(headless),
                        executable_path=executable_path,
                        user_data_dir=str(user_data_dir),
                        parallelism=int(parallelism),
                        start_profile_num=int(start_profile_num),
                        timeout_s=int(timeout_s),
                        model_choice="Nano Banana Pro",
                    )
                    s_missing_pro.write(res_missing_pro)
                    if int(res_missing_pro.get("errors") or 0):
                        s_missing_pro.update(label="Догенерация PRO/NBP завершилась с ошибками", state="error")
                    else:
                        s_missing_pro.update(label="Догенерация PRO/NBP завершена", state="complete")
            st.session_state["bulkfast_force_disk_rescan"] = True
            items = list(st.session_state.get("bulk_saved_items") or [])
    with col_missing_info:
        if _missing_tasks_all or _missing_pro_tasks_all:
            st.caption(
                f"Fast недогенерировано: {len(_missing_tasks_all)} | Fast готово: {len(_missing_tasks_ready)} | "
                f"PRO/NBP недогенерировано: {len(_missing_pro_tasks_all)} | PRO/NBP готово: {len(_missing_pro_tasks_ready)} | "
                f"параллельно окон: {int(parallelism)} | стартовый профиль: {int(start_profile_num)}"
            )
            if _has_any_regen_running:
                st.caption("Кнопка станет доступна, когда завершатся текущие задачи пересоздания.")
        else:
            st.caption("Все Fast и PRO/NBP prompt-ы для выбранного run dir уже имеют сохраненные картинки.")

    if show_all_prompts_in_results:
        # Build a "task list" view so we can show placeholders for missing images.
        _tasks_now = _build_tasks(st.session_state.get("bulk_articles") or articles, run_base_dir)

        # Map last known saved image per (article_idx, prompt_idx)
        last_saved: dict[tuple[int, int], dict[str, Any]] = {}
        for it in items:
            try:
                key = (int(it.get("article_idx") or 0), int(it.get("prompt_idx") or 0))
            except Exception:
                continue
            # keep the latest occurrence (end wins)
            last_saved[key] = it

        # Group tasks by out_dir
        by_dir_tasks: dict[str, list[PromptTask]] = {}
        for t in _tasks_now:
            by_dir_tasks.setdefault(str(t.out_dir), []).append(t)

        for out_dir, ts in by_dir_tasks.items():
            with st.expander(f"{out_dir} ({len(ts)} промптов)", expanded=False):
                cols = st.columns(int(gallery_cols))
                for j, t in enumerate(ts):
                    aidx = int(t.article_idx)
                    pidx = int(t.prompt_idx)
                    k = (aidx, pidx)
                    it = last_saved.get(k)
                    p = str((it or {}).get("path") or "")

                    c = cols[j % int(gallery_cols)]
                    with c:
                        if p:
                            try:
                                st.image(p, caption=os.path.basename(p), use_container_width=True)
                            except Exception:
                                st.write(p)

                            is_running_for_this = _is_regen_running_for(aidx, pidx, replace_path=p)
                            if is_running_for_this:
                                st.caption("⏳ Пересоздаю...")

                            _img_key = hashlib.md5(p.encode("utf-8", errors="ignore")).hexdigest()[:10]
                            if st.button(
                                "🔄 Пересоздать",
                                key=f"bulk_regen_under_img_{aidx}_{pidx}_{_img_key}",
                                disabled=is_running_for_this,
                                help=f"Пересоздать prompt #{aidx}.{pidx} (запустится параллельно, ограничение = Параллельно окон)",
                            ):
                                try:
                                    _submit_bulk_regen(aidx, pidx, replace_path=p)
                                except Exception as e:
                                    st.error(f"Ошибка старта пересоздания: {e}")
                        else:
                            # Placeholder state for failed/missing generations.
                            st.warning("Не сгенерировано")
                            is_running_for_this = _is_regen_running_for(aidx, pidx)
                            if is_running_for_this:
                                st.caption("⏳ Пересоздаю...")

                            if st.button(
                                "🔄 Пересоздать промпт",
                                key=f"bulk_regen_missing_{aidx}_{pidx}",
                                disabled=is_running_for_this,
                                help=f"Пересоздать prompt #{aidx}.{pidx} (попытается снова сохранить картинку)",
                            ):
                                try:
                                    _submit_bulk_regen(aidx, pidx, replace_path="")
                                except Exception as e:
                                    st.error(f"Ошибка старта пересоздания: {e}")

    else:
        # Legacy behavior: only show saved images
        if items:
            # Group by out_dir for expanders
            by_dir: dict[str, list[dict[str, Any]]] = {}
            for it in items:
                out_dir = str(it.get("out_dir") or "")
                by_dir.setdefault(out_dir, []).append(it)

            for out_dir, its in by_dir.items():
                with st.expander(f"{out_dir} ({len(its)} файлов)", expanded=False):
                    cols = st.columns(int(gallery_cols))
                    for j, it in enumerate(its):
                        p = str(it.get("path") or "")
                        aidx = int(it.get("article_idx") or 0)
                        pidx = int(it.get("prompt_idx") or 0)

                        c = cols[j % int(gallery_cols)]
                        with c:
                            try:
                                st.image(p, caption=os.path.basename(p), use_container_width=True)
                            except Exception:
                                st.write(p)

                            is_running_for_this = _is_regen_running_for(aidx, pidx, replace_path=p)
                            if is_running_for_this:
                                st.caption("⏳ Пересоздаю...")

                            _img_key = hashlib.md5(p.encode("utf-8", errors="ignore")).hexdigest()[:10]
                            if st.button(
                                "🔄 Пересоздать",
                                key=f"bulk_regen_under_img_{aidx}_{pidx}_{_img_key}",
                                disabled=is_running_for_this,
                                help=f"Пересоздать prompt #{aidx}.{pidx} (запустится параллельно, ограничение = Параллельно окон)",
                            ):
                                try:
                                    _submit_bulk_regen(aidx, pidx, replace_path=p)
                                except Exception as e:
                                    st.error(f"Ошибка старта пересоздания: {e}")
        else:
            # Fallback to legacy map
            saved_map: dict[str, list[str]] = st.session_state.get("bulk_saved") or {}
            if saved_map:
                for out_dir, paths in saved_map.items():
                    with st.expander(f"{out_dir} ({len(paths)} файлов)", expanded=False):
                        cols = st.columns(int(gallery_cols))
                        for j, p in enumerate(paths):
                            c = cols[j % int(gallery_cols)]
                            with c:
                                try:
                                    st.image(p, caption=os.path.basename(p), use_container_width=True)
                                except Exception:
                                    st.write(p)

    # ---------------- PRO results (NBP) inside Fast tab ----------------
    try:
        payload0 = st.session_state.get("bulk_payload") or {}
    except Exception:
        payload0 = {}

    pro_tasks_for_fast_results = _build_pro_tasks_from_payload(payload0, str(run_base_dir))
    if pro_tasks_for_fast_results:
        show_pro_in_fast_results = st.checkbox(
            "Показать PRO результаты (NBP) в Fast-результатах",
            value=bool(st.session_state.get("bulkfast_show_pro_results", True)),
            key="bulkfast_show_pro_results",
            help="Показывает картинки вида N_pro_* прямо здесь, рядом с обычными Fast результатами.",
        )

        if show_pro_in_fast_results:
            pro_tasks = list(pro_tasks_for_fast_results)

            if pro_tasks:
                st.subheader("PRO результаты (NBP)")

                # Best-effort load existing saved pro images from disk
                pro_disk = _scan_pro_saved_items_from_disk(str(run_base_dir))

                # Group tasks by out_dir
                by_dir_pro: dict[str, list[ProTask]] = {}
                for t in pro_tasks:
                    by_dir_pro.setdefault(str(t.out_dir), []).append(t)

                def _submit_pro_regen(_t: ProTask, *, replace_path: str = "") -> None:
                    """Submit PRO regeneration via the same background mechanism as normal Fast regen.

                    This enables parallel windows/profiles and the same running indicator.
                    """

                    st.session_state["bulk_regen_last_click_ts"] = time.time()

                    regen_workers = max(1, min(12, int(parallelism)))

                    ex0 = st.session_state.get("bulk_regen_executor")
                    cur_w = int(getattr(ex0, "_max_workers", 0) or 0) if ex0 else 0
                    if (ex0 is None) or (cur_w != regen_workers):
                        try:
                            if ex0:
                                ex0.shutdown(wait=False, cancel_futures=False)
                        except Exception:
                            pass
                        st.session_state.bulk_regen_executor = concurrent.futures.ThreadPoolExecutor(max_workers=regen_workers)

                    executor = st.session_state.bulk_regen_executor

                    # Prompt (respect edited prompt from Pro tab)
                    pkey = f"bulkpro_prompt_{_t.article_idx}_{_t.prompt_idx}"
                    prompt_raw = (st.session_state.get(pkey) or str(_t.prompt) or "").strip()
                    prompt_raw = _sanitize_prompt(prompt_raw)
                    if not prompt_raw:
                        st.error("Пустой PRO prompt")
                        return

                    # Same variation logic as fast regen, but apply to RAW prompt (before wrapper)
                    try:
                        prompt_var = _apply_regen_prefix_variation(prompt_raw, key=f"pro.{int(_t.task_idx)}")
                    except Exception:
                        prompt_var = prompt_raw

                    final_prompt = _wrap_prompt_for_nbp_fast(prompt_var)

                    # PRO regen uses virtual prompt_idx to keep jobs unique and avoid collisions with normal prompts.
                    pro_pidx = int(1000 + int(_t.task_idx))

                    with st.session_state["bulk_regen_lock"]:
                        st.session_state.bulk_regen_job_seq += 1
                        job_id = f"job_{st.session_state.bulk_regen_job_seq}_{int(_t.article_idx)}_{pro_pidx}"

                    # Shared profile pool sized by parallelism (ensures different windows/profiles)
                    sig = (str(user_data_dir or ""), int(regen_workers), int(start_profile_num))
                    if st.session_state.get("bulk_regen_profile_pool") is None or st.session_state.get("bulk_regen_pool_sig") != sig:
                        for d in list(st.session_state.get("bulk_regen_tmp_profiles") or []):
                            try:
                                shutil.rmtree(d, ignore_errors=True)
                            except Exception:
                                pass
                        pp, tmps = _prepare_profile_pool(
                            user_data_dir,
                            max_workers=int(regen_workers),
                            start_profile_num=int(start_profile_num),
                        )
                        st.session_state.bulk_regen_profile_pool = pp
                        st.session_state.bulk_regen_tmp_profiles = tmps
                        st.session_state.bulk_regen_pool_sig = sig

                    shared_pool = st.session_state.bulk_regen_profile_pool

                    t_var = BulkTask(
                        article_idx=int(_t.article_idx),
                        article_title=str(_t.article_title),
                        prompt_idx=int(pro_pidx),
                        prompt=str(final_prompt),
                        out_dir=str(_t.out_dir),
                    )

                    def _job_fn(_t2=t_var, _pp=shared_pool):
                        return _run_one_task(
                            _t2,
                            url=url,
                            headless=bool(headless),
                            executable_path=executable_path,
                            model_choice=model_choice,
                            timeout_s=int(timeout_s),
                            profile_pool=_pp,
                        )

                    fut = executor.submit(_job_fn)

                    jobs = st.session_state.get("bulk_regen_jobs") or {}
                    jobs[job_id] = {
                        "status": "running",
                        "kind": "pro",
                        "article_idx": int(_t.article_idx),
                        "prompt_idx": int(pro_pidx),
                        "replace_path": str(replace_path or ""),
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "started_at": datetime.now().isoformat(timespec="seconds"),
                        "future": fut,
                        # PRO-specific metadata for post-processing
                        "pro_task_idx": int(_t.task_idx),
                        "pro_prompt_raw": str(prompt_raw),
                        "pro_out_dir": str(_t.out_dir),
                    }
                    st.session_state.bulk_regen_jobs = jobs

                    q: queue.Queue = st.session_state.get("bulk_regen_result_queue")

                    def _done_cb(_fut, _job_id=job_id, _q=q):
                        try:
                            r = _fut.result()
                            _q.put(
                                {
                                    "job_id": _job_id,
                                    "result": r,
                                    "ended_at": datetime.now().isoformat(timespec="seconds"),
                                }
                            )
                        except Exception as e:
                            _q.put(
                                {
                                    "job_id": _job_id,
                                    "error": str(e),
                                    "ended_at": datetime.now().isoformat(timespec="seconds"),
                                }
                            )

                    try:
                        fut.add_done_callback(_done_cb)
                    except Exception:
                        pass

                    st.session_state["bulk_regen_dirty_counter"] = max(3, int(st.session_state.get("bulk_regen_dirty_counter") or 0))

                for out_dir, ts in by_dir_pro.items():
                    with st.expander(f"PRO: {out_dir} ({len(ts)} промптов)", expanded=False):
                        cols = st.columns(int(gallery_cols))
                        for j, t in enumerate(ts):
                            c = cols[j % int(gallery_cols)]
                            with c:
                                it = pro_disk.get(int(t.task_idx)) or {}
                                p = str(it.get("path") or "")

                                if p:
                                    try:
                                        st.image(p, caption=os.path.basename(p), use_container_width=True)
                                    except Exception:
                                        st.write(p)
                                else:
                                    st.warning("Не сгенерировано")

                                pro_pidx = int(1000 + int(t.task_idx))
                                is_running_for_this = _is_regen_running_for(int(t.article_idx), int(pro_pidx), replace_path=p)
                                if is_running_for_this:
                                    st.caption("⏳ Генерация...")

                                if st.button(
                                    "🔄 Пересоздать промпт",
                                    key=f"bulkfast_pro_regen_{int(t.task_idx)}_{hashlib.md5(str(out_dir).encode('utf-8', errors='ignore')).hexdigest()[:8]}",
                                    disabled=is_running_for_this,
                                    help=f"Пересоздать PRO prompt #{int(t.task_idx)} (через FAST, сохранит как N_pro_*)",
                                ):
                                    try:
                                        _submit_pro_regen(t, replace_path=p)
                                    except Exception as e:
                                        st.error(f"Failed to submit PRO regen: {e}")
            else:
                st.info("PRO tasks not found in payload")

    errs = st.session_state.get("bulk_errors") or []
    if errs:
        st.error(f"Ошибок: {len(errs)}")
        with st.expander("Ошибки", expanded=False):
            st.json(errs)


def _render_pro_tab() -> None:
    st.subheader("Gemini generation via browser (Nano Banana Pro / bulk)")
    st.caption(
        "Аналог Tab2 из app_unified_streamlit.py, но сразу для всех статей из payload. "
        "Генерируются первые 4 промпта каждой статьи (если есть)."
    )

    # ---------------- Inputs (like unified Tab2) ----------------
    nbp_url = st.selectbox("URL интерфейса", DEFAULT_URLS, index=0, key="bulkpro_url")
    nbp_headless = st.checkbox(
        "Headless режим (не рекомендуется для UI-логина)",
        value=False,
        key="bulkpro_headless",
    )
    nbp_executable_path = st.text_input(
        "Путь к chrome.exe",
        value=st.session_state.get("bulkfast_exe_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"),
        key="bulkpro_exe_path",
    ).strip() or None

    # Cancel flag for Tongyi runs
    st.session_state.setdefault("bulkpro_tongyi_cancel", False)
    st.session_state.setdefault("bulkpro_tongyi_running", False)

    pro_engine = st.selectbox(
        "Engine (генератор)",
        options=["Nano Banana Pro (Gemini)", "Tongyi (HF Space)"],
        index=0,
        key="bulkpro_engine",
        help="Nano Banana Pro = текущая логика Gemini. Tongyi = генерация по чистым промптам в https://tongyi-mai-z-image-turbo.hf.space/.",
    )

    c_stop1, c_stop2 = st.columns([1.2, 6], gap="small")
    with c_stop1:
        if st.button("🛑 STOP Tongyi", key="bulkpro_tongyi_stop"):
            st.session_state["bulkpro_tongyi_cancel"] = True
    with c_stop2:
        if st.session_state.get("bulkpro_tongyi_cancel"):
            st.caption("Tongyi: STOP requested (следующий шаг остановит генерацию)")

    # Visible Tongyi log (for cases when you don't see console output)
    st.session_state.setdefault("bulkpro_tongyi_log_lines", [])
    tongyi_log_ph = st.empty()

    def _tongyi_log(line: str) -> None:
        try:
            lines = st.session_state.get("bulkpro_tongyi_log_lines") or []
            lines.append(str(line))
            # keep last N lines
            if len(lines) > 300:
                lines = lines[-300:]
            st.session_state["bulkpro_tongyi_log_lines"] = lines
            tongyi_log_ph.code("\n".join(lines[-60:]))
        except Exception:
            pass

    # Tongyi options
    tongyi_space_url = st.text_input(
        "Tongyi Space URL",
        value=str(st.session_state.get("bulkpro_tongyi_space_url") or TONGYI_DEFAULT_SPACE_URL),
        key="bulkpro_tongyi_space_url",
    ).strip()
    tongyi_size_text = st.text_input(
        "Tongyi size option text",
        value=str(st.session_state.get("bulkpro_tongyi_size_text") or TONGYI_DEFAULT_SIZE_TEXT),
        key="bulkpro_tongyi_size_text",
        help="Должно совпадать с текстом опции в 'Width x Height (Ratio)' (например: 832x1248 ( 2:3 )).",
    ).strip()
    tongyi_timeout_s = st.number_input(
        "Tongyi timeout per image (sec)",
        min_value=60,
        max_value=3600,
        value=int(st.session_state.get("bulkpro_tongyi_timeout_s") or 900),
        step=30,
        key="bulkpro_tongyi_timeout_s",
    )

    tongyi_headless = st.checkbox(
        "Tongyi headless",
        value=bool(st.session_state.get("bulkpro_tongyi_headless") or False),
        key="bulkpro_tongyi_headless",
        help="Рекомендую выключить (False) для отладки: окно браузера будет видно и не будет 'мигать'.",
    )

    tongyi_start_from = st.number_input(
        "Tongyi bulk: start from prompt #",
        min_value=1,
        max_value=9999,
        value=int(st.session_state.get("bulkpro_tongyi_start_from") or 1),
        step=1,
        key="bulkpro_tongyi_start_from",
        help="1 = Начать с первого промпта в текущем списке. 5 = пропустить первые 4 и начать с 5-го.",
    )

    def _bulkpro_tongyi_set_action(action: str) -> None:
        try:
            st.session_state["bulkpro_tongyi_action"] = str(action)
        except Exception:
            pass

    # ---------------- Queue mode (multiple payloads; sequential; Tongyi only) ----------------
    with st.expander("Tongyi queue (несколько payload за один запуск)", expanded=False):
        st.caption(
            "Если у тебя глобальная ротация IP, параллельные запуски будут мешать друг другу. "
            "Этот режим прогоняет несколько payload JSON СТРОГО ПОСЛЕДОВАТЕЛЬНО: закончили payload #1 → перешли к payload #2. "
            "Сохранение идёт в run dir так же, как и раньше (через mapping по payload_path)."
        )
        queue_enabled = st.checkbox(
            "Enable queue mode (Tongyi only)",
            value=bool(st.session_state.get("bulkpro_tongyi_queue_enabled", False)),
            key="bulkpro_tongyi_queue_enabled",
        )

        # Auto-detect recent payload files (created by app_unified_streamlit.py)
        try:
            max_recent = st.number_input(
                "Show last payload files",
                min_value=1,
                max_value=50,
                value=int(st.session_state.get("bulkpro_tongyi_queue_max_recent") or 10),
                step=1,
                key="bulkpro_tongyi_queue_max_recent",
                help="Ищет в текущей папке файлы tmp_rovodev_bulk_fast_payload_*.json",
                disabled=(not queue_enabled),
            )
        except Exception:
            max_recent = 10

        recent_payloads: list[str] = []
        try:
            cand = list(Path(".").glob("tmp_rovodev_bulk_fast_payload_*.json"))
            cand.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
            recent_payloads = [str(p.resolve()) for p in cand[: int(max_recent)]]
        except Exception:
            recent_payloads = []

        picked_recent: list[str] = []
        if recent_payloads:
            picked_recent = st.multiselect(
                "Pick recent payloads",
                options=recent_payloads,
                default=[],
                key="bulkpro_tongyi_queue_recent_pick",
                disabled=(not queue_enabled),
                help="Отметь нужные payload, затем нажми 'Use selected' — пути добавятся в очередь ниже.",
            )
            c_use, c_clear = st.columns([1, 1])
            with c_use:
                if st.button(
                    "Use selected",
                    key="bulkpro_tongyi_queue_use_selected",
                    disabled=(not queue_enabled),
                ):
                    prev = str(st.session_state.get("bulkpro_tongyi_queue_paths") or "").strip()
                    prev_lines = [ln.strip() for ln in prev.splitlines() if ln.strip()]
                    merged = list(dict.fromkeys([*prev_lines, *[str(x) for x in (picked_recent or []) if str(x).strip()]]))
                    st.session_state["bulkpro_tongyi_queue_paths"] = "\n".join(merged)
                    st.success(f"Added {len(picked_recent or [])} payload(s) to queue")
            with c_clear:
                if st.button(
                    "Clear list",
                    key="bulkpro_tongyi_queue_clear",
                    disabled=(not queue_enabled),
                ):
                    st.session_state["bulkpro_tongyi_queue_paths"] = ""
                    st.success("Queue cleared")
        else:
            st.caption("Recent payload files not found in current folder.")

        queue_text = st.text_area(
            "Payload JSON paths (по одному пути на строку)",
            value=str(st.session_state.get("bulkpro_tongyi_queue_paths") or "").strip(),
            height=120,
            key="bulkpro_tongyi_queue_paths",
            help="Можно вставить вручную, если файл не попал в список recent.",
            disabled=(not queue_enabled),
        )
        try:
            _ql = [ln.strip() for ln in str(queue_text or "").splitlines() if ln.strip()]
            st.caption(f"Queue contains: {len(_ql)} payload(s)")
        except Exception:
            pass

        st.button(
            "Сгенерировать Tongyi для всех payload (queue / sequential)",
            type="primary",
            key="bulkpro_tongyi_queue_run",
            disabled=(not queue_enabled),
            on_click=_bulkpro_tongyi_set_action,
            args=("queue",),
        )

    st.markdown("---")

    # Load payload (shared with fast tab)
    # If user loaded payload in Fast tab, mirror it here automatically.
    try:
        bf = str(st.session_state.get("bulkfast_payload_path") or "").strip()
        bp = str(st.session_state.get("bulkpro_payload_path") or "").strip()
        if bf and not bp:
            st.session_state["bulkpro_payload_path"] = bf
    except Exception:
        pass

    load_file = st.query_params.get("load_file") if hasattr(st, "query_params") else None
    if isinstance(load_file, list):
        load_file = load_file[0] if load_file else None
    load_file = str(load_file).strip() if load_file else ""

    # NOTE: text_input's `value=` is only used on first creation; after that `key` wins.
    _initial_pro_payload = str(st.session_state.get("bulkpro_payload_path") or load_file or "").strip()
    payload_path = st.text_input("Payload JSON path", value=_initial_pro_payload, key="bulkpro_payload_path")

    if "bulk_payload" not in st.session_state:
        st.session_state.bulk_payload = None

    col_load1, col_load2 = st.columns([1, 4])
    with col_load1:
        if st.button("Загрузить payload", type="primary", key="bulkpro_load_payload"):
            try:
                if str(payload_path or "").strip() in {".", "./", ".\\"}:
                    raise ValueError("Payload path resolves to current directory '.'; select a JSON file")
                st.session_state.bulk_payload = _load_payload(payload_path)
                # Sync to Fast tab input too (so both tabs show the same current payload path).
                try:
                    st.session_state["bulkfast_payload_path"] = str(payload_path or "").strip()
                except Exception:
                    pass
                st.success("Payload загружен")
            except Exception as e:
                st.session_state.bulk_payload = None
                st.error(f"Не удалось загрузить payload: {e}")

    # ---------------- Tongyi queue runner (multiple payloads; sequential) ----------------
    # This allows night runs for multiple sites without parallelism (important when IP rotation is global).
    _action = str(st.session_state.get("bulkpro_tongyi_action") or "")
    if _action == "queue" and bool(queue_enabled) and str(pro_engine) == "Tongyi (HF Space)":
        st.session_state["bulkpro_tongyi_action"] = ""
        if bool(st.session_state.get("bulkpro_tongyi_running")):
            st.warning("Tongyi is already running. Stop the current run first (🛑 STOP Tongyi) and wait until it fully stops.")
            return
        st.session_state["bulkpro_tongyi_running"] = True
        st.info("Tongyi queue: starting…")
        if tongyi_generate_batch_detailed is None:
            err = _TONGYI_IMPORT_ERROR or "Unknown import error"
            st.error("Tongyi generator не доступен (не удалось импортировать generate_tongyi_images.py).")
            st.code(err)
            st.session_state["bulkpro_tongyi_running"] = False
            return

        raw_lines = [ln.strip().strip('"').strip("'") for ln in str(queue_text or "").splitlines()]
        q_paths = [ln for ln in raw_lines if ln]
        if not q_paths:
            st.warning("Queue mode: список payload путей пуст")
            st.session_state["bulkpro_tongyi_running"] = False
            return

        st.session_state.setdefault("bulkpro_saved_paths", [])
        st.session_state.setdefault("bulkpro_errors", [])

        # reset cancel flag on new queue run
        st.session_state["bulkpro_tongyi_cancel"] = False

        total_payloads = len(q_paths)
        q_progress = st.progress(0)

        with st.status(f"Tongyi queue: {total_payloads} payload(ов) — sequential...", expanded=True) as q_status:
            for pi, pth in enumerate(q_paths, 1):
                if bool(st.session_state.get("bulkpro_tongyi_cancel")):
                    st.warning("Tongyi queue: отменено пользователем")
                    st.session_state["bulkpro_tongyi_running"] = False
                    return

                try:
                    pth_norm = str(Path(str(pth)).expanduser().resolve())
                except Exception:
                    pth_norm = str(pth)

                q_status.write(f"Payload {pi}/{total_payloads}: {pth_norm}")

                try:
                    payload_i = _load_payload(pth_norm)
                except Exception as e:
                    st.error(f"Не удалось загрузить payload: {pth_norm}")
                    st.code(str(e))
                    continue

                base_root_i = str(payload_i.get("base_root") or "generate automation")
                has_pro_articles_i = "pro_articles" in payload_i and payload_i.get("pro_articles") is not None
                articles_i = (payload_i.get("pro_articles") or []) if has_pro_articles_i else (payload_i.get("articles") or [])

                try:
                    pro_prompt_count_i = int(payload_i.get("pro_prompt_count") or 4)
                except Exception:
                    pro_prompt_count_i = 4

                # Pick run dir using existing logic (it also preserves mapping in bulk_last_run_dirs.json)
                run_base_dir_i = _suggest_run_dir(base_root_i, payload_path=pth_norm)
                _remember_run_dir(pth_norm, run_base_dir_i)

                tasks_i = _build_pro_tasks_from_articles(
                    list(articles_i or []),
                    str(run_base_dir_i),
                    max_prompts_per_article=int(pro_prompt_count_i),
                )
                if not tasks_i:
                    q_status.write(f"Payload {pi}: нет промптов")
                    continue

                # Flatten snapshot for Tongyi (clean prompts)
                tasks_snapshot_i: list[dict[str, Any]] = []
                for t in tasks_i:
                    pval = _sanitize_prompt(str(getattr(t, 'prompt', '') or '')).strip()
                    if not pval:
                        continue
                    tasks_snapshot_i.append({"task": t, "prompt": pval})

                # Apply start_from per payload
                try:
                    start_from_i = max(1, int(tongyi_start_from or 1))
                except Exception:
                    start_from_i = 1
                if start_from_i > 1:
                    tasks_snapshot_i = tasks_snapshot_i[(start_from_i - 1) :]
                if not tasks_snapshot_i:
                    q_status.write(f"Payload {pi}: после start_from нет задач")
                    continue

                # group tasks by out_dir (article folder)
                grouped_i: dict[str, list[dict[str, Any]]] = {}
                for it in tasks_snapshot_i:
                    task = it["task"]
                    out_dir = str(getattr(task, "out_dir", "") or "").strip() or str(run_base_dir_i)
                    grouped_i.setdefault(out_dir, []).append({"task": task, "prompt": it["prompt"]})

                q_status.write(
                    f"Payload {pi}: задач {sum(len(v) for v in grouped_i.values())}, run_dir={run_base_dir_i}"
                )

                for out_dir, items in grouped_i.items():
                    if bool(st.session_state.get("bulkpro_tongyi_cancel")):
                        st.warning("Tongyi queue: отменено пользователем")
                        st.session_state["bulkpro_tongyi_running"] = False
                        return

                    prompts = [
                        str(x.get("prompt") or "").strip()
                        for x in (items or [])
                        if str(x.get("prompt") or "").strip()
                    ]
                    if not prompts:
                        continue

                    def _cb(i: int, n: int, _prompt: str, _item: dict):
                        # Rough global progress across payloads.
                        try:
                            frac = (pi - 1) / max(1, total_payloads)
                            q_progress.progress(
                                min(
                                    1.0,
                                    frac
                                    + (0.999 / max(1, total_payloads))
                                    * (float(i) / max(1.0, float(n))),
                                )
                            )
                        except Exception:
                            pass

                    def _on_saved(ii: int, _tt: int, saved_path: str) -> str:
                        # ii is 1-based index within this group
                        idx0 = max(0, int(ii) - 1)
                        if idx0 >= len(items):
                            return saved_path
                        task0 = items[idx0].get("task")
                        prompt0 = str(items[idx0].get("prompt") or "")
                        final_p = _move_image_to_pro_named_file(
                            src_path=str(saved_path),
                            out_dir=str(getattr(task0, "out_dir", out_dir) or out_dir),
                            task_idx=int(getattr(task0, "task_idx", 0) or 0),
                            prompt_raw=prompt0,
                        )
                        try:
                            _tongyi_log(
                                f"[rename] {os.path.basename(str(saved_path))} -> {os.path.basename(str(final_p))}"
                            )
                        except Exception:
                            pass
                        return str(final_p)

                    try:
                        results = tongyi_generate_batch_detailed(
                            space_url=str(tongyi_space_url or TONGYI_DEFAULT_SPACE_URL),
                            prompts=list(prompts),
                            out_dir=str(out_dir),
                            headless=bool(tongyi_headless),
                            hosts_start_pos=None,
                            cancel_check=lambda: bool(st.session_state.get("bulkpro_tongyi_cancel")),
                            max_wait_sec=int(tongyi_timeout_s),
                            size_text=str(tongyi_size_text or TONGYI_DEFAULT_SIZE_TEXT),
                            progress_callback=_cb,
                            log_callback=_tongyi_log,
                            on_image_saved=_on_saved,
                        )
                    except Exception as e:
                        msg = str(e)
                        if "Cancelled" in msg or "Canceled" in msg:
                            st.warning("Tongyi queue: отменено")
                            st.session_state["bulkpro_tongyi_running"] = False
                            return

                        st.code(msg)
                        for x in items:
                            st.session_state["bulkpro_errors"].append(
                                {"task_idx": int(x["task"].task_idx), "error": msg}
                            )
                        continue

                    for idx, r in enumerate(results or []):
                        task = items[idx]["task"] if idx < len(items) else None
                        if r.get("path") and task is not None:
                            src = str(r.get("path") or "")
                            prompt_raw = str(items[idx].get("prompt") or "") if idx < len(items) else ""
                            final_p = _move_image_to_pro_named_file(
                                src_path=src,
                                out_dir=str(getattr(task, "out_dir", out_dir) or out_dir),
                                task_idx=int(getattr(task, "task_idx", 0) or 0),
                                prompt_raw=prompt_raw,
                            )
                            st.session_state["bulkpro_saved_paths"].append(str(final_p))
                        else:
                            st.session_state["bulkpro_errors"].append(
                                {
                                    "task_idx": int(task.task_idx) if task else None,
                                    "error": str(r.get("error") or "Unknown"),
                                }
                            )

                st.session_state["bulkpro_saved_paths"] = list(
                    dict.fromkeys(st.session_state.get("bulkpro_saved_paths") or [])
                )
                try:
                    q_progress.progress(min(1.0, float(pi) / float(total_payloads)))
                except Exception:
                    pass

            q_status.update(label="Tongyi queue finished", state="complete")

        if st.session_state.get("bulkpro_errors"):
            st.error(f"Ошибок: {len(st.session_state.get('bulkpro_errors') or [])}")
        else:
            st.success("Готово")
        st.session_state["bulkpro_tongyi_running"] = False
        return

    payload = st.session_state.bulk_payload
    if not payload:
        st.info("Укажите путь к payload JSON (его создаёт Tab0 в app_unified_streamlit.py) и нажмите 'Загрузить payload'.")
        return

    base_root = str(payload.get("base_root") or "generate automation")

    # In payload from app_unified_streamlit.py:
    # - payload["articles"] contains fast prompts (typically AFTER first 4)
    # - payload["pro_articles"] contains first N prompts (raw) for Pro tab (if present)
    #   where N is stored in payload["pro_prompt_count"] (legacy: 4, one-image mode: 2).
    has_pro_articles = "pro_articles" in payload and payload.get("pro_articles") is not None
    articles = (payload.get("pro_articles") or []) if has_pro_articles else (payload.get("articles") or [])

    # How many prompts to use per article in Pro tab.
    try:
        pro_prompt_count = int(payload.get("pro_prompt_count") or 4)
    except Exception:
        pro_prompt_count = 4
    pro_prompt_count = max(1, min(12, pro_prompt_count))

    # Run directory (restore previous if possible; shared with Fast tab via bulk_run_base_dir)
    if "bulk_run_base_dir" not in st.session_state:
        try:
            st.session_state.bulk_run_base_dir = _suggest_run_dir(base_root, payload_path=payload_path)
        except Exception:
            st.session_state.bulk_run_base_dir = _suggest_run_dir("generate automation", payload_path=payload_path)

    existing_runs = _list_existing_run_dirs(base_root)
    with st.expander("Run folder (восстановление / выбор)", expanded=False):
        if existing_runs:
            picked = st.selectbox(
                "Existing run dirs",
                options=existing_runs,
                index=0,
                key="bulkpro_existing_run_dirs",
                help="Выбери существующую папку, чтобы подтянуть уже сгенерированные картинки после перезапуска.",
            )
            if st.button("Use selected run dir", key="bulkpro_use_selected_run"):
                sel = str(picked)
                st.session_state.bulk_run_base_dir = sel
                # IMPORTANT: keep the text_input widget in sync, otherwise it overwrites bulk_run_base_dir on rerun
                try:
                    st.session_state["bulkpro_run_base_dir"] = sel
                except Exception:
                    pass
                # Clear cached UI lists so disk scan/preview reflects the selected folder
                try:
                    st.session_state["bulkpro_saved_paths"] = []
                    st.session_state["bulkpro_errors"] = []
                except Exception:
                    pass
                try:
                    st.rerun()
                except Exception:
                    pass
        if st.button("Create NEW run dir", key="bulkpro_create_new_run"):
            newd = _ensure_run_base_dir(base_root)
            st.session_state.bulk_run_base_dir = newd
            try:
                st.session_state["bulkpro_run_base_dir"] = newd
            except Exception:
                pass
            try:
                st.session_state["bulkpro_saved_paths"] = []
                st.session_state["bulkpro_errors"] = []
            except Exception:
                pass
            try:
                st.rerun()
            except Exception:
                pass

    run_base_dir = st.text_input(
        "Run dir (корень сохранения для этого запуска)",
        value=st.session_state.bulk_run_base_dir,
        key="bulkpro_run_base_dir",
    )
    st.session_state.bulk_run_base_dir = run_base_dir
    _remember_run_dir(payload_path, run_base_dir)

    # Prompts source for Pro tab: keep it separate from Fast tab edits.
    # Fast tab stores edited prompts in st.session_state.bulk_articles (but that list contains *fast* prompts,
    # usually AFTER the first 4). If we reuse it here, Pro would incorrectly show only those.
    #
    # IMPORTANT: refresh Pro articles when payload changes. Otherwise Pro UI sticks to the first loaded payload.
    _pro_payload_sig = str(payload_path or "").strip()
    if st.session_state.get("bulkpro_last_payload_sig") != _pro_payload_sig:
        st.session_state["bulkpro_last_payload_sig"] = _pro_payload_sig
        st.session_state["bulk_pro_articles"] = list(articles or [])
        # Reset a few UI selections that depend on tasks/prompts
        for k in ["bulkpro_prompt_pick", "bulkpro_prompt_pick2", "bulkpro_selected_task", "bulkpro_selected_prompt"]:
            try:
                if k in st.session_state:
                    del st.session_state[k]
            except Exception:
                pass

    articles_src = st.session_state.get("bulk_pro_articles") or articles

    tasks = _build_pro_tasks_from_articles(articles_src, run_base_dir, max_prompts_per_article=pro_prompt_count)
    st.subheader(f"Pro-задания: {len(tasks)}")
    if not tasks:
        st.warning("В payload нет промптов")
        return

    base_profile = st.text_input(
        "Базовый user-data-dir (профиль). Можно использовать .chrome_automation_profile_1.._N",
        value=os.path.abspath(".chrome_automation_profile"),
        key="bulkpro_base_profile",
    )
    base_profile = _normalize_user_data_dir(base_profile) or base_profile
    st.caption(f"Реально используется: {base_profile}")

    start_profile_num = st.number_input(
        "Стартовый номер профиля (если 1 → будут использованы _1,_2,_3,...) ",
        min_value=1,
        max_value=999,
        value=int(st.session_state.get("bulkpro_start_profile_num", 1) or 1),
        step=1,
        key="bulkpro_start_profile_num",
    )

    keep_windows_open = st.checkbox(
        "Оставить окна Chrome открытыми после запуска",
        value=True,
        key="bulkpro_keep_open",
        help="Если включено — задачи выполняются последовательно, окна остаются открытыми (как в unified Tab2).",
    )

    parallelism = st.number_input(
        "Параллельно окон (concurrency)",
        min_value=1,
        max_value=12,
        value=4,
        step=1,
        key="bulkpro_parallelism",
        help="Для параллельного режима выключите 'Оставить окна Chrome открытыми'.",
    )

    retries = st.number_input(
        "Повторов (retry) на один промпт",
        min_value=0,
        max_value=5,
        value=2,
        step=1,
        key="bulkpro_retries",
    )

    st.markdown("---")

    # Manual mode helper: rename next downloaded image to expected name
    enable_dl_watcher = st.checkbox(
        "Manual download: auto-rename next downloaded image",
        value=bool(st.session_state.get("bulkpro_enable_dl_watcher", True)),
        key="bulkpro_enable_dl_watcher",
        help=(
            "Если включено: после нажатия 'Open profile' будет запущен watcher папки Downloads. "
            "Когда ты в Gemini нажмёшь Download, скрипт поймает новый файл и переименует его в имя из 'Copy name'."
        ),
    )
    downloads_dir = st.text_input(
        "Downloads folder (для watcher)",
        value=str(st.session_state.get("bulkpro_downloads_dir") or _default_downloads_dir()),
        key="bulkpro_downloads_dir",
        help="Папка, куда Chrome реально скачивает файлы. Обычно C:/Users/<you>/Downloads.",
    )

    # Storage
    st.session_state.setdefault("bulkpro_saved_paths", [])
    st.session_state.setdefault("bulkpro_errors", [])
    st.session_state.setdefault("bulkpro_keepalive", [])
    st.session_state.setdefault("bulkpro_dl_watch_status", {})
    # old per-task threads are no longer used; a single dispatcher is used instead
    st.session_state.setdefault("bulkpro_dl_dispatcher_thread", None)
    st.session_state.setdefault("bulkpro_dl_dispatcher_watch_dir", "")
    st.session_state.setdefault("bulkpro_dl_queue", [])

    # ---------------- Prompt list UI (group by article) ----------------
    with st.expander("Промпты Pro (первые 4 каждой статьи)", expanded=False):
        # Render tasks with Tab2-like buttons
        for t in tasks:
            st.markdown(f"#### #{t.article_idx}. {t.article_title} — Prompt {t.prompt_idx} (task {t.task_idx})")

            p_key = f"bulkpro_prompt_{t.article_idx}_{t.prompt_idx}"
            pval = st.text_input(
                "Промпт",
                value=str(t.prompt),
                key=p_key,
            )

            # Default mapping: Prompt 1..4 -> profile slots start_profile_num..+3
            slot_num = int(start_profile_num) + (int(t.prompt_idx) - 1)
            default_ud = f"{base_profile}_{slot_num}"
            ud_key = f"bulkpro_ud_{t.article_idx}_{t.prompt_idx}"
            ud_snapshot = st.text_input(
                "Chrome user-data-dir для этого промпта",
                value=st.session_state.get(ud_key, _normalize_user_data_dir(default_ud) or default_ud),
                key=ud_key,
                help="Можно указать конкретный профиль. Если профиля нет, будет создана временная копия при запуске.",
            )

            use_profile_clone = st.checkbox(
                "Clone profile (slower, safer for parallel)",
                value=bool(st.session_state.get("bulkpro_use_profile_clone", False)),
                key=f"bulkpro_clone_{t.task_idx}",
                help=(
                    "Включи, если открываешь параллельно несколько окон С ОДНИМ user-data-dir. "
                    "Если у тебя разные профили (разные user-data-dir) — держи выключенным, так быстрее."
                ),
            )
            # Remember last choice globally
            try:
                st.session_state["bulkpro_use_profile_clone"] = bool(use_profile_clone)
            except Exception:
                pass

            # Buttons row (copy/open/generate)
            c1, c2, c3, c4, _sp = st.columns([1.35, 1.05, 1.2, 1.35, 5.95], gap="small", vertical_alignment="bottom")
            # For Gemini Pro we wrap the prompt with text-only 10:16 ratio instructions.
            # For Tongyi we must send the clean prompt only.
            if str(pro_engine) == "Tongyi (HF Space)":
                final_prompt_preview = (_sanitize_prompt(pval or "").strip() if (pval or "").strip() else "")
            else:
                final_prompt_preview = (
                    f"{NBP_PROMPT_PRE}{_sanitize_prompt(pval or '').strip()}{NBP_PROMPT_POST}" if (pval or '').strip() else ""
                )
            with c1:
                _clipboard_copy_text_button_with_random_prefix(
                    label="Copy prompt",
                    text=final_prompt_preview,
                    key=f"bulkpro_copy_prompt_{t.task_idx}",
                    prefixes=["Будь добр", "плиз", "пожалуйста", "please"],
                )
            with c2:
                latest = _get_latest_pro_saved_image_path(
                    int(t.task_idx),
                    out_dir=str(t.out_dir),
                    run_base_dir=str(run_base_dir),
                )
                pred = _predict_nbp_image_basename(int(t.task_idx), pval, j=1, ext="png", out_dir=str(t.out_dir))
                # IMPORTANT: for Pro manual downloads we always use the predicted pro filename,
                # not the latest file on disk (which may be a fast image and break naming).
                name_to_copy = pred
                _clipboard_copy_text_button(
                    label="Copy name",
                    text=name_to_copy,
                    key=f"bulkpro_copy_name_{t.task_idx}",
                )
            with c3:
                if st.button("Open profile", key=f"bulkpro_open_profile_{t.task_idx}"):
                    # For reliable parallel manual downloads:
                    # 1) Create a per-task cloned profile (so Preferences don't race)
                    # 2) Set per-task download directory
                    base_profile = str(ud_snapshot or "").strip()
                    if use_profile_clone:
                        task_profile_dir = _get_or_create_manual_pro_profile_dir(
                            base_user_data_dir=base_profile,
                            run_base_dir=str(run_base_dir),
                            task_key=str(t.task_idx),
                        )
                    else:
                        task_profile_dir = base_profile
                    task_dl_dir = str(Path(str(run_base_dir)) / "_manual_pro_downloads" / f"task_{int(t.task_idx):04d}")

                    # Start watcher (optional) BEFORE opening the browser
                    try:
                        if enable_dl_watcher and name_to_copy:
                            _start_download_rename_watcher(
                                watch_dir=str(task_dl_dir),
                                expected_basename=str(name_to_copy),
                                task_key=str(t.task_idx),
                                dest_dir=str(t.out_dir),
                                timeout_s=240,
                            )
                    except Exception:
                        pass

                    _open_chrome_window_with_profile(
                        executable_path=nbp_executable_path,
                        user_data_dir=str(task_profile_dir),
                        url=nbp_url,
                        download_dir=str(task_dl_dir),
                    )

            with c4:
                if str(pro_engine) == "Tongyi (HF Space)":
                    if st.button("Generate", key=f"bulkpro_tongyi_one_{t.task_idx}"):
                        if tongyi_generate_batch_detailed is None:
                            st.error("Tongyi generator module not available")
                        else:
                            prompt_clean = _sanitize_prompt(pval or "").strip()
                            if not prompt_clean:
                                st.warning("Пустой промпт")
                            else:
                                try:
                                    # Reset cancel flag on explicit single-generate click
                                    st.session_state["bulkpro_tongyi_cancel"] = False
                                    res_list = tongyi_generate_batch_detailed(
                                        space_url=str(tongyi_space_url or TONGYI_DEFAULT_SPACE_URL),
                                        prompts=[prompt_clean],
                                        out_dir=str(t.out_dir),
                                        headless=bool(tongyi_headless),
                                        hosts_start_pos=None,
                                        cancel_check=lambda: bool(st.session_state.get("bulkpro_tongyi_cancel")),
                                        max_wait_sec=int(tongyi_timeout_s),
                                        size_text=str(tongyi_size_text or TONGYI_DEFAULT_SIZE_TEXT),
                                        progress_callback=None,
                                        log_callback=_tongyi_log,
                                    )
                                    r0 = (res_list or [{}])[0] if res_list else {}
                                    pth = str(r0.get("path") or "")
                                    if pth:
                                        final_pth = _move_image_to_pro_named_file(
                                            src_path=pth,
                                            out_dir=str(t.out_dir),
                                            task_idx=int(t.task_idx),
                                            prompt_raw=prompt_clean,
                                        )
                                        try:
                                            _tongyi_log(f"[rename] {os.path.basename(pth)} -> {os.path.basename(str(final_pth))}")
                                        except Exception:
                                            pass
                                        st.session_state.bulkpro_saved_paths.append(str(final_pth))
                                        st.session_state.bulkpro_saved_paths = list(dict.fromkeys(st.session_state.bulkpro_saved_paths))
                                        st.success(f"Saved: {os.path.basename(str(final_pth))}")
                                    else:
                                        st.session_state.bulkpro_errors.append({"task_idx": int(t.task_idx), "error": str(r0.get("error") or "Unknown")})
                                        st.error(str(r0.get("error") or "Unknown"))
                                except Exception as e:
                                    st.session_state.bulkpro_errors.append({"task_idx": int(t.task_idx), "error": str(e)})
                                    st.error(str(e))

            # Watcher status (non-blocking)
            try:
                st0 = (st.session_state.get("bulkpro_dl_watch_status") or {}).get(str(t.task_idx))
                if st0:
                    okv = st0.get("ok")
                    if okv is True:
                        st.caption(f"Download renamed: {Path(str(st0.get('to') or '')).name}")
                    elif okv is False:
                        st.caption(f"DL watcher: {st0.get('error')}")
                    else:
                        # ok=None means waiting
                        st.caption("DL watcher: waiting for download...")
            except Exception:
                pass

            st.markdown("---")

    def _dedupe_bytes(items):
        out = []
        seen = set()
        for mime, blob in items or []:
            try:
                h = hashlib.sha256(blob).hexdigest()
            except Exception:
                h = None
            if h and h in seen:
                continue
            if h:
                seen.add(h)
            out.append((mime, blob))
        return out

    def _save_imgs_for_task(task: ProTask, imgs, *, prompt_raw: str | None = None):
        import re as _re

        prompt_raw = (prompt_raw or task.prompt or "").strip()
        base_slug = _re.sub(r"[^a-zA-Z0-9_-]+", "_", prompt_raw)
        base_slug = _re.sub(r"_+", "_", base_slug).strip("_")
        if not base_slug:
            base_slug = f"prompt_{task.task_idx}"

        MAX_BASENAME = 110
        prefix = f"{int(task.task_idx)}_pro_"
        prefix_len = len(prefix)
        # suffix _NN.ext
        suffix_len = len("_00.png")
        allowed_slug_len = max(1, MAX_BASENAME - prefix_len - suffix_len)
        slug = base_slug[:allowed_slug_len]

        saved_local = []
        imgs = _dedupe_bytes(imgs)
        for j, (mime, blob) in enumerate(imgs or [], 1):
            ext = "png" if mime == "image/png" else ("jpg" if mime == "image/jpeg" else "bin")
            fname = f"{int(task.task_idx)}_pro_{slug}_{j:02d}.{ext}"
            fpath = os.path.join(task.out_dir, fname)
            os.makedirs(task.out_dir, exist_ok=True)
            with open(fpath, "wb") as f:
                f.write(blob)
            saved_local.append(fpath)
        return saved_local

    def _run_one_pro(task: ProTask, *, prompt_text: str, user_data_dir: str | None, clone_profile: bool, retries_local: int):
        """Worker: run one pro task. No Streamlit calls here."""
        local = {"task_idx": task.task_idx, "saved": [], "errors": []}
        tmp_profile_dir = None
        effective_udir = _normalize_user_data_dir(user_data_dir) if user_data_dir else None

        try:
            if effective_udir and clone_profile:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                tmp_profile_dir = str(Path(f"tmp_rovodev_bulkpro_profile_{ts}_{task.task_idx}").resolve())
                try:
                    _clone_profile_dir(effective_udir, tmp_profile_dir)
                    effective_udir = tmp_profile_dir
                except Exception:
                    tmp_profile_dir = None

            if effective_udir:
                try:
                    os.makedirs(effective_udir, exist_ok=True)
                except Exception:
                    pass

            with sync_playwright() as pw:
                ctx = _launch_persistent_ctx_with_retries(
                    pw,
                    user_data_dir=effective_udir,
                    headless=bool(nbp_headless),
                    executable_path=nbp_executable_path,
                    attempts=6,
                )
                try:
                    page = ctx.new_page()
                    page.set_default_timeout(30000)
                    try:
                        page.goto(nbp_url, wait_until="load")
                    except Exception:
                        pass

                    _wait_input_ready(page, timeout_ms=60000)
                    try:
                        _start_new_chat(page)
                    except Exception:
                        pass

                    _dismiss_overlays(page)
                    try:
                        gph._pick_model(page, "Nano Banana Pro")
                    except Exception as e:
                        local["errors"].append(f"Model pick failed: {e}")

                    final_prompt = f"{NBP_PROMPT_PRE}{_sanitize_prompt(prompt_text)}{NBP_PROMPT_POST}"

                    max_attempts = 1 + int(retries_local or 0)
                    last_exc = None
                    imgs = []
                    for attempt in range(1, max_attempts + 1):
                        try:
                            if attempt > 1:
                                time.sleep(1.2 * (2 ** (attempt - 2)) + random.random())
                                try:
                                    page.reload(wait_until="load")
                                except Exception:
                                    pass
                                try:
                                    _wait_input_ready(page, timeout_ms=60000)
                                except Exception:
                                    pass
                                try:
                                    _start_new_chat(page)
                                except Exception:
                                    pass
                                _dismiss_overlays(page)
                                try:
                                    gph._pick_model(page, "Nano Banana Pro")
                                except Exception:
                                    pass

                            _type_prompt(page, final_prompt)
                            _click_send(page)
                            time.sleep(0.25 + random.random() * 0.75)

                            imgs = _filter_imgs_min_bytes(
                                _wait_and_download_generated_images(
                                    page,
                                    ctx,
                                    timeout_s=170,
                                    max_images=6,
                                    allow_screenshot_fallback=False,
                                )
                            )

                            if imgs:
                                break
                            last_exc = RuntimeError("Empty result (no images)")
                        except Exception as e:
                            last_exc = e
                            continue

                    if not imgs:
                        raise last_exc or RuntimeError("No images")

                    saved_local = _save_imgs_for_task(task, imgs, prompt_raw=prompt_text)
                    local["saved"] = saved_local
                finally:
                    try:
                        ctx.close()
                    except Exception:
                        pass
        except Exception as e:
            local["errors"].append(str(e))
        finally:
            if tmp_profile_dir:
                try:
                    shutil.rmtree(tmp_profile_dir, ignore_errors=True)
                except Exception:
                    pass
        return local

    run_label = "Сгенерировать картинки (Nano Banana Pro / bulk)" if str(pro_engine) != "Tongyi (HF Space)" else "Сгенерировать картинки (Tongyi / bulk)"
    nbp_run = st.button(run_label, type="primary", key="bulkpro_run")
    if nbp_run:
        # Snapshot prompts + per-task profiles from session_state
        tasks_snapshot = []
        for t in tasks:
            pval = (st.session_state.get(f"bulkpro_prompt_{t.article_idx}_{t.prompt_idx}") or str(t.prompt) or "").strip()
            # For Tongyi we must use clean prompt only (no wrapper)
            if str(pro_engine) == "Tongyi (HF Space)":
                pval = _sanitize_prompt(pval or "").strip()
            ud = (st.session_state.get(f"bulkpro_ud_{t.article_idx}_{t.prompt_idx}") or "").strip() or None
            tasks_snapshot.append({"task": t, "prompt": pval, "user_data_dir": _normalize_user_data_dir(ud) if ud else None})

        tasks_snapshot = [x for x in tasks_snapshot if x["prompt"]]
        if not tasks_snapshot:
            st.warning("Нет заполненных промптов")
            return

        st.session_state.bulkpro_errors = []

        # ---------------- Tongyi (prompts-only) ----------------
        if str(pro_engine) == "Tongyi (HF Space)":
            if bool(st.session_state.get("bulkpro_tongyi_running")):
                st.warning("Tongyi is already running. Stop the current run first (🛑 STOP Tongyi) and wait until it fully stops.")
                return
            st.session_state["bulkpro_tongyi_running"] = True

            if tongyi_generate_batch_detailed is None:
                err = _TONGYI_IMPORT_ERROR or "Unknown import error"
                st.error("Tongyi generator не доступен (не удалось импортировать generate_tongyi_images.py).")
                st.code(err)
                st.info("Обычно причина: не установлены зависимости (playwright/requests) или не установлен браузер Playwright.")
                st.session_state["bulkpro_tongyi_running"] = False
                return

            # Start from N-th prompt in the flattened list
            try:
                start_from = max(1, int(tongyi_start_from or 1))
            except Exception:
                start_from = 1
            if start_from > 1:
                tasks_snapshot = tasks_snapshot[(start_from - 1) :]
                if not tasks_snapshot:
                    st.warning("Стартовый индекс больше количества промптов — нечего генерировать")
                    return

            # Reset cancel flag on new run
            st.session_state["bulkpro_tongyi_cancel"] = False

            # group tasks by out_dir (article folder)
            grouped: dict[str, list[dict[str, Any]]] = {}
            for it in tasks_snapshot:
                task = it["task"]
                out_dir = str(getattr(task, "out_dir", "") or "").strip() or str(run_base_dir)
                grouped.setdefault(out_dir, []).append({"task": task, "prompt": it["prompt"]})

            total = sum(len(v) for v in grouped.values())
            progress = st.progress(0)
            done = 0

            with st.status("Запуск Tongyi (sequential)...", expanded=True) as status:
                for out_dir, items in grouped.items():
                    prompts = [x["prompt"] for x in items]

                    def _cb(i, n, prompt, item):
                        nonlocal done
                        done += 1
                        progress.progress(int(done / max(1, total) * 100))
                        # show short status line
                        try:
                            status.write(f"{done}/{total}: {str(prompt)[:80]}")
                        except Exception:
                            pass

                    try:
                        def _on_saved(ii: int, _tt: int, saved_path: str) -> str:
                            # ii is 1-based index within this group
                            idx0 = max(0, int(ii) - 1)
                            if idx0 >= len(items):
                                return saved_path
                            task0 = items[idx0].get("task")
                            prompt0 = str(items[idx0].get("prompt") or "")
                            final_p = _move_image_to_pro_named_file(
                                src_path=str(saved_path),
                                out_dir=str(getattr(task0, "out_dir", out_dir) or out_dir),
                                task_idx=int(getattr(task0, "task_idx", 0) or 0),
                                prompt_raw=prompt0,
                            )
                            try:
                                _tongyi_log(f"[rename] {os.path.basename(str(saved_path))} -> {os.path.basename(str(final_p))}")
                            except Exception:
                                pass
                            return str(final_p)

                        results = tongyi_generate_batch_detailed(
                            space_url=str(tongyi_space_url or TONGYI_DEFAULT_SPACE_URL),
                            prompts=list(prompts),
                            out_dir=str(out_dir),
                            headless=bool(tongyi_headless),
                            hosts_start_pos=None,
                            cancel_check=lambda: bool(st.session_state.get("bulkpro_tongyi_cancel")),
                            max_wait_sec=int(tongyi_timeout_s),
                            size_text=str(tongyi_size_text or TONGYI_DEFAULT_SIZE_TEXT),
                            progress_callback=_cb,
                            log_callback=_tongyi_log,
                            on_image_saved=_on_saved,
                        )
                    except Exception as e:
                        import traceback

                        msg = str(e)
                        # If cancelled, stop whole run without marking as errors
                        if "Отменено" in msg or "Cancelled" in msg or "Canceled" in msg:
                            st.warning("Tongyi: остановлено")
                            return

                        st.error(f"Tongyi error for out_dir={out_dir}")
                        st.code(msg)
                        st.code(traceback.format_exc())

                        # mark all as failed
                        for x in items:
                            st.session_state.bulkpro_errors.append({"task_idx": int(x["task"].task_idx), "error": msg})
                        continue

                    for idx, r in enumerate(results or []):
                        task = items[idx]["task"] if idx < len(items) else None
                        if r.get("path") and task is not None:
                            src = str(r.get("path") or "")
                            prompt_raw = str(items[idx].get("prompt") or "") if idx < len(items) else ""
                            final_p = _move_image_to_pro_named_file(
                                src_path=src,
                                out_dir=str(getattr(task, "out_dir", out_dir) or out_dir),
                                task_idx=int(getattr(task, "task_idx", 0) or 0),
                                prompt_raw=prompt_raw,
                            )
                            try:
                                _tongyi_log(f"[rename] {os.path.basename(src)} -> {os.path.basename(str(final_p))}")
                            except Exception:
                                pass
                            st.session_state.bulkpro_saved_paths.append(str(final_p))
                        else:
                            st.session_state.bulkpro_errors.append({"task_idx": int(task.task_idx) if task else None, "error": str(r.get("error") or "Unknown")})

                st.session_state.bulkpro_saved_paths = list(dict.fromkeys(st.session_state.bulkpro_saved_paths))

            # finish message
            if st.session_state.get("bulkpro_errors"):
                st.error(f"Ошибок: {len(st.session_state.bulkpro_errors)}")
                with st.expander("Ошибки Pro", expanded=False):
                    st.json(st.session_state.bulkpro_errors)
            else:
                st.success("Готово")
            st.session_state["bulkpro_tongyi_running"] = False
            return

        # ---------------- Nano Banana Pro (legacy) ----------------

        # Keep-open mode: sequential, keep references
        if keep_windows_open:
            st.warning("Параллельный режим отключён, потому что включено 'Оставить окна Chrome открытыми'.")

            st.session_state.setdefault("bulkpro_keepalive", [])
            used_udirs = set()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")

            with st.status("Запуск Nano Banana Pro (keep-open, sequential)...", expanded=True) as status:
                p = sync_playwright().start()
                st.session_state.bulkpro_keepalive.append(p)

                for idx, item in enumerate(tasks_snapshot, 1):
                    task = item["task"]
                    prompt_raw = item["prompt"]
                    udir = item.get("user_data_dir")

                    # Prevent using same profile twice in keep-open mode
                    effective_udir = udir
                    cloned_dir = None
                    try:
                        if effective_udir:
                            key = os.path.normcase(os.path.normpath(effective_udir))
                            if key in used_udirs:
                                cloned_dir = str(Path(f"tmp_rovodev_bulkpro_keepopen_{ts}_{idx}").resolve())
                                try:
                                    _clone_profile_dir(effective_udir, cloned_dir)
                                    effective_udir = cloned_dir
                                except Exception:
                                    effective_udir = udir
                            used_udirs.add(os.path.normcase(os.path.normpath(effective_udir or "")))
                    except Exception:
                        pass

                    status.write(f"Task {task.task_idx}: profile={effective_udir or '(none)'}")

                    try:
                        ctx = _launch_persistent_ctx_with_retries(
                            p,
                            user_data_dir=effective_udir,
                            headless=bool(nbp_headless),
                            executable_path=nbp_executable_path,
                            attempts=6,
                        )
                        st.session_state.bulkpro_keepalive.append(ctx)
                        if cloned_dir:
                            st.session_state.bulkpro_keepalive.append({"tmp_profile_dir": cloned_dir})

                        page = ctx.new_page()
                        page.set_default_timeout(30000)
                        page.goto(nbp_url, wait_until="load")
                        _wait_input_ready(page, timeout_ms=60000)
                        try:
                            _start_new_chat(page)
                        except Exception:
                            pass
                        _dismiss_overlays(page)
                        try:
                            gph._pick_model(page, "Nano Banana Pro")
                        except Exception:
                            pass

                        final_prompt = f"{NBP_PROMPT_PRE}{_sanitize_prompt(prompt_raw)}{NBP_PROMPT_POST}"
                        _type_prompt(page, final_prompt)
                        _click_send(page)
                        time.sleep(0.25 + random.random() * 0.75)
                        imgs = _filter_imgs_min_bytes(
                            _wait_and_download_generated_images(page, ctx, timeout_s=170, max_images=6, allow_screenshot_fallback=False)
                        )

                        if imgs:
                            saved_local = _save_imgs_for_task(task, imgs, prompt_raw=prompt_raw)
                            st.session_state.bulkpro_saved_paths.extend(saved_local)
                            st.session_state.bulkpro_saved_paths = list(dict.fromkeys(st.session_state.bulkpro_saved_paths))
                            status.write(f"Task {task.task_idx}: saved {len(saved_local)}")
                        else:
                            st.session_state.bulkpro_errors.append({"task_idx": task.task_idx, "error": "No images"})
                    except Exception as e:
                        st.session_state.bulkpro_errors.append({"task_idx": task.task_idx, "error": str(e)})

        else:
            import concurrent.futures

            max_workers = max(1, min(12, int(parallelism), len(tasks_snapshot)))

            # Detect duplicated profiles
            ud_norm = []
            for it in tasks_snapshot:
                v = it.get("user_data_dir")
                try:
                    v = os.path.normcase(os.path.normpath(v)) if v else None
                except Exception:
                    v = None
                ud_norm.append(v)
            dup_udirs = {v for v in ud_norm if v and ud_norm.count(v) > 1}

            progress = st.progress(0)
            done = 0
            total = len(tasks_snapshot)

            with st.status("Запуск Nano Banana Pro (parallel)...", expanded=True) as status:
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                    futs = []
                    for item in tasks_snapshot:
                        task = item["task"]
                        prompt_raw = item["prompt"]
                        udir = item.get("user_data_dir")
                        udir_key = os.path.normcase(os.path.normpath(udir)) if udir else None
                        need_clone = bool(udir_key and udir_key in dup_udirs)
                        futs.append(
                            ex.submit(
                                _run_one_pro,
                                task,
                                prompt_text=prompt_raw,
                                user_data_dir=udir,
                                clone_profile=need_clone,
                                retries_local=int(retries),
                            )
                        )

                    for fut in concurrent.futures.as_completed(futs):
                        r = fut.result() or {}
                        done += 1
                        saved_local = list(r.get("saved") or [])
                        errs_local = list(r.get("errors") or [])
                        if saved_local:
                            st.session_state.bulkpro_saved_paths.extend(saved_local)
                            st.session_state.bulkpro_saved_paths = list(dict.fromkeys(st.session_state.bulkpro_saved_paths))
                        if errs_local:
                            st.session_state.bulkpro_errors.append({"task_idx": r.get("task_idx"), "errors": errs_local})
                        progress.progress(int(done / max(1, total) * 100))
                        status.write(f"{done}/{total} done")

        if st.session_state.get("bulkpro_errors"):
            st.error(f"Ошибок: {len(st.session_state.bulkpro_errors)}")
            with st.expander("Ошибки Pro", expanded=False):
                st.json(st.session_state.bulkpro_errors)
        else:
            st.success("Готово")


def main() -> None:
    st.set_page_config(page_title="Bulk Gemini Generator", layout="wide")
    st.title("Bulk Gemini Image Generator")

    tab_fast, tab_pro = st.tabs([
        "1) Fast (bulk)",
        "2) Pro (Nano Banana Pro / bulk)",
    ])

    with tab_fast:
        _render_fast_tab()

    with tab_pro:
        _render_pro_tab()


if __name__ == "__main__":
    main()
