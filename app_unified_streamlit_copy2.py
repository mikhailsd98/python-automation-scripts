# Unified Streamlit interface: Gemini (Playwright) → Photoshop watermark removal → WebP conversion
# Usage: streamlit run app_unified_streamlit.py

import os
import sys
import subprocess
import shutil
import queue
import random
from datetime import datetime
from pathlib import Path
from typing import List

import streamlit as st
import streamlit.components.v1 as components
import requests


def _remove_watermark_with_photoshop_single(
    input_path: str | Path,
    *,
    size_or_scale: int = 13,
    out_format: str = "PNG",
    mode: str = "scale",
    margin_left: int = 25,
    margin_bottom: int = 25,
    force: bool = False,
    unique: bool = False,
) -> tuple[Path | None, str | None]:
    """Remove Gemini watermark using Photoshop automation for a single image.

    This reuses the same automation as Tab 3 (photoshop_crop_bottom_right.py).

    Returns (filled_path, error).
    """

    try:
        src = Path(input_path).expanduser().resolve()
        if not src.exists():
            return None, f"Файл не найден: {src}"

        # Photoshop script writes next to source and appends `_filled`.
        # For PNG output, it becomes `<stem>_filled.png`.
        ext = ".png" if out_format.upper() == "PNG" else ".jpg"
        filled_path = src.with_name(f"{src.stem}_filled{ext}")

        # Default behavior: reuse cached result if it exists.
        # In Tab1 we want to regenerate after "пересоздать", so we'll pass force/unique.
        if filled_path.exists() and not force and not unique:
            return filled_path, None

        # If forcing regeneration, remove old cached filled file so Photoshop creates a fresh one.
        if filled_path.exists() and (force or unique):
            try:
                filled_path.unlink(missing_ok=True)  # py3.8+: supported
            except TypeError:
                # Python <3.8 compatibility
                try:
                    if filled_path.exists():
                        filled_path.unlink()
                except Exception:
                    pass

        cmd = [
            sys.executable,
            "photoshop_crop_bottom_right.py",
            str(src),
            str(int(size_or_scale)),
            out_format.upper(),
            mode,
            str(int(margin_left)),
            str(int(margin_bottom)),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return None, f"Photoshop error (code {proc.returncode}): {proc.stderr or proc.stdout}"

        created_path: Path | None = None

        if filled_path.exists():
            created_path = filled_path
        else:
            # Fallback: try to find any *_filled.* created next to src
            candidates = sorted(
                [p for p in src.parent.glob(f"{src.stem}_filled.*") if p.suffix.lower() in (".png", ".jpg", ".jpeg")],
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )
            if candidates:
                created_path = candidates[0]

        if not created_path or not created_path.exists():
            return None, "Не найден файл результата Photoshop (*_filled.*)"

        # If requested, move the filled file to a unique name to avoid reusing old cached outputs.
        if unique:
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                unique_path = created_path.with_name(f"{src.stem}_filled_{ts}{created_path.suffix}")
                # In case of extremely fast repeats, ensure uniqueness
                if unique_path.exists():
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    unique_path = created_path.with_name(f"{src.stem}_filled_{ts}{created_path.suffix}")
                created_path.replace(unique_path)
                created_path = unique_path
            except Exception:
                # If rename fails, still return the created file
                pass

        return created_path, None
    except Exception as e:
        return None, str(e)

# Helper: open folder in OS file explorer
def _clear_article_ui_cache(idx: int | str) -> None:
    """Clear Streamlit widget state for a single article result.

    В Streamlit значения виджетов живут в `st.session_state` по их `key`.
    Если результат статьи (текст/preview/download) обновился, а ключи остались
    прежними, Streamlit может показать старое значение из session_state.

    Мы храним ключи в формате `..._{idx}_{rev}` (rev = версия результата).
    Для надёжности при обновлениях удаляем *все* ключи, относящиеся к данному idx.
    """

    sidx = str(idx)

    # Удаляем все варианты ключей для этого idx (включая старые rev).
    prefixes = [
        f"unif_text_out_{sidx}",
        f"unif_wp_clean_preview_{sidx}",
        f"unif_wp_dl_{sidx}",
    ]

    for k in list(st.session_state.keys()):
        try:
            if not isinstance(k, str):
                continue
            for p in prefixes:
                # match exact key or key with version suffix
                if k == p or k.startswith(p + "_"):
                    del st.session_state[k]
                    break
        except Exception:
            pass


def _bump_article_rev(idx: int) -> int:
    """Increment and return the revision number for an article idx."""

    if "unif_text_rev_by_idx" not in st.session_state or not isinstance(st.session_state.unif_text_rev_by_idx, dict):
        st.session_state.unif_text_rev_by_idx = {}

    cur = int(st.session_state.unif_text_rev_by_idx.get(str(idx), 0) or 0)
    cur += 1
    st.session_state.unif_text_rev_by_idx[str(idx)] = cur
    return cur


def _get_article_rev(idx: int) -> int:
    if "unif_text_rev_by_idx" not in st.session_state or not isinstance(st.session_state.unif_text_rev_by_idx, dict):
        return 0
    return int(st.session_state.unif_text_rev_by_idx.get(str(idx), 0) or 0)



# Helper: open folder in OS file explorer
def _open_folder(path_str: str | None):
    if not path_str:
        return
    try:
        p = str(Path(path_str).expanduser())
        if os.name == 'nt':
            os.startfile(p)  # type: ignore[attr-defined]
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', p])
        else:
            subprocess.Popen(['xdg-open', p])
    except Exception:
        pass


def _fix_common_profile_path_typos(path_str: str) -> str:
    """Fix common copy/paste typos for Chrome profile paths.

    One frequent issue: missing path separator before `.chrome_automation_profile*`,
    resulting in paths like `...\\generate automation.chrome_automation_profile_4`.

    We only apply a conservative fix when `.chrome_automation_profile` occurs in the
    string and the preceding character is not a path separator.
    """
    try:
        s = (path_str or "").strip().strip('"').strip("'")

        # If the provided directory already exists, DO NOT "fix" it.
        # Users may intentionally have a folder name like `generate automation.chrome_automation_profile_4`.
        try:
            if s and Path(os.path.expanduser(s)).exists():
                return s
        except Exception:
            pass

        # Otherwise, try to fix a common typo: missing separator before (.)chrome_automation_profile
        keys = [".chrome_automation_profile", "chrome_automation_profile"]
        for key in keys:
            i = s.find(key)
            if i > 0:
                prev = s[i - 1]
                if prev not in {"/", "\\", os.sep}:
                    sep = "\\" if os.name == "nt" else os.sep
                    candidate = s[:i] + sep + s[i:]
                    # Only apply if the fixed path exists OR the original doesn't exist
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
    """Common Chrome args for Playwright persistent contexts.

    Adds a few stability flags for Windows GPU/Window-handle flakiness.
    """
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
    extra_args: list[str] | None = None,
    attempts: int = 6,
):
    """Launch persistent context with retries for intermittent Windows flakiness.

    - Normalizes user-data-dir (fixes missing path separator before chrome profiles).
    - Creates the dir if needed.
    - Retries several times to mitigate `Browser.getWindowForTarget`.
    """

    norm_udir = _normalize_user_data_dir(user_data_dir) if user_data_dir else None
    if norm_udir:
        try:
            os.makedirs(norm_udir, exist_ok=True)
        except Exception:
            pass

    last_err: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return pw.chromium.launch_persistent_context(
                user_data_dir=norm_udir,
                headless=headless,
                channel="chrome",
                executable_path=executable_path or None,
                args=_chrome_launch_args(extra_args),
            )
        except Exception as e:
            last_err = e
            # exponential-ish backoff
            time.sleep(0.6 + (0.7 * attempt))
    raise last_err or RuntimeError("Failed to launch persistent context")


def _clone_profile_dir(src_dir: str, dst_dir: str) -> None:
    """Clone a Chrome user-data-dir to a temp directory.

    We skip caches and lock files. This is used to avoid profile locks when running
    multiple windows/contexts concurrently or when keep-open mode needs unique profiles.
    """

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
            # Common lock/port files
            if n.upper() == "LOCK" or n.lower() in {"lockfile", "devtoolsactiveport"}:
                ignored.append(n)
                continue
            if n.lower() in {"service worker", "serviceworker"}:
                ignored.append(n)
                continue
        return ignored

    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=False, ignore=_ignore_profile)


# Local modules
import convert_to_webp  # we will call convert_to_webp.run_streamlit_app() inside a tab
import gemini_pw_helpers as gph

# Use the original helpers from gemini_playwright_streamlit for full parity
from gemini_playwright_streamlit import (
    BASE_IMAGE_PATH,
    _wait_input_ready,
    _start_new_chat,
    _attach_image,
    _wait_image_attached,
    _dismiss_overlays,
    _type_prompt,
    _click_send,
    _wait_and_download_generated_images,
    _debug_dom,
    _count_responses,
    _has_generated_images,
    _regenerate_prompt,
)
from playwright.sync_api import sync_playwright
from concurrent.futures import ThreadPoolExecutor
import threading

# --- Concurrency safety ---
# IMPORTANT: Playwright persistent contexts share the underlying Chrome profile (user-data-dir).
# If two threads accidentally use the same profile simultaneously, they can end up typing/sending
# into the same Gemini window while it is already generating, which triggers Gemini UI errors.
# We therefore enforce a per-profile lock.
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

# Default URLs (same as original UI)
DEFAULT_URLS = ["https://gemini.google.com/app", "https://aistudio.google.com/app"]
import platform, time, urllib.request

# Custom Streamlit component: paste image via Ctrl+V
_paste_image = components.declare_component(
    "paste_image",
    path=str(Path("paste_image_component").resolve()),
)

# Windows asyncio policy tweak (as in original app)
import asyncio
if platform.system() == "Windows":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# Minimal local helpers to avoid importing the full Streamlit app module
from typing import Tuple

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


def _wait_and_collect_images_simple(page, timeout_s: int = 90, max_images: int = 6) -> List[Tuple[str, bytes]]:
    """Simple fallback collector: waits for Gemini image containers, then screenshots visible images.
    Returns list of (mime, bytes) tuples.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _has_generated_images_simple(page):
            break
        time.sleep(0.4)
    # Screenshot strategy
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
            if len(results) >= max_images:
                break
            try:
                ims = last.query_selector_all(psel)
            except Exception:
                ims = []
            for im in ims:
                if len(results) >= max_images:
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


# ---------------- Text-generation helpers (Gemini UI) ----------------


def _try_parse_article_json(text: str) -> dict | None:
    """Parse article JSON from Gemini output.

    Supports raw JSON or fenced blocks like ```json ... ```.
    Returns dict or None.

    Note about newlines:
    - Proper JSON uses real newlines (or \n escape) and json.loads will decode them.
    - Sometimes the model double-escapes and returns literal backslash-n sequences ("\\n").
      We normalize those into real newlines for downstream Gutenberg/HTML rendering.
    """

    def _repair_unescaped_quotes_in_json(s: str) -> str:
        """Best-effort repair for common model JSON mistakes.

        Main target: unescaped double quotes inside JSON string values, e.g.:
          "title": "7 Ways to Master "Quiet Luxury" with ..."

        We keep this conservative: only when we are *inside* a JSON string and
        encounter a non-escaped `"` that does *not* look like the end of a JSON
        string token, we convert it to `\"`.

        This is heuristic, but works well for typical LLM outputs.
        """

        def _next_non_ws(text: str, start: int) -> str | None:
            for j in range(start, len(text)):
                c = text[j]
                if not c.isspace():
                    return c
            return None

        out: list[str] = []
        in_str = False
        esc = False

        for i, ch in enumerate(s or ""):
            if not in_str:
                out.append(ch)
                if ch == '"':
                    in_str = True
                    esc = False
                continue

            # in_str
            if esc:
                out.append(ch)
                esc = False
                continue

            if ch == "\\":
                out.append(ch)
                esc = True
                continue

            if ch == '"':
                nxt = _next_non_ws(s, i + 1)
                # If it looks like a normal string terminator (key or value), keep it.
                if nxt in {":", ",", "}", "]"}:
                    out.append(ch)
                    in_str = False
                    continue

                # Otherwise treat as stray quote inside string value.
                out.append('\\"')
                continue

            out.append(ch)

        return "".join(out)

    def _loads_with_repair(candidate: str):
        import json as _json

        try:
            return _json.loads(candidate)
        except _json.JSONDecodeError:
            fixed = _repair_unescaped_quotes_in_json(candidate)
            return _json.loads(fixed)

    def _deep_normalize_newlines(obj):
        if isinstance(obj, str):
            # Convert literal backslash escapes into real newlines if they slipped through.
            # This is safe for typical article text and prevents Gutenberg from showing "\\n".
            return (
                obj.replace("\\r\\n", "\n")
                .replace("\\n", "\n")
                .replace("\\r", "\n")
            )
        if isinstance(obj, list):
            return [_deep_normalize_newlines(x) for x in obj]
        if isinstance(obj, dict):
            return {k: _deep_normalize_newlines(v) for k, v in obj.items()}
        return obj

    def _canonicalize_article_json_keys(obj):
        """Normalize common key-name deviations from Gemini into a canonical schema.

        Goal: downstream code should reliably find:
        - featured_image
        - excerpt
        - sections[].prompt1 / sections[].prompt2
        - sections[].h2 / sections[].text
        - conclusion_heading

        We keep original keys unless we can confidently map them.
        """
        import re as _re

        def norm(k: str) -> str:
            k = (k or "").strip().lower()
            k = k.replace("-", " ").replace("_", " ")
            k = _re.sub(r"\s+", " ", k).strip()
            return k

        if isinstance(obj, list):
            return [_canonicalize_article_json_keys(x) for x in obj]

        if not isinstance(obj, dict):
            return obj

        out: dict = {}
        for k, v in obj.items():
            nk = norm(k) if isinstance(k, str) else k
            v2 = _canonicalize_article_json_keys(v)

            # Top-level common mappings
            # Gemini sometimes outputs typos like "featurge".
            if nk in (
                "featured image",
                "featuredimage",
                "feature image",
                "featured img",
                "featured prompt",
                "featurge",
                "feauture",
                "feaimage",
                "feautured",
                "featured",
                "feature",
            ):
                out["featured_image"] = v2
                continue
            if nk in ("featured_image",):
                out["featured_image"] = v2
                continue
            if nk in ("excerpt", "summary", "meta description", "meta"):
                out["excerpt"] = v2
                continue
            if nk in ("conclusion heading", "conclusion_heading", "conclusion title"):
                out["conclusion_heading"] = v2
                continue

            # Section-level mappings (will be applied also on nested dicts)
            if nk in ("h2", "heading", "section heading", "section title"):
                out["h2"] = v2
                continue
            if nk in ("text", "body", "content", "section text"):
                out["text"] = v2
                continue

            # prompt1/prompt2 sometimes come as "prompt 1", "vertical image prompt 2", etc.
            if isinstance(nk, str) and "prompt" in nk:
                m = _re.search(r"(\d+)\s*$", nk)
                if m and m.group(1) in ("1", "2"):
                    out[f"prompt{m.group(1)}"] = v2
                    continue

            # Default: keep key as-is
            out[k] = v2

        # If the model used featuredImage camelCase, bring it over
        if "featured_image" not in out:
            fi = obj.get("featuredImage")
            if isinstance(fi, str) and fi.strip():
                out["featured_image"] = fi

        # Defensive normalization: featured_image must describe a wide 2:1 horizontal, monolithic scene.
        fi2 = out.get("featured_image")
        if isinstance(fi2, str) and fi2.strip():
            s = fi2.strip()
            # Remove common wrong orientation cues
            s = _re.sub(r"\bvertical\b", "horizontal", s, flags=_re.IGNORECASE)
            s = _re.sub(r"\bportrait\b", "landscape", s, flags=_re.IGNORECASE)
            # Ensure we explicitly request 2:1 wide banner and prohibit collages/panels.
            must_add = "WIDE HORIZONTAL 2:1 banner, monolithic seamless single scene, no collage/split-screen/panels/frames/borders"
            if "2:1" not in s and "2x1" not in s.lower():
                s = f"{must_add}. {s}"
            elif not _re.search(r"\bhorizontal\b|\blandscape\b|\bwide\b", s, flags=_re.IGNORECASE):
                s = f"{must_add}. {s}"
            out["featured_image"] = s

        return out

    try:
        import json as _json
        import re as _re

        t = (text or "").strip()
        if not t:
            return None

        # Strip fenced code blocks
        m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, flags=_re.DOTALL | _re.IGNORECASE)
        if m:
            cand = m.group(1)
            data = _loads_with_repair(cand)
            if not isinstance(data, dict):
                return None
            data = _deep_normalize_newlines(data)
            data = _canonicalize_article_json_keys(data)
            return data

        # Fallback: find first {...} span
        i = t.find("{")
        j = t.rfind("}")
        if i != -1 and j != -1 and j > i:
            cand = t[i : j + 1]
            data = _loads_with_repair(cand)
            if not isinstance(data, dict):
                return None
            data = _deep_normalize_newlines(data)
            data = _canonicalize_article_json_keys(data)
            return data
        return None
    except Exception:
        return None


def _article_json_to_markdown(data: dict) -> str:
    """Render parsed article JSON to Markdown."""
    title = (data.get("title") or "").strip()
    intro = (data.get("introduction") or "").strip()
    concl_h = (data.get("conclusion_heading") or "Conclusion").strip() or "Conclusion"
    concl_t = (data.get("conclusion") or "").strip()

    md: list[str] = []
    if title:
        md.append(f"# {title}")
        md.append("")
    if intro:
        md.append(intro)
        md.append("")

    sections = data.get("sections") or []
    if isinstance(sections, list):
        for s in sections:
            if not isinstance(s, dict):
                continue
            h2 = (s.get("h2") or s.get("heading") or "").strip()
            body = (s.get("text") or "").strip()
            if h2:
                md.append(f"## {h2}")
            if body:
                md.append(body)
            md.append("")

    if concl_t:
        md.append(f"## {concl_h}")
        md.append(concl_t)
        md.append("")

    return "\n".join(md).strip()


def _article_json_image_prompts(data: dict) -> list[str]:
    """Extract ordered image prompts from structured article JSON.

    IMPORTANT:
    - This function returns ONLY per-section vertical prompts (prompt1/prompt2).
    - It does NOT include `featured_image` because featured images are generated
      via a separate workflow (2x1) and should not be routed into Tab2/Tab3.
    

    Gemini sometimes returns slightly different key names, e.g.:
    - "prompt2" -> "vertical image prompt 2" / "prompt 2" / "Prompt_2"

    We keep strict priority for canonical keys (prompt1/prompt2), but also
    accept common variants to avoid losing prompts.
    """

    def _norm_key(k: str) -> str:
        import re as _re

        k = (k or "").strip().lower()
        k = k.replace("_", " ")
        k = _re.sub(r"\s+", " ", k).strip()
        return k

    def _get_prompt_variant(section: dict, idx: int) -> str:
        # 1) Canonical keys first
        direct_keys = [f"prompt{idx}", f"prompt_{idx}"]
        for dk in direct_keys:
            try:
                v = section.get(dk)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            except Exception:
                pass

        # 2) Variant keys (normalized matching)
        # Examples: "prompt 2", "vertical image prompt 2", "image prompt 2"
        wanted_suffix = str(idx)
        for k, v in (section or {}).items():
            if not isinstance(k, str):
                continue
            nk = _norm_key(k)
            # must mention prompt and end with the right number (avoid grabbing random fields)
            if "prompt" not in nk:
                continue
            if not nk.endswith(wanted_suffix):
                continue
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    prompts: list[str] = []

    sections = data.get("sections") or []
    if isinstance(sections, list):
        for s in sections:
            if not isinstance(s, dict):
                continue
            p1 = _get_prompt_variant(s, 1)
            p2 = _get_prompt_variant(s, 2)
            if p1:
                prompts.append(p1)
            if p2:
                prompts.append(p2)

    # de-dup (preserve order)
    seen: set[str] = set()
    out: list[str] = []
    for p in prompts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _clean_article_for_gutenberg(text: str) -> str:
    """Clean generated output for pasting into WordPress Gutenberg.

    Removes service lines like:
    - Excerpt: ...
    - Featured Image: ...
    - Prompt 1/2: ...
    - Amazon Search Phrases: ...

    Important: Gemini sometimes outputs H2 headings as plain standalone lines
    (without '##' or 'H2:'). We apply heuristics to detect such lines and
    convert them to Markdown H2 so Gutenberg can preserve structure.
    """
    import re as _re

    t = (text or "").splitlines()
    out: list[str] = []

    drop_prefixes = (
        "excerpt:",
        "featured image:",
        "prompt 1:",
        "prompt 2:",
        "amazon search phrases:",
    )

    for ln in t:
        s = ln.strip()
        if not s:
            out.append("")
            continue

        low = s.lower()
        if any(low.startswith(p) for p in drop_prefixes):
            continue

        # Normalize headings to Markdown so Gutenberg keeps structure on paste.
        m2 = _re.match(r"^h2\s*[:\-]?\s*(.+)$", s, flags=_re.IGNORECASE)
        if m2:
            out.append("## " + m2.group(1).strip())
            continue
        m3 = _re.match(r"^h3\s*[:\-]?\s*(.+)$", s, flags=_re.IGNORECASE)
        if m3:
            out.append("### " + m3.group(1).strip())
            continue

        # Heuristic: Gemini sometimes outputs section headings as numbered lines like "1. Some Heading".
        # Convert such short numbered lines into H2 to avoid Gutenberg turning them into ordered lists.
        mnum = _re.match(r"^(\d+)[\.)]\s+(.+)$", s)
        if mnum:
            candidate = mnum.group(2).strip()
            if len(candidate) <= 140 and candidate.count(".") <= 1:
                out.append("## " + candidate)
                continue

        out.append(ln)

    # Remove excessive blank lines
    cleaned: list[str] = []
    prev_blank = False
    for ln in out:
        is_blank = (ln.strip() == "")
        if is_blank and prev_blank:
            continue
        cleaned.append(ln.rstrip())
        prev_blank = is_blank

    # Ensure first non-empty line is a Markdown H1
    for i, ln in enumerate(cleaned):
        if ln.strip():
            if not ln.lstrip().startswith("#"):
                cleaned[i] = "# " + ln.strip()
            break

    # No heuristic heading detection here: we rely on explicit structure (prefer JSON output).

    return "\n".join(cleaned).strip()


def _article_markdownish_to_gutenberg_blocks(md: str) -> str:
    """Convert markdown-ish text to Gutenberg block markup (most reliable).

    Output is HTML with Gutenberg comments, suitable for pasting into WordPress Code Editor.
    """
    import html as _html
    import re as _re

    def esc(s: str) -> str:
        return _html.escape(s, quote=False)

    def inline(s: str) -> str:
        s = esc(s)
        s = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        return s

    lines = (md or "").splitlines()
    out: list[str] = []

    ul_items: list[str] = []
    ol_items: list[str] = []

    def flush_lists():
        nonlocal ul_items, ol_items
        if ul_items:
            li = "\n".join([f"<li>{inline(x)}</li>" for x in ul_items])
            out.append("<!-- wp:list --><ul>\n" + li + "\n</ul><!-- /wp:list -->")
            ul_items = []
        if ol_items:
            li = "\n".join([f"<li>{inline(x)}</li>" for x in ol_items])
            out.append("<!-- wp:list {\"ordered\":true} --><ol>\n" + li + "\n</ol><!-- /wp:list -->")
            ol_items = []

    for raw in lines:
        st = raw.strip()
        if not st:
            flush_lists()
            continue

        if st.startswith("### "):
            flush_lists()
            h = inline(st[4:])
            out.append(f"<!-- wp:heading {{\"level\":3}} --><h3>{h}</h3><!-- /wp:heading -->")
            continue
        if st.startswith("## "):
            flush_lists()
            h = inline(st[3:])
            out.append(f"<!-- wp:heading {{\"level\":2}} --><h2>{h}</h2><!-- /wp:heading -->")
            continue
        if st.startswith("# "):
            flush_lists()
            h = inline(st[2:])
            out.append(f"<!-- wp:heading {{\"level\":1}} --><h1>{h}</h1><!-- /wp:heading -->")
            continue

        m_ol = _re.match(r"^(\d+)\.\s+(.*)$", st)
        if m_ol:
            if ul_items:
                flush_lists()
            ol_items.append(m_ol.group(2).strip())
            continue
        if st.startswith("- ") or st.startswith("* "):
            if ol_items:
                flush_lists()
            ul_items.append(st[2:].strip())
            continue

        flush_lists()
        p = inline(st)
        out.append(f"<!-- wp:paragraph --><p>{p}</p><!-- /wp:paragraph -->")

    flush_lists()
    return "\n\n".join(out).strip()


def _article_markdownish_to_html(md: str) -> str:
    """Convert a simple markdown-ish text into HTML suitable for Gutenberg paste.

    Supports:
    - # / ## / ### headings
    - unordered lists starting with - or *
    - ordered lists like 1. item
    - **bold**

    This is intentionally simple and defensive.
    """
    import html as _html
    import re as _re

    lines = (md or "").splitlines()

    def esc(s: str) -> str:
        return _html.escape(s, quote=False)

    def inline(s: str) -> str:
        # bold **text**
        s = esc(s)
        s = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        return s

    out: list[str] = []
    in_ul = False
    in_ol = False

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    for raw in lines:
        s = raw.rstrip()
        st = s.strip()

        if not st:
            close_lists()
            continue

        if st.startswith("### "):
            close_lists()
            out.append(f"<h3>{inline(st[4:])}</h3>")
            continue
        if st.startswith("## "):
            close_lists()
            out.append(f"<h2>{inline(st[3:])}</h2>")
            continue
        if st.startswith("# "):
            close_lists()
            out.append(f"<h1>{inline(st[2:])}</h1>")
            continue

        m_ol = _re.match(r"^(\d+)\.\s+(.*)$", st)
        if m_ol:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{inline(m_ol.group(2))}</li>")
            continue

        if st.startswith("- ") or st.startswith("* "):
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline(st[2:])}</li>")
            continue

        # paragraph
        close_lists()
        out.append(f"<p>{inline(st)}</p>")

    close_lists()
    return "\n".join(out).strip()


def _extract_image_prompts_from_article(text: str) -> list[str]:


    """Extract image prompts (featured + Prompt 1/2...) from generated article text.

    Expected lines:
    - Featured Image: [...]
    - Prompt 1: [...]
    - Prompt 2: [...]

    Returns prompts in order: featured first (if present), then all Prompt 1/2 occurrences.
    """
    import re as _re

    t = text or ""

    def _grab(label: str) -> list[str]:
        out: list[str] = []
        # Prefer bracket form
        pat_br = _re.compile(rf"^{_re.escape(label)}\s*:\s*\[(.*)\]\s*$", flags=_re.IGNORECASE | _re.MULTILINE)
        for m in pat_br.finditer(t):
            v = (m.group(1) or "").strip()
            if v:
                out.append(v)
        # Fallback: no brackets
        pat_nb = _re.compile(rf"^{_re.escape(label)}\s*:\s*(.+?)\s*$", flags=_re.IGNORECASE | _re.MULTILINE)
        for m in pat_nb.finditer(t):
            v = (m.group(1) or "").strip()
            if v and v not in out:
                out.append(v)
        return out

    # NOTE: Featured Image prompt is handled by a separate 2x1 generation flow.
    # Do NOT include it into the Tab2/Tab3 prompt routing list.
    p1 = _grab("Prompt 1")
    p2 = _grab("Prompt 2")

    prompts: list[str] = []

    # Interleave Prompt 1/2 by occurrence order in the text.
    # We will do a single scan to keep order stable.
    items: list[tuple[int, str]] = []
    for lab in ("Prompt 1", "Prompt 2"):
        pat = _re.compile(rf"^{_re.escape(lab)}\s*:\s*(?:\[(.*)\]|(.+))\s*$", flags=_re.IGNORECASE | _re.MULTILINE)
        for m in pat.finditer(t):
            v = (m.group(1) or m.group(2) or "").strip()
            if v:
                items.append((m.start(), v))
    for _, v in sorted(items, key=lambda x: x[0]):
        prompts.append(v)

    # De-duplicate while preserving order
    seen = set()
    uniq: list[str] = []
    for p in prompts:
        key = p.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


PIN_INTRO_INSTRUCTION = (
    "Use the attached reference image as inspiration for the first section of the article that comes after the introduction. "
    "This section should immediately grab the reader's attention with text that is relevant to the context of the reference image."
    "Do NOT mention the reference image, any platform, or where the reader came from. "
    "Make sure the first section’s two vertical image prompts are especially scroll-stopping: "
    "one strong establishing/hero shot and one complementary detail shot, both realistic and coherent with the section’s text."
    "Try to provide more details in your generated prompts to ensure the images look creative rather than like generic stock photos, especially for Prompt1."
)

# When we do NOT attach a per-title image, we still want the first section to feel like a strong "hero" opener
# and we want the first section's images to be especially attention-grabbing.
FIRST_SECTION_HOOK_INSTRUCTION = (
    "For the first image of the first section of the article that comes after the introduction: make it a powerful hook that instantly grabs attention. "
    "Then ensure the first section’s two vertical image prompts are the most scroll-stopping in the whole article: "
    "(1) an establishing/hero shot that shows the full scene (beautiful, high realistic, creative, full interior/lifestyle moment), "
    "with strong photorealistic composition and details), and (2) a complementary detail shot. "
    "Both images must be realistic, cohesive with the section text, and contain no text/logos/watermarks."
    "Try to provide more details in your generated prompts to ensure the images look creative rather than like generic stock photos, especially for Prompt1."
)


def _sanitize_prompt(text: str) -> str:
    """Remove known garbage tails that sometimes get appended to prompts.

    User report: prompt ends with fragments like:
      "from Amazon based on these phrafor being there.\nComparative and Opinion-Ba"

    We keep this very conservative: only remove when we detect these exact markers.
    """

    import re as _re

    s = "" if text is None else str(text)

    # Normalize whitespace a bit so we can match across line breaks.
    s_norm = s

    # If the garbage marker exists, drop everything from it to the end.
    markers = [
        r"\bfrom\s+Amazon\s+based\s+on\s+these\b",
        r"\bComparative\s+and\s+Opinion\-?Ba\b",
        r"\bphrafor\b",
    ]

    # Find earliest marker occurrence (case-insensitive) and truncate.
    cut_at = None
    for m in markers:
        try:
            mm = _re.search(m, s_norm, flags=_re.IGNORECASE)
            if mm:
                cut_at = mm.start() if cut_at is None else min(cut_at, mm.start())
        except Exception:
            continue

    if cut_at is not None and cut_at >= 0:
        s = s[:cut_at].rstrip()

    return s.strip()


def _build_article_prompt_from_title(
    title: str,
    template: str,
    *,
    include_pin_intro_instruction: bool = False,
) -> str:
    """Build the final prompt for article generation.

    We primarily keep the prompt text "as is" and only substitute the article title.

    Supported placeholders:
    - `[article_title]` (preferred, as in the provided prompt)
    - `{title}` (legacy)

    If `include_pin_intro_instruction=True`, we append an extra instruction to the prompt
    (used when we also attach a per-title "pin" image).
    """
    import re as _re

    t = (title or "").strip()
    tpl = (template or "").strip()
    if not tpl:
        tpl = "Write an article titled \"[article_title]\""

    # Safety: if the user ever pasted an old generated prompt back into the template,
    # we don't want the (pin) instruction to leak into text-only runs.
    if not include_pin_intro_instruction and tpl:
        leak_patterns = [
            # Legacy leaked instruction (older versions) / user-pasted instructions.
            # Make matching robust to punctuation, line breaks, and optional Pinterest mentions.
            # We only remove the pin-related instruction sentences/paragraphs.
            # IMPORTANT: do NOT remove whole paragraphs.
            # If the user pasted the pin-block in the middle of a long paragraph, "remove until blank line"
            # can accidentally delete unrelated useful instructions that follow (length limits, image placement, etc.).
            # We therefore remove ONLY the pin/Pinterest-related sentences/segments.
            r"The first section of the article should be dedicated.*?dont need it\.?\s*",
            r"The first section of the article should be dedicated.*?do not generate the images yourself under any circumstances\.?\s*",
            r"The first section of the article should be dedicated[^\n\r]*?pin I gave[^\n\r]*?(?:\.|$)\s*",
            r"The first section of the article should be dedicated[^\n\r]*?\bpin\b[^\n\r]*?(?:\.|$)\s*",
            r"Please\s+dont\s+mention\s+anything\s+about\s+Pinterest.*?(?:\.|$)\s*",
            r"dont\s+mention\s+anything\s+about\s+Pinterest.*?(?:\.|$)\s*",
            # Current pin-mode instruction text (in case user pasted it into the template)
            r"Use the attached reference image as inspiration for the first section of the article that comes after the introduction\..*?coherent with the section(?:'|\u2019)s text\.?\s*",
        ]
        for pat in leak_patterns:
            try:
                tpl = _re.sub(pat, "", tpl, flags=_re.IGNORECASE | _re.DOTALL).strip()
            except Exception:
                pass

    out = tpl.replace("[article_title]", t)
    out = out.replace("{title}", t)

    if include_pin_intro_instruction:
        # Append as a separate paragraph to reduce the chance of breaking user-provided templates.
        out = (out.rstrip() + "\n\n" + PIN_INTRO_INSTRUCTION.strip()).strip()
    else:
        # When there is no attached per-title image, still enforce a strong first-section hook
        # and "hero + detail" image prompts for section 1.
        if FIRST_SECTION_HOOK_INSTRUCTION.strip() not in out:
            out = (out.rstrip() + "\n\n" + FIRST_SECTION_HOOK_INSTRUCTION.strip()).strip()

    return out.strip()


# ---------------- Featured image generation helpers ----------------

FEATURED_IMAGE_BASE_PATH = "2x1.jpg"


def _goto_gemini_resilient(page, url: str, *, timeout_ms: int = 120000, attempts: int = 3) -> str:
    """Navigate to Gemini UI with retries and less brittle waiting.

    Why: `wait_until="load"` often hangs on Gemini (long-polling, service worker, etc.)
    and causes 30s Playwright timeouts. This function prefers `domcontentloaded`,
    uses a bigger navigation timeout, and can fall back from gemini.google.com to
    aistudio.google.com.

    Returns the URL that succeeded.
    """

    urls: list[str] = [url]
    if (url or "").startswith("https://gemini.google.com/"):
        urls.append("https://aistudio.google.com/app")

    last_err: Exception | None = None

    # Navigation timeout is separate from default timeout.
    try:
        page.set_default_navigation_timeout(timeout_ms)
    except Exception:
        pass

    for u in urls:
        for att in range(1, max(1, int(attempts)) + 1):
            try:
                _assert_page_alive(page)
                # domcontentloaded is usually enough for the Gemini UI shell.
                page.goto(u, wait_until="domcontentloaded")

                # Now wait for the input to be usable.
                _wait_input_ready(page, timeout_ms=min(90000, timeout_ms))
                _dismiss_overlays(page)
                _assert_page_alive(page)
                return u
            except Exception as e:
                last_err = e
                # Small backoff; Gemini can be flaky under load.
                time.sleep(0.8 + 0.6 * att)
                continue

    raise last_err or RuntimeError("Failed to open Gemini UI")


def _featured_image_worker(
    *,
    idx: int,
    title: str,
    featured_prompt: str,
    url: str,
    headless: bool,
    executable_path: str | None,
    profile_dir: str,
    model_choice: str,
    timeout_s: int = 120,
    base_dir: str,
) -> dict:
    """Worker: generate a single featured image (2x1 ratio) for an article.
    
    Returns dict with: idx, title, featured_prompt, saved_path, error
    """
    result: dict = {
        "idx": idx,
        "title": title,
        "featured_prompt": featured_prompt,
        "saved_path": None,
        "error": None,
        "url_used": None,
    }
    
    if not featured_prompt or not featured_prompt.strip():
        result["error"] = "Empty featured_prompt"
        return result
    
    # Build the full prompt with 2x1.jpg template.
    # We over-specify constraints because some image models tend to create diptych/split-screen outputs.
    full_prompt = (
        "You will receive a base image named 2x1.jpg with an empty/white area. "
        "Your task: extend/inpaint to fully fill the empty area so the final result is ONE single continuous photography.\n\n"
        f"SCENE PROMPT: {featured_prompt.strip()}\n\n"
        "HARD REQUIREMENTS (must follow exactly):\n"
        "- Keep the final image in 2:1 horizontal ratio (WIDE landscape banner).\n"
        "- Fill ALL empty/white space. No blank areas anywhere.\n"
        "- Output must be monolithic and seamless: ONE continuous scene only (single frame).\n"
        "- ABSOLUTELY NO collage/diptych/triptych/split-screen/panels/frames/borders/multiple images.\n"
        "- ABSOLUTELY NO seams: no vertical divider, no horizontal divider, no center split, no gutter, no different scenes on left/right halves.\n"
        "- Do NOT mirror/duplicate the scene across halves. Do NOT make left/right variations.\n"
        "- Avoid any composition that looks like two separate photos stitched together.\n"
        "- No text, no logos, no watermarks.\n"
        "- Match lighting, perspective, and color across the entire image for a natural seamless look.\n"
        "If you cannot comply with these requirements, regenerate until you can."
    )
    # Please replace the empty space in the 2x1.jpg image provided to you using this Prompt: 
    # A high-resolution, vertical interior photography shot of a sun-drenched French Country bedroom. 
    # The focal point is a vintage sage green wardrobe with a distressed finish. 
    # The background features delicate pink floral wallpaper. Soft linen bedding and a wicker 
    # basket filled with straw hats sit nearby. Cinematic lighting, 8k resolution, hyper-realistic 
    # textures. DO NOT LEAVE BLANK WHITE SPACE, THIS IS IMPORTANT AND KEEP 2x1 RATIO. IMPORTANT: 
    # THE image must be monolithic and seamless, it should not contain two or more separate images 
    # or a collage layout. 

    try:
        with sync_playwright() as p:
            ctx = _launch_persistent_ctx_with_retries(
                p,
                user_data_dir=profile_dir,
                headless=headless,
                executable_path=executable_path,
            )
            try:
                page = ctx.new_page()
                # Default timeout for locators/actions (keep 30s); navigation handled separately.
                page.set_default_timeout(30000)

                # Resilient navigation (Gemini often never reaches full "load")
                url_used = _goto_gemini_resilient(page, url, timeout_ms=120000, attempts=3)
                result["url_used"] = url_used

                # Pick model
                try:
                    gph._pick_model(page, model_choice)
                except Exception:
                    pass
                
                # Start new chat
                try:
                    _start_new_chat(page)
                except Exception:
                    pass
                
                _wait_input_ready(page, timeout_ms=60000)
                _dismiss_overlays(page)
                
                # Attach 2x1.jpg
                if not os.path.exists(FEATURED_IMAGE_BASE_PATH):
                    result["error"] = f"Base image not found: {FEATURED_IMAGE_BASE_PATH}"
                    return result
                
                ok = _attach_image(page, FEATURED_IMAGE_BASE_PATH)
                if not ok:
                    result["error"] = "Failed to attach 2x1.jpg"
                    return result
                
                time.sleep(0.5)
                
                # Type prompt and send
                _type_prompt(page, full_prompt)
                _click_send(page)
                
                # Wait for images
                imgs = _wait_and_download_generated_images(page, ctx, timeout_s=timeout_s, max_images=1)
                if not imgs and _has_generated_images(page):
                    imgs = _wait_and_download_generated_images(page, ctx, timeout_s=20, max_images=1)
                
                if not imgs:
                    result["error"] = "No images generated"
                    return result
                
                # Save the first image
                os.makedirs(base_dir, exist_ok=True)
                
                # Naming: idx_featured_<sanitized_title>.png
                import re as _re
                title_slug = _re.sub(r'[^\w\s-]', '', title.lower())
                title_slug = _re.sub(r'[\s_-]+', '_', title_slug)[:50]
                
                fname = f"{idx}_featured_{title_slug}.png"
                fpath = os.path.join(base_dir, fname)
                
                # Handle both bytes and (mime, bytes) tuple formats
                first_img = imgs[0]
                if isinstance(first_img, tuple):
                    # Format: (mime, bytes)
                    img_data = first_img[1]
                else:
                    # Format: bytes
                    img_data = first_img
                
                with open(fpath, "wb") as f:
                    f.write(img_data)
                
                result["saved_path"] = fpath
                return result
                
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        result["error"] = str(e)
        return result


def _assert_page_alive(page) -> None:
    """Fail fast if page/window was closed.

    `page.is_closed()` is not always enough (depending on how the window was closed),
    so we also do a tiny `evaluate` ping.
    """
    try:
        if page.is_closed():
            raise RuntimeError("Page closed")
    except Exception:
        # If Playwright throws here, treat as closed.
        raise RuntimeError("Page closed")

    try:
        page.evaluate("1")
    except Exception as e:
        if "closed" in str(e).lower() or "target" in str(e).lower():
            raise RuntimeError("Page closed")


def _wait_for_new_text_response(page, prev_count: int, timeout_s: int = 120) -> bool:
    """Wait until Gemini produces a new response container.

    NOTE: For text generation this can happen very early (e.g. an empty container / "thinking" state),
    so this function is only a *first* stage. Final readiness is determined by
    `_wait_for_text_generation_to_finish`.
    """
    deadline = time.time() + max(5, int(timeout_s))
    while time.time() < deadline:
        try:
            _assert_page_alive(page)
            if _count_responses(page) > int(prev_count or 0):
                return True
        except Exception as e:
            # If user closed the window manually, fail fast so caller can retry.
            if "closed" in str(e).lower():
                raise
        time.sleep(0.4)
    return False


def _wait_for_text_generation_to_finish(page, timeout_s: int = 180) -> bool:
    """Wait until Gemini finishes generating text.

    Primary signal: the Stop button disappears.
    Your UI has `aria-label="Остановить генерацию ответа"` while generation is in progress.

    Fallback signal: last response text stops changing for a short stability window.
    """
    timeout_s = max(10, int(timeout_s))
    deadline = time.time() + timeout_s

    stop_locators = [
        'button.send-button.stop[aria-label="Остановить генерацию ответа"]',
        'button[aria-label="Остановить генерацию ответа"]',
        'button.send-button.stop[aria-label="Stop generating response"]',
        'button[aria-label="Stop generating response"]',
    ]

    def _stop_visible() -> bool:
        for sel in stop_locators:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    # Fast path: if stop is visible, wait until it disappears.
    # If we never see Stop at all (some UIs / fast generations), don't wait the full timeout,
    # go to text-stability fallback quickly.
    saw_stop = False
    observe_stop_until = time.time() + 3.0

    while time.time() < deadline:
        try:
            _assert_page_alive(page)

            if _stop_visible():
                saw_stop = True
                time.sleep(0.35)
                continue

            if saw_stop:
                # Stop button is gone => generation likely finished
                time.sleep(0.8)
                return True

            if time.time() > observe_stop_until:
                break
        except Exception as e:
            if "closed" in str(e).lower():
                raise
        time.sleep(0.35)

    # Fallback: stability check (text stops changing)
    stable_for_s = 2.5
    last_txt = None
    last_change = time.time()
    deadline2 = time.time() + 30
    while time.time() < deadline2:
        try:
            _assert_page_alive(page)
            txt = _extract_last_response_text(page)
        except Exception as e:
            if "closed" in str(e).lower():
                raise
            txt = ""
        if txt != last_txt:
            last_txt = txt
            last_change = time.time()
        else:
            if (time.time() - last_change) >= stable_for_s and (txt or "").strip():
                return True
        time.sleep(0.4)

    return False


def _extract_last_response_text(page) -> str:
    """Best-effort extraction of the last assistant response text from Gemini UI."""
    # Try a "last response container" and take its visible text.
    # Priority order: most specific selectors first to avoid capturing UI elements.
    selectors = [
        "message-content",  # Most specific: actual message content
        ".message-content",
        ".model-response-text",
        ".response-content",
        ".presented-response-container",
        ".response-container-content",
        "structured-content-container",
        ".response-container",
        "model-response",
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel).last
            if not loc or loc.count() == 0:
                continue
            txt = (loc.inner_text() or "").strip()
            # Filter out UI noise: if text starts with typical UI elements, skip it
            if txt and len(txt) > 200 and not _looks_like_ui_noise(txt):
                return txt
        except Exception:
            continue

    # Fallback: try to get all response containers and filter
    try:
        all_responses = page.locator("div[class*='response'], div[class*='message']").all()
        for resp in reversed(all_responses):  # Start from last
            try:
                txt = (resp.inner_text() or "").strip()
                if txt and len(txt) > 200 and not _looks_like_ui_noise(txt):
                    return txt
            except Exception:
                continue
    except Exception:
        pass

    # Try markdown code blocks (Gemini often wraps JSON in ```json blocks)
    try:
        code_blocks = page.locator("pre, code, .code-block").all()
        for block in reversed(code_blocks):
            try:
                txt = (block.inner_text() or "").strip()
                if txt and len(txt) > 200 and not _looks_like_ui_noise(txt):
                    # If it's JSON or looks like article content, return it
                    if txt.startswith('{') or txt.startswith('[') or "sections" in txt.lower():
                        return txt
            except Exception:
                continue
    except Exception:
        pass

    # Last resort fallback: page-level inner_text, but clean it
    try:
        txt = (page.locator("body").inner_text() or "").strip()
        # Try to extract the actual response by filtering out noise
        txt = _clean_extracted_text(txt)
        return txt
    except Exception:
        return ""


def _looks_like_ui_noise(text: str) -> bool:
    """Check if text looks like UI elements rather than actual content."""
    if not text:
        return True
    
    # Noise indicators (common UI text that appears in Gemini UI, not in responses)
    noise_markers = [
        "Перейти на Google AI Plus",
        "Мой контент",
        "Please replace the empty space",
        "Change the white",
        "DO NOT LEAVE BLANK WHITE SPACE",
        "Чат с Gemini",
        "С чего начнем",
        "Здравствуйте",
        "ratio image using this Prompt",
    ]
    
    # Strong indicators that this is UI noise
    for marker in noise_markers:
        if marker in text:
            return True
    
    # If text starts with "Gemini" and is short, likely UI
    first_line = text.split('\n')[0].strip()
    if first_line == "Gemini" or first_line.startswith("Gemini\n"):
        return True
    
    # Check if text looks like a series of image prompts (not article text)
    lines = text.split('\n')
    prompt_keywords = ["Please replace", "Change the white", "DO NOT LEAVE BLANK"]
    prompt_line_count = sum(1 for line in lines if any(kw in line for kw in prompt_keywords))
    
    # If more than 30% of lines are image prompts, this is likely UI noise
    if len(lines) > 5 and prompt_line_count / len(lines) > 0.3:
        return True
    
    return False


def _clean_extracted_text(text: str) -> str:
    """Clean extracted text from Gemini UI by removing common noise patterns."""
    if not text:
        return ""
    
    # Split into lines for processing
    lines = text.split('\n')
    cleaned_lines = []
    
    # Patterns to skip entirely (UI noise)
    skip_patterns = [
        "Gemini",
        "Перейти на Google AI Plus",
        "Google AI Plus",
        "Мой контент",
        "Please replace the empty space",
        "Change the white",
        "DO NOT LEAVE BLANK WHITE SPACE",
        "Чат с Gemini",
        "С чего начнем",
        "Здравствуйте",
        "ratio image using this Prompt",
        "IMPORTANT: THE image must be",
        "Cinematic lighting",
    ]
    
    in_valid_content = False
    valid_content_start_idx = -1
    
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        
        # Skip empty lines at the start
        if not in_valid_content and not line_stripped:
            continue
        
        # Check if this line is noise
        is_noise = any(pattern in line for pattern in skip_patterns)
        
        if is_noise:
            continue
        
        # Detect start of valid content (JSON or article text)
        if not in_valid_content:
            # JSON start
            if line_stripped.startswith('{') or line_stripped.startswith('['):
                in_valid_content = True
                valid_content_start_idx = i
            # Article title pattern
            elif len(line_stripped) > 20 and not any(c in line_stripped for c in ['(', ')', '[', ']']):
                in_valid_content = True
                valid_content_start_idx = i
        
        if in_valid_content:
            cleaned_lines.append(line)
    
    # Join and return
    result = '\n'.join(cleaned_lines).strip()
    
    # If we got very little content, return original (might be a parsing issue)
    if len(result) < 100:
        return text
    
    return result


def _article_text_worker(
    *,
    idx: int,
    title: str,
    prompt: str,
    url: str,
    headless: bool,
    executable_path: str | None,
    profile_dir: str,
    model_choice: str | None,
    timeout_s: int = 180,
    base_image_path: str | None = None,
) -> dict:
    """Worker: open Gemini UI, (optionally) attach an image, send prompt, extract response text."""
    result: dict = {
        "idx": idx,
        "title": title,
        "prompt": prompt,
        "text": "",
        "error": None,
        "profile_dir": profile_dir,
        "base_image_path": base_image_path,
    }

    # Concurrency guard: never drive the same Chrome profile (user-data-dir) from two threads.
    # If this happens, both workers can type/click in the same Gemini tab and you get a
    # duplicate paste/send while generation is running  Gemini shows "Something went wrong".
    lk = _get_profile_lock(profile_dir)
    acquired = False

    try:
        acquired = lk.acquire(timeout=max(30, int(timeout_s)))
        if not acquired:
            result["error"] = f"Profile is busy (locked): {profile_dir}"
            return result

        with sync_playwright() as p:
            ctx = _launch_persistent_ctx_with_retries(
                p,
                user_data_dir=profile_dir,
                headless=headless,
                executable_path=executable_path,
            )
            try:
                page = ctx.new_page()
                page.set_default_timeout(30000)
                page.goto(url, wait_until="load")
                _wait_input_ready(page, timeout_ms=60000)
                _dismiss_overlays(page)

                try:
                    if model_choice:
                        gph._pick_model(page, model_choice)
                except Exception:
                    # Not fatal
                    pass

                try:
                    _start_new_chat(page)
                except Exception:
                    pass

                _wait_input_ready(page, timeout_ms=60000)
                _dismiss_overlays(page)

                # Optionally attach a "pin" image for context before sending the prompt.
                if base_image_path:
                    try:
                        if os.path.exists(base_image_path):
                            ok_img = _attach_image(page, base_image_path)
                            if ok_img:
                                _wait_image_attached(page, timeout_ms=10000)
                        else:
                            # Don't fail the whole run, but surface it.
                            result["error"] = f"Base image not found: {base_image_path}"
                    except Exception as e:
                        # Non-fatal; continue with text-only prompt.
                        result["error"] = f"Image attach failed: {e}"

                prev = 0
                try:
                    prev = _count_responses(page)
                except Exception:
                    prev = 0

                _type_prompt(page, prompt)

                # Guard: sometimes Gemini UI wipes the editor content right after automation inserts it
                # (text flashes for a moment, then disappears). If we press Send with an empty editor,
                # the window will appear "stuck" while we wait for a response that will never come.
                def _editor_len() -> int:
                    try:
                        return int(
                            page.evaluate(
                                """() => {
                                  const sels = [
                                    "div.ql-editor.textarea.new-input-ui[contenteditable='true']",
                                    "div.ql-editor[contenteditable='true']",
                                    "[contenteditable='true'][role='textbox']",
                                  ];
                                  for (const sel of sels){
                                    const el = document.querySelector(sel);
                                    if (el){
                                      const t = (el.innerText || el.textContent || '').trim();
                                      return t.length;
                                    }
                                  }
                                  return 0;
                                }"""
                            )
                            or 0
                        )
                    except Exception:
                        return 0

                exp_len = len((prompt or '').strip())
                cur_len = _editor_len()
                if exp_len > 50 and cur_len < max(20, int(exp_len * 0.75)):
                    # One quick retry
                    _type_prompt(page, prompt)
                    cur_len = _editor_len()

                if exp_len > 50 and cur_len < max(20, int(exp_len * 0.75)):
                    result["error"] = (
                        "Prompt was not reliably inserted into Gemini input (editor content vanished / truncated). "
                        "Please rerun this item or switch to manual paste."
                    )
                    return result

                _click_send(page)

                ok = _wait_for_new_text_response(page, prev, timeout_s=min(timeout_s, 90))
                if not ok:
                    result["error"] = f"Timeout: не дождались появления ответа за {min(timeout_s, 90)} сек"
                    return result

                finished = _wait_for_text_generation_to_finish(page, timeout_s=timeout_s)
                if not finished:
                    # We'll still attempt extraction, but mark as incomplete.
                    result["error"] = f"Timeout: генерация не завершилась за {timeout_s} сек (пробую забрать текст как есть)"

                txt = _extract_last_response_text(page)
                if not (txt or "").strip():
                    # Keep previous error if any
                    if not result.get("error"):
                        result["error"] = "Ответ получен, но текст не удалось извлечь из DOM"
                else:
                    result["text"] = txt
                return result
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        result["error"] = str(e)
        return result
    finally:
        if acquired:
            try:
                lk.release()
            except Exception:
                pass


def _should_retry_article_error(err: str | None) -> bool:
    if not err:
        return False
    e = str(err).lower()
    retry_markers = [
        "timeout",
        "target closed",
        "browser closed",
        "page closed",
        "has been closed",
        "context closed",
        "connection closed",
        "websocket",
        "net::",
    ]
    return any(m in e for m in retry_markers)


def _article_text_worker_with_retries(
    *,
    retries: int,
    idx: int,
    title: str,
    prompt: str,
    url: str,
    headless: bool,
    executable_path: str | None,
    profile_dir: str,
    model_choice: str | None,
    timeout_s: int,
    base_image_path: str | None = None,
) -> dict:
    """Run `_article_text_worker` with automatic retries on timeout / manual window close."""
    attempts = max(1, int(retries) + 1)  # retries=0 => 1 attempt
    last_res: dict | None = None
    for attempt in range(1, attempts + 1):
        res = _article_text_worker(
            idx=idx,
            title=title,
            prompt=prompt,
            url=url,
            headless=headless,
            executable_path=executable_path,
            profile_dir=profile_dir,
            model_choice=model_choice,
            timeout_s=timeout_s,
            base_image_path=base_image_path,
        )
        last_res = res

        # Success => stop
        if (res.get("text") or "").strip() and not (res.get("error") or ""):
            res["attempt"] = attempt
            res["attempts_total"] = attempts
            return res

        # If we still have attempts left, retry aggressively when output is empty.
        # This covers cases where the user manually closed the window but Playwright didn't
        # surface a clear "Target closed" marker in the error.
        if attempt < attempts and not (res.get("text") or "").strip():
            time.sleep(1.2)
            continue

        # Otherwise fall back to marker-based retry decision
        err = res.get("error")
        if attempt >= attempts or not _should_retry_article_error(str(err) if err is not None else None):
            res["attempt"] = attempt
            res["attempts_total"] = attempts
            return res

        # Brief backoff before retry
        time.sleep(1.2)

    return last_res or {"idx": idx, "title": title, "prompt": prompt, "text": "", "error": "Unknown error"}


st.set_page_config(page_title="Unified: Gemini → Photoshop → WebP", layout="wide")
st.title("Unified workflow: Gemini (Playwright) → Photoshop fill → WebP")

# -------- Query-param import: prefill Tab 1 prompts from Tab 0 article outputs --------
# We use this to open a new browser tab and automatically inject image prompts into Tab 1.

def _get_query_param_value(key: str) -> str | None:
    try:
        v = st.query_params.get(key)
        if isinstance(v, list):
            return v[0] if v else None
        return v
    except Exception:
        try:
            qp = st.experimental_get_query_params()  # type: ignore[attr-defined]
            v = qp.get(key)
            return v[0] if isinstance(v, list) and v else (v if isinstance(v, str) else None)
        except Exception:
            return None


def _clear_query_params() -> None:
    try:
        st.query_params.clear()
    except Exception:
        try:
            st.experimental_set_query_params()  # type: ignore[attr-defined]
        except Exception:
            pass


def _try_import_prompts_from_query() -> None:
    raw = _get_query_param_value("load_img_prompts")
    if not raw:
        return
    try:
        import base64 as _b64
        import json as _json
        payload = _b64.urlsafe_b64decode(raw.encode("utf-8")).decode("utf-8", errors="ignore")
        data = _json.loads(payload)
        fast_prompts: list[str] = []
        pro_prompts: list[str] = []

        if isinstance(data, dict):
            # New format: split prompts by target tab
            pro_prompts = data.get("pro_prompts") or []
            fast_prompts = data.get("fast_prompts") or []
            # Backward compatibility
            if not pro_prompts and not fast_prompts:
                fast_prompts = data.get("prompts") or []
        else:
            # Old format: everything goes to fast tab
            fast_prompts = data

        if not isinstance(fast_prompts, list):
            fast_prompts = []
        if not isinstance(pro_prompts, list):
            pro_prompts = []

        fast_prompts = [str(p).strip() for p in fast_prompts if str(p).strip()]
        pro_prompts = [str(p).strip() for p in pro_prompts if str(p).strip()]

        # Defensive cleanup: sometimes a whole article JSON blob can accidentally end up
        # as a "prompt" (e.g., if a copy/paste or model output got mixed into the list).
        # If we detect such a blob, re-extract the vertical prompts from it.
        def _expand_if_article_blob(maybe_prompt: str) -> list[str]:
            s = (maybe_prompt or "").strip()
            if not s:
                return []

            # Only treat as an "article blob" if it *actually* parses as our article JSON.
            # If parsing fails for any reason, keep the original string as-is (never drop prompts).
            if s.startswith("{") and ("\"excerpt\"" in s or "\"sections\"" in s or "\"featured_image\"" in s):
                try:
                    art = _try_parse_article_json(s)
                    if isinstance(art, dict) and isinstance(art.get("sections"), list):
                        extracted = _article_json_image_prompts(art)
                        if extracted:
                            return extracted
                except Exception:
                    pass

            return [s]

        fast2: list[str] = []
        for p in fast_prompts:
            fast2.extend(_expand_if_article_blob(p))
        pro2: list[str] = []
        for p in pro_prompts:
            pro2.extend(_expand_if_article_blob(p))

        fast_prompts = [p for p in fast2 if p]
        pro_prompts = [p for p in pro2 if p]

        if not fast_prompts and not pro_prompts:
            return

        # Prefill Tab 1 (fast)
        if fast_prompts:
            # IMPORTANT: clear existing widget state for prompt inputs.
            # Otherwise Streamlit will keep stale values for keys like `unif_pw_prompt_0`,
            # which can make the visible fields look "shifted"/skipped after import.
            try:
                for k in list(st.session_state.keys()):
                    if isinstance(k, str) and k.startswith("unif_pw_prompt_"):
                        del st.session_state[k]
            except Exception:
                pass

            st.session_state.unif_pw_prompts = fast_prompts
            # Also explicitly seed widget keys so the first render cannot pick up stray stale values.
            # Streamlit gives widget-key state higher priority than the `value=` argument.
            for i, p in enumerate(fast_prompts):
                st.session_state[f"unif_pw_prompt_{i}"] = p
            # Remove any leftover prompt widget keys beyond the new length
            try:
                n = len(fast_prompts)
                for k in list(st.session_state.keys()):
                    if isinstance(k, str) and k.startswith("unif_pw_prompt_"):
                        try:
                            idx = int(k.split("_")[-1])
                        except Exception:
                            continue
                        if idx >= n:
                            del st.session_state[k]
            except Exception:
                pass

            st.session_state.unif_pw_imported_from_query = True

        # Prefill Tab 2 (pro / Nano Banana)
        if pro_prompts:
            st.session_state.unif_nbp_tasks = [
                {"prompt": p, "user_data_dir": os.path.abspath(".chrome_automation_profile")}
                for p in pro_prompts
            ]
            st.session_state.unif_nbp_imported_from_query = True

        _clear_query_params()

        # Auto-switch to the relevant tab via small JS click.
        # Prefer Pro tab if pro_prompts exist, else Fast tab.
        tab_label = "2) Gemini Generate" if pro_prompts else "1) Gemini Generate"
        components.html(
            f"""
            <script>
            (function(){{
              function clickTab(){{
                const btns = Array.from(window.parent.document.querySelectorAll('button'));
                const target = btns.find(b => (b.innerText||'').includes('{tab_label}'));
                if(target){{ target.click(); return true; }}
                return false;
              }}
              let tries=0;
              const t = setInterval(()=>{{
                tries++;
                if(clickTab() || tries>20){{ clearInterval(t); }}
              }}, 250);
            }})();
            </script>
            """,
            height=0,
        )
    except Exception:
        # Don't break the app if query parsing fails
        try:
            _clear_query_params()
        except Exception:
            pass


_try_import_prompts_from_query()

# Глобальная настройка корня генераций (по умолчанию — локальная папка). Можно указать абсолютный путь на диске.
if "unif_base_root" not in st.session_state:
    # Попробуем использовать Desktop path из Windows-профиля пользователя, если доступен
    try:
        default_root = str((Path("generate automation")).resolve())
    except Exception:
        default_root = "generate automation"
    st.session_state.unif_base_root = default_root

st.session_state.unif_base_root = st.text_input(
    "Базовая папка генераций (корень)",
    value=st.session_state.unif_base_root,
    key="ui_unif_base_root"
)


# Allocate unique run directory per app launch (date-based with incrementing suffix)
from pathlib import Path as _PathAlias

def _get_run_base_dir() -> str:
    if "unif_run_base_dir" in st.session_state and st.session_state.unif_run_base_dir:
        return st.session_state.unif_run_base_dir
    # Use configurable base root from session state
    base_root = st.session_state.get("unif_base_root", "generate automation")
    root = _PathAlias(base_root)
    date_str = datetime.now().strftime("%Y-%m-%d")
    base = root / date_str
    cand = base
    if cand.exists():
        i = 1
        while True:
            cand = _PathAlias(f"{base}_{i}")
            if not cand.exists():
                break
            i += 1
    # Cache absolute path
    st.session_state.unif_run_base_dir = str(_PathAlias(cand).resolve())
    return st.session_state.unif_run_base_dir

# Initialize and pin run dir early to avoid double-increment from multiple initial renders
_ = _get_run_base_dir()

# Shared helper

def _get_all_saved_paths() -> list[str]:
   """Return all saved image paths across tabs.

   We keep Tab1 (fast) and Tab2 (pro) outputs in separate session_state keys to prevent
   accidental overwrites/deletes during regeneration.

   This helper provides a unified view for tabs like Photoshop/WebP.
   """
   fast = list(st.session_state.get("unif_fast_saved_paths") or [])
   pro = list(st.session_state.get("unif_pro_saved_paths") or [])
   # Preserve order but avoid duplicates
   out: list[str] = []
   seen: set[str] = set()
   for p in fast + pro:
       if not p:
           continue
       if p in seen:
           continue
       out.append(p)
       seen.add(p)
   return out


def _extract_prompt_idx(name: str) -> int | None:
   """Parse prompt index from filename supporting both old and new naming.
   New:  <idx>_<slug>_<vv>.<ext>
   Old:  <ii>_<vv>_[reN_]<slug>.<ext>
   """
   try:
       import re as _re
       b = os.path.basename(name)
       m = _re.match(r"^(\d+)_((?:re\d+_)?[^.]+)_(\d{2})(?:_filled)?\.(?:png|jpg|jpeg|bin|webp)$", b, flags=_re.IGNORECASE)
       if m:
           return int(m.group(1))
       m2 = _re.match(r"^(\d{2})_\d{2}_(?:re\d+_)?(.+)\.(?:png|jpg|jpeg|bin)$", b, flags=_re.IGNORECASE)
       if m2:
           return int(m2.group(1))
       return None
   except Exception:
       return None

def _cleanup_old_prompt_files(base_dir: str, idx: int, keep_paths: List[str]) -> int:
   """Delete old files for a given prompt index in base_dir, except those in keep_paths.
   Supports both old (ii_vv_[reN_]slug.ext) and new (idx_slug_vv.ext) naming.
   Returns number of files deleted."""
   try:
       import glob
       exts = {'.png', '.jpg', '.jpeg', '.bin'}
       # Normalize keep paths
       keep_set = set(os.path.normcase(os.path.normpath(os.path.abspath(p))) for p in (keep_paths or []))
       deleted = 0
       patterns = [
           os.path.join(base_dir, f"{idx:02d}_??_*.*"),   # old style
           os.path.join(base_dir, f"{idx}_*_*.*"),       # new style
       ]
       seen = set()
       for pat in patterns:
           for fpath in glob.glob(pat):
               try:
                   if os.path.splitext(fpath)[1].lower() not in exts:
                       continue
                   ap = os.path.normcase(os.path.normpath(os.path.abspath(fpath)))
                   if ap in keep_set or ap in seen:
                       continue
                   # Extra guard: ensure this file indeed belongs to this idx for new format
                   base = os.path.basename(ap)
                   import re as _re
                   if not (_re.match(rf"^{idx}_", base) or _re.match(rf"^{idx:02d}_", base)):
                       continue

                   # IMPORTANT: do not delete Nano Banana Pro images from Tab 2.
                   # They use the naming convention: <idx>_pro_<slug>_<vv>.<ext>
                   # Tab 1 regeneration must only clean up NON-pro images.
                   if _re.match(rf"^{idx}_pro_", base) or _re.match(rf"^{idx:02d}_pro_", base):
                       continue

                   os.remove(ap)
                   seen.add(ap)
                   deleted += 1
               except Exception:
                   pass
       return deleted
   except Exception:
       return 0

def _is_cdp_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/json/version", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


tab0, tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "0) Generate Article Texts (Gemini UI / multi-window)",
    "1) Gemini Generate (Playwright)",
    "2) Gemini Generate (Nano Banana Pro / multi-window)",
    "3) Remove watermark in Photoshop",
    "4) Normalize Pins to 640×1024 (crop)",
    "5) Convert to WebP",
    "6) Regenerate Titles & Descriptions (Gemini API)",
    "7) Regenerate Product Images (Gemini UI)",
])

# ---------------- Tab 0: Article texts (Gemini UI / multi-window) ----------------
with tab0:
    st.subheader("Generate article texts via Gemini UI (multi-window)")
    st.caption(
        "Вставьте список заголовков (по одному на строку). Для каждого заголовка откроется Gemini в отдельном профиле "
        "(параллельно) и будет сгенерирован текст статьи. Результаты появятся ниже."
    )

    # --- State init ---
    if "unif_text_titles_raw" not in st.session_state:
        st.session_state.unif_text_titles_raw = ""
    if "unif_text_template" not in st.session_state:
        st.session_state.unif_text_template = (
            "Write a 1,500-word SEO article titled \"[article_title]\" that is both engaging and informative. The article must be written as if you are having a friendly, informal conversation with a fellow enthusiast. Ensure that every instruction below is followed precisely, producing a final output that is dynamic, user-friendly, and thoroughly human in its tone and style.\n\n"
            "Style & Tone Requirements:\n"
            "Conversational and Informal:\n"
            "Write as if youre talking to a friend. The tone should be relaxed, engaging, and approachable.\n"
            "Use everyday language; avoid overly formal or academic language.\n"
            "Ensure the narrative flows naturally and doesnt sound scripted or robotic.\n"
            "Occasional Sarcasm & Humor:\n"
            "Inject light sarcasm and humor to keep the reader engaged. Use these elements sparinglyonly enough to maintain a playful tone without overwhelming the content.\n"
            "The humor should be witty and subtle; ensure it does not detract from the main points.\n"
            "Personal Touch and Experience:\n"
            "Include personal opinions or anecdotes where relevant. This adds authenticity and builds trust with the reader.\n"
            "When describing features or comparing products, mention personal experiences to make the content more relatable.\n"
            "Active Voice Only:\n"
            "Write every sentence in the active voice. For example, use I love this feature instead of This feature is loved by many.\n"
            "Double-check your sentences to avoid any passive constructions.\n"
            "Engagement Through Rhetorical Questions:\n"
            "Insert rhetorical questions throughout the article to engage the reader and provoke thought. For example: Ever wondered why this works so well?\n"
            "These questions should serve as conversation starters and not be overused.\n"
            "Use of Slang & Abbreviations:\n"
            "Occasionally incorporate common internet slang such as FYI, IMO, etc., as well as a few emoticons (e.g., :) or :/).\n"
            "Limit these to 23 instances per article to keep the content playful yet professional.\n"
            "Formatting & Structural Requirements:\n"
            "Introduction:\n"
            "Begin with a short, punchy introduction that immediately hooks the reader.\n"
            "Avoid generic openers like In todays world.. or dive into\n"
            "The introduction should quickly address the readers needs and set the tone for the rest of the article.\n"
            "Headings and Subheadings:\n"
            "Organize the article using H2 headings for each major section or point.\n"
            "Use H3 headings to break down subtopics within each H2 section when necessary.\n"
            "Ensure the headings are clear and descriptive to guide the reader through the content.\n"
            "Paragraph Structure:\n"
            "Keep paragraphs short and punchyideally 34 sentences per paragraph.\n"
            "Avoid long blocks of text to ensure readability on both desktop and mobile devices.\n"
            "Each paragraph should be focused and convey a single idea clearly.\n"
            "Bullet Points & Lists:\n"
            "When presenting technical details, features, or comparisons, use bullet points or numbered lists.\n"
            "These lists should break down information in an easy-to-digest format.\n"
            "Bold Key Information:\n"
            "Throughout the article, bold the most important points, features, or pieces of information. This helps draw the readers attention to the essential parts of your message.\n"
            "Content and SEO Requirements:\n"
            "Conciseness and Clarity:\n"
            "Every sentence should contribute directly to the articles purpose. Avoid filler phrases such as dive into or in modern times.\n"
            "Be clear and directevery point should have a reason for being there.\n"
            "Comparative and Opinion-Based Commentary:\n"
            "When comparing products, techniques, or ideas, include clear and honest comparisons that offer genuine insights.\n"
            "Support your opinions with logical reasoning and, when possible, real-life examples.\n"
            "SEO Optimization:\n"
            "Ensure the content is optimized for SEO by naturally including relevant keywords related to \"[article_title]\".\n"
            "The language should be SEO-friendly without sacrificing readability or the conversational tone.\n"
            "Avoid AI Fluff:\n"
            "Do not include generic, AI-generated fluff such as overly used phrases like dive into or clichés.\n"
            "The writing must be human, direct, and purposeful, ensuring that every word adds value.\n"
            "Detailed Writing Instructions:\n"
            "Introduction Section:\n"
            "Open with a captivating hook. Immediately address the readers needs or concerns related to article title\n"
            "State your personal connection or experience with the topic if possible.\n"
            "Main Body:\n"
            "Divide the main content into multiple sections, each introduced by an H2 heading.\n"
            "Within each section, use H3 subheadings where necessary to break down complex ideas.\n"
            "Incorporate bullet points or numbered lists for technical details or feature comparisons.\n"
            "Bold important terms, key features, or takeaways to emphasize their importance.\n"
            "Tone and Engagement:\n"
            "Maintain a conversational tone by writing as though youre chatting with a friend.\n"
            "Use rhetorical questions throughout to encourage reader engagement.\n"
            "Occasionally inject a touch of sarcasm or witty humor to make the article enjoyable without undermining its professionalism.\n"
            "Sprinkle in internet slang (e.g., FYI, IMO) and emoticons no more than 23 times in the entire article.\n"
            "Sentence Structure:\n"
            "Write in a clear, active voice. Ensure every sentence is dynamic and direct.\n"
            "Avoid complex, multi-clause sentences that might dilute the clarity of your points.\n"
            "Each paragraph should be conciseaim for 34 sentences per paragraph to ensure readability.\n"
            "Conclusion:\n"
            "End with a concise summary that reiterates the key points.\n"
            "Offer a final, engaging thought or call to action that encourages the reader to reflect or take the next step.\n"
            "Leave the reader with a memorable final impression, perhaps by reintroducing a humorous or personal touch.\n\n"
            "For each section of the article, I'd like you to create a prompt to generate TWO vertical realistic images. They should not depict exactly the same thing, they should be different, but they should reflect the meaning of the section. Also give me a prompt to generate ONE featured image that reflects the whole article and will be used as the thumbnail/cover. IMPORTANT: this featured image must be WIDE HORIZONTAL LANDSCAPE 2:1 (2x1) banner, ONE monolithic seamless scene, and MUST NOT be a collage/diptych/triptych/split-screen/panels/frames/borders, and MUST look like a single seamless photo. Do NOT use the words 'vertical' or 'portrait' in the featured image prompt. I'd like you to understand that I want to place affiliate links under each of these images, and I want you to immediately provide me with a list of products related to the image (there are similar products in the image) that I can search on Amazon and select the right products for each section of the blog post. I'd like at least three or six product phrases so that I can easily search on Amazon for the blog section image and add about four products from Amazon based on these phrases (The phrases do not necessarily have to refer to the same product from the two vertical pictures above; they can refer to either one product from one of the two vertical pictures above or several different products from these pictures). The first section of the article should be dedicated to something similar to the pin I gave and described above, but Please don't mention anything about Pinterest or that anyone came from Pinterest to this article or from anywhere else, or that the reader saw that pin before, dont need it. It is also important that you do not generate the images yourself under any circumstances. I only need you to know what is in the image and give the prompts for their generation, and that's all. The article should be self-contained and not too promotional. Also, after the title but before the large image of the post and the introduction, my blog post should have a small annotation (exerpt or, I don't know, a preamble) that briefly describes what's in the article to intrigue readers. It should be a maximum of 200 characters.\n"
            "Images in article sections will be placed below the section text, but not above it. Don't start the article with the phrase \"Let's be real for a second\", be a bit more creative, try something catchy but appropritate and relevant to the context of the text.\n"
            "Try to avoid making articles too long. A 1,500-character intro is a bit muchreaders might lose interest! :) Please try to keep each section (including the intro and conclusion) under 700 characters. Also, try to keep the number of sections to seven or fewerunless, of course, the title specifically calls for a certain number, like '9 Traditional Holiday Decor Ideas for...'\n\n"
            "OUTPUT FORMAT (follow strictly):\n"
            "- Return ONLY valid JSON (no markdown, no code fences, no commentary).\n"
            "- Do NOT include HTML tags like <h1>, <h2>, etc.\n"
            "- Headings must be provided only via JSON fields (title, sections[].h2, conclusion_heading).\n"
            "- Do NOT number section headings unless the title explicitly requires it.\n"
            "- IMPORTANT: Use EXACT key names from the schema below. Do NOT rename keys, do NOT add alternative keys (e.g. do NOT output 'Featured Image' or 'vertical image prompt 2' as keys).\n"
            "- If you include lists inside any \"text\" fields, format them as Markdown lists (each bullet line must start with '- ' and ordered items must start with '1. ', '2. ', etc.). Do NOT use '•' characters.\n\n"
            "The JSON schema must be:\n"
            "{\n"
            "  \"excerpt\": \"...<=200 chars...\",\n"
            "  \"featured_image\": \"WIDE HORIZONTAL LANDSCAPE 2:1 (2x1) banner, ONE monolithic seamless scene, no collage/split-screen/panels/frames/borders, no text/logos/watermarks. (Do NOT use the words 'vertical' or 'portrait' here.) ...\",\n"
            "  \"title\": \"...article title...\",\n"
            "  \"introduction\": \"...intro text...\",\n"
            "  \"sections\": [\n"
            "    {\n"
            "      \"h2\": \"...section heading...\",\n"
            "      \"text\": \"...section text...\",\n"
            "      \"prompt1\": \"...vertical image prompt 1...\",\n"
            "      \"prompt2\": \"...vertical image prompt 2...\",\n"
            "      \"amazon_search_phrases\": [\"phrase 1\", \"phrase 2\", \"phrase 3\"]\n"
            "    }\n"
            "  ],\n"
            "  \"conclusion_heading\": \"Conclusion\",\n"
            "  \"conclusion\": \"...conclusion text...\"\n"
            "}\n"
        )
    if "unif_text_results" not in st.session_state:
        st.session_state.unif_text_results = []  # list[dict]

    # --- Settings (separate keys so we don't affect Tab 1/2) ---
    default_url = st.session_state.get("unif_url", DEFAULT_URLS[0])
    if "unif_text_url" not in st.session_state:
        st.session_state.unif_text_url = default_url
    if "unif_text_headless" not in st.session_state:
        st.session_state.unif_text_headless = False
    if "unif_text_exe_path" not in st.session_state:
        st.session_state.unif_text_exe_path = st.session_state.get("unif_exe_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
    if "unif_text_user_data_dir" not in st.session_state:
        st.session_state.unif_text_user_data_dir = os.path.abspath(".chrome_automation_profile")
    if "unif_text_parallelism" not in st.session_state:
        st.session_state.unif_text_parallelism = 3
    if "unif_text_use_numbered_profiles" not in st.session_state:
        st.session_state.unif_text_use_numbered_profiles = True
    if "unif_text_timeout_s" not in st.session_state:
        st.session_state.unif_text_timeout_s = 180
    if "unif_text_retries" not in st.session_state:
        st.session_state.unif_text_retries = 1

    cols0 = st.columns([2, 2, 2, 1])
    with cols0[0]:
        st.selectbox(
            "URL интерфейса Gemini",
            DEFAULT_URLS,
            index=DEFAULT_URLS.index(st.session_state.unif_text_url) if st.session_state.unif_text_url in DEFAULT_URLS else 0,
            key="unif_text_url",
        )
    with cols0[1]:
        st.checkbox("Headless режим", value=bool(st.session_state.unif_text_headless), key="unif_text_headless")
    with cols0[2]:
        st.number_input(
            "Timeout на статью (сек)",
            min_value=30,
            max_value=900,
            value=int(st.session_state.unif_text_timeout_s),
            step=10,
            key="unif_text_timeout_s",
        )
    with cols0[3]:
        st.number_input(
            "Retries",
            min_value=0,
            max_value=3,
            value=int(st.session_state.unif_text_retries),
            step=1,
            key="unif_text_retries",
            help="Авто-повтор при зависании/таймауте или если окно закрыли вручную.",
        )

    st.text_input(
        "Путь к chrome.exe",
        value=st.session_state.unif_text_exe_path,
        key="unif_text_exe_path",
    )

    st.text_input(
        "Базовый профиль (user-data-dir)",
        value=st.session_state.unif_text_user_data_dir,
        key="unif_text_user_data_dir",
        help="Например: .chrome_automation_profile. Если включены numbered profiles, то будут использованы _1..N.",
    )

    # Profile for single-item regeneration (button "Пересоздать")
    if "unif_text_regen_profile" not in st.session_state:
        # Can be either a number ("1") meaning <base>_1, or a full path to a user-data-dir.
        st.session_state.unif_text_regen_profile = ""
    st.text_input(
        "Профиль для Пересоздать (номер или полный путь)",
        value=st.session_state.unif_text_regen_profile,
        key="unif_text_regen_profile",
        help=(
            "Оставьте пустым: будет выбран _1 (если существует) или будет создана временная копия базового профиля. "
            "Если указать число (например 3) — откроется <Базовый профиль>_3. "
            "Если указать путь — он будет использован как user-data-dir напрямую."
        ),
    )

    cols1 = st.columns([2, 2])
    with cols1[0]:
        st.checkbox(
            "Использовать профили .chrome_automation_profile_1..N (быстрее)",
            value=bool(st.session_state.unif_text_use_numbered_profiles),
            key="unif_text_use_numbered_profiles",
        )
    with cols1[1]:
        st.number_input(
            "Параллельно окон (concurrency)",
            min_value=1,
            max_value=12,
            value=int(st.session_state.unif_text_parallelism),
            step=1,
            key="unif_text_parallelism",
        )
        
        st.text_input(
            "Номера профилей (через запятую)",
            value="",
            key="unif_text_profile_numbers",
            help="Например: 2,4,5 откроет .chrome_automation_profile_2, _4, _5. Оставьте пустым для использования стандартного параллелизма."
        )
        
        st.selectbox(
            "Модель для генерации текстов",
            options=["Быстрая", "Думающая", "Pro"],
            index=0,
            key="unif_text_model_choice",
            help="Выберите модель Gemini для генерации текстов статей"
        )

    # Get selected model
    model_choice_text = st.session_state.get("unif_text_model_choice", "Быстрая")

    st.markdown("---")
    st.text_area(
        "Заголовки статей (по одному на строку)",
        value=st.session_state.unif_text_titles_raw,
        height=200,
        key="unif_text_titles_raw",
    )

    st.text_area(
        "Шаблон промпта (используйте {title})",
        value=st.session_state.unif_text_template,
        height=220,
        key="unif_text_template",
    )

    # Quick fix button: in case the user accidentally pasted an old generated prompt
    # (with pin/Pinterest-specific instructions) back into the template.
    # This keeps the workflow smooth without forcing a full reset of the whole template.
    cols_tpl = st.columns([1, 3])
    with cols_tpl[0]:
        if st.button("Очистить шаблон от pin/Pinterest текста", key="unif_text_tpl_clean"):
            import re as _re
            tpl0 = st.session_state.get("unif_text_template") or ""
            leak_patterns = [
                # Remove ONLY the pin/Pinterest-related segments, do NOT remove the whole paragraph.
                r"The first section of the article should be dedicated.*?dont need it\.?\s*",
                r"The first section of the article should be dedicated.*?do not generate the images yourself under any circumstances\.?\s*",
                r"Please\s+dont\s+mention\s+anything\s+about\s+Pinterest.*?(?:\.|$)\s*",
                r"dont\s+mention\s+anything\s+about\s+Pinterest.*?(?:\.|$)\s*",
                r"Use the attached reference image as inspiration for the first section of the article that comes after the introduction\..*?coherent with the section(?:'|\u2019)s text\.?\s*",
            ]
            for pat in leak_patterns:
                try:
                    tpl0 = _re.sub(pat, "", tpl0, flags=_re.IGNORECASE | _re.DOTALL).strip()
                except Exception:
                    pass
            st.session_state.unif_text_template = tpl0
            st.success("Готово: убрал pin/Pinterest-инструкции из шаблона")
            st.rerun()
    with cols_tpl[1]:
        st.caption("Если вы когда-то вставляли полный промпт обратно в шаблон, эта кнопка уберёт блоки про pin/Pinterest.")

    # --- Optional mode: attach one image per title ("pin") + add special instruction to prompt ---
    if "unif_text_use_pin_images" not in st.session_state:
        st.session_state.unif_text_use_pin_images = False
    if "unif_text_pin_images_tmp_dir" not in st.session_state:
        st.session_state.unif_text_pin_images_tmp_dir = None
    if "unif_text_pin_image_paths" not in st.session_state:
        st.session_state.unif_text_pin_image_paths = []

    st.markdown("#### Pin image mode (optional)")
    st.checkbox(
        "Attach 1 image per title + add pin-like intro instruction",
        value=bool(st.session_state.unif_text_use_pin_images),
        key="unif_text_use_pin_images",
        help=(
            "When enabled, the script will attach a corresponding image in Gemini UI for each title, "
            "and will append an extra instruction to the prompt so that the first section matches the image context. "
            "Default is OFF (no images, no extra instruction)."
        ),
    )

    uploaded_pin_imgs = st.file_uploader(
        "Upload images (one per title). Best: same order as titles.",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
        key="unif_text_pin_images_upload",
        disabled=not bool(st.session_state.unif_text_use_pin_images),
    )

    # Manual cleanup helper for temp pin-image folder
    if st.session_state.get("unif_text_pin_images_tmp_dir"):
        if st.button("Cleanup uploaded pin images (delete temp folder)", key="unif_text_pin_cleanup"):
            try:
                shutil.rmtree(st.session_state.unif_text_pin_images_tmp_dir, ignore_errors=True)
            except Exception:
                pass
            st.session_state.unif_text_pin_images_tmp_dir = None
            st.session_state.unif_text_pin_image_paths = []
            st.success("Cleaned")

    def _slugify_simple(s: str) -> str:
        import re as _re
        s = (s or "").lower()
        s = _re.sub(r"[^a-z0-9]+", " ", s)
        s = _re.sub(r"\s+", " ", s).strip()
        return s

    def _materialize_uploaded_images(files) -> list[str]:
        if not files:
            return []
        # Cleanup previous tmp dir (best-effort)
        prev = st.session_state.get("unif_text_pin_images_tmp_dir")
        if prev:
            try:
                shutil.rmtree(prev, ignore_errors=True)
            except Exception:
                pass
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp_dir = Path(f"tmp_rovodev_article_pin_images_{ts}")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        out_paths: list[str] = []
        for uf in files:
            try:
                name = Path(uf.name).name
                outp = tmp_dir / name
                with open(outp, "wb") as f:
                    f.write(uf.getbuffer())
                out_paths.append(str(outp.resolve()))
            except Exception:
                continue
        st.session_state.unif_text_pin_images_tmp_dir = str(tmp_dir.resolve())
        st.session_state.unif_text_pin_image_paths = out_paths
        return out_paths

    # Persist uploads to disk (Gemini UI can only attach files by path)
    if st.session_state.unif_text_use_pin_images and uploaded_pin_imgs is not None:
        _materialize_uploaded_images(uploaded_pin_imgs)

    def _map_pin_images_to_titles(titles_local: list[str], img_paths: list[str]) -> dict[int, str]:
        """Return mapping {1-based title index -> image path}."""
        if not titles_local or not img_paths:
            return {}
        # 1) If counts match, map by order.
        if len(img_paths) == len(titles_local):
            return {i + 1: img_paths[i] for i in range(len(titles_local))}

        # 2) Otherwise try match by filename containing slugified title.
        norm_titles = [(_slugify_simple(t), t) for t in titles_local]
        norm_imgs = [(_slugify_simple(Path(p).stem), p) for p in img_paths]
        used = set()
        mapped: dict[int, str] = {}
        for i, (nt, _orig) in enumerate(norm_titles, start=1):
            best = None
            for ni, p in norm_imgs:
                if p in used:
                    continue
                if nt and nt in ni:
                    best = p
                    break
            if best:
                mapped[i] = best
                used.add(best)

        # 3) Fill remaining in order for the rest (up to min lengths)
        if len(mapped) < min(len(titles_local), len(img_paths)):
            remaining = [p for p in img_paths if p not in used]
            for i in range(1, len(titles_local) + 1):
                if i in mapped:
                    continue
                if not remaining:
                    break
                mapped[i] = remaining.pop(0)
        return mapped

    def _parse_titles(raw: str) -> list[str]:
        return [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]

    titles = _parse_titles(st.session_state.unif_text_titles_raw)
    st.write(f"Тайтов к генерации: {len(titles)}")

    # Current pin mapping preview (usable both for generation and single-item re-run)
    use_pin_mode_ui = bool(st.session_state.get("unif_text_use_pin_images"))
    pin_img_paths_ui = list(st.session_state.get("unif_text_pin_image_paths") or [])
    pin_map_ui = _map_pin_images_to_titles(titles, pin_img_paths_ui) if use_pin_mode_ui else {}
    if use_pin_mode_ui:
        st.caption(f"Pin images loaded: {len(pin_img_paths_ui)} | mapped to titles: {len(pin_map_ui)}/{len(titles)}")

    run_text_btn = st.button("Сгенерировать тексты", type="primary", key="unif_text_run")

    if run_text_btn:
        if not titles:
            st.warning("Добавьте хотя бы один тайтл")
        else:
            import concurrent.futures

            url_snapshot = st.session_state.unif_text_url
            headless_snapshot = bool(st.session_state.unif_text_headless)
            exe_snapshot = st.session_state.unif_text_exe_path or None
            base_profile_snapshot = _normalize_user_data_dir(st.session_state.unif_text_user_data_dir) or st.session_state.unif_text_user_data_dir
            use_numbered = bool(st.session_state.unif_text_use_numbered_profiles)
            # Parse profile numbers from input
            profile_numbers_str = st.session_state.get("unif_text_profile_numbers", "").strip()
            profile_numbers = []
            if profile_numbers_str:
                try:
                    profile_numbers = [int(x.strip()) for x in profile_numbers_str.split(",") if x.strip()]
                except ValueError:
                    st.error("Ошибка: номера профилей должны быть числами через запятую (например: 2,4,5)")
                    profile_numbers = []
            
            # Concurrency (max simultaneously opened windows)
            desired_parallelism = max(1, min(12, int(st.session_state.unif_text_parallelism)))

            if profile_numbers:
                # Use specific numbered profiles, but still respect concurrency.
                # We can have more profiles in the pool than concurrent workers.
                max_workers = min(len(profile_numbers), desired_parallelism)
            else:
                # Use default parallelism
                max_workers = desired_parallelism
            timeout_s = int(st.session_state.unif_text_timeout_s)
            retries = int(st.session_state.unif_text_retries)

            template_snapshot = st.session_state.unif_text_template
            use_pin_mode = use_pin_mode_ui
            pin_map = pin_map_ui
            if use_pin_mode and not pin_map:
                st.warning("Pin image mode is ON, but no images were mapped. Running in text-only mode.")
                use_pin_mode = False

            # Prepare a pool of profile dirs to avoid profile locks.
            profile_pool: queue.Queue = queue.Queue()
            tmp_profiles: list[str] = []

            def _prepare_profile_slot(slot_idx: int) -> dict:
                """Return {dir,is_temp} for a slot."""
                # Prefer numbered profile
                if use_numbered:
                    cand = f"{base_profile_snapshot}_{slot_idx}"
                    cand_norm = _normalize_user_data_dir(cand) or cand
                    try:
                        if Path(cand_norm).exists():
                            return {"dir": cand_norm, "is_temp": False}
                    except Exception:
                        pass

                # Fallback: clone base profile
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                tmp_dir = str(Path(f"tmp_rovodev_article_profile_{ts}_{slot_idx}").resolve())
                try:
                    _clone_profile_dir(base_profile_snapshot, tmp_dir)
                except Exception:
                    # If clone fails, try to use base directly (may fail if locked)
                    tmp_dir = base_profile_snapshot
                    return {"dir": tmp_dir, "is_temp": False}
                tmp_profiles.append(tmp_dir)
                return {"dir": tmp_dir, "is_temp": True}

            # Create profile pool based on specific numbers or range
            if profile_numbers:
                # Use specific numbered profiles under the configured base user-data-dir.
                base_profile_name = base_profile_snapshot
                for prof_num in profile_numbers:
                    prof_path = f"{base_profile_name}_{prof_num}"
                    prof_path_norm = _normalize_user_data_dir(prof_path) or prof_path
                    
                    # Check if profile exists
                    try:
                        if Path(prof_path_norm).exists():
                            profile_pool.put({"dir": prof_path_norm, "is_temp": False})
                        else:
                            st.warning(f"Профиль {prof_path} не найден, пропускаем")
                    except Exception:
                        st.warning(f"Не удалось проверить профиль {prof_path}")
            else:
                # Use default numbered profiles or temp profiles
                for s in range(1, max_workers + 1):
                    slot = _prepare_profile_slot(s)
                    profile_pool.put(slot)

            def _worker_one(i: int, title_i: str) -> dict:
                slot = profile_pool.get()
                try:
                    base_img = pin_map.get(i) if use_pin_mode else None
                    prompt_i = _build_article_prompt_from_title(
                        title_i,
                        template_snapshot,
                        include_pin_intro_instruction=bool(use_pin_mode and base_img),
                    )
                    return _article_text_worker_with_retries(
                        retries=retries,
                        idx=i,
                        title=title_i,
                        prompt=prompt_i,
                        url=url_snapshot,
                        headless=headless_snapshot,
                        executable_path=exe_snapshot,
                        profile_dir=slot["dir"],
                        model_choice=model_choice_text,
                        timeout_s=timeout_s,
                        base_image_path=base_img,
                    )
                finally:
                    profile_pool.put(slot)

            results: list[dict] = [{"idx": i + 1, "title": titles[i], "prompt": "", "text": "", "error": "not started"} for i in range(len(titles))]

            st.info(f"Будет одновременно открыто окон: {max_workers} (профилей в пуле: {profile_pool.qsize()})")
            with st.spinner("Генерация текстов... (окна будут открываться параллельно)"):
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                    futs = {ex.submit(_worker_one, i + 1, t): (i + 1) for i, t in enumerate(titles)}
                    for fut in concurrent.futures.as_completed(futs):
                        idx_done = futs[fut]
                        try:
                            res = fut.result()
                        except Exception as e:
                            res = {"idx": idx_done, "title": titles[idx_done - 1], "prompt": "", "text": "", "error": str(e)}
                        if 1 <= idx_done <= len(results):
                            results[idx_done - 1] = res

            # Cleanup temp profiles created for this run
            for d in tmp_profiles:
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass

            st.session_state.unif_text_results = results

            # Bump per-article revisions so Streamlit widget keys change and never
            # reuse stale values from previous runs.
            for _r in results:
                try:
                    _idx = int(_r.get("idx") or 0)
                    if _idx > 0:
                        _bump_article_rev(_idx)
                        _clear_article_ui_cache(_idx)
                except Exception:
                    pass

            st.success("Готово")

    # ---- Results ----
    results = st.session_state.get("unif_text_results") or []
    if results:
        st.markdown("---")
        st.subheader("Результаты")

        for r in results:
            idx = int(r.get("idx") or 0)
            title = r.get("title") or ""
            prompt = r.get("prompt") or ""
            text = r.get("text") or ""
            err = r.get("error")

            with st.container(border=True):
                st.markdown(f"**#{idx}. {title}**")
                try:
                    _att = int(r.get("attempt") or 0)
                    _tot = int(r.get("attempts_total") or 0)
                    if _att and _tot and _tot > 1:
                        st.caption(f"Попытка: {_att}/{_tot}")
                except Exception:
                    pass
                if err and str(err) not in {"None", ""}:
                    st.error(str(err))
                if prompt:
                    with st.expander("Промпт", expanded=False):
                        st.code(prompt)

                rev = _get_article_rev(idx)

                edited_text = st.text_area(
                    "Текст статьи",
                    value=text,
                    height=260,
                    key=f"unif_text_out_{idx}_{rev}",
                )

                # Button to refresh prompts after manual text edits
                if st.button("🔄 Обновить промпты", key=f"unif_refresh_prompts_{idx}_{rev}", help="Пересчитать количество промптов после редактирования текста"):
                    # Update the text in session state with edited value
                    for j, rr in enumerate(st.session_state.unif_text_results):
                        if int(rr.get("idx") or 0) == idx:
                            st.session_state.unif_text_results[j]["text"] = edited_text
                            break
                    # Bump revision to force UI update
                    _bump_article_rev(idx)
                    st.rerun()

                # Open Tab 1 (image generation) in a new browser tab and prefill prompts.
                # Prefer structured JSON output (no guessing). Fallback to regex extraction.
                # Use edited_text to ensure we're parsing the current (possibly edited) version
                current_text = edited_text if edited_text else text
                article_json = _try_parse_article_json(current_text)
                if isinstance(article_json, dict):
                    try:
                        img_prompts = _article_json_image_prompts(article_json)
                    except Exception:
                        img_prompts = []
                else:
                    try:
                        img_prompts = _extract_image_prompts_from_article(current_text)
                    except Exception:
                        img_prompts = []

                # Gutenberg/preview helper block should show for every successful text,
                # even if we failed to parse image prompts.
                try:
                    import base64 as _b64
                    import json as _json

                    # 1) Optional link to image generation (only when we have prompts)
                    if img_prompts:
                        # Prompt routing rules:
                        # - Standard mode: first 4 prompts -> Pro tab, rest -> Fast tab
                        # - Pin image mode: Pro tab gets prompts 2-4 (skip prompt #1),
                        #   Fast tab gets prompts 5..end (prompt #1 is discarded)
                        use_pin_for_this_article = bool(st.session_state.get("unif_text_use_pin_images"))
                        if use_pin_for_this_article:
                            pro_prompts = img_prompts[1:4]
                            fast_prompts = img_prompts[4:]
                        else:
                            pro_prompts = img_prompts[:4]
                            fast_prompts = img_prompts[4:]

                        payload = _json.dumps(
                            {"title": title, "pro_prompts": pro_prompts, "fast_prompts": fast_prompts},
                            ensure_ascii=False,
                        )
                        encoded = _b64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
                        href = f"?load_img_prompts={encoded}"
                        st.markdown(
                            f"<a href=\"{href}\" target=\"_blank\">Открыть генерацию картинок для статьи (промптов: {len(img_prompts)})</a>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.caption("Не нашёл строки Featured Image / Prompt 1 / Prompt 2 в тексте (генератор картинок не откроется)")

                    # 2) Gutenberg helper (always)
                    gutenberg_url = "https://nestingmuse.com/wp-admin/post-new.php"
                    st.markdown(
                        f"<a href=\"{gutenberg_url}\" target=\"_blank\">Открыть Gutenberg (новый пост)</a>",
                        unsafe_allow_html=True,
                    )

                    if isinstance(article_json, dict):
                        cleaned_for_wp = _article_json_to_markdown(article_json)
                    else:
                        cleaned_for_wp = _clean_article_for_gutenberg(current_text)

                    # Reliable clipboard copy: must happen inside a real user gesture (HTML button click).
                    # Streamlit button click is not considered a clipboard "user gesture" by most browsers.
                    b64_txt = _b64.b64encode(cleaned_for_wp.encode("utf-8")).decode("ascii")
                    html_for_wp = _article_markdownish_to_html(cleaned_for_wp)
                    html_for_wp_wrapped = f"<div>{html_for_wp}</div>"
                    b64_html = _b64.b64encode(html_for_wp_wrapped.encode("utf-8")).decode("ascii")

                    blocks_for_wp = _article_markdownish_to_gutenberg_blocks(cleaned_for_wp)
                    b64_blocks = _b64.b64encode(blocks_for_wp.encode("utf-8")).decode("ascii")

                    st.download_button(
                        "Скачать текст статьи для Gutenberg (.txt)",
                        data=cleaned_for_wp,
                        file_name=f"article_{idx}.txt",
                        mime="text/plain",
                        key=f"unif_wp_dl_{idx}_{rev}",
                    )

                    # Excerpt copy (prefer JSON)
                    excerpt_val = ""
                    if isinstance(article_json, dict):
                        excerpt_val = str(article_json.get("excerpt") or "").strip()
                    if not excerpt_val:
                        # fallback for non-JSON outputs
                        try:
                            import re as _re
                            m = _re.search(r"^Excerpt:\s*\[(.*)\]\s*$", text or "", flags=_re.IGNORECASE | _re.MULTILINE)
                            if m:
                                excerpt_val = (m.group(1) or "").strip()
                        except Exception:
                            pass

                    b64_excerpt = _b64.b64encode(excerpt_val.encode("utf-8")).decode("ascii")

                    components.html(
                        f"""
                        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                          <button id="wp_copy_btn_{idx}_{rev}" style="padding:6px 10px;">Copy (HTML formatting)</button>
                          <button id="wp_copy_blocks_btn_{idx}_{rev}" style="padding:6px 10px;">Copy (Gutenberg Blocks)</button>
                          <button id="wp_copy_excerpt_btn_{idx}_{rev}" style="padding:6px 10px;">Copy Excerpt</button>
                          <span id="wp_copy_status_{idx}_{rev}" style="font-size:13px;"></span>
                        </div>
                        <script>
                        (function(){{
                          const btn = document.getElementById('wp_copy_btn_{idx}_{rev}');
                          const btnBlocks = document.getElementById('wp_copy_blocks_btn_{idx}_{rev}');
                          const btnExcerpt = document.getElementById('wp_copy_excerpt_btn_{idx}_{rev}');
                          const st = document.getElementById('wp_copy_status_{idx}_{rev}');
                          const b64Text = '{b64_txt}';
                          const b64Html = '{b64_html}';
                          const b64Blocks = '{b64_blocks}';
                          const b64Excerpt = '{b64_excerpt}';
                          function b64ToUtf8(b64){{
                            const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
                            return new TextDecoder('utf-8').decode(bytes);
                          }}

                          btn.addEventListener('click', async () => {{
                            try {{
                              const txt = b64ToUtf8(b64Text);
                              const html = b64ToUtf8(b64Html);

                              if (navigator.clipboard && navigator.clipboard.write && window.ClipboardItem) {{
                                const item = new ClipboardItem({{
                                  'text/plain': new Blob([txt], {{type: 'text/plain'}}),
                                  'text/html': new Blob([html], {{type: 'text/html'}})
                                }});
                                await navigator.clipboard.write([item]);
                              }} else {{
                                await navigator.clipboard.writeText(txt);
                              }}

                              st.textContent = 'Copied (HTML). Paste in Gutenberg (Ctrl+V).';
                            }} catch(e) {{
                              st.textContent = 'Clipboard blocked. Use preview/download.';
                            }}
                          }});

                          btnBlocks.addEventListener('click', async () => {{
                            try {{
                              const blocks = b64ToUtf8(b64Blocks);
                              await navigator.clipboard.writeText(blocks);
                              st.textContent = 'Copied (Gutenberg Blocks). In WP: open Code editor, paste, then switch back to Visual.';
                            }} catch(e) {{
                              st.textContent = 'Clipboard blocked. Use preview/download.';
                            }}
                          }});

                          btnExcerpt.addEventListener('click', async () => {{
                            try {{
                              const ex = b64ToUtf8(b64Excerpt);
                              await navigator.clipboard.writeText(ex);
                              st.textContent = 'Copied excerpt. Paste into WP excerpt field.';
                            }} catch(e) {{
                              st.textContent = 'Clipboard blocked. Copy excerpt from preview.';
                            }}
                          }});
                        }})();
                        </script>
                        """,
                        height=60,
                    )

                    if excerpt_val:
                        st.caption(f"Excerpt: {excerpt_val}")

                    with st.expander("Чистая статья для Gutenberg (preview)", expanded=False):
                        st.text_area(
                            "",
                            value=cleaned_for_wp,
                            height=260,
                            key=f"unif_wp_clean_preview_{idx}_{rev}",
                        )
                except Exception:
                    # Don't break the whole article card if any helper fails
                    if img_prompts:
                        st.caption(f"Промптов для картинок: {len(img_prompts)}")

                # Regenerate single title (sync)
                if st.button(f"Пересоздать #{idx}", key=f"unif_text_regen_{idx}"):
                    url_snapshot = st.session_state.unif_text_url
                    headless_snapshot = bool(st.session_state.unif_text_headless)
                    exe_snapshot = st.session_state.unif_text_exe_path or None
                    base_profile_snapshot = _normalize_user_data_dir(st.session_state.unif_text_user_data_dir) or st.session_state.unif_text_user_data_dir
                    timeout_s = int(st.session_state.unif_text_timeout_s)
                    retries = int(st.session_state.unif_text_retries)
                    template_snapshot = st.session_state.unif_text_template

                    # Profile selection for single-item regeneration
                    # - empty: prefer <base>_1 if exists, otherwise clone base
                    # - number N: use <base>_N if exists, otherwise fallback to <base>_1/clone
                    # - path: use the given user-data-dir directly (no suffixing)
                    regen_profile_raw = str(st.session_state.get("unif_text_regen_profile", "") or "").strip()

                    def _resolve_regen_profile_dir(base_profile: str, raw: str) -> tuple[str, str | None]:
                        """Return (profile_dir, tmp_dir_to_cleanup)."""
                        # Raw path: if it looks like a path (contains slash or drive) and exists, use it.
                        raw_norm = _normalize_user_data_dir(raw) if raw else None
                        if raw_norm:
                            try:
                                if Path(raw_norm).exists():
                                    return raw_norm, None
                            except Exception:
                                pass

                        # Raw number: treat as suffix
                        num = None
                        if raw:
                            try:
                                num = int(raw)
                            except Exception:
                                num = None

                        # Prefer requested numbered profile
                        if num is not None:
                            try:
                                cand = _normalize_user_data_dir(f"{base_profile}_{num}") or f"{base_profile}_{num}"
                                if Path(cand).exists():
                                    return cand, None
                            except Exception:
                                pass

                        # Fallback to _1
                        try:
                            cand = _normalize_user_data_dir(f"{base_profile}_1") or f"{base_profile}_1"
                            if Path(cand).exists():
                                return cand, None
                        except Exception:
                            pass

                        # Last resort: clone base profile (avoids locks)
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        tmp_dir_local = str(Path(f"tmp_rovodev_article_regen_{ts}_{idx}").resolve())
                        try:
                            _clone_profile_dir(base_profile, tmp_dir_local)
                            return tmp_dir_local, tmp_dir_local
                        except Exception:
                            return base_profile, None

                    prof, tmp_dir = _resolve_regen_profile_dir(base_profile_snapshot, regen_profile_raw)

                    base_img = pin_map_ui.get(idx) if use_pin_mode_ui else None
                    prompt_i = _build_article_prompt_from_title(
                        title,
                        template_snapshot,
                        include_pin_intro_instruction=bool(use_pin_mode_ui and base_img),
                    )
                    res = _article_text_worker_with_retries(
                        retries=retries,
                        idx=idx,
                        title=title,
                        prompt=prompt_i,
                        url=url_snapshot,
                        headless=headless_snapshot,
                        executable_path=exe_snapshot,
                        profile_dir=prof,
                        model_choice=model_choice_text,
                        timeout_s=timeout_s,
                        base_image_path=base_img,
                    )

                    # Cleanup
                    if tmp_dir:
                        try:
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                        except Exception:
                            pass

                    # Update in-place
                    for j, rr in enumerate(st.session_state.unif_text_results):
                        if int(rr.get("idx") or 0) == idx:
                            st.session_state.unif_text_results[j] = res
                            break
                    # Bump revision + clear any cached widget values for this idx
                    _bump_article_rev(idx)
                    _clear_article_ui_cache(idx)
                    st.rerun()

    # ---- Featured Image Generation ----
    if results:
        st.markdown("---")
        st.subheader("Генерация Featured Images")
        st.caption("Сгенерируйте featured images (2x1 ratio) для статей, у которых есть промпт Featured Image.")
        
        # Extract articles with featured image prompts
        articles_with_featured = []
        for r in results:
            idx = int(r.get("idx") or 0)
            title = r.get("title") or ""
            text = r.get("text") or ""
            
            # Try JSON first
            article_json = _try_parse_article_json(text)
            if isinstance(article_json, dict):
                feat_prompt = (article_json.get("featured_image") or article_json.get("featuredImage") or "").strip()
            else:
                # Fallback to regex extraction
                try:
                    img_prompts = _extract_image_prompts_from_article(text)
                    feat_prompt = img_prompts[0] if img_prompts else ""
                except Exception:
                    feat_prompt = ""
            
            if feat_prompt:
                articles_with_featured.append({"idx": idx, "title": title, "featured_prompt": feat_prompt})
        
        if articles_with_featured:
            st.write(f"Статей с Featured Image промптом: {len(articles_with_featured)}")
            
            # Settings for featured image generation
            feat_cols = st.columns([2, 2, 2, 3])
            with feat_cols[0]:
                feat_parallelism = st.number_input(
                    "Параллельно окон",
                    min_value=1,
                    max_value=12,
                    value=3,
                    key="feat_img_parallelism"
                )
            with feat_cols[1]:
                feat_timeout = st.number_input(
                    "Timeout (сек)",
                    min_value=30,
                    max_value=300,
                    value=120,
                    key="feat_img_timeout"
                )
            with feat_cols[2]:
                feat_model = st.selectbox(
                    "Модель",
                    options=["Быстрая", "Думающая", "Pro"],
                    index=0,
                    key="feat_img_model_choice"
                )
            with feat_cols[3]:
                feat_profile_numbers = st.text_input(
                    "Номера профилей (через запятую)",
                    value="2,4,5",
                    key="feat_img_profile_numbers",
                    help="Например: 2,4,5 откроет .chrome_automation_profile_2, _4, _5"
                )
            
            if st.button("🖼️ Сгенерировать Featured Images", type="primary", key="generate_featured_btn"):
                import concurrent.futures
                
                url_snap = st.session_state.unif_text_url
                headless_snap = bool(st.session_state.unif_text_headless)
                exe_snap = st.session_state.unif_text_exe_path or None
                
                # Parse profile numbers from input
                profile_numbers_str = feat_profile_numbers.strip()
                profile_numbers = []
                if profile_numbers_str:
                    try:
                        profile_numbers = [int(x.strip()) for x in profile_numbers_str.split(",") if x.strip()]
                    except ValueError:
                        st.error("Ошибка: номера профилей должны быть числами через запятую (например: 2,4,5)")
                        st.stop()
                
                if not profile_numbers:
                    st.error("Укажите хотя бы один номер профиля")
                    st.stop()
                
                # Determine base directory for saving featured images
                base_dir_featured = _get_run_base_dir()
                
                # Prepare profile pool with specific numbered profiles
                profile_pool_feat: queue.Queue = queue.Queue()
                base_profile_name = ".chrome_automation_profile"
                
                for prof_num in profile_numbers:
                    prof_path = f"{base_profile_name}_{prof_num}"
                    prof_path_norm = _normalize_user_data_dir(prof_path) or prof_path
                    
                    # Check if profile exists
                    try:
                        if not Path(prof_path_norm).exists():
                            st.warning(f"Профиль {prof_path} не найден, пропускаем")
                            continue
                    except Exception:
                        pass
                    
                    profile_pool_feat.put({"dir": prof_path_norm, "is_temp": False, "num": prof_num})
                
                if profile_pool_feat.empty():
                    st.error("Не найдено ни одного указанного профиля")
                    st.stop()
                
                num_profiles = profile_pool_feat.qsize()
                max_workers_feat = min(int(feat_parallelism), num_profiles)
                st.info(f"Доступно профилей: {num_profiles}, будет использовано окон: {max_workers_feat}")
                
                def _worker_feat(art: dict) -> dict:
                    slot = profile_pool_feat.get()
                    try:
                        result = _featured_image_worker(
                            idx=art["idx"],
                            title=art["title"],
                            featured_prompt=art["featured_prompt"],
                            url=url_snap,
                            headless=headless_snap,
                            executable_path=exe_snap,
                            profile_dir=slot["dir"],
                            model_choice=feat_model,
                            timeout_s=int(feat_timeout),
                            base_dir=base_dir_featured,
                        )
                        # Preserve the featured_prompt in the result for display and regeneration
                        result["featured_prompt"] = art["featured_prompt"]
                        result["title"] = art["title"]
                        return result
                    finally:
                        profile_pool_feat.put(slot)
                
                feat_results: list[dict] = []
                with st.spinner(f"Генерация featured images ({len(articles_with_featured)} шт.)..."):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers_feat) as ex:
                        futs = {ex.submit(_worker_feat, a): a["idx"] for a in articles_with_featured}
                        for fut in concurrent.futures.as_completed(futs):
                            try:
                                res = fut.result()
                                feat_results.append(res)
                            except Exception as e:
                                idx_err = futs[fut]
                                # Try to preserve title/prompt for the failed item so user can regenerate it.
                                art = next((a for a in articles_with_featured if a.get("idx") == idx_err), None)
                                feat_results.append(
                                    {
                                        "idx": idx_err,
                                        "title": (art or {}).get("title", ""),
                                        "featured_prompt": (art or {}).get("featured_prompt", ""),
                                        "error": str(e),
                                    }
                                )
                
                # Store results in session state
                if "unif_featured_results" not in st.session_state:
                    st.session_state.unif_featured_results = []
                st.session_state.unif_featured_results = feat_results
                
                st.success(f"Готово! Сгенерировано {len([r for r in feat_results if r.get('saved_path')])} featured images.")
                st.rerun()
            
            # Show featured image results
            if "unif_featured_results" in st.session_state and st.session_state.unif_featured_results:
                st.markdown("### Результаты генерации Featured Images")
                
                # Initialize webp conversion state
                if "unif_featured_webp" not in st.session_state:
                    st.session_state.unif_featured_webp = {}
                
                for res in st.session_state.unif_featured_results:
                    idx = res.get("idx")
                    title = res.get("title", "")
                    saved_path = res.get("saved_path")
                    error = res.get("error")
                    
                    with st.container(border=True):
                        st.markdown(f"**#{idx}. {title}**")

                        # We want the user to be able to regenerate even if the generation failed.
                        feat_prompt = (res.get("featured_prompt") or "").strip()

                        if error:
                            st.error(f"Ошибка: {error}")

                        if saved_path:
                            st.success(f"✅ Сохранено: {saved_path}")
                            try:
                                st.image(saved_path, width=400)
                            except Exception:
                                pass

                        # Show the prompt used (also in error state)
                        if feat_prompt:
                            with st.expander("Промпт", expanded=False):
                                st.text_area(
                                    "Featured Image Prompt",
                                    value=feat_prompt,
                                    height=100,
                                    key=f"feat_prompt_view_{idx}",
                                    disabled=True,
                                )

                        def _regen_featured_image_now():
                            """Regenerate featured image for this idx using the stored prompt.

                            We keep it available even when the previous generation failed.
                            """

                            if not feat_prompt:
                                st.warning("Нет промпта для пересоздания")
                                return

                            url_snap = st.session_state.unif_text_url
                            headless_snap = bool(st.session_state.unif_text_headless)
                            exe_snap = st.session_state.unif_text_exe_path or None
                            base_dir_featured = _get_run_base_dir()

                            # Get first available profile from the configured list
                            feat_profile_numbers_str = st.session_state.get("feat_img_profile_numbers", "2,4,5")
                            try:
                                profile_numbers = [int(x.strip()) for x in feat_profile_numbers_str.split(",") if x.strip()]
                                prof_num = profile_numbers[0] if profile_numbers else 2
                            except Exception:
                                prof_num = 2

                            base_profile_name = ".chrome_automation_profile"
                            prof_path = f"{base_profile_name}_{prof_num}"
                            prof_path_norm = _normalize_user_data_dir(prof_path) or prof_path

                            # Get model choice
                            feat_model_local = st.session_state.get("feat_img_model_choice", "Быстрая")
                            feat_timeout_local = int(st.session_state.get("feat_img_timeout", 120))

                            with st.spinner(f"Пересоздаю featured image #{idx}..."):
                                new_result = _featured_image_worker(
                                    idx=idx,
                                    title=title,
                                    featured_prompt=feat_prompt,
                                    url=url_snap,
                                    headless=headless_snap,
                                    executable_path=exe_snap,
                                    profile_dir=prof_path_norm,
                                    model_choice=feat_model_local,
                                    timeout_s=feat_timeout_local,
                                    base_dir=base_dir_featured,
                                )

                            # Update result in session state
                            for i, r in enumerate(st.session_state.unif_featured_results):
                                if r.get("idx") == idx:
                                    new_result["featured_prompt"] = feat_prompt
                                    new_result["title"] = title
                                    st.session_state.unif_featured_results[i] = new_result

                                    # Clear derived WebP for this idx (any rev)
                                    keys_to_delete = [
                                        k
                                        for k in list(st.session_state.unif_featured_webp.keys())
                                        if isinstance(k, str) and k.startswith(f"webp_{idx}_")
                                    ]
                                    for k in keys_to_delete:
                                        del st.session_state.unif_featured_webp[k]
                                    break

                            st.rerun()

                        # WebP conversion button (only makes sense when we have an image)
                        if saved_path:
                            # WebP conversion button
                            # IMPORTANT: make keys depend on the current image path ("revision") so that after regeneration
                            # we don't keep stale WebP/copy widgets.
                            import hashlib

                            saved_path_str = str(saved_path) if saved_path else ""
                            # IMPORTANT: `saved_path` may be overwritten in-place on "пересоздать" (same filename).
                            # So we include file mtime/size into the revision to force UI + copy widget refresh.
                            if saved_path_str:
                                try:
                                    _p = Path(saved_path_str)
                                    _st = _p.stat() if _p.exists() else None
                                    _sig = f"{saved_path_str}|{getattr(_st, 'st_mtime_ns', '')}|{getattr(_st, 'st_size', '')}"
                                except Exception:
                                    _sig = saved_path_str
                                image_rev = hashlib.md5(_sig.encode("utf-8")).hexdigest()[:10]
                            else:
                                image_rev = "none"
                            webp_key = f"webp_{idx}_{image_rev}"
                            webp_path = st.session_state.unif_featured_webp.get(webp_key)
                            
                            col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 1])
                            
                            def _unique_webp_path(base: Path) -> Path:
                                """Return a unique path by adding a timestamp suffix if the file already exists."""
                                if not base.exists():
                                    return base
                                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                                return base.with_name(f"{base.stem}_{ts}{base.suffix}")

                            with col_btn1:
                                # Always allow reconvert for the CURRENT image (use rev in the button key)
                                if st.button(
                                    "🧽 Убрать watermark и конвертировать в WebP",
                                    key=f"btn_webp_{idx}_{image_rev}",
                                ):
                                    from convert_to_webp import convert_image_to_webp

                                    # Use the saved_path shown in THIS UI block (it's already the latest for this result)
                                    current_saved_path = Path(saved_path)

                                    # 1) Remove watermark in Photoshop (same logic as Tab 3)
                                    filled_path, ps_err = _remove_watermark_with_photoshop_single(
                                        current_saved_path,
                                        size_or_scale=13,
                                        out_format="PNG",
                                        mode="scale",
                                        margin_left=25,
                                        margin_bottom=25,
                                        force=True,
                                        unique=True,
                                    )
                                    if not filled_path:
                                        st.error(f"Ошибка удаления watermark: {ps_err}")
                                    else:
                                        # 2) Convert filled image to WebP
                                        # Use a unique output name so the folder always gets a NEW file after regeneration/reconvert.
                                        webp_out_path = _unique_webp_path(filled_path.with_suffix(".webp"))
                                        success, err = convert_image_to_webp(
                                            filled_path,
                                            webp_out_path,
                                            quality=85,
                                            lossless=False,
                                            keep_metadata=True,
                                            method=6,
                                            icc_profile=True,
                                        )

                                        if success:
                                            st.session_state.unif_featured_webp[webp_key] = str(webp_out_path)
                                            st.rerun()
                                        else:
                                            st.error(f"Ошибка конвертации: {err}")
                                elif webp_path:
                                    st.success(f"✅ WebP: {Path(webp_path).name}")
                            
                            with col_btn2:
                                if webp_path and Path(webp_path).exists():
                                    # Copy path button with JavaScript
                                    # Make DOM ids depend on image_rev to avoid stale JS listeners after regeneration.
                                    safe_path = webp_path.replace(chr(92), chr(92) + chr(92))
                                    copy_html = f"""
                                    <div style=\"margin-top: 0px;\">
                                        <span style=\"display:none\">rev:{image_rev}</span>
                                        <button id=\"copy_btn_{idx}_{image_rev}\" 
                                                style=\"background-color: #ff4b4b; color: white; border: none; 
                                                       padding: 8px 16px; border-radius: 4px; cursor: pointer;
                                                       font-size: 14px; font-family: sans-serif;\">
                                            📋 Скопировать путь
                                        </button>
                                        <span id=\"copy_status_{idx}_{image_rev}\" style=\"margin-left: 10px; color: green;\"></span>
                                    </div>
                                    <script>
                                    (function() {{
                                        const btn = document.getElementById('copy_btn_{idx}_{image_rev}');
                                        const status = document.getElementById('copy_status_{idx}_{image_rev}');
                                        const path = '{safe_path}';

                                        if (!btn) return;

                                        // Ensure we don't accumulate multiple listeners across reruns
                                        btn.onclick = async function() {{
                                            try {{
                                                await navigator.clipboard.writeText(path);
                                                status.textContent = '✅ Скопировано!';
                                                setTimeout(() => {{ status.textContent = ''; }}, 2000);
                                            }} catch (err) {{
                                                status.textContent = '❌ Ошибка: ' + err.message;
                                            }}
                                        }};
                                    }})();
                                    </script>
                                    """
                                    # Some Streamlit versions don't support the `key` argument for components.html
                                    try:
                                        st.components.v1.html(copy_html, height=50, key=f"copy_html_{idx}_{image_rev}")
                                    except TypeError:
                                        st.components.v1.html(copy_html, height=50)
                            
                            with col_btn3:
                                # Regenerate featured image (available when we have an image)
                                if feat_prompt and st.button("🔄 Пересоздать промпт", key=f"regen_feat_prompt_{idx}"):
                                    _regen_featured_image_now()
                        else:
                            st.warning("Не сгенерировано")
                            # Even when generation failed, allow re-generation from the stored prompt.
                            if feat_prompt and st.button("🔄 Пересоздать промпт", key=f"regen_feat_prompt_{idx}"):
                                _regen_featured_image_now()
        else:
            st.info("Нет статей с featured image промптами")


# ---------------- Tab 1: Gemini Generate (defaults reused) ----------------
with tab1:
    st.subheader("Gemini generation via browser (Playwright)")

    # Выбор модели Gemini (отображается выбор перед автозаполнением промпта)
    model_choice = st.selectbox(
        "Модель Gemini для автозапросов",
        ["Быстрая", "Думающая"],
        index=0,
        key="unif_model_choice",
        help="Будет выбрана в интерфейсе перед вставкой промпта."
    )

    url = st.selectbox("URL интерфейса", DEFAULT_URLS, index=0, key="unif_url")
    headless = st.checkbox("Headless режим", value=False, key="unif_headless")
    use_auto_profile = st.checkbox("Отдельный профиль для автоматики (рекомендуется)", value=True, key="unif_auto_profile")
    # Поддержка обновления user-data-dir через временное состояние
    if "_tmp_new_uddir" in st.session_state:
        st.session_state["unif_user_data_dir"] = st.session_state.pop("_tmp_new_uddir")
    user_data_dir = st.text_input("Путь к профилю (user-data-dir)", value=st.session_state.get("unif_user_data_dir", os.path.abspath(".chrome_automation_profile")), key="unif_user_data_dir")
    resolved_user_data_dir = _normalize_user_data_dir(user_data_dir) or os.path.abspath(os.path.expanduser(user_data_dir))
    st.caption("Подсказка: по умолчанию используется .chrome_automation_profile. Можете дописать _1 (например, .chrome_automation_profile_1) для второго аккаунта.")
    st.caption(f"Реально используется: {resolved_user_data_dir}")
    # Используем абсолютный путь дальше по коду
    user_data_dir = resolved_user_data_dir

    # Если пользователь ввёл путь с опечаткой (часто: пропущен `\\` перед .chrome_automation_profile_*),
    # аккуратно «применяем» исправление через временный ключ и rerun.
    # Нельзя напрямую менять st.session_state['unif_user_data_dir'] после создания виджета.
    try:
        raw_input = (st.session_state.get("unif_user_data_dir") or "").strip()
        if raw_input and resolved_user_data_dir and _normalize_user_data_dir(raw_input) != resolved_user_data_dir:
            st.session_state["_tmp_new_uddir"] = resolved_user_data_dir
            st.rerun()
    except Exception:
        pass
    executable_path = st.text_input("Путь к chrome.exe (по умолчанию C:/Program Files/Google/Chrome/Application/chrome.exe)", value=r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", key="unif_exe_path")
    # CDP removed from Tab 1 (simplifies behavior and avoids confusion)
    use_cdp = False
    cdp_url = ""


    # Multiprompt UI
    if "unif_pw_prompts" not in st.session_state:
        st.session_state.unif_pw_prompts = [""]

    if st.session_state.get("unif_pw_imported_from_query"):
        st.success("Промпты импортированы из статьи (Tab 0) и вставлены в поля ниже")
        # Show only once
        st.session_state.unif_pw_imported_from_query = False

    st.markdown("**Промпты**")
    nprompts = []
    for i, val in enumerate(st.session_state.unif_pw_prompts):
        nv = st.text_input(f"Промпт #{i+1}", value=val, key=f"unif_pw_prompt_{i}")
        nprompts.append(nv)
    col_add, col_rem = st.columns([1,1])
    with col_add:
        if st.button("+ Добавить поле", key="unif_pw_add"):
            st.session_state.unif_pw_prompts.append("")
            st.rerun()
    with col_rem:
        if len(st.session_state.unif_pw_prompts) > 1 and st.button("− Убрать последнее", key="unif_pw_rem"):
            st.session_state.unif_pw_prompts = st.session_state.unif_pw_prompts[:-1]
            st.rerun()
    st.session_state.unif_pw_prompts = nprompts

    def _poll_regen_jobs():
        # Apply completed futures to session_state (must run in main thread)
        jobs = st.session_state.get("unif_regen_jobs", {})
        pending = st.session_state.get("unif_regen_pending", {})  # idx -> list[path]
        changed = False

        for job_id, job in list(jobs.items()):
            fut = job.get("future")
            if not fut or job.get("status") in {"done", "error"}:
                continue
            if fut.done():
                # Release reserved profile (if any)
                try:
                    reserved = job.get("reserved_profile_dir")
                    if reserved:
                        in_use = set(st.session_state.get("unif_regen_profile_in_use") or set())
                        if reserved in in_use:
                            in_use.remove(reserved)
                            st.session_state["unif_regen_profile_in_use"] = in_use
                except Exception:
                    pass

                try:
                    res = fut.result()
                    job["status"] = "done"
                    job["result"] = res
                    idx = int(res.get("idx"))
                    new_saved = res.get("new_saved") or []

                    # Batch mode: don't update UI images immediately.
                    # Store latest result per idx; final commit happens when ALL running jobs finish.
                    pending[str(idx)] = list(new_saved)
                    changed = True
                except Exception as e:
                    job["status"] = "error"
                    job["error"] = str(e)
                    changed = True

        st.session_state["unif_regen_jobs"] = jobs
        st.session_state["unif_regen_pending"] = pending

        # Prune finished jobs to avoid unbounded growth (otherwise each rerun becomes slower over time).
        # Keep running jobs + small tail of recent completed jobs for debugging.
        try:
            _keep_tail = 15
            _running = {k: v for k, v in (jobs or {}).items() if v.get("status") == "running"}
            if _running:
                # when running, keep everything (so we can still read results as they complete)
                pass
            else:
                _done = [(k, v) for k, v in (jobs or {}).items() if v.get("status") in {"done", "error"}]
                # sort by created_at if present
                def _key(it):
                    try:
                        return (it[1].get("created_at") or "")
                    except Exception:
                        return ""
                _done_sorted = sorted(_done, key=_key)
                _done_tail = dict(_done_sorted[-_keep_tail:])
                jobs = {**_done_tail}  # keep only tail
        except Exception:
            pass

        # If nothing is running anymore, commit all pending results at once.
        has_running = any(j.get("status") == "running" for j in (jobs or {}).values())
        if (not has_running) and pending:
            cur = st.session_state.get("unif_fast_saved_paths", [])
            cur_kept = list(cur)

            # Remove all idx that we are going to update
            for idx_s in list(pending.keys()):
                try:
                    idxi = int(idx_s)
                except Exception:
                    continue
                cur_kept = [p for p in cur_kept if _extract_prompt_idx(p) != idxi]

            # Add new files + cleanup old on disk per idx
            for idx_s, new_saved in list(pending.items()):
                try:
                    idxi = int(idx_s)
                except Exception:
                    continue
                try:
                    if new_saved:
                        _cleanup_old_prompt_files(os.path.dirname(new_saved[0]), idxi, list(new_saved))
                except Exception:
                    pass
                cur_kept.extend(list(new_saved))

            st.session_state.unif_fast_saved_paths = cur_kept
            # Keep global last-run list for other tabs (Photoshop/WebP)
            # NOTE: keep fast/pro saved paths separate to avoid Tab1 regen touching Tab2 (pro) outputs.
            # st.session_state.unif_saved_paths = cur_kept
            # Update displayed snapshot only once, after ALL regen jobs finished.
            st.session_state["unif_fast_display_saved_paths"] = list(cur_kept)
            st.session_state["unif_regen_pending"] = {}
            # a few extra reruns so images repaint (autorefresh will stop after counter reaches 0)
            st.session_state["unif_regen_dirty_counter"] = 3

    def _submit_regen_job(idx: int, force_sync: bool = False):
        # Enqueue a regeneration job; safe to call from button click
        with st.session_state["unif_regen_lock"]:
            st.session_state.unif_regen_job_seq += 1
            job_id = f"job_{st.session_state.unif_regen_job_seq}_{idx}"

        # Tab 1 regen should use fast prompts (placeholders depend on this list)
        final_prompts = st.session_state.get("unif_fast_final_prompts") or st.session_state.get("unif_final_prompts", [])
        if not final_prompts:
            st.error("Нет final_prompts. Сначала сгенерируйте картинки (кнопка '2) Сгенерировать все промпты').")
            return
        if idx < 1 or idx > len(final_prompts):
            st.error(f"Некорректный индекс промпта: {idx}")
            return

        # Tab 1: regeneration is always queued/parallel (non-blocking) to allow multiple clicks (multi-window style).
        regen_parallel = True
        if force_sync:
            regen_parallel = True
        # CDP removed from Tab 1, so no CDP gating here.

        if regen_parallel and not st.session_state.get("unif_auto_profile", True):
            st.error("Параллельная перегенерация требует user-data-dir. Включите 'Отдельный профиль для автоматики'.")
            return

        user_data_dir = st.session_state.get("unif_user_data_dir")
        if not user_data_dir:
            st.error("Не задан user-data-dir")
            return

        base_dir = st.session_state.get("unif_last_base_dir", _get_run_base_dir())
        url = st.session_state.get("unif_url", DEFAULT_URLS[0])
        headless = bool(st.session_state.get("unif_headless", False))
        executable_path = st.session_state.get("unif_exe_path") or None
        model_choice = st.session_state.get("unif_model_choice")
        prompt_text = final_prompts[idx - 1]

        # Regen prompt variation: prepend a rotating "polite" word to avoid sending identical prompt to Gemini
        # (Gemini may return identical images for identical prompts).
        try:
            prefixes = ["будь добр", "пожалуйста", "плиз", "please"]
            # remove existing prefix if present
            low = prompt_text.strip().lower()
            for pref in prefixes:
                if low.startswith(pref + " ") or low == pref:
                    prompt_text = prompt_text.strip()[len(pref):].lstrip()
                    break
            counts = st.session_state.get("unif_regen_prefix_counts", {})
            c = int(counts.get(str(idx), 0) or 0)
            pref = prefixes[c % len(prefixes)]
            counts[str(idx)] = c + 1
            st.session_state["unif_regen_prefix_counts"] = counts
            prompt_text = f"{pref} {prompt_text}".strip()
        except Exception:
            pass

        max_workers = int(st.session_state.get("unif_pw_parallelism", 3) or 3)
        max_workers = max(1, min(12, max_workers))

        # If parallel regen is disabled, run synchronously (old behavior) and update UI immediately.
        if not regen_parallel:
            try:
                # Determine output dir for this prompt idx (if possible)
                _saved = st.session_state.get("unif_fast_saved_paths", [])
                _dirs = [os.path.dirname(p) for p in _saved if _extract_prompt_idx(p) == idx]
                base_dir_eff = (_dirs[0] if _dirs else base_dir)

                # Close any existing persistent context to release Chrome profile lock before we re-launch it.
                try:
                    _ctx0 = st.session_state.get("unif_pw_ctx")
                    if _ctx0 is not None:
                        try:
                            _ctx0.close()
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    _pw0 = st.session_state.get("unif_pw_browser")
                    if _pw0 is not None:
                        try:
                            _pw0.stop()
                        except Exception:
                            pass
                except Exception:
                    pass
                st.session_state.unif_pw_ctx = None
                st.session_state.unif_pw_page = None
                st.session_state.unif_pw_browser = None

                # Prefer using already-open Playwright context/page from the main tab.
                # This avoids Chrome profile locks and Windows PermissionDenied on locked DB/cache files.
                # NOTE: We do NOT reuse Playwright objects saved in session_state here.
                # They are thread/greenlet-affine and may crash with:
                # "cannot switch to a different thread (which happens to have exited)".
                # Instead we regenerate using a fresh persistent context on the SAME user-data-dir.
                is_alive = False

                if is_alive:
                    try:
                        try:
                            cur_url = page_live.url or ""
                        except Exception:
                            cur_url = ""
                        if ("gemini.google.com" not in cur_url) and ("aistudio.google.com" not in cur_url):
                            try:
                                page_live.goto(url, wait_until="load")
                            except Exception:
                                pass

                        try:
                            _start_new_chat(page_live)
                        except Exception:
                            pass
                        _wait_input_ready(page_live, timeout_ms=30000)
                        _dismiss_overlays(page_live)

                        try:
                            if model_choice:
                                gph._pick_model(page_live, model_choice)
                        except Exception:
                            pass

                        _, new_saved = _regenerate_prompt(
                            page_live,
                            ctx_live,
                            prompt_text,
                            idx,
                            base_dir_eff,
                            max_images=1,
                            attach_base=True,
                            base_image_path=BASE_IMAGE_PATH,
                            model_choice=model_choice,
                        )
                    except Exception as e:
                        # Playwright objects are thread-affine. If the page/context were created in a different thread
                        # (e.g., during parallel generation), using them here may fail with:
                        # "cannot switch to a different thread (which happens to have exited)".
                        # In that case we silently fall back to an isolated Playwright run below.
                        msg = str(e)
                        if "cannot switch to a different thread" in msg:
                            is_alive = False
                        else:
                            st.error(f"Ошибка при перегенерации: {e}")
                            return

                    if is_alive:
                        # Update session_state list and return
                        if new_saved:
                            try:
                                deleted = _cleanup_old_prompt_files(os.path.dirname(new_saved[0]), idx, new_saved)
                            except Exception:
                                deleted = 0
                            cur = st.session_state.get("unif_fast_saved_paths", [])
                            new_list = [p for p in cur if _extract_prompt_idx(p) != idx]
                            new_list.extend(list(new_saved))
                            st.session_state.unif_fast_saved_paths = new_list
                            # st.session_state.unif_saved_paths = new_list
                            st.success(
                                f"Готово: пересоздано {len(new_saved)} файлов для промпта #{idx}. Удалено старых: {deleted}"
                            )
                        else:
                            st.warning("Перегенерация не вернула файлов")
                        return new_saved

                # Fallback: if there is no live page/context, try isolated run (may fail on Windows if profile is locked).
                result = {"new_saved": [], "error": None}

                def _regen_worker_sync():
                    try:
                        from playwright.sync_api import sync_playwright as _sp
                        p = _sp().start()
                        try:
                            # IMPORTANT: do not reuse the main user-data-dir here.
                            # If the main Tab already has a persistent context open, Chrome profile is locked and
                            # Playwright will fail with "Target page, context or browser has been closed".
                            # Use a lightweight temporary copy of the profile.
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            tmp_root = Path(f"tmp_rovodev_regen_sync_profile_{ts}_{idx}").resolve()

                            def _ignore_profile(dirpath: str, names: list[str]):
                                skip_exact = {
                                    "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
                                    "Crashpad", "Crash Reports",
                                }
                                skip_prefix = ("Singleton",)
                                ignored = []
                                for n in names:
                                    if n in skip_exact or any(n.startswith(p) for p in skip_prefix):
                                        ignored.append(n)
                                        continue
                                    if n.lower() in {"service worker", "serviceworker"}:
                                        ignored.append(n)
                                        continue
                                return ignored

                            # Use the real profile directory (same login/cookies). Before this worker runs we
                            # close any previous persistent context that might be holding a lock.

                            ctx_local = _launch_persistent_ctx_with_retries(
                                p,
                                user_data_dir=user_data_dir,
                                headless=headless,
                                executable_path=executable_path,
                            )
                            pg = ctx_local.new_page()

                            pg.set_default_timeout(30000)
                            try:
                                cur_url = pg.url or ""
                            except Exception:
                                cur_url = ""
                            if ("gemini.google.com" not in cur_url) and ("aistudio.google.com" not in cur_url):
                                try:
                                    pg.goto(url, wait_until="load")
                                except Exception:
                                    pass

                            try:
                                _start_new_chat(pg)
                            except Exception:
                                pass
                            _wait_input_ready(pg, timeout_ms=30000)
                            _dismiss_overlays(pg)

                            try:
                                if model_choice:
                                    gph._pick_model(pg, model_choice)
                            except Exception:
                                pass

                            _, ns = _regenerate_prompt(pg, ctx_local, prompt_text, idx, base_dir_eff, max_images=1, attach_base=True, base_image_path=BASE_IMAGE_PATH)
                            result["new_saved"] = ns or []
                        finally:
                            try:
                                p.stop()
                            except Exception:
                                pass
                            # no tmp profile to cleanup
                            pass
                    except Exception as e:
                        result["error"] = str(e)

                t = threading.Thread(target=_regen_worker_sync, daemon=True)
                t.start()
                t.join()

                if result["error"]:
                    st.error(f"Ошибка при перегенерации: {result['error']}")
                    return

                new_saved = result["new_saved"]
                if new_saved:
                    try:
                        deleted = _cleanup_old_prompt_files(os.path.dirname(new_saved[0]), idx, new_saved)
                    except Exception:
                        deleted = 0
                    cur = st.session_state.get("unif_fast_saved_paths", [])
                    new_list = []
                    for p in cur:
                        if _extract_prompt_idx(p) == idx:
                            continue
                        new_list.append(p)
                    new_list.extend(list(new_saved))
                    st.session_state.unif_fast_saved_paths = new_list
                    # st.session_state.unif_saved_paths = new_list
                    st.success(f"Готово: пересоздано {len(new_saved)} файлов для промпта #{idx}. Удалено старых: {deleted}")
                else:
                    st.warning("Перегенерация не вернула файлов")
                return new_saved
            except Exception as e:
                st.error(f"Ошибка при перегенерации: {e}")
                return

        # Parallel regen enabled: queue job(s)
        # Use at least 2 workers for regen, независимо от unif_pw_parallelism (иначе параллельность "ломается").
        # Regen parallelism follows the UI setting "Параллельно окон (concurrency)".
        # If you set concurrency=1, regen jobs will queue and run one-by-one.
        regen_workers = max(1, int(st.session_state.get("unif_pw_parallelism", 3) or 3))
        st.session_state["unif_regen_executor_target"] = regen_workers

        ex0 = st.session_state.get("unif_regen_executor")
        jobs0 = st.session_state.get("unif_regen_jobs", {})
        has_running0 = any(j.get("status") == "running" for j in (jobs0 or {}).values())

        if ex0 is None:
            st.session_state.unif_regen_executor = ThreadPoolExecutor(max_workers=regen_workers)
        else:
            try:
                cur_w = int(getattr(ex0, "_max_workers", regen_workers))
            except Exception:
                cur_w = regen_workers

            # Recreate executor if concurrency changed (up or down). Only safe when no running jobs.
            if cur_w != regen_workers and not has_running0:
                try:
                    ex0.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                st.session_state.unif_regen_executor = ThreadPoolExecutor(max_workers=regen_workers)

        # Clone the chosen profile to avoid Chrome profile-lock in parallel windows (same approach as multi-window).
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp_root = Path(f"tmp_rovodev_regen_profile_{ts}_{job_id}").resolve()

        def _ignore_profile(dirpath: str, names: list[str]):
            skip_exact = {
                "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
                "Crashpad", "Crash Reports",
                "DawnGraphiteCache", "DawnWebGPUCache", "GraphiteDawnCache",
            }
            skip_prefix = ("Singleton",)
            ignored = []
            for n in names:
                if n in skip_exact or any(n.startswith(p) for p in skip_prefix):
                    ignored.append(n)
                    continue
                if n.lower() in {"service worker", "serviceworker"}:
                    ignored.append(n)
                    continue
                if n.upper() == "LOCK" or n.lower() in {"lockfile", "devtoolsactiveport"}:
                    ignored.append(n)
                    continue
            return ignored

        # Worker: run regenerate in a dedicated profile.
        # If `profile_dir` is a numbered profile (direct), we do NOT delete it.
        # If it is a temporary clone, we clean it up.
        def _worker(profile_dir: str, *, is_temp_clone: bool):
            try:
                with sync_playwright() as p:
                    ctx = _launch_persistent_ctx_with_retries(
                        p,
                        user_data_dir=profile_dir,
                        headless=headless,
                        executable_path=executable_path,
                    )
                    try:
                        page = ctx.new_page()
                        page.set_default_timeout(30000)
                        page.goto(url, wait_until="load")
                        _debug_dom(page)
                        _wait_input_ready(page, timeout_ms=60000)

                        try:
                            if model_choice:
                                gph._pick_model(page, model_choice)
                        except Exception:
                            pass

                        imgs, new_saved = _regenerate_prompt(
                            page,
                            ctx,
                            prompt_text,
                            idx,
                            base_dir,
                            max_images=1,
                            attach_base=True,
                            base_image_path=BASE_IMAGE_PATH,
                        )
                        return {"idx": idx, "new_saved": new_saved, "imgs": imgs, "profile_dir": profile_dir}
                    finally:
                        try:
                            ctx.close()
                        except Exception:
                            pass
            finally:
                if is_temp_clone:
                    shutil.rmtree(profile_dir, ignore_errors=True)

        # Prefer numbered profiles for regen (round-robin). Fallback to a temporary clone if none available.
        reserved_profile_dir = _pick_regen_profile_round_robin()
        if reserved_profile_dir:
            fut = st.session_state.unif_regen_executor.submit(
                _worker,
                reserved_profile_dir,
                is_temp_clone=False,
            )
            profile_dir_for_job = reserved_profile_dir
        else:
            # Fallback: clone base profile (slower but always works)
            try:
                shutil.copytree(user_data_dir, Path(tmp_root), dirs_exist_ok=False, ignore=_ignore_profile)
            except Exception:
                # if copy fails, still try to run directly on the base profile
                pass
            fut = st.session_state.unif_regen_executor.submit(
                _worker,
                str(tmp_root),
                is_temp_clone=True,
            )
            profile_dir_for_job = str(tmp_root)

        st.session_state.unif_regen_jobs[job_id] = {
            "job_id": job_id,
            "idx": idx,
            "status": "running",
            "future": fut,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "profile_dir": profile_dir_for_job,
            "reserved_profile_dir": reserved_profile_dir,
        }

    # Poll completed regen jobs on every rerun
    _poll_regen_jobs()

    # If user changed concurrency during regen, rebuild executor after jobs finish.
    try:
        tgt = int(st.session_state.get("unif_regen_executor_target", 0) or 0)
        ex0 = st.session_state.get("unif_regen_executor")
        jobs0 = st.session_state.get("unif_regen_jobs", {})
        has_running0 = any(j.get("status") == "running" for j in (jobs0 or {}).values())
        if ex0 is not None and tgt > 0 and not has_running0:
            cur_w = int(getattr(ex0, "_max_workers", tgt))
            if cur_w != tgt:
                try:
                    ex0.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                st.session_state.unif_regen_executor = ThreadPoolExecutor(max_workers=tgt)
    except Exception:
        pass

    # Auto-rerun UI (NO page reload) while there are running regen jobs,
    # and a few extra reruns after completion to ensure images update.
    #
    # We use client-side postMessage timer because Streamlit won't rerun on background completion by itself.
    jobs = st.session_state.get("unif_regen_jobs", {})
    has_running = any(j.get("status") == "running" for j in (jobs or {}).values())
    dirty_counter = int(st.session_state.get("unif_regen_dirty_counter", 0) or 0)

    # Decrease counter once jobs are no longer running
    if (not has_running) and dirty_counter > 0:
        st.session_state["unif_regen_dirty_counter"] = dirty_counter - 1
        dirty_counter = dirty_counter - 1

    # Client-side autorefresh while jobs are running.
    #
    # IMPORTANT: `components.html` runs inside a sandboxed iframe (origin "null"), so it cannot reliably
    # trigger Streamlit reruns via postMessage in all environments. The `streamlit-autorefresh` component
    # triggers reruns from the main app and works reliably.
    pending = st.session_state.get("unif_regen_pending", {})
    if has_running or dirty_counter > 0 or bool(pending):
        try:
            from streamlit_autorefresh import st_autorefresh

            # Reduce visible "flicker" during long-running regen: poll slower while running, fast after finish.
            interval = 4000 if has_running else 1000
            st_autorefresh(interval=interval, key="unif_regen_autorefresh")
        except Exception:
            st.warning(
                "Автообновление результатов перегенерации недоступно (не установлен streamlit-autorefresh). "
                "Установите: pip install streamlit-autorefresh. Пока что обновление появится после любого клика в UI."
            )

    with st.sidebar:
        st.markdown("### Базовое изображение (белое)")
        if os.path.exists(BASE_IMAGE_PATH):
            st.image(BASE_IMAGE_PATH, caption=BASE_IMAGE_PATH, use_container_width=True)
        else:
            st.error(f"Базовый файл не найден: {BASE_IMAGE_PATH}")
        # Блок перегенерации как в оригинале — использует session_state
        st.markdown("---")
        st.markdown("#### Перегенерация")

        st.caption("Перегенерация работает параллельно по умолчанию: кнопки 'Пересоздать промпт' ставят задачи в очередь и открывают отдельные окна (копии профиля).")
        st.session_state["unif_regen_parallel"] = True
        # Сайдбар-перегенерацию убрали: используйте кнопки "Пересоздать промпт #..." под каждой картинкой в табе.
        st.caption("Перегенерация доступна под каждой картинкой в основной области (кнопки 'Пересоздать промпт').")

    st.markdown("---")
    st.markdown("**Параллельная генерация (несколько окон)**")
    st.caption(
        "Важно: один и тот же Chrome user-data-dir нельзя открыть параллельно в нескольких окнах. "
        "Поэтому для параллельности используются временные *копии* выбранного профиля. "
        "Логин/куки обычно сохраняются, но если ваш профиль очень большой — копирование может занять время."
    )
    pw_parallel = st.checkbox(
        "Включить параллельную генерацию (multi-window)",
        value=False,
        key="unif_pw_parallel",
    )
    pw_use_numbered_profiles = st.checkbox(
        "Использовать профили .chrome_automation_profile_1..N (быстро, без копирования)",
        value=True,
        key="unif_pw_use_numbered_profiles",
        help=(
            "Если включено, то для Prompt #1 будет использован .chrome_automation_profile_1, "
            "для Prompt #2 — .chrome_automation_profile_2 и т.д. Это быстрее, чем копировать профиль. "
            "Если какого-то профиля не хватает — для него будет создана временная копия (fallback)."
        ),
    )
    pw_parallelism = st.number_input(
        "Параллельно окон (concurrency)",
        min_value=1,
        max_value=12,
        value=3,
        step=1,
        key="unif_pw_parallelism",
        help="Сколько окон запускать одновременно."
    )

    colA, colB = st.columns([1,1])
    with colA:
        open_btn = st.button("1) Открыть/подключиться (Playwright)", type="secondary", key="unif_open")
    with colB:
        go_btn = st.button("2) Сгенерировать все промпты", type="primary", key="unif_generate")

    if "unif_pw_ctx" not in st.session_state:
        st.session_state.unif_pw_ctx = None
        st.session_state.unif_pw_browser = None
        st.session_state.unif_pw_page = None
        st.session_state.unif_prev_resp_count_pw = 0

    # Parallel regeneration job manager (Tab 1)
    st.session_state.setdefault("unif_regen_jobs", {})  # job_id -> dict
    st.session_state.setdefault("unif_regen_job_seq", 0)
    st.session_state.setdefault("unif_regen_executor", None)
    st.session_state.setdefault("unif_regen_lock", threading.Lock())
    # Snapshot of displayed results to avoid UI "jumping" during autorefresh reruns
    st.session_state.setdefault(
        "unif_fast_display_saved_paths",
        list(st.session_state.get("unif_fast_saved_paths", []) or []),
    )
    st.session_state.setdefault("unif_regen_profile_in_use", set())
    st.session_state.setdefault("unif_regen_rr_idx", 0)

    # Profile pool for parallel regeneration: use pre-created profiles (.chrome_automation_profile_1, _2, ...)
    if "unif_regen_profile_pool" not in st.session_state:
        profs = []
        try:
            for name in sorted(os.listdir(".")):
                if name.startswith(".chrome_automation_profile_") and os.path.isdir(name):
                    profs.append(str(Path(name).resolve()))
        except Exception:
            profs = []
        st.session_state["unif_regen_profile_pool"] = profs

    def _pick_regen_profile_round_robin() -> str | None:
        """Pick a numbered profile for regen jobs.

        Uses round-robin ordering and respects `unif_regen_profile_in_use` so the same
        user-data-dir is never used concurrently.

        Returns profile_dir or None if no suitable profile is available.
        """
        try:
            pool = list(st.session_state.get("unif_regen_profile_pool") or [])
            if not pool:
                # Re-scan lazily
                try:
                    pool = []
                    for name in sorted(os.listdir(".")):
                        if name.startswith(".chrome_automation_profile_") and os.path.isdir(name):
                            pool.append(str(Path(name).resolve()))
                    st.session_state["unif_regen_profile_pool"] = pool
                except Exception:
                    pool = []
            if not pool:
                return None

            in_use = set(st.session_state.get("unif_regen_profile_in_use") or set())
            start = int(st.session_state.get("unif_regen_rr_idx", 0) or 0)
            for off in range(len(pool)):
                i = (start + off) % len(pool)
                cand = pool[i]
                if cand in in_use:
                    continue
                # reserve
                in_use.add(cand)
                st.session_state["unif_regen_profile_in_use"] = in_use
                st.session_state["unif_regen_rr_idx"] = (i + 1) % len(pool)
                return cand
            return None
        except Exception:
            return None

    def _close_unif_pw_handles():
        """Best-effort cleanup for the single-window Playwright session stored in session_state."""
        try:
            ctx0 = st.session_state.get("unif_pw_ctx")
            if ctx0 is not None:
                try:
                    ctx0.close()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            pw0 = st.session_state.get("unif_pw_browser")
            if pw0 is not None:
                try:
                    pw0.stop()
                except Exception:
                    pass
        except Exception:
            pass
        st.session_state.unif_pw_ctx = None
        st.session_state.unif_pw_page = None
        st.session_state.unif_pw_browser = None

    # Open/connect
    if open_btn:
        try:
            # If user clicks Open multiple times, do not keep stacking Chrome instances.
            _close_unif_pw_handles()
            if use_auto_profile:
                os.makedirs(user_data_dir, exist_ok=True)
            playwright = sync_playwright().start()
            browser = _launch_persistent_ctx_with_retries(
                playwright,
                user_data_dir=user_data_dir if use_auto_profile else None,
                headless=headless,
                executable_path=executable_path or None,
            )
            page = browser.new_page()
            page.set_default_timeout(30000)
            page.goto(url, wait_until="load")
            st.session_state.unif_pw_ctx = browser
            st.session_state.unif_pw_browser = playwright
            st.session_state.unif_pw_page = page
            try:
                _wait_input_ready(page, timeout_ms=10000)
                st.success("Поле ввода найдено. Можно сразу нажимать '2) Сгенерировать все промпты'.")
            except Exception:
                st.info("Если требуется — войдите в аккаунт Google в открывшемся окне, затем нажмите '2) Сгенерировать все промпты'.")
            st.session_state.unif_prev_resp_count_pw = _count_responses(page)
        except Exception as e:
            st.error(f"Ошибка при открытии: {e}")

    # Generate
    if go_btn:
        try:
            # If we are going to run non-parallel generation, make sure we don't leak old windows.
            # (parallel mode uses isolated contexts per worker and cleans them up itself)
            if not bool(st.session_state.get("unif_pw_parallel", False)):
                _close_unif_pw_handles()

            # Snapshot prompts first
            pre = "Please Change the white 10:16 ratio image using this Prompt: "
            post = "PLEASE DO NOT LEAVE BLANK WHITE SPACE, THIS IS IMPORTANT "
            final_prompts = [f"{pre}{_sanitize_prompt((p or '').strip())}{post}" for p in st.session_state.unif_pw_prompts if (p or '').strip()]

            if pw_parallel:
                if not use_auto_profile:
                    st.error("Параллельный режим требует выбранный user-data-dir. Включите 'Отдельный профиль для автоматики'.")
                    raise RuntimeError("Parallel mode requires user-data-dir")
                if not final_prompts:
                    st.warning("Нет заполненных промптов")
                    raise RuntimeError("No prompts")
                if not os.path.exists(BASE_IMAGE_PATH):
                    st.error(f"Файл {BASE_IMAGE_PATH} не найден")
                    raise RuntimeError("Missing base image")

                base_dir = _get_run_base_dir()
                try:
                    os.makedirs(base_dir, exist_ok=True)
                except Exception:
                    pass
                st.session_state.unif_last_base_dir = str(_PathAlias(base_dir).resolve())

                max_workers = int(st.session_state.get("unif_pw_parallelism", 3) or 3)
                max_workers = max(1, min(12, max_workers, len(final_prompts)))

                # Prefer using existing numbered profiles (.chrome_automation_profile_1..N) to avoid slow copying.
                # Fallback: if a numbered profile is missing, we create a lightweight temp copy for that slot.
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                tmp_root = Path(f"tmp_rovodev_pw_profiles_{ts}").resolve()
                tmp_root.mkdir(parents=True, exist_ok=True)

                def _ignore_profile(dirpath: str, names: list[str]):
                    skip_exact = {
                        "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
                        "Crashpad", "Crash Reports",
                    }
                    skip_prefix = ("Singleton",)
                    ignored = []
                    for n in names:
                        if n in skip_exact or any(n.startswith(p) for p in skip_prefix):
                            ignored.append(n)
                            continue
                        if n.lower() in {"service worker", "serviceworker"}:
                            ignored.append(n)
                            continue
                    return ignored

                # NOTE: We don't pre-create copies anymore.
                # In parallel mode, each prompt runs in its own profile (.chrome_automation_profile_1..N).
                # If a numbered profile is missing, we will clone the base profile on-demand for that prompt.

                import concurrent.futures

                # Profile pool: only use slots 1..max_workers. This allows reusing profile_1/profile_2/... when
                # you have more prompts than concurrency (e.g. 3 prompts with concurrency=2).
                profile_pool: queue.Queue = queue.Queue()

                def _prepare_profile_slot(slot_idx: int) -> str:
                    # Try numbered profile
                    if bool(st.session_state.get("unif_pw_use_numbered_profiles", True)):
                        cand = Path(f".chrome_automation_profile_{slot_idx}")
                        if cand.exists() and cand.is_dir():
                            return str(cand.resolve())
                    # Fallback: clone base profile ONCE per slot
                    dst = tmp_root / f"slot_{slot_idx}"
                    if dst.exists():
                        shutil.rmtree(dst, ignore_errors=True)
                    shutil.copytree(user_data_dir, dst, dirs_exist_ok=False, ignore=_ignore_profile)
                    return str(dst)

                for _slot in range(1, max_workers + 1):
                    profile_pool.put(_prepare_profile_slot(_slot))

                def _run_one_prompt(prompt_idx: int, fp: str, orig_prompt: str):
                    """Run a single prompt in a dedicated Chrome profile slot.

                    Important behavior: if the window/page gets closed prematurely (common flaky case on Windows)
                    or if we time out without images, we retry by re-opening a fresh persistent context for the
                    SAME prompt (and same profile slot) instead of silently moving on to the next prompt.
                    """

                    local_saved: list[str] = []
                    local_errors: list[str] = []
                    profile_dir = None

                    def _is_retryable_error(msg: str) -> bool:
                        m = (msg or "").lower()
                        return any(
                            k in m
                            for k in [
                                "target page, context or browser has been closed",
                                "has been closed",
                                "browser has disconnected",
                                "browser.getwindowfortarget",
                                "timeout",
                                "timed out",
                            ]
                        )

                    try:
                        # Take a free profile slot, run the prompt, then return it to the pool.
                        profile_dir = profile_pool.get()

                        # Retry full browser/session for this prompt if Chrome window closes too early.
                        max_session_attempts = 2
                        for session_attempt in range(1, max_session_attempts + 1):
                            try:
                                with sync_playwright() as p:
                                    ctx = _launch_persistent_ctx_with_retries(
                                        p,
                                        user_data_dir=profile_dir,
                                        headless=headless,
                                        executable_path=executable_path or None,
                                    )
                                    try:
                                        page = ctx.new_page()
                                        page.set_default_timeout(30000)
                                        page.goto(url, wait_until="load")
                                        _debug_dom(page)
                                        _wait_input_ready(page, timeout_ms=60000)

                                        try:
                                            _start_new_chat(page)
                                        except Exception:
                                            pass

                                        imgs = []
                                        for _attempt in range(1, 4):
                                            ok = _attach_image(page, BASE_IMAGE_PATH)
                                            attached_preview = _wait_image_attached(page, timeout_ms=5000)
                                            if not ok or not attached_preview:
                                                continue
                                            _dismiss_overlays(page)
                                            try:
                                                if model_choice:
                                                    gph._pick_model(page, model_choice)
                                            except Exception:
                                                pass
                                            try:
                                                _type_prompt(page, fp)
                                            except Exception as e:
                                                raise RuntimeError(f"Prompt insert failed: {e}")
                                            _click_send(page)

                                            # Main wait. If Gemini is slow, we don't kill the whole run; we'll retry
                                            # this prompt by reopening the context (see outer session_attempt loop).
                                            imgs = _wait_and_download_generated_images(page, ctx, timeout_s=180, max_images=6)
                                            if not imgs and _has_generated_images(page):
                                                imgs = _wait_and_download_generated_images(page, ctx, timeout_s=25, max_images=6)
                                            if imgs:
                                                break

                                        if not imgs:
                                            raise RuntimeError("No images were collected")

                                        import re as _re
                                        import hashlib as _hashlib

                                        base_slug = _re.sub(r"[^a-zA-Z0-9_-]+", "_", (orig_prompt or "").strip())
                                        base_slug = _re.sub(r"_+", "_", base_slug).strip("_")
                                        if not base_slug:
                                            base_slug = f"prompt_{prompt_idx}"
                                        MAX_BASENAME = 110
                                        prefix_len = len(str(prompt_idx)) + 1
                                        suffix_len = 1 + 2
                                        allowed_slug_len = max(1, MAX_BASENAME - prefix_len - suffix_len)
                                        slug = base_slug[:allowed_slug_len]

                                        unique_imgs = []
                                        seen_hash = set()
                                        seen_phashes: list[int] = []

                                        def _phash64(_blob: bytes) -> int | None:
                                            try:
                                                from PIL import Image
                                                import io as _io

                                                img = Image.open(_io.BytesIO(_blob)).convert("L").resize((8, 8))
                                                pixels = list(img.getdata())
                                                avg = sum(pixels) / len(pixels)
                                                bits = 0
                                                for ii, pxx in enumerate(pixels):
                                                    if pxx >= avg:
                                                        bits |= (1 << ii)
                                                return bits
                                            except Exception:
                                                return None

                                        def _hamming(a: int, b: int) -> int:
                                            return (a ^ b).bit_count()

                                        for (mime, blob) in imgs or []:
                                            # Exact bytes hash
                                            try:
                                                h = _hashlib.sha256(blob).hexdigest()
                                            except Exception:
                                                h = None
                                            if h and h in seen_hash:
                                                continue

                                            # Perceptual hash to catch near-duplicates
                                            ph = _phash64(blob)
                                            if ph is not None:
                                                if any(_hamming(ph, prev) <= 2 for prev in seen_phashes):
                                                    continue

                                            if h:
                                                seen_hash.add(h)
                                            if ph is not None:
                                                seen_phashes.append(ph)

                                            unique_imgs.append((mime, blob))

                                        for j, (mime, blob) in enumerate(unique_imgs, 1):
                                            ext = "png" if mime == "image/png" else ("jpg" if mime == "image/jpeg" else "bin")
                                            fname = f"{prompt_idx}_{slug}_{j:02d}.{ext}"
                                            fpath = os.path.join(base_dir, fname)
                                            with open(fpath, "wb") as f:
                                                f.write(blob)
                                            local_saved.append(fpath)

                                        # Success
                                        break
                                    finally:
                                        try:
                                            ctx.close()
                                        except Exception:
                                            pass
                            except Exception as e:
                                # If this was a known flaky/timeout case, retry by reopening context.
                                msg = str(e)
                                if session_attempt < max_session_attempts and _is_retryable_error(msg):
                                    local_errors.append(
                                        f"Prompt #{prompt_idx}: attempt {session_attempt} failed ({msg}); retrying with a fresh window"
                                    )
                                    continue
                                local_errors.append(f"Prompt #{prompt_idx}: {e}")
                                break

                    finally:
                        # Return profile slot back to the pool for the next prompt
                        try:
                            if profile_dir:
                                profile_pool.put(profile_dir)
                        except Exception:
                            pass

                    return {"idx": prompt_idx, "saved": local_saved, "errors": local_errors}

                # Prepare prompt list (preserve indexing)
                orig_prompts = [((p or "").strip()) for p in (st.session_state.unif_pw_prompts or []) if (p or "").strip()]
                tasks = []
                for idx, fp in enumerate(final_prompts, 1):
                    op = orig_prompts[idx - 1] if idx - 1 < len(orig_prompts) else ""
                    tasks.append((idx, fp, op))

                # Inform user if some numbered profiles are missing (we'll fall back to cloning for them)
                if bool(st.session_state.get("unif_pw_use_numbered_profiles", True)):
                    missing = []
                    for slot_idx in range(1, max_workers + 1):
                        cand = Path(f".chrome_automation_profile_{slot_idx}")
                        if not (cand.exists() and cand.is_dir()):
                            missing.append(slot_idx)
                    if missing:
                        st.warning(
                            "Не найдены профили: "
                            + ", ".join([f".chrome_automation_profile_{i}" for i in missing])
                            + ". Для них будут созданы временные копии (медленнее)."
                        )

                status = st.empty()
                progress = st.progress(0)
                done = 0
                total = len(tasks)
                all_saved: list[str] = []
                all_errors: list[str] = []

                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                    futs = [ex.submit(_run_one_prompt, idx, fp, op) for (idx, fp, op) in tasks]
                    for fut in concurrent.futures.as_completed(futs):
                        r = fut.result() or {}
                        all_saved.extend(r.get("saved") or [])
                        all_errors.extend(r.get("errors") or [])
                        done += 1
                        progress.progress(min(1.0, done / max(1, total)))

                progress.progress(1.0)
                status.write("Готово")

                # Cleanup only temporary cloned profiles
                with st.spinner("Удаляю временные копии профиля..."):
                    shutil.rmtree(tmp_root, ignore_errors=True)

                if all_errors:
                    st.error("Ошибки:\n" + "\n".join(all_errors))

                # Persist for sidebar regeneration
                st.session_state.unif_fast_saved_paths = list(dict.fromkeys(all_saved))
                # st.session_state.unif_saved_paths = list(dict.fromkeys(all_saved))  # keep separate fast/pro lists
                st.session_state.unif_final_prompts = final_prompts
                # Tab 1 UI uses this to show placeholders + regen buttons for missing prompt indices
                st.session_state.unif_fast_final_prompts = final_prompts
                if all_saved:
                    st.success(f"Сохранено файлов: {len(all_saved)} в папку: {base_dir}")
                    try:
                        st.session_state.ps_src_dir = st.session_state.unif_last_base_dir
                    except Exception:
                        pass
                else:
                    st.warning("Не удалось сохранить файлы")

            else:
                # Non-parallel: use a single persistent context and keep it in session_state for later regeneration.
                # IMPORTANT: this block must stay inside the `else` branch; otherwise a new window can be opened
                # on each rerun and performance degrades over time.
                playwright = sync_playwright().start()
                browser = _launch_persistent_ctx_with_retries(
                    playwright,
                    user_data_dir=user_data_dir if use_auto_profile else None,
                    headless=headless,
                    executable_path=executable_path or None,
                )
                ctx = browser
                page = browser.new_page()
                page.set_default_timeout(30000)
                # Persist handles in session_state for later 'Пересоздать'
                st.session_state.unif_pw_ctx = ctx
                st.session_state.unif_pw_page = page
                st.session_state.unif_pw_browser = playwright
                page.goto(url, wait_until="load")
                _debug_dom(page)
                _wait_input_ready(page, timeout_ms=60000)

            pre = "Please Change the white 10:16 ratio image using this Prompt: "
            post = "PLEASE DO NOT LEAVE BLANK WHITE SPACE, THIS IS IMPORTANT "
            final_prompts = [f"{pre}{_sanitize_prompt((p or '').strip())}{post}" for p in st.session_state.unif_pw_prompts if (p or '').strip()]

            base_dir = _get_run_base_dir()
            try:
                os.makedirs(base_dir, exist_ok=True)
            except Exception:
                pass
            # Обновляем unif_last_base_dir сразу на новый абсолютный путь
            st.session_state.unif_last_base_dir = str(_PathAlias(base_dir).resolve())

            progress = st.progress(0)
            status = st.empty()
            saved_paths: List[str] = []

            for idx, fp in enumerate(final_prompts, 1):
                try:
                    _start_new_chat(page)
                except Exception:
                    pass
                if not os.path.exists(BASE_IMAGE_PATH):
                    st.error(f"Файл {BASE_IMAGE_PATH} не найден")
                    break

                imgs = []
                err = None

                for attempt in range(1, 3):
                    ok = _attach_image(page, BASE_IMAGE_PATH)
                    attached_preview = _wait_image_attached(page, timeout_ms=5000)
                    if not ok or not attached_preview:
                        continue
                    _dismiss_overlays(page)
                    # Попробовать выбрать модель из UI
                    try:
                        mc = st.session_state.get('unif_model_choice')
                        if mc:
                            gph._log(f"[menu] (main) вызываю _pick_model: {mc}", force=True)
                            picked = gph._pick_model(page, mc)
                            gph._log(f"[menu] (main) результат _pick_model: {picked}", force=True)
                    except Exception as e:
                        gph._log(f"[menu] (main) ошибка _pick_model: {e}", force=True)
                    try:
                        _type_prompt(page, fp)
                    except Exception as e:
                        err = f"Prompt insert failed: {e}"
                        continue
                    _click_send(page)
                    imgs = _wait_and_download_generated_images(page, ctx, timeout_s=150, max_images=6)
                    if not imgs and _has_generated_images(page):
                        imgs = _wait_and_download_generated_images(page, ctx, timeout_s=15, max_images=6)
                    if imgs:
                        break

                # Save images to base_dir with a prompt slug
                # Build slug from the original prompt (without service pre/post), prefixed by prompt index like "1_..."
                import re as _re
                try:
                    orig_prompt = (st.session_state.unif_pw_prompts[idx-1] or "").strip()
                except Exception:
                    orig_prompt = (fp or "").strip()
                # Build safe slug: alnum, dash, underscore; collapse repeats; trim
                base_slug = _re.sub(r"[^a-zA-Z0-9_-]+", "_", orig_prompt)
                base_slug = _re.sub(r"_+", "_", base_slug).strip("_")
                if not base_slug:
                    base_slug = f"prompt_{idx}"
                # Compute a max basename length that leaves room for later "_filled" and extension
                # We target basename (without extension) up to 200 chars: <idx>_<slug>_<nn>
                MAX_BASENAME = 110
                prefix_len = len(str(idx)) + 1  # "{idx}_"
                suffix_len = 1 + 2  # "_" + 2 digits
                allowed_slug_len = max(1, MAX_BASENAME - prefix_len - suffix_len)
                slug = base_slug[:allowed_slug_len]
                # De-duplicate identical or near-identical images
                import hashlib as _hashlib
                unique_imgs = []
                _seen_hashes = set()
                _seen_phashes = []  # list of int bitmasks (perceptual hashes)
                def _phash64(_blob: bytes) -> int | None:
                    try:
                        from PIL import Image
                        import io as _io
                        img = Image.open(_io.BytesIO(_blob)).convert("L").resize((8,8))
                        pixels = list(img.getdata())
                        avg = sum(pixels) / len(pixels)
                        bits = 0
                        for i, p in enumerate(pixels):
                            if p >= avg:
                                bits |= (1 << i)
                        return bits
                    except Exception:
                        return None
                def _hamming(a: int, b: int) -> int:
                    return (a ^ b).bit_count()
                for (mime, blob) in imgs:
                    # First, exact bytes hash
                    h = None
                    try:
                        h = _hashlib.sha256(blob).hexdigest()
                    except Exception:
                        pass
                    if h and h in _seen_hashes:
                        continue
                    # Then, perceptual hash to catch near-duplicates
                    ph = _phash64(blob)
                    if ph is not None:
                        is_dup = any(_hamming(ph, prev) <= 2 for prev in _seen_phashes)
                        if is_dup:
                            continue
                    # Accept
                    if h:
                        _seen_hashes.add(h)
                    if ph is not None:
                        _seen_phashes.append(ph)
                    unique_imgs.append((mime, blob))

                for j, (mime, blob) in enumerate(unique_imgs, 1):
                    ext = "png" if mime == "image/png" else ("jpg" if mime == "image/jpeg" else "bin")
                    fname = f"{idx}_{slug}_{j:02d}.{ext}"
                    fpath = os.path.join(base_dir, fname)
                    try:
                        with open(fpath, "wb") as f:
                            f.write(blob)
                        saved_paths.append(fpath)
                    except Exception as e:
                        st.error(f"Ошибка сохранения {fname}: {e}")

                progress.progress(int(idx/ max(1, len(final_prompts)) * 100))
                status.text(f"{idx}/{len(final_prompts)} prompts processed…")

            if saved_paths:
                # Ensure unique file paths (avoid duplicates in UI)
                _seen_paths = set()
                _unique_saved = []
                for _p in saved_paths:
                    if _p not in _seen_paths:
                        _unique_saved.append(_p)
                        _seen_paths.add(_p)
                st.success(f"Сохранено файлов: {len(_unique_saved)} в папку: {base_dir}")
                # Сохраняем в session_state, чтобы не пропадало после перерендера
                st.session_state.unif_fast_saved_paths = _unique_saved
                # st.session_state.unif_saved_paths = _unique_saved  # keep separate fast/pro lists
                # Keep display snapshot in sync for initial generation output
                st.session_state.unif_fast_display_saved_paths = list(_unique_saved)
                st.session_state.unif_last_base_dir = str(_PathAlias(base_dir).resolve())
                st.session_state.unif_final_prompts = final_prompts
                # Tab 1 UI uses this to show placeholders + regen buttons for missing prompt indices
                st.session_state.unif_fast_final_prompts = final_prompts
                # Обновляем дефолт для Photoshop вкладки, чтобы она показывала именно текущую генерацию
                try:
                    st.session_state.ps_src_dir = st.session_state.unif_last_base_dir
                except Exception:
                    pass
            else:
                st.warning("Не удалось сохранить изображения. Проверьте логин/генерацию.")
        except Exception as e:
            # Best-effort cleanup for parallel Tab 1 temp profiles if we failed before reaching cleanup
            try:
                _tr = locals().get("tmp_root")
                if _tr and isinstance(_tr, Path) and _tr.name.startswith("tmp_rovodev_pw_profiles_"):
                    shutil.rmtree(_tr, ignore_errors=True)
            except Exception:
                pass
            st.error(f"Ошибка генерации: {e}")

    # Render a stable snapshot during autorefresh so the results section doesn't "jump" every second.
    _disp = st.session_state.get("unif_fast_display_saved_paths")
    saved_paths = _disp if _disp else st.session_state.get("unif_fast_saved_paths", [])
    base_dir = st.session_state.get("unif_last_base_dir", _get_run_base_dir())

    # Regen status (used to tune autorefresh rate)
    jobs0 = st.session_state.get("unif_regen_jobs", {})
    has_running0 = any(j.get("status") == "running" for j in (jobs0 or {}).values())

    final_prompts_snapshot = (st.session_state.get("unif_fast_final_prompts") or st.session_state.get("unif_final_prompts") or [])
    if saved_paths or final_prompts_snapshot:
        with st.expander(
            "Показать сохранённые файлы (кликните для раскрытия)",
            expanded=True,
        ):
            # Group existing saved files by prompt index
            by_prompt: dict[int, list[str]] = {}
            for p in (saved_paths or []):
                idxp = _extract_prompt_idx(p)
                if idxp is None:
                    by_prompt.setdefault(0, []).append(p)
                else:
                    by_prompt.setdefault(int(idxp), []).append(p)

            # Expected prompt indices are driven by final_prompts, so we can show "empty slots"
            # for prompts that did not produce images (e.g., browser closed / generation failed).
            expected_n = len(final_prompts_snapshot)
            indices: list[int]
            if expected_n > 0:
                indices = list(range(1, expected_n + 1))
            else:
                # Fallback: if we don't have prompts, show whatever we can infer from filenames
                indices = sorted(k for k in by_prompt.keys() if k != 0)

            for idx in indices:
                st.write(f"Промпт #{idx}")

                paths_for_idx = sorted(by_prompt.get(idx, []))
                cols = st.columns(4)

                if paths_for_idx:
                    for i, pth in enumerate(paths_for_idx):
                        with cols[i % 4]:
                            slot = st.empty()
                            try:
                                try:
                                    _mtime = os.stat(pth).st_mtime_ns
                                except Exception:
                                    _mtime = None
                                with open(pth, "rb") as _f:
                                    _b = _f.read()
                                slot.image(
                                    _b,
                                    caption=f"{os.path.basename(pth)}" + (f" (v={_mtime})" if _mtime else ""),
                                    use_container_width=True,
                                )
                            except Exception:
                                slot.write(os.path.basename(pth))
                else:
                    # Keep a visible placeholder so the prompt isn't "lost" in UI.
                    with cols[0]:
                        st.warning("Нет результата (картинка не была сохранена)")

                # Button is always available, even if there is no image
                if st.button(f"Пересоздать промпт #{idx}", key=f"regen_inline_{idx}"):
                    # Non-blocking: enqueue regeneration job (parallel windows via cloned profile)
                    st.session_state["unif_regen_last_user_action"] = time.time()
                    _submit_regen_job(idx)
                    # Start/ensure autorefresh is armed in this same run (without forcing an immediate rerun).
                    try:
                        from streamlit_autorefresh import st_autorefresh
                        st_autorefresh(interval=1000, key="unif_regen_autorefresh")
                    except Exception:
                        pass
                    st.info("Задача перегенерации поставлена в очередь…")

            # Show files with unknown prompt index (if any)
            unknown_paths = sorted(by_prompt.get(0, []))
            if unknown_paths:
                st.markdown("---")
                st.write("Файлы без распознанного номера промпта")
                cols = st.columns(4)
                for i, pth in enumerate(unknown_paths):
                    with cols[i % 4]:
                        try:
                            with open(pth, "rb") as _f:
                                _b = _f.read()
                            st.image(_b, caption=os.path.basename(pth), use_container_width=True)
                        except Exception:
                            st.write(os.path.basename(pth))
                if False:
                    try:
                        playwright = sync_playwright().start()
                        # Жёсткий сценарий: полностью закрываем существующий браузер CDP, затем поднимаем новый
                        exe_state = st.session_state.get("unif_exe_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
                        udd_state = st.session_state.get("unif_user_data_dir", r"C:\\temp\\chrome-debug")
                        cdp_url_state = st.session_state.get("unif_cdp_url", "http://127.0.0.1:9222")

                        def _is_cdp_up(url: str) -> bool:
                            try:
                                with urllib.request.urlopen(url + "/json/version", timeout=1) as resp:
                                    return resp.status == 200
                            except Exception:
                                return False

                        # DEPRECATED: старые попытки перезапуска браузера удалены. Дальше используем только текущую сессию как в оригинале.
                        # В Streamlit колбэки выполняются в другом потоке, поэтому объекты Playwright
                        # из предыдущего шага использовать нельзя. Подключаемся заново к CDP и забираем существующую вкладку Gemini.
                        # Выполняем всю работу перегенерации в отдельном потоке с отдельным sync Playwright,
                        # чтобы избежать конфликтов с asyncio event loop Streamlit.
                        exe_state = st.session_state.get("unif_exe_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
                        udd_state = st.session_state.get("unif_user_data_dir", r"C:\\temp\\chrome-debug")
                        cdp_url_state = st.session_state.get("unif_cdp_url", "http://127.0.0.1:9222")

                        final_prompts = st.session_state.get("unif_final_prompts", [])
                        if 1 <= idx <= len(final_prompts):
                            pt = final_prompts[idx-1]
                        else:
                            pt = final_prompts[0] if final_prompts else ""
                        base_dir_state = st.session_state.get("unif_last_base_dir", _get_run_base_dir())

                        result = {"new_saved": [], "error": None}

                        def _regen_worker():
                            try:
                                from playwright.sync_api import sync_playwright as _sp
                                import urllib.request as _ul
                                p = _sp().start()
                                def _is_cdp_up(url: str) -> bool:
                                    try:
                                        with _ul.urlopen(url + "/json/version", timeout=1) as resp:
                                            return resp.status == 200
                                    except Exception:
                                        return False
                                if not _is_cdp_up(cdp_url_state):
                                    cmd = [exe_state, f"--remote-debugging-port={cdp_url_state.split(':')[-1]}", f"--user-data-dir={udd_state}", "--lang=ru-RU"]
                                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                    deadline = time.time() + 10
                                    while time.time() < deadline and not _is_cdp_up(cdp_url_state):
                                        time.sleep(0.3)
                                b = p.chromium.connect_over_cdp(cdp_url_state)
                                c = b.contexts[0] if b.contexts else b.new_context()
                                pg = c.pages[0] if c.pages else c.new_page()
                                pg.set_default_timeout(30000)
                                # Ensure Gemini page is open only if not already on a Gemini page
                                url_state = st.session_state.get("unif_url", DEFAULT_URLS[0])
                                try:
                                    cur_url = pg.url or ""
                                except Exception:
                                    cur_url = ""
                                if ("gemini.google.com" not in cur_url) and ("aistudio.google.com" not in cur_url):
                                    try:
                                        pg.goto(url_state, wait_until="load")
                                    except Exception:
                                        pass
                                # как в оригинале: новый чат, готовность поля
                                try:
                                    _start_new_chat(pg)
                                except Exception:
                                    pass
                                try:
                                    _wait_input_ready(pg, timeout_ms=30000)
                                    _dismiss_overlays(pg)
                                except Exception:
                                    try:
                                        pg.reload(wait_until="load")
                                    except Exception:
                                        pass
                                    try:
                                        _start_new_chat(pg)
                                    except Exception:
                                        pass
                                    _wait_input_ready(pg, timeout_ms=30000)
                                    _dismiss_overlays(pg)
                                _, ns = _regenerate_prompt(pg, c, pt, idx, base_dir_state, max_images=1, attach_base=True, base_image_path=BASE_IMAGE_PATH)
                                result["new_saved"] = ns or []
                                try:
                                    p.stop()
                                except Exception:
                                    pass
                            except Exception as _e:
                                result["error"] = str(_e)
                        import threading
                        t = threading.Thread(target=_regen_worker, daemon=True)
                        t.start()
                        t.join()
                        if result["error"]:
                            raise RuntimeError(result["error"]) 
                        new_saved = result["new_saved"]

                        if new_saved:
                            # Удаляем старые файлы для этого промпта в папке, оставляем только новые
                            try:
                                deleted = _cleanup_old_prompt_files(os.path.dirname(new_saved[0]), idx, new_saved)
                            except Exception:
                                deleted = 0
                            # Заменяем предыдущие пути в session_state на новые reN-файлы
                            cur = st.session_state.get("unif_fast_saved_paths", [])
                            import re as _re
                            new_list = []
                            for p in cur:
                                _idxp = _extract_prompt_idx(p)
                                if _idxp == idx:
                                    continue
                                new_list.append(p)
                            new_list.extend(list(new_saved))
                            st.session_state.unif_fast_saved_paths = new_list
                            # st.session_state.unif_saved_paths = new_list
                            st.success(f"Готово: пересоздано {len(new_saved)} файлов для промпта #{idx}. Удалено старых: {deleted}")
                            st.rerun()
                        else:
                            st.warning("Перегенерация не вернула файлов")
                    except Exception as e:
                        st.error(f"Ошибка при перегенерации: {e}")

# ---------------- Tab 2: Gemini Generate (Nano Banana Pro / multi-window) ----------------
with tab2:
    st.subheader("Gemini generation via browser (Nano Banana Pro / отдельные окна)")
    st.caption(
        "Эта вкладка делает то же, что и Tab 1, но: для каждого промпта можно выбрать свой Chrome user-data-dir, "
        "и при запуске для каждого заполненного промпта открывается отдельное окно Chrome. "
        "В каждом окне будет выбран режим/model 'Nano Banana Pro' (если он доступен в меню моделей)."
    )

    nbp_url = st.selectbox(
        "URL интерфейса",
        DEFAULT_URLS,
        index=DEFAULT_URLS.index(st.session_state.get("unif_url", DEFAULT_URLS[0])) if st.session_state.get("unif_url", DEFAULT_URLS[0]) in DEFAULT_URLS else 0,
        key="unif_nbp_url",
    )
    nbp_headless = st.checkbox(
        "Headless режим (не рекомендуется для UI-логина)",
        value=False,
        key="unif_nbp_headless",
    )
    nbp_executable_path = st.text_input(
        "Путь к chrome.exe",
        value=st.session_state.get("unif_exe_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"),
        key="unif_nbp_exe_path",
    )

    st.markdown("**Задания (промпт + профиль)**")
    if "unif_nbp_tasks" not in st.session_state:
        # Each item: {"prompt": str, "user_data_dir": str}
        st.session_state.unif_nbp_tasks = [{"prompt": "", "user_data_dir": os.path.abspath(".chrome_automation_profile")}]

    if st.session_state.get("unif_nbp_imported_from_query"):
        st.success("Первые 3 промпта импортированы из статьи (Tab 0) и вставлены в Nano Banana Pro")
        st.session_state.unif_nbp_imported_from_query = False

    tasks_new = []
    for i, t in enumerate(st.session_state.unif_nbp_tasks):
        st.markdown(f"#### Prompt #{i+1}")
        pval = st.text_input(f"Промпт #{i+1}", value=t.get("prompt", ""), key=f"unif_nbp_prompt_{i}")
        default_ud = t.get("user_data_dir") or os.path.abspath(".chrome_automation_profile")
        ud = st.text_input(
            f"Chrome user-data-dir для промпта #{i+1}",
            value=default_ud,
            key=f"unif_nbp_uddir_{i}",
            help="Например: .chrome_automation_profile, .chrome_automation_profile_1, ... (разные аккаунты/сессии).",
        )
        tasks_new.append({"prompt": pval, "user_data_dir": _normalize_user_data_dir(ud) or os.path.abspath(os.path.expanduser(ud))})
        st.markdown("---")
    st.session_state.unif_nbp_tasks = tasks_new

    col_add, col_rem = st.columns([1, 1])
    with col_add:
        if st.button("+ Добавить поле", key="unif_nbp_add"):
            st.session_state.unif_nbp_tasks.append({"prompt": "", "user_data_dir": os.path.abspath(".chrome_automation_profile")})
            st.rerun()
    with col_rem:
        if len(st.session_state.unif_nbp_tasks) > 1 and st.button("− Убрать последнее", key="unif_nbp_rem"):
            st.session_state.unif_nbp_tasks = st.session_state.unif_nbp_tasks[:-1]
            st.rerun()

    keep_windows_open = st.checkbox(
        "Оставить окна Chrome открытыми после запуска",
        value=True,
        key="unif_nbp_keep_open",
        help="Если включено, окна останутся открытыми (для ручной проверки). Важно: Streamlit не сможет их автоматически закрыть."
    )

    nbp_parallelism = st.number_input(
        "Параллельно окон (concurrency)",
        min_value=1,
        max_value=12,
        value=3,
        step=1,
        help="Сколько окон/профилей запускать одновременно. Для параллельного режима нужно выключить 'Оставить окна открытыми'.",
        key="unif_nbp_parallelism",
    )

    nbp_retries = st.number_input(
        "Повторов (retry) на один промпт",
        min_value=0,
        max_value=5,
        value=2,
        step=1,
        help="Если Gemini UI пишет 'Something went wrong / Что-то пошло не так' или картинки не успели появиться — попробуем повторить с паузой.",
        key="unif_nbp_retries",
    )

    nbp_run = st.button("Сгенерировать картинки (Nano Banana Pro)", type="primary", key="unif_nbp_run")

    if nbp_run:
        # IMPORTANT: do not access st.session_state from background threads.
        # We snapshot all inputs first, then run Playwright work synchronously.
        base_dir = _get_run_base_dir()
        try:
            os.makedirs(base_dir, exist_ok=True)
        except Exception:
            pass

        tasks_snapshot = [
            {
                "prompt": (t.get("prompt") or "").strip(),
                "user_data_dir": _normalize_user_data_dir((t.get("user_data_dir") or "").strip()),
            }
            for t in (st.session_state.get("unif_nbp_tasks") or [])
        ]
        url_snapshot = st.session_state.get("unif_nbp_url", DEFAULT_URLS[0])
        headless_snapshot = bool(st.session_state.get("unif_nbp_headless", False))
        exe_snapshot = st.session_state.get("unif_nbp_exe_path") or None
        keep_open_snapshot = bool(st.session_state.get("unif_nbp_keep_open", True))
        retries_snapshot = int(st.session_state.get("unif_nbp_retries", 2) or 0)

        tasks_snapshot = [t for t in tasks_snapshot if t["prompt"]]
        if not tasks_snapshot:
            st.warning("Нет заполненных промптов. Добавьте хотя бы один промпт и повторите.")
        else:
            result = {"saved": [], "errors": []}

            # Keep references in session_state if user wants windows open, otherwise they may close immediately
            if keep_open_snapshot:
                st.session_state.setdefault("unif_nbp_keepalive", [])

            import contextlib

            _use_status = hasattr(st, "status")
            if _use_status:
                _cm = st.status("Запуск Nano Banana Pro (multi-window)...", expanded=True)
                status = _cm.__enter__()
            else:
                # Fallback for older Streamlit: show a spinner + a text area for logs
                _cm = st.spinner("Запуск Nano Banana Pro (multi-window)...")
                _cm.__enter__()
                status = st.empty()

            try:
                from playwright.sync_api import sync_playwright as _sp

                # Parallel mode: only safe when keep_open is False (we must close contexts inside workers).
                if keep_open_snapshot:
                    status.write("⚠️ Параллельный режим отключён, потому что включено 'Оставить окна открытыми'.\n"
                                 "Выключите эту опцию, чтобы окна и отправка шли параллельно.")

                    def _launch_ctx_with_retries(pw, *, user_data_dir: str | None):
                        last_err: Exception | None = None
                        for attempt in range(1, 4):
                            try:
                                return _launch_persistent_ctx_with_retries(
                                    pw,
                                    user_data_dir=user_data_dir,
                                    headless=headless_snapshot,
                                    executable_path=exe_snapshot,
                                )
                            except Exception as e:
                                last_err = e
                                # On Windows, Chrome launch can be flaky while OS is still creating the window.
                                # Retry a couple of times.
                                time.sleep(0.7 * attempt)
                        raise last_err or RuntimeError("Failed to launch persistent context")

                    p = _sp().start()
                    st.session_state.unif_nbp_keepalive.append(p)

                    used_udirs: set[str] = set()
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

                    for idx, item in enumerate(tasks_snapshot, 1):
                        prompt_raw = item["prompt"]
                        udir = (item.get("user_data_dir") or "").strip() or None
                        udir = _normalize_user_data_dir(udir) if udir else None

                        # In keep-open mode we might need multiple windows using the same base profile.
                        # Chrome can't open the same user-data-dir twice, so we auto-clone when repeated.
                        effective_udir = udir
                        cloned_dir = None
                        try:
                            if effective_udir:
                                udir_key = os.path.normcase(os.path.normpath(effective_udir))
                                if udir_key in used_udirs:
                                    cloned_dir = str(Path(f"tmp_rovodev_nbp_profile_{ts}_{idx}").resolve())
                                    try:
                                        _clone_profile_dir(effective_udir, cloned_dir)
                                        effective_udir = cloned_dir
                                    except Exception:
                                        # If cloning fails, fall back to original and let Chrome decide.
                                        effective_udir = udir
                                used_udirs.add(os.path.normcase(os.path.normpath(effective_udir or "")))
                        except Exception:
                            pass

                        status.write(f"Окно #{idx}: профиль={effective_udir or '(без профиля)'}")

                        if effective_udir:
                            try:
                                os.makedirs(effective_udir, exist_ok=True)
                            except Exception:
                                pass

                        ctx = _launch_ctx_with_retries(p, user_data_dir=effective_udir)
                        st.session_state.unif_nbp_keepalive.append(ctx)
                        if cloned_dir:
                            st.session_state.unif_nbp_keepalive.append({"tmp_profile_dir": cloned_dir})

                        page = ctx.new_page()
                        page.set_default_timeout(30000)
                        try:
                            page.goto(url_snapshot, wait_until="load")
                            _debug_dom(page)
                            _wait_input_ready(page, timeout_ms=60000)

                            try:
                                _start_new_chat(page)
                            except Exception:
                                pass

                            if os.path.exists(BASE_IMAGE_PATH):
                                for _ in range(2):
                                    ok = _attach_image(page, BASE_IMAGE_PATH)
                                    if ok and _wait_image_attached(page, timeout_ms=5000):
                                        break

                            _dismiss_overlays(page)

                            try:
                                gph._pick_model(page, "Nano Banana Pro")
                            except Exception as e:
                                result["errors"].append(f"Prompt #{idx}: не удалось выбрать модель Nano Banana Pro ({e})")

                            pre = "Please Change the white 10:16 ratio image using this Prompt: "
                            post = "PLEASE DO NOT LEAVE BLANK WHITE SPACE, THIS IS IMPORTANT "
                            final_prompt = f"{pre}{_sanitize_prompt(prompt_raw)}{post}"

                            try:
                                _type_prompt(page, final_prompt)
                            except Exception as e:
                                result["errors"].append(f"Prompt #{idx}: insert failed ({e})")
                                continue
                            _click_send(page)

                            imgs = _wait_and_download_generated_images(page, ctx, timeout_s=150, max_images=6)
                            if not imgs and _has_generated_images(page):
                                imgs = _wait_and_download_generated_images(page, ctx, timeout_s=20, max_images=6)

                            import re as _re
                            import hashlib as _hashlib

                            base_slug = _re.sub(r"[^a-zA-Z0-9_-]+", "_", prompt_raw)
                            base_slug = _re.sub(r"_+", "_", base_slug).strip("_")
                            if not base_slug:
                                base_slug = f"prompt_{idx}"

                            MAX_BASENAME = 110
                            prefix = f"{idx}_pro_"
                            prefix_len = len(prefix)
                            suffix_len = 1 + 2
                            allowed_slug_len = max(1, MAX_BASENAME - prefix_len - suffix_len)
                            slug = base_slug[:allowed_slug_len]

                            seen = set()
                            uniq = []
                            for mime, blob in imgs or []:
                                try:
                                    h = _hashlib.sha256(blob).hexdigest()
                                except Exception:
                                    h = None
                                if h and h in seen:
                                    continue
                                if h:
                                    seen.add(h)
                                uniq.append((mime, blob))

                            saved_local = []
                            for j, (mime, blob) in enumerate(uniq, 1):
                                ext = "png" if mime == "image/png" else ("jpg" if mime == "image/jpeg" else "bin")
                                fname = f"{idx}_pro_{slug}_{j:02d}.{ext}"
                                fpath = os.path.join(base_dir, fname)
                                with open(fpath, "wb") as f:
                                    f.write(blob)
                                saved_local.append(fpath)

                            result["saved"].extend(saved_local)
                            status.write(f"Окно #{idx}: сохранено {len(saved_local)} файлов")
                        except Exception as e:
                            result["errors"].append(f"Prompt #{idx}: {e}")

                else:
                    import concurrent.futures

                    max_workers = int(st.session_state.get("unif_nbp_parallelism", 3) or 3)
                    max_workers = max(1, min(12, max_workers))

                    progress = st.progress(0)
                    done = 0
                    total = len(tasks_snapshot)

                    def _run_one(task_idx: int, prompt_raw: str, udir: str | None, *, clone_profile: bool, retries: int):
                        # No Streamlit calls here.
                        local_result = {"idx": task_idx, "saved": [], "errors": []}
                        tmp_profile_dir = None
                        effective_udir = _normalize_user_data_dir(udir) if udir else None

                        def _jitter_sleep(base: float, spread: float):
                            try:
                                time.sleep(max(0.0, base + (random.random() * spread)))
                            except Exception:
                                pass

                        try:
                            # In parallel mode, multiple tasks must not share the same Chrome user-data-dir.
                            # We only clone when we detected duplicates.
                            if effective_udir and clone_profile:
                                try:
                                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                                    tmp_profile_dir = str(Path(f"tmp_rovodev_nbp_profile_{ts}_{task_idx}").resolve())
                                    _clone_profile_dir(effective_udir, tmp_profile_dir)
                                    effective_udir = tmp_profile_dir
                                except Exception:
                                    # If cloning fails, fall back to original and hope it isn't shared.
                                    tmp_profile_dir = None

                            if effective_udir:
                                try:
                                    os.makedirs(effective_udir, exist_ok=True)
                                except Exception:
                                    pass

                            with _sp() as p:
                                # Launch with a couple retries (Windows flakiness)
                                last_err: Exception | None = None
                                ctx = None
                                for attempt in range(1, 4):
                                    try:
                                        ctx = _launch_persistent_ctx_with_retries(
                                            p,
                                            user_data_dir=effective_udir,
                                            headless=headless_snapshot,
                                            executable_path=exe_snapshot,
                                        )
                                        break
                                    except Exception as e:
                                        last_err = e
                                        time.sleep(0.7 * attempt)
                                if ctx is None:
                                    raise last_err or RuntimeError("Failed to launch persistent context")

                                try:
                                    page = ctx.new_page()
                                    page.set_default_timeout(30000)
                                    page.goto(url_snapshot, wait_until="load")
                                    _debug_dom(page)
                                    _wait_input_ready(page, timeout_ms=60000)

                                    try:
                                        _start_new_chat(page)
                                    except Exception:
                                        pass

                                    # Stagger starts a bit to avoid request bursts.
                                    _jitter_sleep(0.25 * (task_idx % 5), 0.6)

                                    if os.path.exists(BASE_IMAGE_PATH):
                                        for _ in range(2):
                                            ok = _attach_image(page, BASE_IMAGE_PATH)
                                            if ok and _wait_image_attached(page, timeout_ms=5000):
                                                break

                                    _dismiss_overlays(page)

                                    try:
                                        gph._pick_model(page, "Nano Banana Pro")
                                    except Exception as e:
                                        local_result["errors"].append(f"не удалось выбрать модель Nano Banana Pro ({e})")

                                    pre = "Please Change the white 10:16 ratio image using this Prompt: "
                                    post = "PLEASE DO NOT LEAVE BLANK WHITE SPACE, THIS IS IMPORTANT "
                                    final_prompt = f"{pre}{_sanitize_prompt(prompt_raw)}{post}"

                                    max_attempts = 1 + int(retries or 0)

                                    last_exc: Exception | None = None
                                    imgs = []
                                    for attempt in range(1, max_attempts + 1):
                                        try:
                                            if attempt > 1:
                                                # Exponential backoff + jitter
                                                _jitter_sleep(1.2 * (2 ** (attempt - 2)), 1.0)
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
                                                if os.path.exists(BASE_IMAGE_PATH):
                                                    for _ in range(2):
                                                        ok = _attach_image(page, BASE_IMAGE_PATH)
                                                        if ok and _wait_image_attached(page, timeout_ms=5000):
                                                            break
                                                _dismiss_overlays(page)

                                            try:
                                                _type_prompt(page, final_prompt)
                                            except Exception as e:
                                                last_exc = RuntimeError(f"Prompt insert failed: {e}")
                                                continue
                                            _click_send(page)

                                            imgs = _wait_and_download_generated_images(page, ctx, timeout_s=170, max_images=6)
                                            if not imgs and _has_generated_images(page):
                                                imgs = _wait_and_download_generated_images(page, ctx, timeout_s=25, max_images=6)

                                            if imgs:
                                                break
                                            last_exc = RuntimeError("Пустой результат (нет картинок)")
                                        except Exception as e:
                                            last_exc = e
                                            # continue retry loop
                                            continue

                                    if not imgs and last_exc:
                                        raise last_exc

                                    import re as _re
                                    import hashlib as _hashlib

                                    base_slug = _re.sub(r"[^a-zA-Z0-9_-]+", "_", prompt_raw)
                                    base_slug = _re.sub(r"_+", "_", base_slug).strip("_")
                                    if not base_slug:
                                        base_slug = f"prompt_{task_idx}"

                                    MAX_BASENAME = 110
                                    prefix = f"{task_idx}_pro_"
                                    prefix_len = len(prefix)
                                    suffix_len = 1 + 2
                                    allowed_slug_len = max(1, MAX_BASENAME - prefix_len - suffix_len)
                                    slug = base_slug[:allowed_slug_len]

                                    seen = set()
                                    uniq = []
                                    for mime, blob in imgs or []:
                                        try:
                                            h = _hashlib.sha256(blob).hexdigest()
                                        except Exception:
                                            h = None
                                        if h and h in seen:
                                            continue
                                        if h:
                                            seen.add(h)
                                        uniq.append((mime, blob))

                                    for j, (mime, blob) in enumerate(uniq, 1):
                                        ext = "png" if mime == "image/png" else ("jpg" if mime == "image/jpeg" else "bin")
                                        fname = f"{task_idx}_pro_{slug}_{j:02d}.{ext}"
                                        fpath = os.path.join(base_dir, fname)
                                        with open(fpath, "wb") as f:
                                            f.write(blob)
                                        local_result["saved"].append(fpath)
                                finally:
                                    try:
                                        ctx.close()
                                    except Exception:
                                        pass
                        except Exception as e:
                            local_result["errors"].append(str(e))
                        finally:
                            if tmp_profile_dir:
                                shutil.rmtree(tmp_profile_dir, ignore_errors=True)
                        return local_result

                    # Detect duplicated profiles (same user-data-dir used by multiple tasks)
                    _ud_norm = []
                    for it in tasks_snapshot:
                        try:
                            v = (it.get("user_data_dir") or "").strip() or None
                            v = _normalize_user_data_dir(v) if v else None
                            if v:
                                v = os.path.normcase(os.path.normpath(v))
                        except Exception:
                            v = None
                        _ud_norm.append(v)
                    dup_udirs = {v for v in _ud_norm if v and _ud_norm.count(v) > 1}

                    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                        futs = []
                        for idx, item in enumerate(tasks_snapshot, 1):
                            prompt_raw = item["prompt"]
                            udir = (item.get("user_data_dir") or "").strip() or None
                            udir_norm = _normalize_user_data_dir(udir) if udir else None
                            udir_key = os.path.normcase(os.path.normpath(udir_norm)) if udir_norm else None
                            need_clone = bool(udir_key and udir_key in dup_udirs)
                            futs.append(ex.submit(_run_one, idx, prompt_raw, udir, clone_profile=need_clone, retries=retries_snapshot))

                        for fut in concurrent.futures.as_completed(futs):
                            r = fut.result()
                            done += 1
                            progress.progress(done / max(1, total))

                            if r.get("errors"):
                                for e in r["errors"]:
                                    result["errors"].append(f"Prompt #{r['idx']}: {e}")
                            if r.get("saved"):
                                result["saved"].extend(r["saved"])
                                status.write(f"Окно #{r['idx']}: сохранено {len(r['saved'])} файлов")
                            else:
                                status.write(f"Окно #{r['idx']}: файлов не сохранено")

                if _use_status:
                    try:
                        status.update(label="Готово", state="complete", expanded=True)
                    except Exception:
                        pass
            except Exception as e:
                if _use_status:
                    try:
                        status.update(label=f"Ошибка: {e}", state="error", expanded=True)
                    except Exception:
                        pass
                result["errors"].append(str(e))
            finally:
                try:
                    _cm.__exit__(None, None, None)
                except Exception:
                    pass

            if result.get("errors"):
                st.error("Ошибки:\n" + "\n".join(result["errors"]))
            if result.get("saved"):
                st.success(f"Сохранено файлов: {len(result['saved'])} в папку: {base_dir}")
                # Store Pro outputs separately so Tab1 regeneration can't overwrite/delete them.
                st.session_state.unif_pro_saved_paths = list(dict.fromkeys(result["saved"]))
                st.session_state.unif_last_base_dir = str(_PathAlias(base_dir).resolve())
                try:
                    st.session_state.ps_src_dir = st.session_state.unif_last_base_dir
                except Exception:
                    pass

# ---------------- Tab 3: Photoshop watermark removal (batch) ----------------
with tab3:
    st.subheader("Photoshop: удалить ватермарк (Content-Aware Fill нижний правый)")
    st.caption("Параметры по умолчанию: 13, Все форматы (как в исходнике), scale, отступы 25/25.")

    # Определяем папку текущей генерации:
    # 1) по сохранённым файлам (dirname последнего сохранённого);
    # 2) иначе из session_state.unif_last_base_dir;
    # 3) иначе сегодняшняя дата в "generate automation".
    saved_paths = _get_all_saved_paths()
    last_gen_dir = None
    if saved_paths:
        try:
            last_gen_dir = os.path.dirname(saved_paths[-1])
        except Exception:
            last_gen_dir = None
    if not last_gen_dir:
        last_gen_dir = st.session_state.get("unif_last_base_dir")
    if not last_gen_dir:
        last_gen_dir = str((Path("generate automation") / datetime.now().strftime("%Y-%m-%d")).resolve())
    # Автоподстановка актуальной папки (можно отключить чекбоксом)
    if "ps_follow_latest" not in st.session_state:
        st.session_state.ps_follow_latest = True
    # Если режим авто включён, или поле ещё не задано — обновляем на последнюю папку генерации
    if st.session_state.ps_follow_latest or ("ps_src_dir" not in st.session_state):
        st.session_state.ps_src_dir = str(Path(last_gen_dir).expanduser())

    st.checkbox("Автоматически подставлять последнюю папку генерации", value=st.session_state.ps_follow_latest, key="ps_follow_latest")

    # Поле ввода для ручного изменения папки. По умолчанию — текущая генерация
    src_dir_text = st.text_input(
        "Папка с изображениями для Photoshop",
        value=st.session_state.get("ps_src_dir", str(Path(last_gen_dir).expanduser())),
        key="ps_src_dir"
    )
    st.button("Открыть папку", on_click=_open_folder, args=(src_dir_text,), key="btn_open_ps_src")

    # Сканирование папки и выбор файлов (по умолчанию — все)
    folder = Path(src_dir_text).expanduser()
    exts = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".psd")
    found_images: List[Path] = []
    if folder.exists() and folder.is_dir():
        try:
            found_images = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts]
            # Если в корне мало файлов, попробуем углублённый поиск
            if not found_images:
                found_images = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in exts]
        except Exception:
            found_images = []
    else:
        st.info("Укажите существующую папку с изображениями.")

    st.write(f"Найдено файлов: {len(found_images)}")

    # Опции выбора
    pick_manually = st.checkbox("Выбрать файлы вручную", value=False, key="ps_pick_manual")
    selected_files: List[str] = []
    if pick_manually and found_images:
        # Покажем мультивыбор по именам файлов
        names = [f.name for f in found_images]
        chosen = st.multiselect("Выберите файлы для обработки", options=names, default=names)
        name_to_path = {f.name: str(f) for f in found_images}
        selected_files = [name_to_path[n] for n in chosen]
    else:
        selected_files = [str(p) for p in found_images]

    # Альтернатива: перетаскивание и загрузка файлов напрямую
    st.markdown("— или —")
    uploads = st.file_uploader(
        "Перетащите сюда изображения (PNG, JPG, JPEG, TIF, TIFF, WEBP, BMP, PSD)",
        type=["png", "jpg", "jpeg", "tif", "tiff", "webp", "bmp", "psd"],
        accept_multiple_files=True,
        key="ps_uploads"
    )
    uploaded_temp_paths: List[str] = []
    uploaded_temp_dir = None
    if uploads:
        # Сохраним загруженные файлы во временную папку для текущего запуска
        import time as _t
        ts = _t.strftime("%Y%m%d_%H%M%S")
        uploaded_temp_dir = Path(f"tmp_rovodev_ps_uploads_{ts}")
        try:
            uploaded_temp_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        for uf in uploads:
            try:
                # uf is UploadedFile; write to disk preserving name
                outp = uploaded_temp_dir / uf.name
                with open(outp, "wb") as fh:
                    fh.write(uf.getbuffer())
                uploaded_temp_paths.append(str(outp))
            except Exception:
                continue
        if uploaded_temp_paths:
            selected_files = uploaded_temp_paths
            st.info(f"Загружено файлов: {len(uploaded_temp_paths)}. Будут обработаны именно они.")
    # Запомним временную папку в состоянии, чтобы убрать после обработки
    st.session_state["ps_uploaded_temp_dir"] = str(uploaded_temp_dir) if uploaded_temp_dir else None

    size_or_scale = st.number_input("size_or_scale (для scale = делитель, 13 => 1/13 меньшей стороны)", min_value=1.0, max_value=512.0, value=13.0, step=1.0, key="ps_size")
    out_format = st.selectbox("Формат вывода", ["PNG", "JPEG"], index=0, key="ps_fmt")
    mode = st.selectbox("Режим", ["scale", "fixed"], index=0, key="ps_mode")
    margin_left = st.number_input("Отступ слева, px", min_value=0, max_value=200, value=25, step=1, key="ps_ml")
    margin_bottom = st.number_input("Отступ снизу, px", min_value=0, max_value=200, value=25, step=1, key="ps_mb")

    run_btn = st.button("Запустить Photoshop обработку (batch)", type="primary", key="ps_run")

    if run_btn:
        folder = Path(src_dir_text).expanduser()
        if not folder.exists() or not folder.is_dir():
            st.error("Папка не существует или это не директория")
        else:
            images = selected_files
            if not images:
                st.warning("Не найдено изображений для обработки.")
            else:
                st.info(f"Запускаю обработку файлов: {len(images)}…")
                # Вызываем Photoshop напрямую на выбранных файлах, без предварительной конвертации JPG→PNG
                fmt_arg = out_format
                cmd = [sys.executable, "photoshop_crop_bottom_right.py", *images,
                       str(int(size_or_scale)) if mode=="scale" else str(int(size_or_scale)), fmt_arg, mode, str(int(margin_left)), str(int(margin_bottom))]
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True)
                    if proc.returncode == 0:
                        st.success("Photoshop обработка завершена. Файлы с суффиксом _filled сохранены рядом с исходными.")
                        # Запомним фактическую исходную папку, использованную при запуске Photoshop
                        try:
                            st.session_state["ps_run_src_dir_final"] = str(Path(src_dir_text).expanduser())
                        except Exception:
                            pass
                        # Если файлы были загружены через drag&drop (во временную папку),
                        # автоматически перенесём результаты *_filled.* в указанную папку src_dir_text
                        try:
                            from pathlib import Path as _P
                            import shutil as _sh
                            dest_dir = _P(src_dir_text).expanduser()
                            up_tmp = st.session_state.get("ps_uploaded_temp_dir")
                            moved = 0
                            if up_tmp:
                                tmp_dir = _P(up_tmp)
                                if tmp_dir.exists() and dest_dir.exists():
                                    for q in sorted(tmp_dir.glob("*_filled.*")):
                                        if q.suffix.lower() not in (".png", ".jpg", ".jpeg"):
                                            continue
                                        target = dest_dir / q.name
                                        # Разрешение конфликтов имени: добавим (1), (2), ... если нужно
                                        if target.exists():
                                            stem = target.stem
                                            suff = target.suffix
                                            k = 1
                                            while True:
                                                cand = dest_dir / f"{stem}({k}){suff}"
                                                if not cand.exists():
                                                    target = cand
                                                    break
                                                k += 1
                                        try:
                                            _sh.move(str(q), str(target))
                                            moved += 1
                                        except Exception:
                                            # попробуем копировать как fallback
                                            try:
                                                _sh.copy2(str(q), str(target))
                                                moved += 1
                                            except Exception:
                                                pass
                            if moved:
                                st.info(f"Перенесено файлов в папку для Photoshop: {moved} → {dest_dir}")
                        except Exception:
                            pass
                        # Покажем, где искать результаты и выведем превью
                        try:
                            from pathlib import Path as _P
                            import re as _re
                            out_dirs = sorted(set(_P(p).parent for p in images))
                            up_tmp = st.session_state.get("ps_uploaded_temp_dir")
                            # Если был перенос из временной папки — показываем целевую папку
                            if up_tmp:
                                out_dirs = [ _P(src_dir_text).expanduser() ]
                            else:
                                if up_tmp:
                                    out_dirs = list(dict.fromkeys(out_dirs + [_P(up_tmp)]))
                            total_found = 0
                            for d in out_dirs:
                                if not d or not _P(d).exists():
                                    continue
                                filled = [q for q in _P(d).glob("*_filled.*") if q.suffix.lower() in (".png", ".jpg", ".jpeg")]
                                if not filled:
                                    continue
                                st.markdown(f"**Папка результатов:** {str(d)}")
                                cols = st.columns(4)
                                for i, q in enumerate(sorted(filled)):
                                    total_found += 1
                                    with cols[i % 4]:
                                        try:
                                            st.image(str(q), caption=q.name, use_container_width=True)
                                        except Exception:
                                            st.write(q.name)
                                        st.code(str(q), language=None)
                            if total_found == 0:
                                st.info("Пока не найдено файлов *_filled рядом с исходниками. Проверьте права записи и исходное расширение.")
                        except Exception as _e:
                            st.info(f"Не удалось отобразить результаты: {_e}")
                    else:
                        st.error(f"Photoshop вернул код {proc.returncode}. STDERR: {proc.stderr}\nSTDOUT: {proc.stdout}")
                except Exception as e:
                    st.error(f"Ошибка запуска Photoshop: {e}")

# ---------------- Tab 4: Normalize Pins to fixed size before WebP ----------------
with tab4:
    st.subheader("Normalize Pins to 640×1024 (crop to fit)")
    st.caption("Этап перед WebP. По умолчанию берём *_filled изображения из последней генерации и приводим к размеру 640×1024, обрезая лишнее по длинной стороне. Можно также перетащить дополнительные файлы любого формата.")

    from PIL import Image
    import io as _io

    # Источник по умолчанию: та же, что использовалась на вкладке Photoshop / последняя генерация
    def _guess_today_latest_dir() -> str | None:
        try:
            import re as _re
            root = Path("generate automation").expanduser()
            today = datetime.now().strftime("%Y-%m-%d")
            if not root.exists():
                return None
            best = None
            best_n = -1
            for d in root.iterdir():
                if not d.is_dir():
                    continue
                name = d.name
                if name == today:
                    n = 0
                else:
                    m = _re.match(rf"^{today}_(\d+)$", name)
                    if not m:
                        continue
                    n = int(m.group(1))
                if n >= best_n:
                    best_n = n
                    best = d
            return str(best.expanduser()) if best is not None else None
        except Exception:
            return None

    # Вычисляем актуальную папку по тем же приоритетам, что и Photoshop, плюс fallback на самую свежую сегодняшнюю
    latest_default_dir = None
    latest_default_dir = st.session_state.get("ps_run_src_dir_final") or st.session_state.get("ps_src_dir") or None
    if not latest_default_dir:
        latest_default_dir = st.session_state.get("unif_last_base_dir") or None
    if not latest_default_dir:
        saved_paths = _get_all_saved_paths()
        if saved_paths:
            try:
                latest_default_dir = os.path.dirname(saved_paths[-1])
            except Exception:
                latest_default_dir = None
    if not latest_default_dir:
        latest_default_dir = _guess_today_latest_dir()

    # Режим авто-подстановки, как во вкладке Photoshop
    if "norm_follow_latest" not in st.session_state:
        st.session_state.norm_follow_latest = True
    if st.session_state.norm_follow_latest or ("norm_src_dir" not in st.session_state):
        st.session_state["norm_src_dir"] = latest_default_dir or ""

    col_l, col_r = st.columns([2,1])
    with col_l:
        src_dir_text = st.text_input("Папка с исходными изображениями", value=st.session_state.get("norm_src_dir", latest_default_dir or ""), key="norm_src_dir")
        st.button("Открыть папку", on_click=_open_folder, args=(src_dir_text,), key="btn_open_norm_src")
        include_filled_only = st.checkbox("Только *_filled (после Photoshop)", value=True, key="norm_only_filled")
        # drag&drop дополнительные картинки любого формата
        uploaded_files = st.file_uploader("Дополнительно перетащите файлы (png/jpg/webp и др.)", accept_multiple_files=True)
    with col_r:
        target_w = st.number_input("Ширина", min_value=64, max_value=4096, value=640, step=1)
        target_h = st.number_input("Высота", min_value=64, max_value=4096, value=1024, step=1)
        try_match_size = st.checkbox("Стараться сохранить размер файла (JPEG/WebP)", value=True, help="Подбор качества для достижения веса, близкого к исходному (±10%).")

    # Куда сохраняем
    # Формируем папку сохранения из текущего поля источника (оно уже показывает верный путь с суффиксом _N)
    computed_outdir = str(Path(src_dir_text).expanduser() / f"normalized_{target_w}x{target_h}") if src_dir_text else ""
    # Если включён режим авто-подстановки — синхронизируем путь сохранения с источником и размером
    if st.session_state.get("norm_follow_latest", True):
        st.session_state["norm_out_dir"] = computed_outdir
    base_outdir = st.text_input(
        "Папка для сохранения",
        value=st.session_state.get("norm_out_dir", computed_outdir),
        key="norm_out_dir",
    )
    st.button("Открыть папку", on_click=_open_folder, args=(base_outdir,), key="btn_open_norm_out")
    if base_outdir:
        try:
            Path(base_outdir).expanduser().mkdir(parents=True, exist_ok=True)
        except Exception as e:
            st.error(f"Не удалось создать папку для сохранения: {e}")
    st.caption(f"Источник: {src_dir_text or '—'} → Сохранение: {base_outdir or '—'}")

    def _iter_src_files() -> list[Path]:
        paths: list[Path] = []
        # из папки
        if src_dir_text:
            pdir = Path(src_dir_text).expanduser()
            if pdir.is_dir():
                exts = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
                # только файлы верхнего уровня; жёсткий фильтр по окончанию стема на _filled
                pat = "*_filled.*" if include_filled_only else "*"
                total_scanned = 0
                skipped_non_filled = 0
                for p in pdir.glob(pat):
                    if not p.is_file():
                        continue
                    total_scanned += 1
                    if p.suffix.lower() in exts:
                        stem_low = p.stem.lower()
                        if include_filled_only and (not stem_low.endswith("_filled")):
                            skipped_non_filled += 1
                            continue
                        paths.append(p)
                # Отобразим краткую статистику отбора
                st.caption(f"Найдено в папке: {len(paths)} файлов для обработки (просканировано: {total_scanned}, пропущено (не *_filled): {skipped_non_filled})")
        # из upload'а
        for uf in uploaded_files or []:
            try:
                # Загруженные файлы всегда принимаем (по вашему требованию), независимо от *_filled
                tmp_name = f"tmp_upload_{uf.name}"
                tmp_base = Path(base_outdir).expanduser() if base_outdir else Path('.')
                tmp_base.mkdir(parents=True, exist_ok=True)
                tmp_path = tmp_base / tmp_name
                with open(tmp_path, "wb") as f:
                    f.write(uf.getbuffer())
                paths.append(tmp_path)
            except Exception as e:
                st.warning(f"Не удалось принять загруженный файл {uf.name}: {e}")
        return paths

    def _crop_to_fit(img: Image.Image, tw: int, th: int) -> Image.Image:
        src_w, src_h = img.size
        scale = max(tw / src_w, th / src_h)
        new_w, new_h = int(round(src_w * scale)), int(round(src_h * scale))
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        # центрируем crop
        left = max(0, (new_w - tw) // 2)
        top = max(0, (new_h - th) // 2)
        right = left + tw
        bottom = top + th
        return resized.crop((left, top, right, bottom))

    def _save_match_size(img: Image.Image, out_path: Path, orig_size: int) -> None:
        ext = out_path.suffix.lower()
        if ext in (".jpg", ".jpeg", ".webp"):
            # бинарный поиск качества, чтобы попасть в ±10% от исходника
            lo, hi = 30, 95
            target = orig_size
            best = None
            for _ in range(8):
                q = (lo + hi) // 2
                buf = _io.BytesIO()
                params = {}
                if ext in (".jpg", ".jpeg"):
                    params = dict(format="JPEG", quality=q, optimize=True, subsampling="4:2:0")
                else:
                    params = dict(format="WEBP", quality=q, method=6)
                try:
                    img.save(buf, **params)
                    size = buf.tell()
                except Exception:
                    size = 10**12
                best = (q, buf.getvalue(), size)
                # условие приближения
                if abs(size - target) <= max(1, int(target * 0.10)):
                    break
                if size > target:
                    hi = q - 1
                else:
                    lo = q + 1
            # финальная запись
            if best is not None:
                with open(out_path, "wb") as f:
                    f.write(best[1])
            else:
                img.save(out_path)
        else:
            # PNG/BMP: просто сохраняем, без гарантии веса
            try:
                if ext == ".png":
                    img.save(out_path, format="PNG", optimize=True)
                else:
                    img.save(out_path)
            except Exception:
                img.save(out_path)

    run_norm = st.button("Нормализовать", type="primary")

    if run_norm:
        src_files = _iter_src_files()
        if not src_files:
            st.warning("Нет входных файлов. Укажите папку или загрузите файлы.")
        else:
            prog = st.progress(0)
            done = 0
            out_paths: list[str] = []
            for p in src_files:
                try:
                    with Image.open(p) as im:
                        im = im.convert("RGB") if im.mode not in ("RGB", "RGBA") else im
                        out = _crop_to_fit(im, int(target_w), int(target_h))
                        out_name = p.stem + f"_{int(target_w)}x{int(target_h)}" + p.suffix
                        out_path = Path(base_outdir) / out_name if base_outdir else Path(out_name)
                        if try_match_size:
                            # Взвешенно подбираем качество. Для входных PNG/BMP при сохранении в PNG просто сохраняем без гарантии веса.
                            try:
                                orig_size = p.stat().st_size if p.exists() else len(open(p, 'rb').read())
                            except Exception:
                                try:
                                    with open(p, 'rb') as _fh:
                                        orig_size = len(_fh.read())
                                except Exception:
                                    orig_size = 0
                            _save_match_size(out, out_path, orig_size)
                        else:
                            # сохранить в исходный формат с разумными параметрами
                            ext = p.suffix.lower()
                            if ext in (".jpg", ".jpeg"):
                                out.save(out_path, format="JPEG", quality=90, subsampling="4:2:0", optimize=True)
                            elif ext == ".png":
                                out.save(out_path, format="PNG", optimize=True)
                            elif ext == ".webp":
                                out.save(out_path, format="WEBP", quality=80, method=6)
                            else:
                                out.save(out_path)
                        out_paths.append(str(out_path))
                except Exception as e:
                    st.error(f"Ошибка обработки {p}: {e}")
                done += 1
                prog.progress(int(done / max(1, len(src_files)) * 100))
            st.success(f"Готово. Сохранено: {len(out_paths)} файлов в {base_outdir}")
            # Запомним папку для следующей вкладки WebP как дефолт
            st.session_state["normalized_last_outdir"] = str(Path(base_outdir).resolve())
            # Покажем предпросмотр
            if out_paths:
                st.markdown("---")
                st.subheader("Результаты нормализации")
                cols = st.columns(4)
                for i, pth in enumerate(sorted(out_paths)):
                    with cols[i % 4]:
                        try:
                            st.image(pth, caption=os.path.basename(pth), use_container_width=True)
                        except Exception:
                            st.write(os.path.basename(pth))

# ---------------- Tab 5: WebP conversion (embed existing UI) ----------------
with tab5:
    st.subheader("Convert images to WebP")
    # По умолчанию берём результаты нормализации, если есть; иначе последнюю базовую папку генерации
    default_webp_dir = st.session_state.get("normalized_last_outdir") or st.session_state.get("unif_last_base_dir")
    if not default_webp_dir:
        try:
            from pathlib import Path as _P
            from datetime import datetime as _Dt
            default_webp_dir = str((_P("generate automation") / _Dt.now().strftime("%Y-%m-%d")).expanduser())
        except Exception:
            default_webp_dir = ""

    # Строим базовый outdir и имя сессии по аналогии с предыдущей реализацией
    try:
        gen_dir = Path(default_webp_dir).expanduser()
        suffix = ""
        try:
            name = gen_dir.name
            import re as _re
            m = _re.search(r"_([0-9]+)$", name)
            if m:
                suffix = m.group(0)  # like _9
        except Exception:
            pass
        default_outdir = str(gen_dir)
        default_session = f"webp{suffix or ''}"
    except Exception:
        default_outdir = None
        default_session = None

    # Встраиваем UI конвертера WebP
    convert_to_webp.run_streamlit_app(
        embed=True,
        default_folder=default_webp_dir,
        only_filled_default=False,
        default_base_outdir=default_outdir,
        default_session_name=default_session,
        follow_latest_defaults=True,
    )
    st.button("Открыть папку", on_click=_open_folder, args=(default_webp_dir,), key="btn_open_webp_src")

# ---------------- Tab 6: Titles & Descriptions regeneration (Gemini API) ----------------
with tab6:
    st.subheader("Regenerate product Titles & Descriptions (Gemini API)")
    st.caption("Эта вкладка использует только модель Gemini Flash для генерации (models/gemini-2.5-flash). При ошибках будет показана подробная причина от Google API.")

    import json as _json
    from collections import deque as _deque
    import time
    import random

    # --- Gemini API config (mirrors generate_pinterest_texts.py, text-only variant) ---
    GEMINI_API_KEYS = [
        "Your_gemini_api_keys",
    ]
    MODELS = [
        "models/gemini-2.5-flash",
    ]
    RATE_LIMITS = {
        "models/gemini-2.5-flash": 10,
    }

    if "g_current_key_idx" not in st.session_state:
        st.session_state.g_current_key_idx = 0
    if "g_current_model_idx" not in st.session_state:
        st.session_state.g_current_model_idx = 0
    if "g_rate_windows" not in st.session_state:
        st.session_state.g_rate_windows = {i: _deque() for i in range(len(MODELS))}
    if "g_last_429_at" not in st.session_state:
        st.session_state.g_last_429_at = {i: 0.0 for i in range(len(MODELS))}

    def _g_get_current_url():
        model = MODELS[st.session_state.g_current_model_idx]
        key = GEMINI_API_KEYS[st.session_state.g_current_key_idx]
        return f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent?key={key}"

    def _g_wait_for_rate_slot(model_idx: int):
        name = MODELS[model_idx]
        rpm = RATE_LIMITS.get(name, 15)
        dq = st.session_state.g_rate_windows[model_idx]
        while True:
            now = time.time()
            while dq and (now - dq[0]) >= 60:
                dq.popleft()
            if len(dq) < rpm:
                dq.append(now)
                return
            sleep_for = max(1, int(60 - (now - dq[0]) + 1))
            time.sleep(sleep_for)

    def _g_call_gemini_text(prompt_text: str, timeout_sec: int = 90, max_duration_sec: int = 180, log=None) -> tuple[str | None, str | None]:
        # Text-only version of generateContent, with model/key rotation and rate limiting.
        # Returns (result_text, error_message). If generation fails within max_duration_sec, result_text is None
        # and error_message contains the last seen Google API error (status + body) or a fallback reason.
        import re as _re
        start_ts = time.time()
        last_error: str | None = None
        while True:
            # Stop if exceeded max duration
            if (time.time() - start_ts) > max_duration_sec:
                if last_error is None:
                    last_error = "Generation timed out without a specific error message."
                return None, last_error

            if st.session_state.g_current_model_idx >= len(MODELS):
                st.session_state.g_current_model_idx = 0
                st.session_state.g_current_key_idx += 1
                if st.session_state.g_current_key_idx >= len(GEMINI_API_KEYS):
                    st.session_state.g_current_key_idx = 0
                    time.sleep(60)
                st.session_state.g_last_429_at = {i: 0.0 for i in range(len(MODELS))}
                continue

            url = _g_get_current_url()
            model_name = MODELS[st.session_state.g_current_model_idx]
            key_idx = st.session_state.g_current_key_idx
            if log:
                log(f"Using model={model_name}, key_idx={key_idx}. Waiting for rate slot...")
            else:
                print(f"[TD] Using model={model_name}, key_idx={key_idx}. Waiting for rate slot...")
            _g_wait_for_rate_slot(st.session_state.g_current_model_idx)

            # Extra jitter delay to reduce Gemini API rate-limit bursts
            jitter_s = random.uniform(4.0, 8.0)
            if log:
                log(f"Jitter sleep {jitter_s:.1f}s before request (anti rate-limit)...")
            time.sleep(jitter_s)

            headers = {"Content-Type": "application/json"}
            payload = {
                "contents": [{"parts": [{"text": prompt_text}]}]
            }
            try:
                if log:
                    log("Sending request to Google API...")
                else:
                    print("[TD] Sending request to Google API...")
                resp = requests.post(url, json=payload, headers=headers, timeout=timeout_sec)
                if log:
                    log(f"Received response: status={resp.status_code}")
                else:
                    print(f"[TD] Received response: status={resp.status_code}")
                # Keep raw body for better error reporting if raise_for_status fails later
                raw_text = None
                try:
                    raw_text = resp.text
                except Exception:
                    raw_text = None
                resp.raise_for_status()
                data = resp.json() or {}
                pf = data.get("promptFeedback") or {}
                if "blockReason" in pf:
                    # Surface block reason to UI
                    br = pf.get("blockReason")
                    details = pf.get("blockReasonMessage") or pf.get("safetyRatings")
                    last_error = f"Prompt blocked by Google: {br}. Details: {details}"
                    return None, last_error
                cand = (data.get("candidates") or [])
                if cand:
                    content = cand[0].get("content") or {}
                    parts = content.get("parts") or []
                    if parts and "text" in parts[0]:
                        return parts[0]["text"], None
                # Unknown structure -> switch model but keep trace
                last_error = f"Unexpected response structure: {data!r}"
                if log:
                    log("Unexpected response structure. Will rotate model or retry.")
                st.session_state.g_current_model_idx += 1
                time.sleep(5)
                continue
            except requests.exceptions.RequestException as e:
                resp = getattr(e, 'response', None)
                status_code = resp.status_code if resp is not None else None
                body = None
                try:
                    body = resp.text if resp is not None else None
                except Exception:
                    body = None
                last_error = f"HTTP error from Google: status={status_code}, body={body}"
                if log:
                    log(f"HTTP error: status={status_code}. Body snippet: {str(body)[:400] if body else 'None'}")
                else:
                    print(f"[TD] HTTP error: status={status_code}. Body snippet: {str(body)[:200] if body else 'None'}")
                if status_code == 429:
                    now = time.time()
                    last = st.session_state.g_last_429_at[st.session_state.g_current_model_idx]
                    if (now - last) > 120:
                        st.session_state.g_last_429_at[st.session_state.g_current_model_idx] = now
                        time.sleep(61)
                    else:
                        st.session_state.g_current_model_idx += 1
                    continue
                elif status_code in (400, 404):
                    st.session_state.g_current_model_idx += 1
                    continue
                elif status_code in (401, 403):
                    st.session_state.g_current_key_idx += 1
                    if st.session_state.g_current_key_idx >= len(GEMINI_API_KEYS):
                        st.session_state.g_current_key_idx = 0
                        time.sleep(60)
                    st.session_state.g_current_model_idx = 0
                    time.sleep(3)
                    continue
                elif status_code and status_code >= 500:
                    time.sleep(15)
                    continue
                else:
                    time.sleep(10)
                    continue
            except Exception as e:
                last_error = f"Unexpected client error: {e}"
                time.sleep(10)
                continue

    # --- Dynamic pairs UI ---
    if "td_pairs" not in st.session_state:
        st.session_state.td_pairs = [{"title": "", "desc": ""}]

    st.markdown("Введите пары исходного Title и полного Description для регенерации. Можно добавлять больше пар.")

    new_pairs = []
    for i, pair in enumerate(st.session_state.td_pairs):
        st.markdown(f"#### Пара #{i+1}")
        t = st.text_input(f"Title #{i+1}", value=pair.get("title", ""), key=f"td_title_{i}")
        d = st.text_area(f"Full description #{i+1}", value=pair.get("desc", ""), key=f"td_desc_{i}", height=140)
        new_pairs.append({"title": t, "desc": d})
        st.markdown("---")

    col_add, col_rem = st.columns([1,1])
    with col_add:
        if st.button("+ Добавить пару", key="td_add"):
            st.session_state.td_pairs.append({"title": "", "desc": ""})
            st.rerun()
    with col_rem:
        if len(st.session_state.td_pairs) > 1 and st.button("− Убрать последнюю", key="td_rem"):
            st.session_state.td_pairs = st.session_state.td_pairs[:-1]
            st.rerun()
    st.session_state.td_pairs = new_pairs

    run_btn = st.button("Сгенерировать для всех пар", type="primary", key="td_run")

    def _build_prompt(src_title: str, src_full_desc: str) -> str:
        # Force strict JSON and valid HTML tags in long_description_html
        return (
            "You will rewrite and regenerate product marketing content from a provided original title and a full description.\n" \
            "Rules:\n" \
            "- Language: English only.\n" \
            "- This is an affiliate page: do NOT say 'our product' or imply ownership.\n" \
            "- Output EXACTLY one JSON object (no markdown fences, no extra text). Use valid JSON with double quotes for keys and strings.\n" \
            "- Provide these keys:\n" \
            "  \"short_title\": Shorten the original title, keeping the core product type and optionally the brand.\n" \
            "  \"short_description\": One paragraph, persuasive sales-style, no lists, suitable to appear at the right of the product image on a store page.\n" \
            "  \"long_description_html\": Valid HTML string. Start with <h2>Why you should buy this product.</h2>. Use <p> for paragraphs. If you add subheadings, use <h5> only.\n" \
            "Return only the JSON object.\n\n" \
            f"Original title to shorten: \"{src_title.strip()}\"\n" \
            f"Original full description:\n{src_full_desc.strip()}\n"
        )

    if run_btn:
        results = []
        status = st.status("Processing pairs...", expanded=True)
        with status:
            for i, pair in enumerate(st.session_state.td_pairs, 1):
                st.write(f"Queueing pair #{i} ...")
            st.write("Starting generation...")

        for i, pair in enumerate(st.session_state.td_pairs, 1):
            with st.spinner(f"Generating pair #{i} ..."):
                log_lines = []
                log_box = st.empty()
                def _log(msg: str):
                    ts = time.strftime('%H:%M:%S')
                    line = f"[{ts}] {msg}"
                    log_lines.append(line)
                    try:
                        log_box.caption("\n".join(log_lines))
                    except Exception:
                        pass
                title_in = (pair.get("title") or "").strip()
                desc_in = (pair.get("desc") or "").strip()
                if not title_in and not desc_in:
                    results.append({"index": i, "error": "Empty inputs"})
                    continue
                prompt = _build_prompt(title_in, desc_in)
                text, g_error = _g_call_gemini_text(prompt, timeout_sec=45, max_duration_sec=90, log=_log)
                if log_lines:
                    log_box.caption("\n".join(log_lines))
                if not text:
                    # Record Google error message to show in UI
                    results.append({"index": i, "error": g_error or "Unknown error from Google API"})
                    continue
                item = {"index": i, "raw": text}
                # Try parse JSON robustly
                parsed = None
                s = text.strip()
                # strip code fences or leading/trailing junk
                if s.startswith("```"):
                    try:
                        s = s.split("```", 2)[1]
                    except Exception:
                        pass
                # sometimes models prepend words like 'json' or explanations
                for prefix in ("json", "JSON", "Result:"):
                    if s.lower().startswith(prefix.lower()):
                        s = s[len(prefix):].lstrip(':\n ')
                # attempt to find first {...} json object
                try:
                    first_brace = s.find('{')
                    last_brace = s.rfind('}')
                    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                        s_obj = s[first_brace:last_brace+1]
                        parsed = _json.loads(s_obj)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    item.update({
                        "short_title": parsed.get("short_title"),
                        "short_description": parsed.get("short_description"),
                        "long_description_html": parsed.get("long_description_html"),
                    })
                else:
                    # try to salvage by splitting lines that look like key: value
                    try:
                        tmp = {}
                        for line in s.splitlines():
                            if '"short_title"' in line or 'short_title' in line:
                                tmp['short_title'] = line.split(':',1)[1].strip().strip('",')
                            if '"short_description"' in line or 'short_description' in line:
                                tmp['short_description'] = line.split(':',1)[1].strip().strip('",')
                            if '"long_description_html"' in line or 'long_description_html' in line:
                                tmp['long_description_html'] = line.split(':',1)[1].strip()
                        if tmp:
                            item.update(tmp)
                    except Exception:
                        pass
                results.append(item)

        status.update(label="Done", state="complete")

        # Render
        for item in results:
            st.markdown(f"### Result for pair #{item.get('index', '?')}")
            if item.get("error"):
                st.error(item["error"]) 
                continue
            st.write("Shortened title:")
            st.success(item.get("short_title") or "—")
            st.write("Short description:")
            st.write(item.get("short_description") or "—")
            st.write("Long description (HTML):")
            html = item.get("long_description_html") or item.get("raw") or ""
            st.markdown(html, unsafe_allow_html=True)
            st.markdown("---")

# ---------------- Tab 7: Regenerate product images (Gemini UI) ----------------
with tab7:
    st.subheader("Regenerate product images (Gemini UI)")
    st.caption(
        "Эта вкладка открывает Chrome с выбранным профилем и создаёт до 6 вкладок Gemini. "
        "В каждой вкладке она просто вставляет промпт (без отправки), чтобы вы могли быстро нажать Generate/Send вручную."
    )

    # Параметры запуска браузера: максимально похоже на Tab 1
    # (используем отдельные ключи, но подставляем значения из Tab 1 по умолчанию)
    regen_url = st.selectbox(
        "URL интерфейса Gemini",
        DEFAULT_URLS,
        index=DEFAULT_URLS.index(st.session_state.get("unif_url", DEFAULT_URLS[0])) if st.session_state.get("unif_url", DEFAULT_URLS[0]) in DEFAULT_URLS else 0,
        key="unif_regen_url",
    )
    regen_headless = st.checkbox(
        "Headless режим",
        value=st.session_state.get("unif_headless", False),
        key="unif_regen_headless",
    )
    regen_use_auto_profile = st.checkbox(
        "Отдельный профиль для автоматики (рекомендуется)",
        value=st.session_state.get("unif_auto_profile", True),
        key="unif_regen_auto_profile",
    )

    if "_tmp_new_uddir_regen" in st.session_state:
        st.session_state["unif_regen_user_data_dir"] = st.session_state.pop("_tmp_new_uddir_regen")

    regen_user_data_dir = st.text_input(
        "Путь к профилю (user-data-dir)",
        value=st.session_state.get(
            "unif_regen_user_data_dir",
            st.session_state.get("unif_user_data_dir", os.path.abspath(".chrome_automation_profile")),
        ),
        key="unif_regen_user_data_dir",
    )
    regen_user_data_dir = os.path.abspath(os.path.expanduser(regen_user_data_dir))
    st.caption(f"Реально используется: {regen_user_data_dir}")

    regen_executable_path = st.text_input(
        "Путь к chrome.exe",
        value=st.session_state.get("unif_exe_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"),
        key="unif_regen_exe_path",
    )

    regen_use_cdp = st.checkbox(
        "Запуск и подключение по CDP (рекомендуется)",
        value=st.session_state.get("unif_use_cdp", True),
        key="unif_regen_use_cdp",
    )

    if "_tmp_new_cdp_regen" in st.session_state:
        st.session_state["unif_regen_cdp_url"] = st.session_state.pop("_tmp_new_cdp_regen")

    regen_cdp_url = st.text_input(
        "CDP URL",
        value=st.session_state.get("unif_regen_cdp_url", st.session_state.get("unif_cdp_url", "http://127.0.0.1:9222")),
        key="unif_regen_cdp_url",
    )

    # ----- UI: titles + pasted image per item -----
    if "unif_regen_items" not in st.session_state:
        st.session_state.unif_regen_items = [{"title": "", "image_dataurl": None}]

    st.markdown("### Products (Title + Image)")
    st.caption(
        "Для каждого товара можно вставить картинку через Ctrl+V (скопируйте картинку на сайте → кликните в область вставки → Ctrl+V). "
        "При запуске картинка будет прикреплена в Gemini вместе с текстом."
    )

    new_items = []
    for i, item in enumerate(st.session_state.unif_regen_items):
        st.markdown(f"#### Item #{i+1}")
        # Paste image block
        img_dataurl = _paste_image(key=f"unif_regen_paste_{i}")
        # Preserve previous value if component returns None on reruns
        prev_img = item.get("image_dataurl")
        if img_dataurl is None:
            img_dataurl = prev_img
        t = st.text_input(f"Title #{i+1}", value=item.get("title", ""), key=f"unif_regen_title_{i}")
        new_items.append({"title": t, "image_dataurl": img_dataurl})
        st.markdown("---")

    col_add, col_rem = st.columns([1, 1])
    with col_add:
        if st.button("+ Добавить поле", key="unif_regen_add"):
            st.session_state.unif_regen_items.append({"title": "", "image_dataurl": None})
            st.rerun()
    with col_rem:
        if len(st.session_state.unif_regen_items) > 1 and st.button("− Убрать последнее", key="unif_regen_rem"):
            st.session_state.unif_regen_items = st.session_state.unif_regen_items[:-1]
            st.rerun()

    st.session_state.unif_regen_items = new_items

    st.markdown("---")

    run_regen_btn = st.button("Открыть Gemini и вставить промпты", type="primary", key="unif_regen_run")

    def _build_regen_image_prompt(title: str) -> str:
        title = (title or "").strip()
        return (
            f"мне нужен этот же самый ({title}) но важно чтобы было с другим ракурсом и фоном "
            "но при этом важно чтобы ты старался не искажать товар а просто показал с другого ракурса и другим фоном. "
            "создай такое изображение пожалуйста. изображение должно быть максимально реалистичным. "
            "это для карточки товаров в интернет магазине поэтому товар должен быть отчетливо виден. "
            "СМЕНА РАКУРСА ОЧЕНЬ ВАЖНА"
        )

    if run_regen_btn:
        items_clean = [
            {"title": (it.get("title") or "").strip(), "image_dataurl": it.get("image_dataurl")}
            for it in st.session_state.unif_regen_items
            if (it.get("title") or "").strip()
        ]
        if not items_clean:
            st.error("Добавьте хотя бы один Title.")
        else:
            # Открываем вкладок ровно по количеству заполненных (непустых) тайтлов
            items_to_process = items_clean

            result = {"ok": False, "error": None, "tabs": 0}

            def _worker():
                try:
                    from playwright.sync_api import sync_playwright as _sp
                    import urllib.request as _ul
                    def _is_up(url: str) -> bool:
                        try:
                            with _ul.urlopen(url + "/json/version", timeout=1) as resp:
                                return resp.status == 200
                        except Exception:
                            return False

                    p = _sp().start()
                    ctx = None

                    if regen_use_cdp:
                        # Поднимем Chrome с remote debugging если ещё не поднят
                        if not _is_up(regen_cdp_url):
                            cmd = [
                                regen_executable_path,
                                f"--remote-debugging-port={regen_cdp_url.split(':')[-1]}",
                                f"--user-data-dir={regen_user_data_dir}",
                                "--lang=ru-RU",
                            ]
                            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            deadline = time.time() + 10
                            while time.time() < deadline and not _is_up(regen_cdp_url):
                                time.sleep(0.3)

                        browser = p.chromium.connect_over_cdp(regen_cdp_url)
                        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                    else:
                        # Persistent context
                        if regen_use_auto_profile:
                            os.makedirs(regen_user_data_dir, exist_ok=True)
                        browser = _launch_persistent_ctx_with_retries(
                            p,
                            user_data_dir=regen_user_data_dir if regen_use_auto_profile else None,
                            headless=regen_headless,
                            executable_path=regen_executable_path or None,
                        )
                        ctx = browser

                    # Создаём вкладки и прикрепляем картинку + вставляем текст
                    for i, item in enumerate(items_to_process, 1):
                        title = (item.get("title") or "").strip()
                        image_dataurl = item.get("image_dataurl")
                        page = ctx.new_page()
                        page.set_default_timeout(30000)
                        try:
                            page.goto(regen_url, wait_until="load")
                        except Exception:
                            # если load завис — пробуем domcontentloaded
                            try:
                                page.goto(regen_url, wait_until="domcontentloaded")
                            except Exception:
                                pass

                        # Входим в новый чат и ждём поле ввода
                        try:
                            _start_new_chat(page)
                        except Exception:
                            pass
                        try:
                            _wait_input_ready(page, timeout_ms=60000)
                            _dismiss_overlays(page)
                        except Exception:
                            # fallback: перезагрузка
                            try:
                                page.reload(wait_until="load")
                            except Exception:
                                pass
                            try:
                                _start_new_chat(page)
                            except Exception:
                                pass
                            _wait_input_ready(page, timeout_ms=60000)
                            _dismiss_overlays(page)

                        # Если есть картинка (вставленная в Streamlit) — прикрепляем её в Gemini
                        if image_dataurl:
                            try:
                                import base64 as _b64
                                import tempfile as _tf
                                import re as _re
                                m = _re.match(r"^data:(image/[^;]+);base64,(.+)$", image_dataurl)
                                if m:
                                    mime = m.group(1)
                                    b64 = m.group(2)
                                    ext = ".png"
                                    if "jpeg" in mime or "jpg" in mime:
                                        ext = ".jpg"
                                    elif "webp" in mime:
                                        ext = ".webp"
                                    raw = _b64.b64decode(b64)
                                    tmp = _tf.NamedTemporaryFile(prefix="tmp_rovodev_paste_", suffix=ext, delete=False)
                                    try:
                                        tmp.write(raw)
                                        tmp.flush()
                                    finally:
                                        tmp.close()
                                    ok = _attach_image(page, tmp.name)
                                    _wait_image_attached(page, timeout_ms=10000)
                                    try:
                                        os.unlink(tmp.name)
                                    except Exception:
                                        pass
                                    if ok:
                                        _dismiss_overlays(page)
                            except Exception:
                                pass

                        # Выбираем модель "Быстрая" по умолчанию (как в Tab 1)
                        try:
                            gph._pick_model(page, "Быстрая")
                        except Exception:
                            pass

                        if (title or "").strip():
                            _type_prompt(page, _build_regen_image_prompt(title))
                            # Отправляем запрос
                            # Send ONLY via the explicit send button helper.
                            # Enter fallback caused accidental sends / typing into the wrong area on some accounts.
                            try:
                                _click_send(page)
                            except Exception:
                                pass
                            # Небольшая пауза, чтобы запрос успел уйти
                            time.sleep(0.8)
                        # Переходим к следующей вкладке (следующий item)

                    result["ok"] = True
                    result["tabs"] = len(items_to_process)
                    try:
                        p.stop()
                    except Exception:
                        pass
                except Exception as e:
                    result["error"] = str(e)

            import threading
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join()

            if result.get("error"):
                st.error(f"Ошибка: {result['error']}")
            else:
                st.success(f"Готово: открыто вкладок и вставлено промптов: {result.get('tabs', 0)}")
                st.info("Теперь можно перейти в Chrome и нажать Generate/Send в каждой вкладке.")
