# Streamlit + Playwright: Gemini UI image generation (no API)
# Usage: pip install streamlit playwright
#        playwright install chromium
#        streamlit run gemini_playwright_streamlit.py

import os
import io
import time
import base64
import tempfile
import shutil
from typing import Callable, List, Tuple, Optional, Set
from datetime import datetime
import re
import random
import copy
import threading
from contextlib import contextmanager

import streamlit as st

# Guard Streamlit UI calls when running from background threads (ThreadPoolExecutor).
# Otherwise Streamlit prints noisy "missing ScriptRunContext" warnings and may stall.
try:
    from streamlit.runtime.scriptrunner import get_script_run_ctx  # type: ignore
except Exception:  # pragma: no cover
    get_script_run_ctx = None  # type: ignore


def _st_guard(method_name: str) -> None:
    try:
        orig = getattr(st, method_name)
    except Exception:
        return

    def _wrapped(*args, **kwargs):
        try:
            if get_script_run_ctx is None or get_script_run_ctx() is None:
                return None
        except Exception:
            return None
        return orig(*args, **kwargs)

    try:
        setattr(st, method_name, _wrapped)
    except Exception:
        pass


for _m in ("write", "warning", "error", "info", "markdown", "toast"):
    _st_guard(_m)

import gemini_pw_helpers as gph
from gemini_pw_helpers import (
    _upgrade_gphotos_url,
    _log,
    _hsleep,
    _hthink,
    _maybe_pause,
    _debug_dom,
    _ensure_tmp_file,
    _wait_image_attached,
    _attach_image,
    _find_editor,
    _dismiss_overlays,
    _wait_input_ready,
    _start_new_chat,
    _type_prompt,
    _click_send,
)

# Verbose logs flag (set True to see detailed diagnostics)
verbose_logs = False

# Native Chrome downloads can land outside Playwright's downloads_path.  A file
# can be claimed by only one parallel Tab3 worker before it is written under
# that worker's final output name.
_NATIVE_DOWNLOAD_CLAIMS_LOCK = threading.Lock()
_NATIVE_DOWNLOAD_CLAIMS: set[str] = set()
# Gemini may bypass a worker's private download path and write into the one
# shared Windows Downloads directory.  In that case concurrent clicks are not
# attributable to workers by filename or timestamp alone.  Tab3 therefore
# serializes only the final click-and-file-confirmation phase; image generation
# itself remains fully parallel.
_NATIVE_DOWNLOAD_CLICK_LOCK = threading.Lock()


@contextmanager
def _native_download_cross_process_lock(timeout_s: float = 300.0):
    """Serialize a native Gemini Download click across Streamlit processes.

    ``threading.Lock`` protects workers only inside one Python process.  When
    the same app is launched on several ports, Chrome can still put downloads
    from all of them into the shared Windows Downloads folder.  A named Windows
    mutex is visible to every local Streamlit process, making a newly-created
    file attributable to the one click currently holding this lock.

    The non-Windows path intentionally remains a no-op: this particular native
    Downloads fallback is used by the Windows Chrome workflow.
    """
    if os.name != "nt":
        yield
        return

    handle = None
    acquired = False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        # ``Local`` scopes the mutex to the current interactive Windows session,
        # which is exactly where all local Chrome/Streamlit instances run.
        handle = kernel32.CreateMutexW(
            None,
            False,
            r"Local\GenerateAutomationGeminiNativeDownload",
        )
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")

        wait_ms = max(1, min(int(float(timeout_s) * 1000), 0xFFFFFFFE))
        wait_result = int(kernel32.WaitForSingleObject(handle, wait_ms))
        wait_object_0 = 0x00000000
        wait_abandoned = 0x00000080
        if wait_result not in (wait_object_0, wait_abandoned):
            if wait_result == 0x00000102:
                raise TimeoutError("timed out waiting for the cross-process native Download slot")
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
        acquired = True
        yield
    finally:
        if handle:
            try:
                if acquired:
                    kernel32.ReleaseMutex(handle)
            finally:
                kernel32.CloseHandle(handle)
# Keep helper module log verbosity in sync
try:
    gph.verbose_logs = verbose_logs
except Exception:
    pass

# Fix Windows event loop policy for subprocess in Streamlit (Playwright needs Proactor loop on Windows)
import asyncio, platform, subprocess, time, urllib.request, json
if platform.system() == "Windows":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# Playwright (sync) API
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# Gemini UI (classic) and AI Studio.
# AI Studio "new_chat" is a separate mode with a different DOM (textarea + Run button + download overlay).
DEFAULT_URLS = [
    "https://gemini.google.com/app",
    "https://aistudio.google.com/app",
    "https://aistudio.google.com/prompts/new_chat?model=gemini-2.5-flash-image",
]
BASE_IMAGE_PATH = "10x16.jpg"



# --- Human-like delay helpers ---
_def_min_typing_ms = (55, 120)  # per-char delay range


def _regenerate_prompt(
    page,
    ctx,
    prompt: str,
    idx: int,
    base_dir: str,
    max_images: int = 1,
    attach_base: bool = True,
    base_image_path: str = None,
    model_choice: Optional[str] = None,
    require_browser_download: bool = False,
    browser_download_dir: str | None = None,
    native_download_dirs: List[str] | None = None,
    serialize_native_download_click: bool = False,
    download_debug_hook: Callable[[str], None] | None = None,
    native_download_confirmation_timeout_s: float = 20.0,
):
    """Regenerate images for a given prompt in a fresh chat.

    Returns:
        (images, saved_paths)

    Notes:
        This function previously contained a duplicated "fallback" implementation that was
        unreachable due to an early `return` in the first exception handler. In some UI states
        (e.g. transient Gemini UI glitches), regeneration could fail immediately which in turn
        caused the Playwright context to close right away.

        We now do a small, safe retry (2 attempts) in the SAME browser window.
    """

    def _extract_existing_slug(directory: str, idx_val: int) -> str | None:
        """Reuse the same slug as existing *fast* images for this idx.

        IMPORTANT: Do NOT pick up Nano Banana Pro files which are named like
        `<idx>_pro_<slug>_<vv>.<ext>`.
        """
        try:
            import re as _re

            for name in sorted(os.listdir(directory)):
                # Hard-skip Nano Banana Pro files by their filename prefix.
                if _re.match(rf"^{idx_val}_pro_", name, flags=_re.IGNORECASE) or _re.match(
                    rf"^{idx_val:02d}_pro_", name, flags=_re.IGNORECASE
                ):
                    continue

                m = _re.match(
                    rf"^{idx_val}_(.+)_\d{{2}}(?:_filled)?\.(?:png|jpg|jpeg|bin|webp)$",
                    name,
                    flags=_re.IGNORECASE,
                )
                if not m:
                    continue
                candidate = (m.group(1) or "").strip()
                if candidate.lower().startswith("pro_"):
                    continue
                return candidate
        except Exception:
            pass
        return None

    def _build_slug() -> str:
        existing_slug = _extract_existing_slug(base_dir, idx)
        if existing_slug:
            return existing_slug

        try:
            orig_prompts = st.session_state.get("unif_pw_prompts") or st.session_state.get("pw_prompts")
            orig_prompt = (orig_prompts[idx - 1] if orig_prompts and len(orig_prompts) >= idx else prompt).strip()
            orig_prompt = (
                orig_prompt.replace("Change the white image using this Prompt:", "")
                .replace("DO NOT LEAVE BLANK WHITE SPACE, THIS IS IMPORTANT", "")
                .strip()
            )
        except Exception:
            orig_prompt = (prompt or "").strip()

        import re as _re

        base_slug = _re.sub(r"[^a-zA-Z0-9_-]+", "_", orig_prompt)
        base_slug = _re.sub(r"_+", "_", base_slug).strip("_")
        if not base_slug:
            base_slug = f"prompt_{idx}"

        # Keep filenames compatible with the initial generation flow.
        MAX_BASENAME = 110
        prefix_len = len(str(idx)) + 1  # "{idx}_"
        suffix_len = 1 + 2  # "_" + 2 digits
        allowed_slug_len = max(1, MAX_BASENAME - prefix_len - suffix_len)
        return base_slug[:allowed_slug_len]

    def _limit_slug_for_full_path(directory: str, prefix: str, slug: str, suffix: str) -> str:
        max_full = 255
        try:
            max_full = int(os.environ.get("BULK_IMAGE_MAX_FULL_PATH") or max_full)
        except Exception:
            max_full = 255

        try:
            dir_len = len(os.path.abspath(directory))
        except Exception:
            dir_len = len(str(directory or ""))

        allowed = int(max_full) - dir_len - 1 - len(prefix) - len(suffix)
        if allowed >= len(slug):
            return slug
        return (slug[: max(1, allowed)].rstrip("_-") or "prompt")

    def _save_images(images: List[Tuple[str, bytes]]) -> List[str]:
        # IMPORTANT: base_dir (out_dir) may not exist yet.
        # The task builder intentionally does NOT create directories to avoid leaving empty folders on UI reruns.
        # So we create it lazily right before saving.
        try:
            os.makedirs(base_dir, exist_ok=True)
        except Exception:
            pass

        slug = _build_slug()
        saved: list[str] = []
        for j, (mime, blob) in enumerate(images or [], 1):
            ext = "png" if mime == "image/png" else ("jpg" if mime == "image/jpeg" else "bin")
            prefix = f"{idx}_"
            suffix = f"_{j:02d}.{ext}"
            slug_for_file = _limit_slug_for_full_path(base_dir, prefix, slug, suffix)
            fname = f"{prefix}{slug_for_file}{suffix}"
            fpath = os.path.join(base_dir, fname)
            try:
                with open(fpath, "wb") as f:
                    f.write(blob)
                saved.append(fpath)
            except Exception as e:
                _log(f"Could not save generated image to {fpath}: {e}")
                # Non-fatal: keep other images.
                pass
        return saved

    # We must never re-send the prompt (or click "New chat") after generation has started.
    # Otherwise the UI will open a new chat and send the same prompt again, which looks like a "bot bug".
    last_err: Exception | None = None
    prompt_sent = False

    # First phase: try to start new chat + send prompt (retry only if we fail BEFORE sending)
    for attempt in range(2):
        try:
            _start_new_chat(page)

            if attach_base:
                try:
                    bip = base_image_path or BASE_IMAGE_PATH
                    if bip and os.path.exists(bip):
                        _attach_image(page, bip)
                        _wait_image_attached(page, timeout_ms=3500 if attempt else 2200)
                except Exception:
                    pass

            _dismiss_overlays(page)

            try:
                if model_choice:
                    gph._pick_model(page, model_choice)
            except Exception:
                pass

            _type_prompt(page, prompt)

            # IMPORTANT: sometimes AI Studio/Gemini ignores the click (prompt stays but generation doesn't start).
            # _click_send() already tries multiple strategies; we additionally require it to report success.
            ok_send = False
            try:
                ok_send = bool(_click_send(page))
            except Exception:
                ok_send = False
            if not ok_send:
                raise RuntimeError("Send/Run did not start generation")

            prompt_sent = True
            break
        except Exception as e:
            last_err = e
            _log(f"Ошибка подготовки/отправки промпта (попытка {attempt+1}/2): {e}")
            try:
                time.sleep(0.8)
            except Exception:
                pass
            continue

    if not prompt_sent:
        if last_err is not None:
            _log(f"Перегенерация не удалась (не смог отправить промпт): {last_err}")
        return [], []

    # Second phase: wait for images. If we don't get anything, do an extended wait in the SAME chat,
    # without clicking anything that would trigger a new prompt send.
    try:
        images = _wait_and_download_generated_images(
            page,
            ctx,
            timeout_s=120,
            max_images=max_images,
            require_browser_download=require_browser_download,
            browser_download_dir=browser_download_dir,
            native_download_dirs=native_download_dirs,
            serialize_native_download_click=serialize_native_download_click,
            download_debug_hook=download_debug_hook,
            native_download_confirmation_timeout_s=native_download_confirmation_timeout_s,
        )
        if images:
            saved = _save_images(images)
            return images, saved

        _log("Изображения не обнаружены за 120 сек — продолжаю ожидание (без повторной отправки промпта)…")
        images = _wait_and_download_generated_images(
            page,
            ctx,
            timeout_s=180,
            max_images=max_images,
            require_browser_download=require_browser_download,
            browser_download_dir=browser_download_dir,
            native_download_dirs=native_download_dirs,
            serialize_native_download_click=serialize_native_download_click,
            download_debug_hook=download_debug_hook,
            native_download_confirmation_timeout_s=native_download_confirmation_timeout_s,
        )
        saved = _save_images(images)
        return images, saved
    except Exception as e:
        _log(f"Ошибка ожидания/скачивания изображений: {e}")
        return [], []




def _has_generated_images(page) -> bool:
    """Return True only for a rendered image, not its early DOM shell.

    Gemini adds ``.attachment-container`` and ``single-image`` before the image
    bytes and its Download control are ready.  Treating those wrapper elements
    as success starts the downloader too early and can make a parallel worker
    exhaust its attempts while the UI is still finishing the same image.
    """

    def _has_ready_visual(scope, selector: str, *, canvas: bool = False) -> bool:
        try:
            loc = scope.locator(selector)
            if loc.count() == 0:
                return False
            return bool(
                loc.evaluate_all(
                    """(els, isCanvas) => els.some((el) => {
                        if (isCanvas) {
                          return (el.width || 0) >= 200 && (el.height || 0) >= 200;
                        }
                        return !!(el.complete && (el.naturalWidth || 0) >= 200
                                  && (el.naturalHeight || 0) >= 200);
                    })""",
                    canvas,
                )
            )
        except Exception:
            return False

    try:
        # Prefer the highest-level last response
        loc_pref = page.locator(".presented-response-container").last
        if loc_pref.count():
            last_loc = loc_pref
        else:
            last_loc = page.locator(
                ".presented-response-container, .response-container-content, structured-content-container, .response-container, div[class*='response']"
            ).last
        if not last_loc or last_loc.count() == 0:
            return False
        image_probes = [
            # Classic Gemini: actual image, not the early single-image wrapper.
            "single-image img.image.animate.loaded",
            "single-image img.image.loaded",
            "single-image img.image",
            ".attachment-container.generated-images img.image.animate.loaded",
            ".attachment-container.generated-images img.image.loaded",
            # AI Studio: only model-turn images, never the source image uploaded
            # by the user.
            "[data-turn-role='Model'] ms-image-chunk img.loaded-image",
            "[data-turn-role='Model'] img.loaded-image",
        ]
        for sel in image_probes:
            if _has_ready_visual(last_loc, sel):
                return True

        canvas_probes = [
            "single-image canvas",
            "[data-turn-role='Model'] ms-image-chunk canvas",
        ]
        for sel in canvas_probes:
            if _has_ready_visual(last_loc, sel, canvas=True):
                return True

        # Last-resort global check is deliberately limited to real image nodes;
        # generic containers here would reintroduce the premature-ready race.
        for sel in image_probes:
            if _has_ready_visual(page, sel):
                return True
        for sel in canvas_probes:
            if _has_ready_visual(page, sel, canvas=True):
                return True
        return False
    except Exception:
        return False


# Cache to avoid spamming identical summaries
_LAST_RESPONSE_SUMMARY = None

def _summarize_response_dom(page, last_container=None):
    global _LAST_RESPONSE_SUMMARY
    def cnt_loc(scope_loc, sel):
        try:
            return scope_loc.locator(sel).count()
        except Exception:
            return 0
    try:
        page_loc = page.locator(':scope')
        scope_loc = last_container if last_container is not None else page_loc
        parts = []
        counts = {
            'model-response (page)': cnt_loc(page_loc, 'model-response, [data-test-id*="model-response"]'),
            'response-container (page)': cnt_loc(page_loc, '.response-container, [data-test-id*="response"], div[class*="response"]'),
            'presented-response-container (page)': cnt_loc(page_loc, '.presented-response-container, [class*="presented"]'),
            'structured-content-container (page)': cnt_loc(page_loc, 'structured-content-container'),
            'generated-images (last)': cnt_loc(scope_loc, '.attachment-container.generated-images, .generated-images'),
            'single-image (last)': cnt_loc(scope_loc, 'single-image'),
            'img.image (last)': cnt_loc(scope_loc, '.image-container img.image, img.image'),
            'img.image.animate.loaded (last)': cnt_loc(scope_loc, 'img.image.animate.loaded, img.image.loaded'),
            'canvas (last)': cnt_loc(scope_loc, 'canvas'),
            'bg-image nodes (last)': cnt_loc(scope_loc, "[style*='background-image']"),
            'generated-image-controls (last)': cnt_loc(scope_loc, '.generated-image-controls, [data-test-id*="generated-image"], [data-test-id*="download"]'),
            'download-btn (last)': cnt_loc(scope_loc, "button[data-test-id*='download']"),
        }
        summary = " | ".join(f"{k}={v}" for k, v in counts.items())
        if summary != _LAST_RESPONSE_SUMMARY:
            _log(f"[DOM] {summary}")
            _LAST_RESPONSE_SUMMARY = summary
    except Exception as e:
        _log(f"[DOM] ошибка сборки сводки: {e}")


ess_response_sels = [
    # Classic Gemini
    ".presented-response-container",
    ".response-container-content",
    "structured-content-container",
    ".attachment-container.generated-images",
    "single-image",
    ".response-container",
    ".mdc-card",
    "div[class*='response']",
    # AI Studio new_chat
    "ms-chat-turn",
    "ms-image-chunk",
    ".chat-session-content",
]


def _count_responses(page) -> int:
    total = 0
    for sel in ess_response_sels:
        total += len(page.query_selector_all(sel))
    return total



def _collect_images_from_last_turn(page, max_images: int = 6) -> List[Tuple[str, bytes]]:
    """Conservative fallback: collect visible IMG elements from the last response container.
    Avoids obvious thumbnails by preferring single-image containers and .image.loaded.
    """
    try:
        containers = []
        for sel in ess_response_sels:
            containers.extend(page.query_selector_all(sel))
        if not containers:
            return []
        last = containers[-1]
        probes = [
            # Classic Gemini
            "single-image img.image.animate.loaded",
            "single-image img.image",
            ".attachment-container.generated-images img.image.loaded",
            ".attachment-container.generated-images img",
            # AI Studio new_chat
            "ms-image-chunk img.loaded-image",
            "ms-image-chunk img",
        ]
        results: List[Tuple[str, bytes]] = []
        seen: Set[str] = set()
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
                # prefer data: or googleusercontent full images; otherwise screenshot
                try:
                    if src.startswith("data:image/"):
                        header, b64 = src.split(",", 1)
                        mime = header.split(":", 1)[1].split(";")[0]
                        data = base64.b64decode(b64)
                        results.append((mime, data))
                        continue
                    if src.startswith("http") and "googleusercontent.com" in src:
                        try:
                            # do not request here (no ctx), fallback to screenshot to avoid blocked fetch
                            pass
                        except Exception:
                            pass
                    data = im.screenshot(type="png")
                    if data:
                        results.append(("image/png", data))
                except Exception:
                    # last resort screenshot
                    try:
                        data = im.screenshot(type="png")
                        if data:
                            results.append(("image/png", data))
                    except Exception:
                        pass
        return results
    except Exception:
        return []

# (Old implementation left for reference below)

    # Find last response-like container
    containers = []
    for sel in ess_response_sels:
        containers.extend(page.query_selector_all(sel))
    if not containers:
        return []
    last = containers[-1]

    imgs = last.query_selector_all("img")
    results: List[Tuple[str, bytes]] = []
    for i, im in enumerate(imgs):
        if len(results) >= max_images:
            break
        try:
            src = im.get_attribute("src") or ""
            if src.startswith("data:image/"):
                header, b64 = src.split(",", 1)
                mime = header.split(":", 1)[1].split(";")[0]
                data = base64.b64decode(b64)
                results.append((mime, data))
            else:
                # CORS/blob or gated — use element screenshot
                data = im.screenshot(type="png")
                results.append(("image/png", data))
        except Exception:
            try:
                data = im.screenshot(type="png")
                results.append(("image/png", data))
            except Exception:
                pass
    return results


def _pick_best_image_elements(page, elements, *, max_images: int = 6):
    """Pick best image element handles among candidates.

    Why: Gemini UI often has multiple <img> in the last response (thumbnails, avatars, etc.).
    When we fall back to element screenshots, we must avoid grabbing small/irrelevant images.

    Strategy:
    - Prefer larger *natural* size (naturalWidth/naturalHeight) when available.
    - Fall back to rendered bounding box size.
    - Filter out obvious avatar/profile images and tiny thumbnails.
    """
    try:
        metas = []
        for el in elements or []:
            try:
                m = el.evaluate(
                    """(img) => {
                        try {
                          const r = img.getBoundingClientRect();
                          const nw = img.naturalWidth || 0;
                          const nh = img.naturalHeight || 0;
                          const cls = (img.getAttribute('class') || '');
                          const alt = (img.getAttribute('alt') || '');
                          const src = (img.getAttribute('src') || '');
                          return {nw, nh, bw: r.width || 0, bh: r.height || 0, cls, alt, src};
                        } catch(e){
                          return {nw:0, nh:0, bw:0, bh:0, cls:'', alt:'', src:''};
                        }
                    }"""
                )
            except Exception:
                m = None
            if not isinstance(m, dict):
                continue

            src = (m.get("src") or "")
            alt = (m.get("alt") or "")
            cls = (m.get("cls") or "")

            # Basic filters for non-target images
            low = (alt + " " + cls).lower()
            if any(k in low for k in ["avatar", "profile", "userpic", "account"]):
                continue
            # Skip very small rendered boxes (thumbnails/icons)
            bw = float(m.get("bw") or 0)
            bh = float(m.get("bh") or 0)
            if bw and bh and (bw * bh) < 300 * 300:
                continue

            nw = int(m.get("nw") or 0)
            nh = int(m.get("nh") or 0)
            nat_area = nw * nh
            box_area = int(bw * bh)
            # Score: natural area first, then box area, then URL length (as tie breaker)
            score = (nat_area, box_area, len(src))
            metas.append((score, el))

        metas.sort(key=lambda x: x[0], reverse=True)
        picked = [el for _, el in metas[: max(1, int(max_images))]]
        return picked
    except Exception:
        # If something goes wrong, do not block the pipeline.
        return list(elements or [])[: max(1, int(max_images))]


def _dom_extract_image_srcs(page, root_el=None) -> List[str]:
    """Extract image src URLs by walking DOM (incl. shadowRoot).

    Important: if root_el is provided (ElementHandle), we only scan inside it.
    This prevents picking up unrelated UI images like profile/avatar.

    Returns list of URLs (strings).
    """
    try:
        srcs: List[str] = page.evaluate(
            """
(root) => {
  const out = [];
  const seen = new Set();
  function add(u){ if(!u) return; if(seen.has(u)) return; seen.add(u); out.push(u); }
  function walk(n){
    if(!n) return;
    if(n.nodeType !== 1) return; // element only
    const el = n;
    if(el.tagName === 'IMG') {
      const s = el.getAttribute('src') || '';
      if(s) add(s);
    }
    // Classic Gemini: inside 'single-image'
    if(el.tagName && el.tagName.toLowerCase() === 'single-image'){
      const imgs = el.querySelectorAll('img.image.animate.loaded, img.image');
      for(const im of imgs){ const s = im.getAttribute('src') || ''; add(s); }
    }
    // AI Studio: inside 'ms-image-chunk'
    if(el.tagName && el.tagName.toLowerCase() === 'ms-image-chunk'){
      const imgs = el.querySelectorAll('img.loaded-image, img');
      for(const im of imgs){ const s = im.getAttribute('src') || ''; add(s); }
    }
    for(const ch of el.children) walk(ch);
    if(el.shadowRoot){ for(const ch of el.shadowRoot.children) walk(ch); }
  }
  const start = root || document.documentElement;
  walk(start);
  return out;
}
            """,
            root_el,
        ) or []
        return [s for s in srcs if isinstance(s, str) and s]
    except Exception:
        return []


def _page_fetch_image_bytes(page, url: str, timeout_ms: int = 60000) -> tuple[str, bytes] | None:
    """Fetch image bytes from within the page context.

    Why: ctx.request.get(url) sometimes fails due to auth/CORS. Also allows reading blob: URLs
    without taking element screenshots (which can include UI chrome).

    Returns: (mime, bytes) or None.
    """

    if not url:
        return None

    try:
        res = page.evaluate(
            """async ({url, timeoutMs}) => {
                const ctrl = new AbortController();
                const t = setTimeout(() => ctrl.abort(), timeoutMs || 60000);
                try {
                  const resp = await fetch(url, {credentials: 'include', signal: ctrl.signal});
                  const blob = await resp.blob();
                  const ct = blob.type || (resp.headers.get('content-type') || '');
                  const buf = await blob.arrayBuffer();
                  const bytes = new Uint8Array(buf);
                  let binary = '';
                  const chunk = 0x8000;
                  for (let i = 0; i < bytes.length; i += chunk) {
                    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                  }
                  const b64 = btoa(binary);
                  return {ok: resp.ok, status: resp.status, ct, b64};
                } catch (e) {
                  return {ok: false, status: 0, ct: '', b64: '', err: String(e)};
                } finally {
                  clearTimeout(t);
                }
            }""",
            {"url": url, "timeoutMs": int(timeout_ms)},
        )
    except Exception as e:
        _log(f"[page-fetch] evaluate failed: {e}")
        return None

    if not isinstance(res, dict) or not res.get("ok"):
        return None

    b64 = res.get("b64") or ""
    if not b64:
        return None

    try:
        data = base64.b64decode(b64)
    except Exception:
        return None

    if not data:
        return None

    mime = (res.get("ct") or "").split(";", 1)[0].strip() or "image/jpeg"
    return (mime, data)


class GeminiUIError(RuntimeError):
    """Raised when Gemini UI shows a fatal error banner/message (e.g. 'Something went wrong')."""


class GeminiRateLimitError(RuntimeError):
    """Raised when the UI indicates a rate limit / too many requests."""


class GeminiDailyLimitError(RuntimeError):
    """Raised when the UI indicates a daily quota/limit (e.g. "can't create more today")."""


def _detect_gemini_ui_error(page) -> str | None:
    """Best-effort detector for *fatal* Gemini UI error banners/messages.

    IMPORTANT:
    - Keep this conservative. Gemini UI may contain generic phrases like "try again" in non-error
      parts of the page (buttons/tooltips), which would cause false positives.
    - Prefer scanning typical *alert/snackbar* containers.

    Returns a short matched text or None.
    """

    import re as _re

    # Conservative patterns (avoid overly generic phrases).
    patterns = [
        r"something\s+went\s+wrong",
        r"что-?то\s+пошло\s+не\s+так",
        r"ошибка",
        r"не\s+удалось",
        # Daily quota / limits (classic Gemini sometimes shows these as banners)
        r"can\s*['’]?t\s+create\s+more\s+images\s+today",
        r"can\s*['’]?t\s+create\s+more\s+today",
        r"reached\s+.*limit\s+today",
        r"you\s+have\s+reached\s+.*limit",
        r"try\s+again\s+tomorrow",
        r"come\s+back\s+tomorrow",
        r"лимит",
        r"сегодня\s+.*нельзя",
        r"попробуйте\s+завтра",
        # AI Studio / Gemini throttling
        r"rate\s+limit",
        r"too\s+many\s+requests",
        # AI Studio internal errors
        r"an\s+internal\s+error\s+has\s+occurred",
        r"internal\s+error\s+has\s+occurred",
    ]

    # Typical UI containers for fatal errors/toasts.
    containers = "[role='alert'], .mat-mdc-snack-bar-container, .mdc-snackbar, mat-snack-bar-container, [aria-live='assertive']"

    try:
        for pat in patterns:
            try:
                # Search inside alert-like containers first.
                loc = page.locator(containers).filter(has_text=_re.compile(pat, _re.IGNORECASE)).first
                if loc and loc.count():
                    try:
                        if loc.is_visible():
                            txt = (loc.inner_text() or "").strip()
                            return txt[:200] if txt else "UI_ERROR"
                    except Exception:
                        return "UI_ERROR"

                # Fallback: global text match, but require visibility AND that it is not a tiny/hidden element.
                loc2 = page.locator(f"text=/{pat}/i").first
                if loc2 and loc2.count():
                    try:
                        if loc2.is_visible():
                            # Heuristic: ignore if element is too small (often hidden or in menus).
                            try:
                                box = loc2.bounding_box()
                            except Exception:
                                box = None
                            if box and (box.get('width', 0) < 40 or box.get('height', 0) < 12):
                                continue
                            txt = (loc2.inner_text() or "").strip()
                            return txt[:200] if txt else "UI_ERROR"
                    except Exception:
                        return "UI_ERROR"
            except Exception:
                continue
    except Exception:
        pass

    return None


def _wait_and_download_generated_images(
    page,
    ctx,
    timeout_s: int = 120,
    max_images: int = 6,
    *,
    allow_screenshot_fallback: bool = False,
    request_timeout_ms: int = 60000,
    require_browser_download: bool = False,
    browser_download_dir: str | None = None,
    native_download_dirs: List[str] | None = None,
    prefer_direct_image_bytes: bool = False,
    download_debug_hook: Callable[[str], None] | None = None,
    serialize_native_download_click: bool = False,
    native_download_confirmation_timeout_s: float = 20.0,
    download_only: bool = False,
) -> List[Tuple[str, bytes]]:
    """Wait until generated images appear and collect their bytes.

    When ``require_browser_download`` is true, success means that Chrome emitted
    a real download event after the UI button click. DOM/network preview bytes
    are intentionally not accepted in that mode, because closing a persistent
    context can otherwise cancel Gemini's still-pending browser download.

    ``download_only`` skips the generation-detection phase and immediately
    looks for a Download control.  It is for callers that have already hovered
    a rendered Gemini image whose toolbar is otherwise lazy-mounted.

    Raises:
        GeminiUIError: when the UI reports a fatal error.
    """
    _log(f"Ожидание генерации изображений до {timeout_s} сек…")
    deadline = time.time() + timeout_s
    # A persistent Chrome channel can occasionally write a native download to
    # its configured directory without exposing a timely Playwright download
    # event. Tab3 supplies a fresh directory per worker, so it is safe to use
    # it as a second, file-system-level completion signal.
    browser_download_dir = os.path.abspath(browser_download_dir) if browser_download_dir else None
    # Gemini's native button can bypass Playwright's downloads_path and use a
    # persistent Chrome profile's own directory (or the Windows Downloads
    # folder).  Tab3 passes all known destinations so a completed native file
    # is still collected and saved to the final result folder.
    native_watch_dirs: List[str] = []
    for candidate in [browser_download_dir, *(native_download_dirs or [])]:
        if not candidate:
            continue
        try:
            normalized = os.path.abspath(str(candidate))
            if normalized not in native_watch_dirs and os.path.isdir(normalized):
                native_watch_dirs.append(normalized)
        except Exception:
            continue
    native_watch_state = {"baseline": {}, "click_started_ns": 0}

    def _download_trace(message: str) -> None:
        """Log normally and, for Tab3, preserve a per-worker diagnostic trace."""

        _log(message)
        if download_debug_hook is not None:
            try:
                download_debug_hook(str(message))
            except Exception:
                pass

    _download_trace(
        "[download] native watch directories: "
        + (", ".join(native_watch_dirs) if native_watch_dirs else "(none)")
    )
    target_imgs: List = []  # DOM element handles for fallback screenshots

    # Network capture for images (collect candidates with url+size+mime)
    net_candidates: List[Tuple[str, int, str]] = []  # (url, size, mime)
    seen_urls = set()
    started_collecting = False
    collect_until: Optional[float] = None
    collect_extensions = 0  # how many times we extended the window (max 2)
    dom_candidates_inloop: List[str] = []
    # Gemini's current toolbar wraps the actionable native <button> in
    # <download-generated-image-button><gem-icon-button>...</...>.  Clicking
    # either custom-element wrapper is not equivalent to clicking that native
    # button, so target the real action control first and retain old fallbacks.
    download_button_selector = (
        "button[data-test-id='download-generated-image-button'], "
        "[data-test-id='download-generated-image-button'] button, "
        "[data-test-id*='download-generated-image'] button, "
        "download-generated-image-button button, "
        "button[aria-label*='Скачать изображение' i], "
        "button[aria-label*='Скачать в полном размере' i], "
        "button[aria-label*='Download image' i], "
        "button[aria-label*='Download' i], "
        "button[mattooltip*='Download' i], "
        "button[mattooltip*='Скачать' i], "
        "button.download-button"
    )
    def _on_response(resp):
        nonlocal started_collecting
        try:
            if not started_collecting:
                return  # игнорим сеть до появления контейнеров изображений
            url = resp.url or ""
            headers = resp.headers or {}
            ctype = headers.get('content-type', '')
            # строго берём только конечные изображения от Google Photos CDN
            ok_host = ("googleusercontent.com" in url)
            if not ok_host:
                return
            # IMPORTANT (Pro stability): do NOT call resp.body() in an event handler.
            # On some Gemini UI variants (notably Pro), certain requests can be long-lived/streaming,
            # and resp.body() may block indefinitely, freezing the whole automation.
            #
            # Instead, use Content-Length when available and defer actual downloading to ctx.request.get.
            try:
                size = int(headers.get('content-length') or 0)
            except Exception:
                size = 0
            # Filter obvious thumbnails when size is known.
            if size and size < 25000:
                return
            if url in seen_urls:
                return
            seen_urls.add(url)
            mime = ctype.split(';')[0] if ctype else 'image/jpeg'
            net_candidates.append((url, size, mime))
            _log(f"[net] candidate: {url[:120]}… size={size} mime={mime}")
        except Exception as e:
            _log(f"[net] response handler err: {e}")
    try:
        page.on("response", _on_response)
    except Exception as e:
        _log(f"[net] cannot attach response handler: {e}")

    # scope: last response container
    last_container = None
    prev_counts = {}
    try:
        while time.time() < deadline:
            # A caller which has already located/hovered the image should not
            # wait for Gemini's response-container heuristics again.  Keep the
            # original deadline intact: it is also the confirmation budget for
            # the native Chrome download below.
            if download_only:
                _download_trace("[download] download-only mode: bypassing generation wait")
                break

            # In strict native-download mode a fully rendered image is enough
            # to begin looking for its toolbar.  Some featured-image layouts
            # place that image outside the classic response container, which
            # used to make this loop wait until the whole generation timeout
            # despite a visible picture in Gemini.
            if require_browser_download and _has_generated_images(page):
                _download_trace("[download] rendered image detected; proceeding to native Download control")
                break

            # Bail out early if Gemini itself shows an error banner.
            ui_err = _detect_gemini_ui_error(page)
            if ui_err:
                low = (ui_err or "").lower()
                if (
                    ("rate limit" in low)
                    or ("too many requests" in low)
                    or ("429" in low)
                ):
                    raise GeminiRateLimitError(f"Gemini rate limit: {ui_err}")

                # Daily quota/limit (common in classic Gemini UI)
                if (
                    ("today" in low and "limit" in low)
                    or ("tomorrow" in low)
                    or ("can\u2019t create" in low)
                    or ("can't create" in low)
                    or ("лимит" in low)
                    or ("попробуйте завтра" in low)
                ):
                    raise GeminiDailyLimitError(f"Gemini daily limit: {ui_err}")

                raise GeminiUIError(f"Gemini UI error: {ui_err}")

            # AI Studio: if the final image is embedded as <img src="data:image/...">, capture it immediately.
            try:
                # AI Studio: only look inside MODEL turns to avoid capturing the user-attached base image (blob:)
                ai_img = page.locator("[data-turn-role='Model'] img.loaded-image").last
                if ai_img and ai_img.count():
                    try:
                        src = ai_img.get_attribute("src") or ""
                    except Exception:
                        src = ""
                    if (not require_browser_download) and src.startswith("data:image/"):
                        header, b64 = src.split(",", 1)
                        mime = header.split(":", 1)[1].split(";", 1)[0]
                        data = base64.b64decode(b64)
                        if data:
                            _log(f"[dom-fast] captured embedded image: mime={mime} bytes={len(data)}")
                            return [(mime or "image/png", data)]

                    # AI Studio sometimes uses blob: URLs for the generated image.
                    if (not require_browser_download) and src.startswith("blob:"):
                        for _ in range(3):
                            got = _page_fetch_image_bytes(page, src, timeout_ms=20000)
                            if got:
                                mime, data = got
                                _log(f"[dom-fast] captured blob image: mime={mime} bytes={len(data)}")
                                return [(mime or "image/png", data)]
                            time.sleep(0.4)

                    # Guaranteed fallback (no UI screenshot): if the generated image is visible, screenshot the <img>.
                    # This prevents "hanging" when URL-based download/fetch intermittently fails.
                    try:
                        if allow_screenshot_fallback and src:
                            shot = ai_img.screenshot(type="png")
                            if shot:
                                _log(f"[dom-fast] screenshot fallback: bytes={len(shot)}")
                                return [("image/png", shot)]
                    except Exception:
                        pass
            except Exception:
                pass

            # if network already captured images, stop early
            if net_candidates:
                _log(f"[net] candidates collected: {len(net_candidates)} — выхожу из ожидания")
                break
            for sel in ess_response_sels:
                c = page.locator(sel).count()
                if prev_counts.get(sel) != c:
                    _log(f"[debug] Найдено контейнеров по селектору '{sel}': {c}")
                    prev_counts[sel] = c
            pref = page.locator(".presented-response-container").last
            last_loc = pref if pref.count() else page.locator(
                ".presented-response-container, .response-container-content, structured-content-container, .response-container, div[class*='response']"
            ).last

            # AI Studio new_chat does not use classic Gemini response containers.
            # Fallback to the last chat turn.
            if (not last_loc) or (last_loc.count() == 0):
                # Prefer the last *model* turn in AI Studio; otherwise last turn.
                try:
                    ai_last_model = page.locator("ms-chat-turn").filter(
                        has=page.locator("[data-turn-role='Model']")
                    ).last
                    if ai_last_model and ai_last_model.count():
                        last_loc = ai_last_model
                    else:
                        ai_last = page.locator("ms-chat-turn").last
                        if ai_last and ai_last.count():
                            last_loc = ai_last
                except Exception:
                    pass

            last_container = last_loc
            # NOTE: _debug_dom() does heavy deep DOM walking and can become extremely slow/hangy
            # on some Gemini UI variants (notably Pro). Keep it only for verbose debugging.
            if verbose_logs:
                _debug_dom(page)
            _summarize_response_dom(page, last_container)
            if last_container and last_container.count():
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                probe_selectors = [
                    # Classic Gemini
                    ".attachment-container.generated-images",
                    "single-image",
                    # AI Studio new_chat (ONLY model turn, ignore user attached image)
                    "[data-turn-role='Model'] ms-image-chunk",
                    "[data-turn-role='Model'] img.loaded-image",
                ]
                found_any = False
                for psel in probe_selectors:
                    cnt = last_container.locator(psel).count()
                    _log(f"[debug] Проба селектора изображений '{psel}': найдено {cnt}")
                    if cnt > 0:
                        found_any = True
                if found_any and not started_collecting:
                    started_collecting = True
                    # AI Studio new_chat usually exposes the final image via data:image/... quickly.
                    # Keep the collection window short to avoid extra wait after the image is already visible.
                    try:
                        is_aistudio = "aistudio.google.com" in (page.url or "")
                    except Exception:
                        is_aistudio = False
                    # In direct-image mode the DOM source is the preferred
                    # original-byte path, so no browser Download click is used.
                    win_s = 0 if require_browser_download else (3 if is_aistudio else 12)
                    collect_until = time.time() + win_s
                    _log(f"[net] детектирован контейнер с изображениями — начинаю сбор кандидатов ({win_s} сек)")
                # While waiting, also try DOM extraction and click Download once after 2s
                if started_collecting:
                    # DOM extract during window (scope to last response to avoid avatars/UI)
                    _root = None
                    try:
                        _root = last_container.element_handle() if last_container else None
                    except Exception:
                        _root = None
                    dom_srcs = _dom_extract_image_srcs(page, _root)
                    # Sources are scoped to the final model response.  Gemini
                    # can serve the generated image from several CDN hosts, so
                    # do not reject a valid direct HTTP source by hostname.
                    dom_filtered = [
                        u
                        for u in dom_srcs
                        if u.startswith(('https://', 'http://', 'data:image/', 'blob:'))
                        and not any(k in u.lower() for k in ['avatar', 'profile', 'userpic', 'account'])
                    ]
                    if dom_filtered and not dom_candidates_inloop:
                        dom_candidates_inloop = dom_filtered
                        _log(f"[dom-extract] in-loop candidates: {len(dom_filtered)}")
                        for i, u in enumerate(dom_filtered[:4]):
                            _log(f"[dom-extract] in-loop url[{i}]={u[:160]}…")

                        # If AI Studio gave us a data:image/... already, return immediately (no extra wait).
                        try:
                            for u in dom_candidates_inloop:
                                if not isinstance(u, str):
                                    continue
                                if (not require_browser_download) and u.startswith('data:image/'):
                                    header, b64 = u.split(',', 1)
                                    mime = header.split(':', 1)[1].split(';', 1)[0]
                                    data = base64.b64decode(b64)
                                    if data:
                                        _log(f"[dom] immediate embedded image: mime={mime} bytes={len(data)}")
                                        return [(mime or 'image/png', data)]
                                if (not require_browser_download) and u.startswith('blob:'):
                                    got = _page_fetch_image_bytes(page, u, timeout_ms=40000)
                                    if got:
                                        mime, data = got
                                        _log(f"[dom] immediate blob image: mime={mime} bytes={len(data)}")
                                        return [(mime or 'image/png', data)]
                        except Exception:
                            pass

                        if prefer_direct_image_bytes:
                            # The image is fully visible and we have its
                            # original source URL/blob.  Skip Chrome's native
                            # Download UI and proceed directly to byte fetch.
                            break

                        # Otherwise do not break early; continue collecting network candidates during the window
                    # Do not click Download here without expect_download: new Gemini downloads directly.
                    # The captured UI download path below performs the single explicit download click.
                    pass
                if collect_until and time.time() > collect_until:
                    if net_candidates or dom_candidates_inloop:
                        _log(f"[window] окно сбора закрыто; net={len(net_candidates)} dom={len(dom_candidates_inloop)}")
                        break
                    else:
                        if require_browser_download:
                            _log("[window] strict browser-download mode: skipping preview collection")
                            break
                        # For AI Studio keep it snappy: no extensions, just exit.
                        try:
                            is_aistudio = "aistudio.google.com" in (page.url or "")
                        except Exception:
                            is_aistudio = False
                        if is_aistudio:
                            _log("[window] AI Studio: окно сбора пустое — выхожу без продлений")
                            break
                        if collect_extensions < 2:
                            collect_until = time.time() + 3
                            collect_extensions += 1
                            _log("[window] продлеваю окно сбора ещё на 3 сек — пока пусто")
                        else:
                            _log("[window] достигнут предел продлений окна ожидания")
                            break
            time.sleep(0.4)
    finally:
        pass

    if last_container and last_container.count():
        try:
            si_cnt = last_container.locator("single-image").count()
            _log(f"[debug] В последнем ответе single-image контейнеров: {si_cnt}")
        except Exception:
            pass
        try:
            gi_cnt = last_container.locator(".attachment-container.generated-images, .generated-images").count()
            _log(f"[debug] В последнем ответе контейнеров generated-images: {gi_cnt}")
        except Exception:
            pass

    if not net_candidates and last_container and last_container.count():
        _log("Пробовал искать видимые узлы, переключился на сетевой перехват, но пока пусто")
    _summarize_response_dom(page, last_container)

    # UI download is captured below with page.expect_download, so avoid untracked clicks here.

    # Detach network listener
    try:
        page.off("response", _on_response)
    except Exception:
        pass

    # Best-quality path:
    # Trigger the same UI download that the user would click manually.
    # This typically yields the full-size asset (e.g., 1456x720) vs DOM thumbnails (~1024px).
    # Best-quality path: trigger the same native Gemini Download action the
    # user clicks manually and wait for its actual completed image bytes.
    if max_images >= 1 and not prefer_direct_image_bytes:
        try:
            import mimetypes

            def _read_completed_browser_file() -> tuple[str, bytes] | None:
                """Read a completed native Chrome download from this worker's dir.

                Gemini/Chrome can leave a *completed* image under a random
                ``.tmp`` name instead of renaming it before the page closes.
                We may accept such a file only in the private directory of this
                worker, only after its size is stable and the image container is
                structurally complete.  ``.crdownload``/``.part`` remain
                in-progress files and are never accepted.
                """
                if not native_watch_dirs:
                    return None

                def _complete_image_mime(data: bytes) -> str | None:
                    """Return MIME only for a self-contained PNG/JPEG/WebP file."""
                    if len(data) < 25000:
                        return None
                    if data.startswith(b"\x89PNG\r\n\x1a\n"):
                        # A finished PNG must contain its IEND chunk.
                        return "image/png" if data.endswith(b"IEND\xaeB`\x82") else None
                    if data.startswith(b"\xff\xd8\xff"):
                        # JPEG EOI lets us distinguish a completed .tmp from a
                        # still-written JPEG without parsing the whole image.
                        return "image/jpeg" if data.rstrip().endswith(b"\xff\xd9") else None
                    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                        # RIFF stores the exact total size at bytes 4..7.
                        declared_size = int.from_bytes(data[4:8], "little") + 8
                        return "image/webp" if declared_size == len(data) else None
                    return None

                try:
                    candidates = []
                    baseline = native_watch_state.get("baseline") or {}
                    click_started_ns = int(native_watch_state.get("click_started_ns") or 0)
                    for watch_dir in native_watch_dirs:
                        for name in os.listdir(watch_dir):
                            path = os.path.join(watch_dir, name)
                            if not os.path.isfile(path):
                                continue
                            stat = os.stat(path)
                            if stat.st_size < 25000:
                                continue
                            previous = baseline.get(path)
                            changed = (
                                previous is None
                                or previous[0] != stat.st_mtime_ns
                                or previous[1] != stat.st_size
                            )
                            # Never pick up an old user download: only files
                            # created or changed after this exact button click
                            # are eligible for this worker.
                            recent = stat.st_mtime_ns >= (click_started_ns - 2_000_000_000)
                            if changed and recent:
                                candidates.append((stat.st_mtime_ns, path))
                    if not candidates:
                        return None
                    for _mtime, path in sorted(candidates, key=lambda x: x[0], reverse=True):
                        # Chrome can rename .tmp -> .jpg between directory
                        # enumeration and this read.  That is normal, so a
                        # vanished candidate must not abort the whole scan and
                        # hide another finished file in the same directory.
                        try:
                            with _NATIVE_DOWNLOAD_CLAIMS_LOCK:
                                if path in _NATIVE_DOWNLOAD_CLAIMS:
                                    continue

                            # A stable size is required before reading
                            # extensionless files: a .tmp can otherwise be
                            # observed mid-write.
                            size_before = os.path.getsize(path)
                            time.sleep(0.35)
                            if os.path.getsize(path) != size_before:
                                continue
                            with open(path, "rb") as f:
                                data = f.read()
                            mime = _complete_image_mime(data)
                            if not mime:
                                continue
                            with _NATIVE_DOWNLOAD_CLAIMS_LOCK:
                                if path in _NATIVE_DOWNLOAD_CLAIMS:
                                    continue
                                _NATIVE_DOWNLOAD_CLAIMS.add(path)

                            # A Gemini native download can ignore Playwright
                            # and land in the user's shared Downloads folder.
                            # Once its bytes are known complete, move that
                            # source into this worker's disposable directory.
                            # The caller then writes the canonical final name.
                            if browser_download_dir:
                                try:
                                    source_parent = os.path.normcase(os.path.abspath(os.path.dirname(path)))
                                    private_parent = os.path.normcase(os.path.abspath(browser_download_dir))
                                    if source_parent != private_parent:
                                        staged_path = os.path.join(
                                            private_parent,
                                            f"captured_native_{time.time_ns()}.tmp",
                                        )
                                        shutil.move(path, staged_path)
                                        _download_trace(
                                            f"[download] moved native source into worker directory: "
                                            f"{os.path.basename(path)}"
                                        )
                                except Exception as move_error:
                                    _download_trace(
                                        f"[download] could not move native source from its original directory: {move_error}"
                                    )
                            _download_trace(
                                f"[download] completed native file detected: "
                                f"{os.path.basename(path)} ({len(data)} bytes)"
                            )
                            return (mime, data)
                        except FileNotFoundError:
                            _download_trace(
                                f"[download] candidate was renamed while scanning: {os.path.basename(path)}"
                            )
                            continue
                        except OSError as candidate_error:
                            _download_trace(
                                f"[download] candidate is not ready yet: {os.path.basename(path)} ({candidate_error})"
                            )
                            continue
                    return None
                except Exception as e:
                    _download_trace(f"[download] native-download directory check failed: {e}")
                    return None

            def _read_download_bytes(dl) -> tuple[str, bytes] | None:
                # Both failure() and path() wait for the browser download to
                # finish.  Do not let the caller close its persistent context
                # while Chrome still has a failed/partial download in flight.
                try:
                    failure = dl.failure()
                except Exception:
                    failure = None
                if failure:
                    _download_trace(f"[download] browser reported failure: {failure}")
                    return None
                try:
                    p = dl.path()
                except Exception:
                    p = None
                if not p:
                    return None
                try:
                    data = open(p, "rb").read()
                except Exception:
                    return None
                if not data:
                    return None
                # mime by suggested filename
                try:
                    name = (dl.suggested_filename or "")
                except Exception:
                    name = ""
                mime, _ = mimetypes.guess_type(name)
                if not mime:
                    mime = "image/jpeg"
                return (mime, data)

            ui_click_attempted = False

            def _native_watch_snapshot() -> str:
                """Compact final filesystem state for a failed native download."""

                entries: list[str] = []
                for watch_dir in native_watch_dirs:
                    try:
                        for name in os.listdir(watch_dir):
                            path = os.path.join(watch_dir, name)
                            if not os.path.isfile(path):
                                continue
                            stat = os.stat(path)
                            entries.append(
                                f"{os.path.basename(watch_dir)}:{name} "
                                f"({stat.st_size} bytes, {stat.st_mtime_ns})"
                            )
                            if len(entries) >= 20:
                                return "; ".join(entries)
                    except Exception as e:
                        entries.append(f"{os.path.basename(watch_dir)}: <scan error {e}>")
                return "; ".join(entries) if entries else "(no files)"

            def _try_one_button_download(btn) -> tuple[str, bytes] | None:
                nonlocal ui_click_attempted
                if not btn or btn.count() == 0:
                    return None
                try:
                    btn.scroll_into_view_if_needed(timeout=800)
                except Exception:
                    pass
                try:
                    btn.hover(timeout=700)
                except Exception:
                    pass

                # Register the Playwright listener before clicking, but do not
                # block in ``expect_download``.  Gemini can write a complete
                # native .tmp file before it emits that event; the old blocking
                # 30-second expectation made an already-finished window look
                # stuck and delayed its final save/close.
                captured_downloads = []

                def _capture_download(download) -> None:
                    captured_downloads.append(download)
                    _download_trace("[download] Playwright download event received")

                native_click_lock_taken = False
                cross_process_click_lock = None
                cross_process_click_lock_taken = False
                try:
                    if serialize_native_download_click:
                        _download_trace("[download] waiting for exclusive native Download slot")
                        _NATIVE_DOWNLOAD_CLICK_LOCK.acquire()
                        native_click_lock_taken = True
                        _download_trace("[download] local native Download slot acquired")
                        _download_trace("[download] waiting for cross-process native Download slot")
                        cross_process_click_lock = _native_download_cross_process_lock()
                        cross_process_click_lock.__enter__()
                        cross_process_click_lock_taken = True
                        _download_trace("[download] cross-process native Download slot acquired")

                    # Snapshot *before* the click.  When the shared Windows
                    # Downloads folder is involved, the exclusive local and
                    # cross-process slots above make every newly-created file
                    # attributable to this one exact Gemini click.
                    snapshot = {}
                    for watch_dir in native_watch_dirs:
                        try:
                            for name in os.listdir(watch_dir):
                                path = os.path.join(watch_dir, name)
                                if os.path.isfile(path):
                                    stat = os.stat(path)
                                    snapshot[path] = (stat.st_mtime_ns, stat.st_size)
                        except Exception:
                            continue
                    native_watch_state["baseline"] = snapshot
                    native_watch_state["click_started_ns"] = time.time_ns()
                    _download_trace(f"[download] watching {len(native_watch_dirs)} native directory(s) after click")
                    page.on("download", _capture_download)
                    try:
                        btn.click(timeout=1500)
                    except Exception:
                        try:
                            btn.click(force=True, timeout=1500)
                        except Exception:
                            raise
                    ui_click_attempted = True
                    _download_trace("[download] Gemini Download button clicked")

                    # A shared native destination is serialized, so cap this
                    # confirmation window.  Normal Chrome downloads finish in
                    # a few seconds; a failed click must not block every other
                    # already-generated pin for the full generation timeout.
                    if serialize_native_download_click:
                        # Generation may have consumed most of ``deadline``
                        # while this worker waited for another process.  The
                        # already-clicked download still deserves its full
                        # confirmation window.
                        watch_deadline = time.time() + max(
                            3.0,
                            float(native_download_confirmation_timeout_s or 20.0),
                        )
                    else:
                        watch_deadline = deadline if browser_download_dir else min(deadline, time.time() + 30.0)
                    page_closure_logged = False
                    while time.time() < watch_deadline:
                        native_file = _read_completed_browser_file()
                        if native_file:
                            _download_trace("[download] native Chrome download captured without waiting for event")
                            return native_file

                        # For Tab3, the native filesystem is authoritative.
                        # Calling ``download.failure()`` / ``download.path()``
                        # here can block until Chrome tears the event object
                        # down and previously produced a false
                        # "Target ... has been closed" before the .tmp had been
                        # observed.  Keep polling the actual file instead.
                        if captured_downloads and not serialize_native_download_click:
                            dl = captured_downloads[-1]
                            got = _read_download_bytes(dl)
                            if got:
                                _download_trace(f"[download] UI download captured: {dl.suggested_filename}")
                                return got

                        # Once Tab3 has clicked the control, the native file
                        # is the only completion signal we need.  Chrome can
                        # close/navigate the page while it renames .tmp to
                        # .jpg/.crdownload; touching ``page`` at that instant
                        # used to abort the file poll with a false
                        # "Target ... has been closed".  Keep this phase
                        # independent of the page.
                        if serialize_native_download_click:
                            try:
                                if page.is_closed() and not page_closure_logged:
                                    _download_trace(
                                        "[download] page closed after click; continuing filesystem confirmation"
                                    )
                                    page_closure_logged = True
                            except Exception:
                                if not page_closure_logged:
                                    _download_trace(
                                        "[download] page became unavailable after click; continuing filesystem confirmation"
                                    )
                                    page_closure_logged = True
                            time.sleep(0.1)
                        else:
                            # Non-Tab3 callers retain the former Playwright
                            # event-dispatch behavior.
                            page.wait_for_timeout(100)

                    if serialize_native_download_click:
                        _download_trace(
                            "[download] no complete native image before confirmation deadline; "
                            f"filesystem: {_native_watch_snapshot()}"
                        )
                except Exception as e:
                    _download_trace(f"[download] Chrome download was not confirmed: {e}")
                finally:
                    try:
                        page.off("download", _capture_download)
                    except Exception:
                        pass
                    if cross_process_click_lock_taken and cross_process_click_lock is not None:
                        try:
                            cross_process_click_lock.__exit__(None, None, None)
                            _download_trace("[download] cross-process native Download slot released")
                        except Exception:
                            pass
                    if native_click_lock_taken:
                        try:
                            _NATIVE_DOWNLOAD_CLICK_LOCK.release()
                            _download_trace("[download] local native Download slot released")
                        except Exception:
                            pass

                return None

            def _try_ui_downloads() -> list[tuple[str, bytes]]:
                """Try the response toolbar first, then the page-level toolbar.

                In the current Gemini UI the toolbar may be mounted outside the
                response container.  The old code searched the whole page only
                when it could not find a response container at all, so it missed
                exactly that layout.
                """
                scopes = []
                try:
                    if last_container and last_container.count():
                        scopes.append(("response", last_container))
                except Exception:
                    pass
                scopes.append(("page", page))

                results: list[tuple[str, bytes]] = []
                max_count = max(1, int(max_images or 1))
                for scope_name, scope in scopes:
                    if len(results) >= max_count:
                        break
                    try:
                        btns = scope.locator(download_button_selector)
                        count = int(btns.count() or 0)
                    except Exception:
                        count = 0
                        btns = None
                    if count:
                        _download_trace(f"[download] {scope_name} controls found: {count}")
                    for i in range(min(max_count - len(results), count)):
                        got = _try_one_button_download(btns.nth(i))
                        if got:
                            results.append(got)
                            continue

                        # The response-level and page-level locators often
                        # resolve to the same Gemini Download control.  Once
                        # it has been clicked, its asynchronous transfer must
                        # be allowed to finish; clicking a fallback control
                        # here cancels/restarts that very transfer.
                        if ui_click_attempted:
                            _download_trace(
                                "[download] first Download click was not confirmed yet; "
                                "not clicking a duplicate control"
                            )
                            return results

                    # The page-level scope is only a fallback for layouts
                    # where the response container had no Download control at
                    # all.  Do not use it to click an already-handled image a
                    # second time.
                    if results:
                        return results
                return results

            # The image element normally appears a little before the toolbar.
            # Poll briefly for the control instead of treating that tiny DOM gap
            # as a failed download.  This adds no delay on the normal path and is
            # deliberately capped so parallel workers keep their previous speed.
            ui_download_deadline = time.monotonic() + 5.0
            while True:
                got_many = _try_ui_downloads()
                if got_many:
                    return got_many
                if ui_click_attempted:
                    # The click handler has already watched the private folder
                    # until its budget expired.  Do not click again: a second
                    # click can create duplicate/orphaned .tmp downloads.
                    break
                if time.monotonic() >= ui_download_deadline:
                    break
                time.sleep(0.25)
        except Exception as e:
            # Never block the pipeline on UI download.
            _log(f"[download] UI download path error: {e}")

    # In strict mode do not turn a preview/network response into a successful
    # result. The caller keeps this page alive and retries until Chrome emits a
    # real download event, instead of closing the context after a UI toast.
    if require_browser_download:
        _download_trace("[download] no confirmed Chrome download yet; keeping the page open")
        return []

    # If still nothing, try aggressively screenshotting visible single-image elements first
    # Direct DOM/blob/URL extraction below must run before any screenshot.
    # In direct-image mode (used by Tab3) this early legacy path is disabled;
    # the caller may explicitly opt into a screenshot only outside that mode.
    if (
        allow_screenshot_fallback
        and not prefer_direct_image_bytes
        and (not net_candidates)
        and last_container
        and last_container.count()
    ):
        try:
            els = page.query_selector_all("single-image img.image.animate.loaded, single-image img.image")
            if els:
                _log(f"[fallback] Снимаю скриншоты с {len(els)} элементов single-image…")
                shots = []
                best = _pick_best_image_elements(page, els, max_images=max_images)
                for el in best[:max_images]:
                    try:
                        # Wait until image is really loaded (avoid placeholders/thumbnails)
                        try:
                            el.evaluate("(img) => img.complete")
                        except Exception:
                            pass
                        data = el.screenshot(type='png')
                        if data:
                            shots.append(("image/png", data))
                    except Exception:
                        pass
                if shots:
                    return shots
        except Exception:
            pass

    # Detect AI Studio failure banner early (so callers can retry with a fresh session).
    try:
        body_txt = (page.inner_text('body') or '').lower()
        if 'failed to generate content' in body_txt and 'permission denied' in body_txt:
            raise RuntimeError('AI Studio: Failed to generate content: permission denied')
    except Exception as e:
        # If we already raised RuntimeError above, re-raise.
        if isinstance(e, RuntimeError):
            raise
        # ignore other transient DOM read errors
        pass

    # AI Studio often embeds the final image directly in DOM as data:image/...; save it without UI downloads.
    try:
        for u in (dom_candidates_inloop or []):
            if isinstance(u, str) and u.startswith('data:image/'):
                header, b64 = u.split(',', 1)
                mime = header.split(':', 1)[1].split(';')[0]
                data = base64.b64decode(b64)
                if data:
                    _log(f"[dom] captured embedded image: mime={mime} bytes={len(data)}")
                    return [(mime or 'image/png', data)]
    except Exception:
        pass

    # If we collected network candidates, choose best and download via ctx
    if net_candidates:
        _log(f"[net] Всего кандидатов: {len(net_candidates)}")
        # Prefer the *largest* available variant, then fetch an upgraded (high-res) URL.
        # NOTE: previously this preferred 's1024', which often produced 1024px thumbnails.
        import re as _re
        def _s_param(u: str) -> int:
            m = _re.search(r"[=/]s(\d+)", u)
            return int(m.group(1)) if m else 0
        def score(u: str, sz: int) -> Tuple[int, int, int]:
            sp = _s_param(u)
            rj = 1 if ("-rj" in u) else 0
            # Bigger 's' means higher resolution.
            return (sp, sz, rj)
        pool = list(net_candidates)

        # If we have DOM candidates from the last response, keep only network URLs that match them.
        # This avoids accidentally picking up profile/avatar images (also hosted on googleusercontent).
        try:
            def _norm(u: str) -> str:
                u = (u or "")
                u = u.split('?', 1)[0]
                # drop common sizing suffixes (=s1024, =w123-h456, etc.)
                u = re.sub(r"=s\d+.*$", "", u)
                u = re.sub(r"=w\d+-h\d+.*$", "", u)
                return u

            dom_norm = {_norm(u) for u in (dom_candidates_inloop or []) if isinstance(u, str)}
            if dom_norm:
                before = len(pool)
                pool = [c for c in pool if _norm(c[0]) in dom_norm]
                _log(f"[net] scoped filter: {before} -> {len(pool)}")
                # If filtering removed everything (DOM extraction failed), fall back to original pool.
                if not pool:
                    pool = list(net_candidates)
        except Exception:
            pass

        pool.sort(key=lambda x: score(x[0], x[1]), reverse=True)
        chosen = pool[:max_images]
        results: List[Tuple[str, bytes]] = []
        def _try_fetch(ctx, url: str):
            # try original first, then upgraded, then strip query; return (mime, data) or None
            try:
                r = ctx.request.get(url, timeout=int(request_timeout_ms or 60000))
                if r.ok:
                    mime = r.headers.get('content-type', 'image/jpeg').split(';')[0]
                    return (mime, r.body())
            except Exception:
                pass
            try:
                u2 = _upgrade_gphotos_url(url)
                if u2 != url:
                    r = ctx.request.get(u2, timeout=int(request_timeout_ms or 60000))
                    if r.ok:
                        mime = r.headers.get('content-type', 'image/jpeg').split(';')[0]
                        return (mime, r.body())
            except Exception:
                pass
            try:
                if '?' in url:
                    u3 = url.split('?', 1)[0]
                    r = ctx.request.get(u3, timeout=int(request_timeout_ms or 60000))
                    if r.ok:
                        mime = r.headers.get('content-type', 'image/jpeg').split(';')[0]
                        return (mime, r.body())
            except Exception:
                pass
            return None
        for url, size, mime in chosen:
            try:
                _log(f"[choose] pick: url={url[:140]}… size={size} mime={mime}")
                got = _try_fetch(ctx, url)
                if got:
                    real_mime, data = got
                    results.append((real_mime, data))
                    _log(f"[save] ok: mime={real_mime} size={len(data)}")
                else:
                    _log(f"[save] fail (400/blocked?) for {url}")
            except Exception as e:
                _log(f"[save] err: {e}")
        if results:
            return results
        # network candidates present but fetch failed.
        # Prefer a non-screenshot fallback first: fetch in page context (handles auth/CORS better).
        for url, _size, _mime in chosen:
            try:
                for cand in (url, _upgrade_gphotos_url(url)):
                    got2 = _page_fetch_image_bytes(page, cand, timeout_ms=60000)
                    if got2:
                        results.append(got2)
                        break
            except Exception:
                continue
        if results:
            return results

        # If explicitly allowed, last resort: element screenshots (can include UI chrome in rare cases).
        if allow_screenshot_fallback:
            try:
                probes = [
                    "single-image img.image.animate.loaded",
                    "single-image img.image",
                    ".attachment-container.generated-images img",
                ]
                all_els = []
                for psel in probes:
                    try:
                        all_els.extend(page.query_selector_all(psel) or [])
                    except Exception:
                        continue
                best = _pick_best_image_elements(page, all_els, max_images=max_images)
                for el in best[:max_images]:
                    try:
                        data = el.screenshot(type='png')
                        if data:
                            return [("image/png", data)]
                    except Exception:
                        continue
            except Exception:
                pass

    # Prefer DOM candidates collected in-loop
    _root2 = None
    try:
        _root2 = last_container.element_handle() if last_container else None
    except Exception:
        _root2 = None
    dom_srcs = dom_candidates_inloop or _dom_extract_image_srcs(page, _root2)
    if dom_srcs:
        _log(f"[dom-extract] candidates: {len(dom_srcs)}")
        for i, u in enumerate(dom_srcs[:6]):
            _log(f"[dom-extract] url[{i}]={u[:160]}…")
        # The source is already scoped to the last *model* response, so accept
        # any direct HTTP image URL as well as data:/blob:.  Gemini changes CDN
        # hosts regularly; restricting this to googleusercontent caused valid
        # generated images to be ignored even though they were visible in UI.
        filtered = [
            u for u in dom_srcs
            if u.startswith(("https://", "http://", "data:image/", "blob:"))
            and not any(token in u.lower() for token in ("avatar", "profile", "userpic"))
        ]
        if filtered:
            def dscore(u: str) -> Tuple[int, int, int, int]:
                # Prefer higher-res URLs when possible
                sp = 0
                try:
                    import re as __re
                    m = __re.search(r"[=/]s(\d+)", u)
                    sp = int(m.group(1)) if m else 0
                except Exception:
                    sp = 0
                blob_bonus = 1 if u.startswith('blob:') else 0
                rj = 1 if ('-rj' in u) else 0
                return (sp, rj, blob_bonus, len(u))
            filtered.sort(key=lambda u: dscore(u), reverse=True)
            chosen = filtered[:max_images]
            results2: List[Tuple[str, bytes]] = []
            for url in chosen:
                try:
                    _log(f"[choose] pick(dom): url={url[:160]}…")
                    if url.startswith('data:image/'):
                        header, b64 = url.split(',', 1)
                        mime = header.split(':', 1)[1].split(';')[0]
                        data = base64.b64decode(b64)
                        results2.append((mime, data))
                        _log(f"[save] ok: mime={mime} size={len(data)} (data url)")
                    elif url.startswith('blob:'):
                        # Prefer downloading the blob bytes from within the page (no screenshots).
                        gotb = None
                        try:
                            gotb = _page_fetch_image_bytes(page, url, timeout_ms=60000)
                        except Exception:
                            gotb = None
                        if gotb:
                            mime_b, data_b = gotb
                            results2.append((mime_b, data_b))
                            _log(f"[save] ok: blob fetch size={len(data_b)}")
                        elif allow_screenshot_fallback:
                            # last resort: element screenshot by locating matching IMG with this src
                            try:
                                im = page.query_selector(f"img[src='{url}']")
                                if im:
                                    data = im.screenshot(type='png')
                                    if data:
                                        results2.append(('image/png', data))
                                        _log(f"[save] ok: blob screenshot size={len(data)}")
                            except Exception as e:
                                _log(f"[save] blob screenshot err: {e}")
                    else:
                        r = ctx.request.get(_upgrade_gphotos_url(url), timeout=60000)
                        content_type = r.headers.get('content-type', 'image/jpeg').split(';')[0]
                        if r.ok and content_type.startswith('image/'):
                            data = r.body()
                            results2.append((content_type, data))
                            _log(f"[save] ok: mime={content_type} size={len(data)}")
                        else:
                            _log(f"[save] http {r.status} content-type={content_type} for {url}")
                except Exception as e:
                    _log(f"[save] err(dom): {e}")
            if results2:
                return results2

    # Fallback: старый поток через DOM (может не сработать при закрытом shadow)
    results: List[Tuple[str, bytes]] = []
    seen_srcs = set()
    for idx_im, im in enumerate(target_imgs, start=1):
        if len(results) >= max_images:
            break
        _log(f"Картинка #{idx_im}: начинаю попытки скачивания…")
        try:
            src = (im.get_attribute("src") or "").strip()
            _log(f"Картинка #{idx_im}: src='{src[:80]}…'")
        except Exception:
            src = ""
        if not src:
            _log(f"Картинка #{idx_im}: src пустой — делаю скриншот элемента")
            try:
                data = im.screenshot(type="png")
                results.append(("image/png", data))
            except Exception as e:
                _log(f"Картинка #{idx_im}: ошибка скриншота: {e}")
            continue
        if src in seen_srcs:
            _log(f"Картинка #{idx_im}: этот src уже скачивали — пропускаю")
            continue
        seen_srcs.add(src)
        if src.startswith("http://") or src.startswith("https://"):
            try:
                _log(f"Картинка #{idx_im}: пробую скачать по URL через context.request…")
                resp = ctx.request.get(_upgrade_gphotos_url(src), timeout=60000)
                _log(f"Картинка #{idx_im}: ответ {resp.status} content-type={resp.headers.get('content-type')}")
                if resp.ok:
                    data = resp.body()
                    mime = resp.headers.get("content-type", "image/jpeg").split(";")[0]
                    results.append((mime, data))
                    _log(f"Картинка #{idx_im}: скачано через URL, mime={mime}, размер={len(data)} байт")
                    continue
            except Exception as e:
                _log(f"Картинка #{idx_im}: ошибка скачивания по URL: {e}")
        if src.startswith("data:image/"):
            try:
                header, b64 = src.split(",", 1)
                mime = header.split(":", 1)[1].split(";")[0]
                data = base64.b64decode(b64)
                results.append((mime, data))
                _log(f"Картинка #{idx_im}: data:URL декодирована, mime={mime}, размер={len(data)} байт")
                continue
            except Exception as e:
                _log(f"Картинка #{idx_im}: ошибка декодирования data:URL: {e}")
        try:
            _log(f"Картинка #{idx_im}: делаю скриншот как fallback")
            data = im.screenshot(type="png")
            results.append(("image/png", data))
        except Exception as e:
            _log(f"Картинка #{idx_im}: ошибка скриншота: {e}")

    if results:
        return results

    # Only after every direct data/URL/blob path has failed, retain a visible
    # image rather than losing the generation.  This is deliberately last so
    # normal successful runs preserve the original image bytes and quality.
    if allow_screenshot_fallback:
        try:
            pics = _collect_images_from_last_turn(page, max_images=max_images)
            if pics:
                _log(f"[fallback] collected {len(pics)} image(s) from last turn via screenshots")
                return pics
        except Exception:
            pass
    return []
    # Find last response-like container
    containers = []
    for sel in ess_response_sels:
        containers.extend(page.query_selector_all(sel))
    if not containers:
        return []
    last = containers[-1]

    imgs = last.query_selector_all("img")
    results: List[Tuple[str, bytes]] = []
    for i, im in enumerate(imgs):
        if len(results) >= max_images:
            break
        try:
            src = im.get_attribute("src") or ""
            if src.startswith("data:image/"):
                header, b64 = src.split(",", 1)
                mime = header.split(":", 1)[1].split(";")[0]
                data = base64.b64decode(b64)
                results.append((mime, data))
            else:
                # CORS/blob or gated — use element screenshot
                data = im.screenshot(type="png")
                results.append(("image/png", data))
        except Exception:
            try:
                data = im.screenshot(type="png")
                results.append(("image/png", data))
            except Exception:
                pass
    return results


st.set_page_config(page_title="Gemini (Playwright UI)", layout="wide")
st.title("Gemini генерация через браузер (Playwright)")

url = st.selectbox("URL интерфейса", DEFAULT_URLS, index=0)
headless = st.checkbox("Headless режим", value=False)
use_auto_profile = st.checkbox("Отдельный профиль для автоматики (рекомендуется)", value=True)
# По умолчанию используем C:\\temp\\chrome-debug для совместимости с CDP
user_data_dir = st.text_input("Путь к профилю (user-data-dir)", value=r"C:\\temp\\chrome-debug")
executable_path = st.text_input("Путь к chrome.exe (по умолчанию C:/Program Files/Google/Chrome/Application/chrome.exe)", value=r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
use_cdp = st.checkbox("Запуск и подключение по CDP (рекомендуется)", value=True)
cdp_url = st.text_input("CDP URL", value="http://127.0.0.1:9222")

# Мультипромпты (как в API-версии)
if "pw_prompts" not in st.session_state:
    st.session_state.pw_prompts = [""]

st.markdown("**Промпты**")
nprompts = []
for i, val in enumerate(st.session_state.pw_prompts):
    nv = st.text_input(f"Промпт #{i+1}", value=val, key=f"pw_prompt_{i}")
    nprompts.append(nv)
col_add, col_rem = st.columns([1,1])
with col_add:
    if st.button("+ Добавить поле", key="pw_add"):
        st.session_state.pw_prompts.append("")
        st.rerun()
with col_rem:
    if len(st.session_state.pw_prompts) > 1 and st.button("− Убрать последнее", key="pw_rem"):
        st.session_state.pw_prompts = st.session_state.pw_prompts[:-1]
        st.rerun()

st.session_state.pw_prompts = nprompts

with st.sidebar:
    st.markdown("### Базовое изображение (белое)")
    if os.path.exists(BASE_IMAGE_PATH):
        st.image(BASE_IMAGE_PATH, caption=BASE_IMAGE_PATH, use_container_width=True)
    else:
        st.error(f"Базовый файл не найден: {BASE_IMAGE_PATH}")

colA, colB = st.columns([1,1])
with colA:
    open_btn = st.button("1) Открыть/подключиться (Playwright)", type="secondary")
with colB:
    go_btn = st.button("2) Сгенерировать все промпты", type="primary")

# Fixed layout placeholders to avoid duplicate blocks during reruns
logs_ph = st.container()
results_ph = st.container()

if "pw_ctx" not in st.session_state:
    st.session_state.pw_ctx = None
    st.session_state.pw_browser = None
    st.session_state.pw_page = None
    st.session_state.prev_resp_count_pw = 0


if open_btn:
    try:
        if use_auto_profile:
            os.makedirs(user_data_dir, exist_ok=True)
        playwright = sync_playwright().start()
        if use_cdp:
            # 1) Попробуем подключиться к уже запущенному Chrome по CDP
            # 2) Если не поднят, сами запустим Chrome с нужными флагами и подключимся
            def _is_cdp_up(url: str) -> bool:
                try:
                    with urllib.request.urlopen(url + "/json/version", timeout=1) as resp:
                        return resp.status == 200
                except Exception:
                    return False

            if not _is_cdp_up(cdp_url):
                # Запускаем Chrome с remote debugging
                cmd = [
                    executable_path,
                    f"--remote-debugging-port={cdp_url.split(':')[-1]}",
                    f"--user-data-dir={user_data_dir}",
                    "--lang=ru-RU",
                ]
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                # ждём, пока поднимется CDP
                deadline = time.time() + 10
                while time.time() < deadline and not _is_cdp_up(cdp_url):
                    time.sleep(0.3)
            browser = playwright.chromium.connect_over_cdp(cdp_url)
            # В CDP-режиме возвращается Browser; берём первый Context
            if getattr(browser, "contexts", None):
                ctx = browser.contexts[0]
            else:
                ctx = browser.new_context()
            # Выберем вкладку с gemini/aistudio, иначе создадим новую
            pages = ctx.pages
            page = None
            for p in pages:
                try:
                    u = (p.url or '').lower()
                except Exception:
                    u = ''
                if 'gemini.google.com' in u or 'aistudio.google.com' in u:
                    page = p
                    break
            if not page:
                page = pages[0] if pages else ctx.new_page()
            page.set_default_timeout(30000)
            page.goto(url, wait_until="load")
            _debug_dom(page)
            st.session_state.pw_ctx = ctx
            st.session_state.pw_browser = playwright
            st.session_state.pw_page = page
        else:
            # Обычный persistent context
            browser = playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir if use_auto_profile else None,
                headless=headless,
                channel='chrome',
                executable_path=executable_path or None,
                # Reduce obvious automation fingerprints (can affect Google AI Studio permissions).
                ignore_default_args=["--enable-automation"],
                args=["--lang=ru-RU", "--disable-blink-features=AutomationControlled"],
            )
            try:
                browser.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    """
                )
            except Exception:
                pass
            page = browser.new_page()
            page.set_default_timeout(30000)
            page.goto(url, wait_until="load")
            st.session_state.pw_ctx = browser
            st.session_state.pw_browser = playwright
            st.session_state.pw_page = page
        # Try detect input
        try:
            _wait_input_ready(page, timeout_ms=10000)
            st.success("Поле ввода найдено. Можно сразу нажимать '2) Сгенерировать все промпты'.")
        except Exception:
            st.info("Если требуется — войдите в аккаунт Google в открывшемся окне, затем нажмите '2) Сгенерировать все промпты'.")
        st.session_state.prev_resp_count_pw = _count_responses(page)
    except Exception as e:
        st.error(f"Ошибка при открытии: {e}")


if go_btn:
    try:
        # Чтобы избежать ошибки "cannot switch to a different thread" в Streamlit,
        # при каждом клике заново подключаемся к CDP и получаем свежие объекты (без хранения их между перерисовками).
        if use_cdp:
            playwright = sync_playwright().start()
            # Убедимся, что CDP доступен; если нет — поднимем Chrome
            def _is_cdp_up(url: str) -> bool:
                try:
                    with urllib.request.urlopen(url + "/json/version", timeout=1) as resp:
                        return resp.status == 200
                except Exception:
                    return False
            if not _is_cdp_up(cdp_url):
                cmd = [
                    executable_path,
                    f"--remote-debugging-port={cdp_url.split(':')[-1]}",
                    f"--user-data-dir={user_data_dir}",
                    "--lang=ru-RU",
                ]
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.time() + 10
                while time.time() < deadline and not _is_cdp_up(cdp_url):
                    time.sleep(0.3)
            browser = playwright.chromium.connect_over_cdp(cdp_url)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            # Выберем вкладку с gemini/aistudio, иначе первую
            pages = ctx.pages
            page = None
            for p in pages:
                try:
                    u = (p.url or '').lower()
                except Exception:
                    u = ''
                if 'gemini.google.com' in u or 'aistudio.google.com' in u:
                    page = p
                    break
            if not page:
                page = pages[0] if pages else ctx.new_page()
            page.set_default_timeout(30000)
        else:
            # Не CDP: попробуем использовать persistent context на лету
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir if use_auto_profile else None,
                headless=headless,
                channel='chrome',
                executable_path=executable_path or None,
                args=["--lang=ru-RU"],
            )
            ctx = browser  # в persistent режиме сам browser является BrowserContext
            page = browser.new_page()
            page.set_default_timeout(30000)

        page.goto(url, wait_until="load")
        _debug_dom(page)
        _wait_input_ready(page, timeout_ms=60000)

        # Build final prompts
        pre = "Change the white image using this Prompt: "
        post = " DO NOT LEAVE BLANK WHITE SPACE, THIS IS IMPORTANT "
        final_prompts = [f"{pre}{(p or '').strip()}{post}" for p in st.session_state.pw_prompts if (p or '').strip()]

        results: List[Tuple[str, List[Tuple[str, bytes]], Optional[str]]] = []  # (prompt, [(mime, bytes)], err)
        for idx, fp in enumerate(final_prompts, 1):
            # Start a fresh chat for each prompt
            try:
                _start_new_chat(page)
            except Exception:
                pass
            if not os.path.exists(BASE_IMAGE_PATH):
                st.error(f"Файл {BASE_IMAGE_PATH} не найден")
                break

            imgs: List[Tuple[str, bytes]] = []
            err: Optional[str] = None

            for attempt in range(1, 3):  # до 2 попыток
                _log(f"Промпт #{idx}: попытка {attempt} из 2")

                # Приложим базовое изображение
                ok = _attach_image(page, BASE_IMAGE_PATH)
                attached_preview = _wait_image_attached(page, timeout_ms=1800)
                if not ok or not attached_preview:
                    _log("Не удалось прикрепить белую картинку (нет превью). Попробую ещё раз другими селекторами…")
                    ok2 = _attach_image(page, BASE_IMAGE_PATH)
                    attached_preview = attached_preview or _wait_image_attached(page, timeout_ms=1500)
                    if not ok2 and not attached_preview:
                        _log("Похоже, картинка так и не прикрепилась. Отправляю только текст промпта…")

                _dismiss_overlays(page)  # закрыть меню/оверлеи

                # Выбор модели (если указан) перед вводом
                try:
                    mc = st.session_state.get('unif_model_choice')
                    if mc:
                        _log("[menu] вызов _pick_model", force=True)
                        ok_pick = gph._pick_model(page, mc)
                        _log(f"[menu] _pick_model вернул: {ok_pick}", force=True)
                except Exception as e:
                    _log(f"[menu] ошибка в _pick_model: {e}", force=True)

                # Вводим текст и отправляем
                _type_prompt(page, fp)
                old = _count_responses(page)
                _click_send(page)

                # Ждём новые контейнеры ответов недолго, затем ждём картинки максимум 150 сек
                t_resp_deadline = time.time() + 20
                while time.time() < t_resp_deadline:
                    if _count_responses(page) > old:
                        break
                    time.sleep(0.4)

                imgs = _wait_and_download_generated_images(page, ctx, timeout_s=150, max_images=6)
                if imgs:
                    break  # успех

                # Если не скачали, но изображения появились — повторная попытка скачивания, без перезагрузки
                if _has_generated_images(page):
                    _log("Изображение видно в UI, но не скачалось. Повторю скачивание ещё раз без перезагрузки…")
                    imgs = _wait_and_download_generated_images(page, ctx, timeout_s=15, max_images=6)
                    if imgs:
                        break

                # Если не удалось — перезагружаем страницу и пробуем ещё раз
                if attempt < 2:
                    _log("Изображение не сгенерировалось (или не скачалось) за 2.5 минуты. Перезагружаю страницу и повторяю попытку…")
                    try:
                        page.reload(wait_until="load")
                    except Exception:
                        try:
                            page.goto(url, wait_until="load")
                        except Exception:
                            pass
                    try:
                        _wait_input_ready(page, timeout_ms=20000)
                    except Exception:
                        pass
                    # малую паузу после reload
                    time.sleep(0.7)
                else:
                    err = "Таймаут генерации (2.5 минуты)"

            if not imgs:
                # старый метод на всякий случай
                imgs = _collect_images_from_last_turn(page, max_images=6)

            if not imgs:
                results.append((fp, [], err or "В ответе не найдены изображения"))
            else:
                results.append((fp, imgs, None))

        # Post-process: persist results in session state for stable re-rendering across reruns
        if results:
            st.session_state['last_results'] = results
        # Не закрываем браузер в CDP-режиме (это ваш Chrome). В non-CDP можно закрыть контекст.
        if not use_cdp:
            try:
                browser.close()
            except Exception:
                pass
        try:
            playwright.stop()
        except Exception:
            pass

    except Exception as e:
        st.error(f"Ошибка автоматизации: {e}")

# =====================
# Рендер галереи с кнопкой "Пересоздать" под каждой картинкой
# =====================
base_dir = os.path.join("generate automation", datetime.now().strftime("%Y-%m-%d"))
os.makedirs(base_dir, exist_ok=True)

st.markdown("---")
render_results = st.session_state.get('last_results', [])
for i, (pt, images, err) in enumerate(render_results, 1):
    with st.expander(f"Промпт #{i}", expanded=True):
        st.code(pt)
        if images:
            cols = st.columns(min(3, len(images))) if len(images) > 1 else st.columns(1)
            for j, (mime, blob) in enumerate(images, 1):
                col = cols[(j-1) % len(cols)]
                with col:
                    st.image(io.BytesIO(blob), use_container_width=False, caption=f"Изображение #{j}")
                    if st.button("Пересоздать", key=f"regen_{i}_{j}"):
                        try:
                            # На клик — переподключаемся к CDP/Chrome и перегенерируем 1 новое изображение для этого промпта
                            if use_cdp:
                                playwright = sync_playwright().start()
                                # Проверим, что CDP доступен, если нет — поднимем Chrome
                                def _is_cdp_up(url_cdp: str) -> bool:
                                    try:
                                        with urllib.request.urlopen(url_cdp + "/json/version", timeout=1) as resp:
                                            return resp.status == 200
                                    except Exception:
                                        return False
                                if not _is_cdp_up(cdp_url):
                                    cmd = [
                                        executable_path,
                                        f"--remote-debugging-port={cdp_url.split(':')[-1]}",
                                        f"--user-data-dir={user_data_dir}",
                                        "--lang=ru-RU",
                                    ]
                                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                    deadline = time.time() + 10
                                    while time.time() < deadline and not _is_cdp_up(cdp_url):
                                        time.sleep(0.3)
                                browser = playwright.chromium.connect_over_cdp(cdp_url)
                                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                                pages = ctx.pages
                                page = pages[0] if pages else ctx.new_page()
                                page.set_default_timeout(30000)
                            else:
                                playwright = sync_playwright().start()
                                browser = playwright.chromium.launch_persistent_context(
                                    user_data_dir=user_data_dir if use_auto_profile else None,
                                    headless=headless,
                                    channel='chrome',
                                    executable_path=executable_path or None,
                                    args=["--lang=ru-RU"],
                                )
                                ctx = browser
                                page = browser.new_page()
                                page.set_default_timeout(30000)
                            page.goto(url, wait_until="load")
                            try:
                                _wait_input_ready(page, timeout_ms=60000)
                            except Exception:
                                pass
                            new_imgs, saved_paths = _regenerate_prompt(page, ctx, pt, i, base_dir, max_images=1, attach_base=True, base_image_path=BASE_IMAGE_PATH, model_choice=st.session_state.get('unif_model_choice'))
                            try:
                                if not use_cdp:
                                    browser.close()
                            except Exception:
                                pass
                            try:
                                playwright.stop()
                            except Exception:
                                pass
                            if new_imgs:
                                # Заменим конкретную картинку j на новую
                                lr = st.session_state.get('last_results', [])
                                if i-1 < len(lr):
                                    old_pt, old_imgs, old_err = lr[i-1]
                                    new_list = list(old_imgs)
                                    new_list[j-1] = new_imgs[0]
                                    lr[i-1] = (old_pt, new_list, None)
                                    st.session_state['last_results'] = lr
                                    st.rerun()
                            else:
                                st.warning("Не удалось пересоздать изображение. Попробуйте ещё раз.")
                        except Exception as e:
                            st.error(f"Ошибка при перегенерации: {e}")
        else:
            st.warning("Не удалось получить изображения для этого промпта.")
            if err:
                st.caption(f"Ошибка: {err}")

st.caption("Этот режим использует Playwright с persistent context. Первый раз войдите в Google, дальше логин сохранится в папке профиля.")


if __name__ == "__main__":
    main()

