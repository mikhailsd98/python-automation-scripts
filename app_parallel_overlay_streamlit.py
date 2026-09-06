# -*- coding: utf-8 -*-
# Usage: streamlit run app_parallel_overlay_streamlit.py
# Parallel Gemini UI (Playwright) image generation:
# - Tab 1: add text overlay to an uploaded base image
# - Tab 2: generate a Pinterest pin from a prompt using text-only 10:16 instructions

import os
import re
import shutil
import time
import queue

# Persist rate-limited profiles across portions within the same Python process.
_GLOBAL_RATE_LIMITED_PROFILES: set[str] = set()
# Persist the next "auto" profile number pointer per base profile dir.
_GLOBAL_NEXT_PROFILE_NUM: dict[str, int] = {}
import sys
import subprocess
import random
import io
import threading
import csv
import zipfile
from datetime import datetime
import concurrent.futures
from pathlib import Path
from typing import List, Tuple, Optional, Dict

from PIL import Image

import streamlit as st
import streamlit.components.v1 as components

import json

import gemini_pw_helpers as gph

# Reuse stable, battle-tested UI automation helpers
from gemini_playwright_streamlit import (
    _wait_input_ready,
    _start_new_chat,
    _attach_image,
    _wait_image_attached,
    _dismiss_overlays,
    _type_prompt,
    _click_send,
    _wait_and_download_generated_images,
    _has_generated_images,
    _debug_dom,
)

from playwright.sync_api import sync_playwright

# Launching Chrome (persistent context) from multiple threads can be flaky on Windows.
# We serialize ONLY the launch phase to avoid "blink and close" behaviour.
_LAUNCH_LOCK = threading.Lock()

# Batch-generation (Tab 3) background worker state (so we can Stop and keep partial results)
_CSVBATCH_LOCK = threading.Lock()
_CSVBATCH_RUNS: dict[str, dict] = {}

# Classic Gemini and AI Studio ("new_chat" is a separate mode with different selectors)
DEFAULT_URLS = [
    "https://gemini.google.com/app",
    "https://aistudio.google.com/app",
    "https://aistudio.google.com/prompts/new_chat?model=gemini-2.5-flash-image",
]

PREFIXES = ["будь добр", "пожалуйста", "плиз", "please"]

# 20 "safe" suffix phrases to increase diversity without shifting style/meaning too much
# (added at the end of the prompt)
RANDOM_WORDS = [
    "пожалуйста",
    "если можно",
    "будьте добры",
    "плиз",
    "прошу",
    "пожалуй",
    "по возможности",
    "если не сложно",
    "заранее спасибо",
    "спасибо",
    "ок",
    "хорошо",
    "kindly",
    "please",
    "thanks",
    "thank you",
    "if possible",
    "when you can",
    "much appreciated",
    "appreciate it",
]


def _open_folder(path_str: str | None) -> tuple[bool, str]:
    """Best-effort open folder in OS file manager.

    Returns: (ok, message)
    """

    if not path_str:
        return False, "Empty path"
    p = str(Path(path_str).expanduser().resolve())
    try:
        if os.name == "nt":
            # More reliable than os.startfile in some Streamlit launch modes
            subprocess.Popen(["explorer", p])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
        return True, p
    except Exception as e:
        return False, f"{p} ({e})"


def _ui_rerun() -> None:
    """Compatibility wrapper for Streamlit rerun API."""

    try:
        # Newer Streamlit
        if hasattr(st, "rerun"):
            st.rerun()
            return
    except Exception:
        pass

    try:
        # Older Streamlit
        if hasattr(st, "experimental_rerun"):
            st.experimental_rerun()
            return
    except Exception:
        pass


def _normalize_user_data_dir(user_data_dir: str | None) -> str | None:
    if not user_data_dir:
        return None
    s = str(user_data_dir).strip().strip('"').strip("'")
    if not s:
        return None
    # Fix a common Windows copy/paste typo: missing path separator before .chrome_automation_profile
    try:
        marker = ".chrome_automation_profile"
        if marker in s and (f"\\{marker}" not in s) and (f"/{marker}" not in s):
            s = s.replace(marker, f"\\{marker}")
    except Exception:
        pass
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


def _cleanup_profile_locks(user_data_dir: str | None) -> None:
    """Best-effort cleanup of Chromium profile lock artefacts.

    When Chrome/Chromium is killed or crashes, it can leave lock files in the
    profile directory. The next startup may then immediately exit ("window
    flashes and closes") until the lock is cleared.

    This function removes only well-known *ephemeral* lock files and does not
    touch user data like Cookies/Preferences.
    """

    if not user_data_dir:
        return

    p = Path(user_data_dir)
    if not p.exists() or not p.is_dir():
        return

    # Common lock artefacts in Chromium profiles (Windows/macOS/Linux)
    candidates = [
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
        "LOCK",
        "lockfile",
        "DevToolsActivePort",
    ]

    for name in candidates:
        fp = p / name
        try:
            if fp.exists():
                fp.unlink()
        except Exception:
            # Ignore: if another process holds the file open, we just retry later.
            pass


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
    """Launch persistent context with retries for intermittent Windows flakiness."""

    norm_udir = _normalize_user_data_dir(user_data_dir) if user_data_dir else None
    norm_downloads = os.path.abspath(downloads_path) if downloads_path else None
    if norm_udir:
        try:
            os.makedirs(norm_udir, exist_ok=True)
        except Exception:
            pass
    if norm_downloads:
        try:
            os.makedirs(norm_downloads, exist_ok=True)
        except Exception:
            pass

    last_err: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            # Important: clean stale locks before every attempt. On Windows this
            # can cause a "blink/close" startup until the lock disappears.
            _cleanup_profile_locks(norm_udir)

            with _LAUNCH_LOCK:
                ctx = pw.chromium.launch_persistent_context(
                    user_data_dir=norm_udir,
                    headless=headless,
                    # Explicitly retain Playwright download objects until their
                    # bytes have been verified and copied to the output folder.
                    accept_downloads=True,
                    # Do not use the Windows Downloads folder. Each worker gets
                    # a private temporary directory, so a native Chrome download
                    # cannot collide with another parallel window or be left as
                    # an orphaned .tmp in the user's Downloads directory.
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
            # Add attempt context; Playwright/Chrome errors are often too generic.
            last_err = RuntimeError(
                f"launch_persistent_context failed (attempt={attempt}/{attempts}, profile={norm_udir}): {e}"
            )
            # Try again after a short backoff; if Chrome is still shutting down,
            # the lock may clear on its own.
            time.sleep(0.6 + (0.7 * attempt))

    raise last_err or RuntimeError("Failed to launch persistent context")


def _get_run_base_dir(kind: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path("generated_images") / f"{kind}_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    return str(out)


def _native_download_watch_dirs(profile_dir: str | None) -> list[str]:
    """Return actual native Chrome download destinations for one profile.

    This only reads Chrome's existing Preferences.  It does not alter profile
    settings.  The system Downloads folder remains a fallback because Gemini's
    current download action can bypass the persisted profile preference.
    """
    candidates: list[str] = []
    try:
        root = Path(profile_dir).resolve() if profile_dir else None
        if root:
            for pref_path in (root / "Default" / "Preferences", root / "Preferences"):
                if not pref_path.is_file():
                    continue
                try:
                    prefs = json.loads(pref_path.read_text(encoding="utf-8"))
                    for section, key in (("download", "default_directory"), ("savefile", "default_directory")):
                        value = ((prefs.get(section) or {}).get(key) or "").strip()
                        if value:
                            candidates.append(value)
                except Exception:
                    continue
                break
    except Exception:
        pass

    try:
        candidates.append(str(Path.home() / "Downloads"))
    except Exception:
        pass

    unique: list[str] = []
    for candidate in candidates:
        try:
            path = str(Path(candidate).resolve())
            if Path(path).is_dir() and path not in unique:
                unique.append(path)
        except Exception:
            continue
    return unique


def _set_profile_download_directory_temporarily(
    profile_dir: str | None, download_dir: str
) -> tuple[list[tuple[Path, bytes]], list[str]]:
    """Point a persistent Chrome profile at one worker's private download dir.

    Gemini's current Download control sometimes bypasses Playwright's
    ``downloads_path`` and delegates to Chrome's profile preference instead.
    We change that preference *before* Chrome starts, then restore the exact
    original bytes after the worker closes.  Each Tab3 worker owns its profile,
    so this does not race another active Chrome process.
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

    pref_paths = [root / "Default" / "Preferences", root / "Preferences"]
    for pref_path in pref_paths:
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
            # Do not open a Save-As prompt: it cannot be handled reliably in a
            # parallel browser worker and would make the file look "lost".
            download["prompt_for_download"] = False
            download["directory_upgrade"] = True

            tmp_path = pref_path.with_name(f"{pref_path.name}.tab3-download-tmp")
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


def _restore_profile_download_directory(backups: list[tuple[Path, bytes]]) -> list[str]:
    """Restore raw Chrome Preferences bytes saved by the temporary pinning."""

    notes: list[str] = []
    for pref_path, original in backups or []:
        try:
            tmp_path = pref_path.with_name(f"{pref_path.name}.tab3-restore-tmp")
            tmp_path.write_bytes(original)
            os.replace(str(tmp_path), str(pref_path))
            notes.append(f"profile preference restored: {pref_path}")
        except Exception as e:
            notes.append(f"profile preference restore failed: {pref_path}: {e}")
    return notes


def _pin_chrome_download_directory(page, download_dir: str) -> str:
    """Pin Chrome's live native-download target through CDP when available."""

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


def _save_uploaded_file(uploaded) -> Optional[str]:
    if uploaded is None:
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_dir = Path(f"tmp_rovodev_overlay_uploads_{ts}")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(uploaded.name).suffix or ".jpg"
    outp = tmp_dir / f"base{suffix}"
    with open(outp, "wb") as f:
        f.write(uploaded.getbuffer())
    return str(outp)


def _build_overlay_prompt(theme: str) -> str:
    theme = (theme or "").strip()
    # NOTE: user enters only the theme; we inject it into the instruction.
    return (
        "Сделай текстовый оверлей этой картинке с надписью: "
        f"\"{theme}\". "
        "Только чтобы картинку было видно полностью, чтобы оверлей текстовый не перекрывал важные части картинки "
        "и чтобы надпись выглядела красиво и модно (это для Pinterest). "
        "Сделай надпись достаточно большой, чтобы можно было прочитать в ленте Pinterest, но не слишком большой, "
        "чтобы дизайн картинки было хорошо видно. "
        "Желательно чтобы надпись была по центру по горизонтали, а по вертикали выбери лучшее место (сверху/снизу/по центру) "
        "в зависимости от дизайна. "
        "Важно: буквы должны быть на стильном подходящем к общему дизайну фоне, чтобы их было легко прочитать; "
        "фон должен быть креативным (не просто однотонный прямоугольник). Создай картинку пожалуйста."
    )


DEFAULT_SITE_NAME = "SpaceOfMuse.com"


def _normalize_site_name(site_name: str | None) -> str:
    s = (site_name or "").strip().strip("\"").strip("'")
    return s or DEFAULT_SITE_NAME


def _pin_prompt_profile(site_name: str | None) -> dict[str, str]:
    site_name_norm = _normalize_site_name(site_name)
    site_key = site_name_norm.strip().lower()

    if site_key == "glowuproutine.com":
        return {
            "site_name": site_name_norm,
            "website_topic": "fashion",
            "article_topic": "Fashion",
            "theme_topic": "fashion",
            "background_subject": "fashion photograph",
        }

    if site_key == "sweethomecookery.com":
        return {
            "site_name": site_name_norm,
            "website_topic": "food",
            "article_topic": "Food",
            "theme_topic": "food",
            "background_subject": "food photograph",
        }

    return {
        "site_name": site_name_norm,
        "website_topic": "home decor",
        "article_topic": "Home Decor",
        "theme_topic": "home decor",
        "background_subject": "interior photograph",
    }


def _build_pin_prompt(text_overlay: str, blog_title: str, *, site_name: str | None = None) -> str:
    """Build the exact English prompt requested for Gemini UI."""

    text_overlay = (text_overlay or "").strip()
    blog_title = (blog_title or "").strip()
    profile = _pin_prompt_profile(site_name)

    return (
        "Create one Pinterest pin in a 10:16 vertical aspect ratio using this prompt: "
        f"I have a {profile['website_topic']} website called {profile['site_name']}, where I write articles on this topic. "
        f"Please create a Pinterest pin for a {profile['article_topic']} article with a text overlay: "
        f"'{text_overlay}'. "
        "Ensure the background image is fully visible and the text does not block important elements. "
        "The typography should be stylish, trendy, and large enough to read while scrolling the feed, but balanced with the overall design. "
        "Center the text horizontally; choose the best vertical placement based on the composition. "
        "Place the letters on a stylish, creative background (not just a simple rectangle) that ensures legibility. "
        f"You decide on the background image; it must be aesthetic, clickable, and relevant to the {profile['theme_topic']} theme. "
        f"Background style requirement: use a hyper-realistic, photorealistic, high-resolution {profile['background_subject']} look (realistic textures, natural lighting, true-to-life colors). "
        "Avoid any cartoon, illustration, anime, painterly, 3D render, CGI, or plastic-looking style. "
        "For context, here is the full title and part of the article this pin will link to, just so you understand the theme: "
        f"'{blog_title}'. "
        "Thank you! Fill the entire 10:16 frame; do not leave blank white space."
    )


def _truncate_to_fit(s: str, max_len: int) -> str:
    s = (s or "").strip()
    if max_len <= 0:
        return ""
    if len(s) <= max_len:
        return s

    # Try to cut on a word boundary, keep room for "..."
    cut = max(0, max_len - 3)
    candidate = s[:cut]
    # Prefer last whitespace within the last ~80 chars to avoid overly aggressive trimming
    window = candidate[-80:]
    idx = window.rfind(" ")
    if idx != -1:
        candidate = candidate[: len(candidate) - len(window) + idx]
    candidate = candidate.rstrip(" \n\t\r\"'")
    return (candidate + "...")[:max_len]


def _build_ideogram_prompt(
    text_overlay: str,
    blog_title: str,
    *,
    max_chars: int = 1500,
    site_name: str | None = None,
) -> str:
    """Build a prompt for Ideogram.

    Differences vs Gemini prompt:
    - No "Avoid any cartoon..." clause (per user request)
    - Removes affiliate-links disclaimer sentence from the context (if present)
    - Truncates the article context part to fit Ideogram length limit
    """

    text_overlay = (text_overlay or "").strip()
    blog_title = (blog_title or "").strip()
    profile = _pin_prompt_profile(site_name)

    # Remove common affiliate disclaimer that hurts prompt quality.
    affiliate_sentence = (
        "This post may contain affiliate links. If you buy through these links, "
        "we may earn a small commission at no extra cost to you. You can learn more in our Privacy Policy."
    )
    blog_title = blog_title.replace(affiliate_sentence, "")
    # Also handle cases where it is separated by newlines/spaces.
    blog_title = re.sub(
        r"This post may contain affiliate links\.[\s\S]*?Privacy Policy\.?",
        "",
        blog_title,
        flags=re.IGNORECASE,
    ).strip()

    prefix = (
        f"I have a {profile['website_topic']} website called {profile['site_name']}, where I write articles on this topic. "
        f"Please create a Pinterest pin for a {profile['article_topic']} article with a text overlay: "
        f"'{text_overlay}'. "
        "Ensure the background image is fully visible and the text does not block important elements. "
        "The typography should be stylish, trendy, and large enough to read while scrolling the feed, but balanced with the overall design. "
        "Center the text horizontally; choose the best vertical placement based on the composition. "
        "Place the letters on a stylish, creative background (not just a simple rectangle) that ensures legibility. "
        f"You decide on the background image; it must be aesthetic, clickable, and relevant to the {profile['theme_topic']} theme. "
        f"Background style requirement: use a hyper-realistic, photorealistic, high-resolution {profile['background_subject']} look (realistic textures, natural lighting, true-to-life colors). "
        "For context, here is the full title and part of the article this pin will link to, just so you understand the theme: "
        "'"
    )

    suffix = "'. Thank you"

    # Compute how much room is left for blog_title so total <= max_chars
    budget = max_chars - len(prefix) - len(suffix)
    truncated_ctx = _truncate_to_fit(blog_title, budget)
    out = f"{prefix}{truncated_ctx}{suffix}"

    # Final hard clamp (safety)
    if len(out) > max_chars:
        out = out[:max_chars].rstrip()
    return out


def _copy_to_clipboard_html(*, text: str, label: str, key: str) -> None:
    """Render a frontend button that copies text to clipboard.

    We render a real HTML button so the clipboard write is performed in direct
    response to a user click (more reliable than triggering JS after a rerun).
    """

    btn_id = f"copy_btn_{re.sub(r'[^a-zA-Z0-9_]+', '_', key)}"
    payload = json.dumps(text)
    safe_label = (label or "Copy").replace("<", "&lt;").replace(">", "&gt;")

    components.html(
        f"""
        <div>
          <button id="{btn_id}" type="button"
            style="
              width: 100%;
              padding: 0.5rem 0.75rem;
              border-radius: 0.5rem;
              border: 1px solid rgba(49, 51, 63, 0.2);
              background: rgb(255, 255, 255);
              color: rgb(49, 51, 63);
              font-weight: 600;
              cursor: pointer;
            ">
            {safe_label}
          </button>
          <div id="{btn_id}_msg" style="margin-top: 0.35rem; font-size: 0.85rem; color: rgba(49, 51, 63, 0.75);"></div>
        </div>
        <script>
          const btn = document.getElementById({json.dumps(btn_id)});
          const msg = document.getElementById({json.dumps(btn_id + "_msg")});
          const text = {payload};
          btn.addEventListener('click', async () => {{
            try {{
              await navigator.clipboard.writeText(text);
              msg.textContent = 'Скопировано в буфер обмена';
              setTimeout(() => msg.textContent = '', 2500);
            }} catch (e) {{
              console.error(e);
              msg.textContent = 'Не удалось скопировать (браузер запретил доступ к clipboard)';
              setTimeout(() => msg.textContent = '', 4000);
            }}
          }});
        </script>
        """,
        height=85,
    )


def _slug(s: str) -> str:
    """Filesystem-friendly slug.

    Keeps Unicode word characters (so Cyrillic is preserved),
    replaces the rest with underscores.
    """

    s = (s or "").strip()
    s = re.sub(r"[^\w-]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "theme"


def _safe_filename(name: str) -> str:
    """Make a filename safe across OSes without stripping Unicode."""

    name = (name or "").strip()
    forbidden = set('<>:"/\\|?*')
    name = "".join(("_" if ch in forbidden else ch) for ch in name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _is_retryable_error(msg: str) -> bool:
    """Return True if the operation should be retried in the same slot.

    IMPORTANT: This is broader than "bannable" errors. Some errors (e.g. user manually
    closed the window) should be retryable but must NOT trigger profile ban.
    """
    m = (msg or "").lower()
    # NOTE: keep this list broad; Gemini UI can fail with generic banners.
    return any(
        k in m
        for k in [
            "window_closed",
            "target page, context or browser has been closed",
            "has been closed",
            "browser has disconnected",
            "browser.getwindowfortarget",
            "timeout",
            "timed out",
            "navigation timeout",
            "page crashed",
            "net::err_",
            "offline_timeout",
            "something went wrong",
            "что-то пошло не так",
            "try again",
            # AI Studio
            "failed to generate content",
            "permission denied",
            "rate limit",
            "too many requests",
            "429",
            "internal error",
            "no images were collected",
            "0 images",
            "не найдено поле ввода",
            "input field",
            "prompt box", 
            "textarea", 
        ]
    )


def _is_bannable_profile_error(msg: str) -> bool:
    """Errors that should permanently ban the current profile for this run.

    We only want to ban profiles that are likely quota/limit related, not user actions
    like manually closing the window.
    """
    m = (msg or "").lower()

    # Do NOT ban on manual close / browser closed.
    non_bannable_substrings = (
        "window_closed",
        "target page, context or browser has been closed",
        "target closed",
        "browser has been closed",
        "page was closed",
        "context was closed",
        "closed by user",
    )
    if any(s in m for s in non_bannable_substrings):
        return False

    # Ban on timeouts / no-images patterns.
    bannable_substrings = (
        "timeout",
        "timed out",
        "navigation timeout",
        "no images were collected",
        "0 images",
    )
    return any(s in m for s in bannable_substrings)


def _looks_offline_error(msg: str) -> bool:
    """Best-effort detection of network-offline navigation errors."""
    m = (msg or "").lower()
    return any(
        k in m
        for k in [
            "err_internet_disconnected",
            "err_network_changed",
            "err_name_not_resolved",
            "dns_probe_finished_no_internet",
            "net::err_",
            "internet disconnected",
            "no internet",
            "нет подключения",
            "нет интернета",
        ]
    )


def _page_looks_offline(page) -> bool:
    """Detect Chrome offline error page (best-effort, low-cost)."""
    try:
        u = str(getattr(page, "url", "") or "")
        if u.startswith("chrome-error://") or ("chromewebdata" in u):
            return True
    except Exception:
        pass
    try:
        t = str(page.title() or "").lower()
        if ("internet" in t) or ("offline" in t) or ("err_" in t):
            return True
    except Exception:
        pass
    return False


def _goto_with_offline_wait(
    page,
    url: str,
    *,
    offline_wait_timeout_s: int,
    stop_requested_fn,
    wait_until: str = "load",
    sleep_s: float = 2.0,
) -> None:
    """Navigate to url; if offline, keep the window open and retry until timeout."""

    try:
        offline_wait_timeout_s = int(offline_wait_timeout_s or 0)
    except Exception:
        offline_wait_timeout_s = 0

    if offline_wait_timeout_s <= 0:
        page.goto(url, wait_until=wait_until)
        return

    deadline = time.monotonic() + float(offline_wait_timeout_s)
    last_err: str | None = None

    while True:
        if stop_requested_fn and stop_requested_fn():
            raise RuntimeError("STOP requested")

        try:
            page.goto(url, wait_until=wait_until)
        except Exception as e:
            last_err = str(e)
            if _looks_offline_error(last_err) and (time.monotonic() < deadline):
                time.sleep(float(sleep_s))
                continue
            raise

        # Some offline cases don't throw but load a chrome-error page.
        if _page_looks_offline(page) and (time.monotonic() < deadline):
            time.sleep(float(sleep_s))
            continue

        if _page_looks_offline(page):
            raise RuntimeError(f"OFFLINE_TIMEOUT after ~{offline_wait_timeout_s}s")

        return


def _parse_profile_numbers(s: str) -> list[int]:
    """Parse comma-separated profile numbers.

    Example: "10,11,12,13" -> [10, 11, 12, 13]
    """

    s = (s or "").strip()
    if not s:
        return []

    out: list[int] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except Exception:
            continue

    # Keep order, drop duplicates
    seen: set[int] = set()
    uniq: list[int] = []
    for n in out:
        if n in seen:
            continue
        seen.add(n)
        uniq.append(n)

    return uniq


def _read_pinterest_posts_csv(
    csv_path: str,
    *,
    start_row: int = 0,
    limit: int | None = None,
) -> list[dict]:
    """Read Pinterest export CSV.

    Expected format (like pinterest_export_*.csv):
      post_text;pin_filename;post_link

    Returns list of dicts with keys:
      - post_text
      - pin_filename
      - post_link
    """

    csv_path = (csv_path or "").strip().strip('"').strip("'")
    if not csv_path:
        raise ValueError("CSV path is empty")
    if not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path)

    rows: list[dict] = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        for i, r in enumerate(reader):
            if i < max(0, int(start_row)):
                continue
            if limit is not None and len(rows) >= int(limit):
                break
            if not r:
                continue
            rows.append(
                {
                    "post_text": (r.get("post_text") or "").strip().strip('"'),
                    "pin_filename": (r.get("pin_filename") or "").strip().strip('"'),
                    "post_link": (r.get("post_link") or "").strip().strip('"'),
                }
            )

    return rows


def _extract_post_context(post_text: str, *, target_chars: int = 1200) -> str:
    """Return ~1000-1200 chars of post text (word-boundary truncated)."""

    target_chars = int(target_chars or 1200)
    target_chars = max(200, min(5000, target_chars))
    return _truncate_to_fit((post_text or "").strip(), target_chars)


def _list_unif_text_autosaves() -> list[str]:
    """List autosave JSON files created by app_unified_streamlit Tab0."""

    try:
        root = Path("autosaves") / "app_unified_streamlit" / "tab0_article_texts"
        files = sorted(
            [p for p in root.glob("**/unif_text_results_*.json") if p.is_file()],
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
        return [str(p) for p in files]
    except Exception:
        return []


def _load_unif_text_results(json_path: str) -> tuple[list[dict], dict]:
    """Load a unif_text_results_*.json autosave.

    Returns:
      (results_list, full_payload)
    """

    p = Path(str(json_path)).expanduser()
    if not p.exists():
        raise FileNotFoundError(str(p))
    obj = json.loads(p.read_text(encoding="utf-8"))
    res = (obj or {}).get("results")
    if not isinstance(res, list):
        raise ValueError("Autosave JSON has no 'results' list")
    return list(res), (obj or {})


def _unif_text_item_to_context(rr: dict, *, target_chars: int = 1200) -> str:
    """Extract a *clean* context snippet from Tab0 autosave items.

    We want to provide Gemini with a small, human-readable slice of the article,
    not the raw JSON structure.

    Output layout (best effort, within target_chars):
      - Title
      - Excerpt
      - Introduction
      - First H2 + its text (and optionally one more section)

    Then hard-truncate to target_chars on a word boundary.
    """

    target_chars = int(target_chars or 1200)
    target_chars = max(200, min(5000, target_chars))

    raw = (rr or {}).get("text")
    if raw is None:
        return ""

    def _clean_line(s: str) -> str:
        s = (s or "").strip()
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _clean_block(s: str) -> str:
        s = (s or "").strip()
        s = re.sub(r"[ \t\r]+", " ", s).strip()
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s

    def _join_blocks(parts: list[str]) -> str:
        parts2: list[str] = []
        for p in parts:
            p = (p or "").strip()
            if not p:
                continue
            parts2.append(_clean_block(p))
        return "\n\n".join(parts2).strip()

    def _extract_from_dict(data: dict) -> str:
        parts: list[str] = []

        title = (rr or {}).get("title") or data.get("title")
        title = _clean_line(str(title)) if title else ""
        if title:
            parts.append(title)

        excerpt = _clean_block(str(data.get("excerpt") or ""))
        if excerpt:
            parts.append(excerpt)

        intro = data.get("introduction") or data.get("intro") or ""
        intro = _clean_block(str(intro or ""))
        if intro:
            parts.append(intro)

        sections = data.get("sections")
        if isinstance(sections, list) and sections:
            used = 0
            for sec in sections:
                if used >= 2:
                    break
                if not isinstance(sec, dict):
                    continue
                h2 = sec.get("h2") or sec.get("heading") or sec.get("title") or ""
                txt = sec.get("text") or sec.get("content") or ""
                h2 = _clean_line(str(h2 or ""))
                txt = _clean_block(str(txt or ""))
                if not (h2 or txt):
                    continue
                if h2:
                    parts.append(h2)
                if txt:
                    parts.append(txt)
                used += 1
        else:
            # fallback to raw body if present
            body = data.get("text") or data.get("article") or data.get("body") or ""
            body = _clean_block(str(body or ""))
            if body:
                parts.append(body)

        return _truncate_to_fit(_join_blocks(parts), target_chars)

    # 1) Best: parse JSON fully
    if isinstance(raw, str):
        s = raw.strip()
        try:
            data = json.loads(s)
            if isinstance(data, dict):
                return _extract_from_dict(data)
            return _truncate_to_fit(_clean_line(str(data)), target_chars)
        except Exception:
            # 2) Tolerant fallback: try to extract a few fields from a broken JSON string
            def _rx_str(key: str) -> str:
                pattern = r'"%s"\s*:\s*"(.*?)"\s*(,|\n|\r|\})' % re.escape(key)
                m = re.search(pattern, s, flags=re.DOTALL)
                if not m:
                    return ""
                val = m.group(1)
                # unescape basic sequences
                try:
                    val = val.replace('\\n', '\n').replace('\\t', ' ').replace('\\"', '"')
                except Exception:
                    pass
                return _clean_block(val)

            title = _clean_line(str((rr or {}).get("title") or ""))
            excerpt = _rx_str("excerpt")
            intro = _rx_str("introduction")
            first_h2 = _rx_str("h2")
            first_text = _rx_str("text")

            parts = [p for p in [title, excerpt, intro, first_h2, first_text] if p]
            if parts:
                return _truncate_to_fit(_join_blocks(parts), target_chars)

            # 3) last resort: plain truncation
            return _truncate_to_fit(_clean_block(s), target_chars)

    # If the autosave stored structured dict already
    if isinstance(raw, dict):
        return _extract_from_dict(raw)

    return _truncate_to_fit(_clean_line(str(raw)), target_chars)


def _guess_post_title(post_text: str, *, max_len: int = 80) -> str:
    """A short label for UI. Best-effort: first sentence / first line."""

    s = (post_text or "").strip()
    if not s:
        return "(empty)"

    # Cut to first sentence-ish
    m = re.split(r"[\n\r]+", s, maxsplit=1)
    first_line = (m[0] if m else s).strip()
    m2 = re.split(r"[.!?] ", first_line, maxsplit=1)
    title = (m2[0] if m2 else first_line).strip()
    title = re.sub(r"\s+", " ", title)
    if len(title) > max_len:
        title = title[: max_len - 3].rstrip() + "..."
    return title


def _csvbatch_set_state(run_id: str, patch: dict) -> None:
    """Thread-safe update for batch-run state."""

    with _CSVBATCH_LOCK:
        st0 = _CSVBATCH_RUNS.get(run_id) or {}
        st0.update(patch or {})
        # Track last update time so other tabs/apps can detect a stuck worker.
        try:
            st0["updated_at"] = time.time()
        except Exception:
            pass
        _CSVBATCH_RUNS[run_id] = st0


def _csvbatch_get_state(run_id: str) -> dict:
    """Read-only accessor for the current batch-run state.

    IMPORTANT:
    Streamlit reruns + background threads can occasionally leave the state in an
    inconsistent intermediate shape (e.g. `done_runs >= total_runs` but `running`
    still True if the worker crashed right after the last update).

    To avoid a UI stuck in "running" forever (and to allow Stage4 orchestration),
    we normalize the returned state defensively.
    """

    with _CSVBATCH_LOCK:
        st0 = dict(_CSVBATCH_RUNS.get(run_id) or {})

    try:
        done_runs = int(st0.get("done_runs") or 0)
        total_runs = int(st0.get("total_runs") or 0)
    except Exception:
        done_runs, total_runs = 0, 0

    finished = bool(st0.get("finished"))
    stopped = bool(st0.get("stopped"))

    # If we know it's finished/stopped, it is definitely not running.
    if finished or stopped:
        st0["running"] = False

    # Strong completion signal: done >= total.
    if total_runs > 0 and done_runs >= total_runs:
        st0["running"] = False
        # If neither stopped nor finished was set, assume finished.
        if (not finished) and (not stopped):
            st0["finished"] = True
            st0["stopped"] = False

    return st0


def _csvbatch_request_stop(run_id: str) -> None:
    _csvbatch_set_state(run_id, {"stop_requested": True})

    # Best-effort: also create a stop-file on disk so the worker can observe STOP
    # even if in-memory state is lost due to Streamlit reruns.
    # We keep it in workspace root and include run_id to avoid path mismatches.
    try:
        stop_path = Path(f"tmp_rovodev_csvbatch_stop_{run_id}").resolve()
        stop_path.write_text("stop requested\n", encoding="utf-8")
        _csvbatch_set_state(run_id, {"stop_file": str(stop_path)})
    except Exception:
        pass


def _csvbatch_worker(*, run_id: str, jobs: list[dict], cfg: dict) -> None:
    """Background worker for Tab 3 batch generation.

    Stop is cooperative: worker checks stop_requested flag between portions.
    Partial results are continuously written to shared state.
    """

    try:
        base_dir = str(Path(cfg["base_dir"]).resolve())
        total_runs = int(cfg.get("total_runs") or 0)

        stop_path = Path(f"tmp_rovodev_csvbatch_stop_{run_id}").resolve()

        # Ensure state exists (and reset transient flags). IMPORTANT: do NOT overwrite
        # stop_requested here, иначе нажатие STOP во время старта может быть потеряно.
        prev_state = _csvbatch_get_state(run_id)

        # Guard against stale STOP-file (e.g. if run_id collided). If stop wasn't requested, remove it.
        try:
            if stop_path.exists() and (not bool(prev_state.get("stop_requested"))):
                stop_path.unlink(missing_ok=True)
        except Exception:
            pass
        _csvbatch_set_state(
            run_id,
            {
                "running": True,
                "stop_requested": bool(prev_state.get("stop_requested")),
                "stopped": False,
                "finished": False,
                "done_runs": 0,
                "total_runs": total_runs,
                "items": [],
                "errors": [],
                "base_dir": base_dir,
                "started_at": time.time(),
                # Diagnostics: record how many jobs were requested for this run.
                "jobs_count": int(len(jobs or [])),
            },
        )

        done_runs = 0
        all_items: list[tuple[str, str, str]] = []
        all_errors: list[str] = []

        for j in jobs:
            for portion_i in range(1, int(j["portions"]) + 1):
                st_now = _csvbatch_get_state(run_id)
                if st_now.get("stop_requested") or stop_path.exists():
                    _csvbatch_set_state(run_id, {"running": False, "stopped": True, "finished": False})
                    return

                _csvbatch_set_state(
                    run_id,
                    {
                        "current": {
                            "post_idx": int(j.get("post_idx") or 0),
                            "portion": int(portion_i),
                            "portions": int(j.get("portions") or 0),
                            "overlay_text": j.get("overlay_text") or "",
                        }
                    },
                )

                overlay_text = j["overlay_text"]
                ctx = j["context"]
                base_prompt3 = _build_pin_prompt(overlay_text, ctx, site_name=j.get("site_name"))
                slug_base = f"{overlay_text}_post{j['post_idx']+1}_p{portion_i}"

                try:
                    # HARD timeout guard: if Playwright hangs, do not keep Stage3 running forever.
                    # Note: we cannot reliably kill Chrome from here, but we can mark the run as stopped
                    # and create a STOP-file so inner loops (if any) can observe it.
                    gen_timeout_s = int(cfg.get("gen_timeout_s") or 180)
                    offline_wait_timeout_s = int(cfg.get("offline_wait_timeout_s") or 90)
                    max_attempts = int(cfg.get("max_session_attempts") or 2)
                    # Include offline-wait window so we don't hard-timeout just because
                    # the network dropped for a short period.
                    hard_timeout_s = max(60, int((gen_timeout_s * max(1, max_attempts)) + offline_wait_timeout_s + 300))

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                        fut = ex.submit(
                            _run_parallel_gemini_ui,
                            url=cfg["url"],
                            model_choice=cfg["model_choice"],
                            headless=cfg["headless"],
                            user_data_dir=cfg["user_data_dir"],
                            profile_numbers=cfg["profile_numbers"],
                            executable_path=cfg["executable_path"],
                            launch_stagger_s=int(cfg["launch_stagger_s"]),
                            base_image_path=cfg["base_image_path"],
                            base_dir=base_dir,
                            slug_base=slug_base,
                            base_prompt=base_prompt3,
                            num_windows=int(cfg["num_windows"]),
                            page_default_timeout_ms=int(cfg.get("page_default_timeout_ms") or 30000),
                            input_ready_timeout_ms=int(cfg.get("input_ready_timeout_ms") or 60000),
                            attach_timeout_ms=int(cfg.get("attach_timeout_ms") or 5000),
                            offline_wait_timeout_s=int(cfg.get("offline_wait_timeout_s") or 90),
                            gen_timeout_s=gen_timeout_s,
                            gen_retry_timeout_s=int(cfg.get("gen_retry_timeout_s") or 45),
                            max_session_attempts=max_attempts,
                            stop_path=str(stop_path),
                            stop_poll_s=2.0,
                            ui=False,
                        )
                        items, errors = fut.result(timeout=hard_timeout_s)
                except concurrent.futures.TimeoutError:
                    try:
                        stop_path.write_text("stop requested (hard-timeout)\n", encoding="utf-8")
                    except Exception:
                        pass
                    _csvbatch_set_state(run_id, {"stop_requested": True, "hard_timeout": True})
                    all_errors.append(
                        f"post{j['post_idx']+1}/portion{portion_i}: HARD TIMEOUT after ~{hard_timeout_s}s"
                    )
                    # Mark stopped and exit run.
                    _csvbatch_set_state(run_id, {"running": False, "stopped": True, "finished": False})
                    return
                except Exception as e:
                    all_errors.append(f"post{j['post_idx']+1}/portion{portion_i}: {e}")
                else:
                    for (pfx, sw, p) in (items or []):
                        display_pfx = f"post{j['post_idx']+1}_portion{portion_i}_{pfx}"
                        all_items.append((display_pfx, sw, p))
                    all_errors.extend(errors or [])

                done_runs += 1
                _csvbatch_set_state(
                    run_id,
                    {
                        "done_runs": done_runs,
                        "items": list(all_items),
                        "errors": list(all_errors),
                    },
                )

                # If STOP was requested while the portion was running, stop before next portion/post.
                st_now = _csvbatch_get_state(run_id)
                stop_exists = bool(stop_path.exists())
                _csvbatch_set_state(run_id, {"stop_checked_after": True, "stop_exists_after": stop_exists})
                if st_now.get("stop_requested") or stop_exists:
                    _csvbatch_set_state(run_id, {"running": False, "stopped": True, "finished": False})
                    return

        _csvbatch_set_state(run_id, {"running": False, "stopped": False, "finished": True})
    except Exception as e:
        # Ensure UI sees the crash reason.
        try:
            prev = _csvbatch_get_state(run_id)
            errs = list(prev.get("errors") or [])
            errs.append(f"Worker crashed: {e}")
            _csvbatch_set_state(
                run_id,
                {
                    "running": False,
                    "stopped": False,
                    "finished": False,
                    "errors": errs,
                },
            )
        except Exception:
            pass


def _csvbatch_start(*, run_id: str, jobs: list[dict], cfg: dict) -> None:
    """Start a batch run in background thread (if not running)."""

    st_now = _csvbatch_get_state(run_id)
    if st_now.get("running"):
        return

    # Pre-initialize state so UI immediately shows "running" even if worker crashes early.
    # Also: remove stop-file from a previous run.
    try:
        try:
            sp = Path(f"tmp_rovodev_csvbatch_stop_{run_id}").resolve()
            if sp.exists():
                sp.unlink()
        except Exception:
            pass

        _csvbatch_set_state(
            run_id,
            {
                "running": True,
                "stop_requested": False,
                "stopped": False,
                "finished": False,
                "done_runs": 0,
                "total_runs": int(cfg.get("total_runs") or 0),
                "items": [],
                "errors": [],
                "base_dir": str(Path(str(cfg.get("base_dir") or "")).resolve()) if cfg.get("base_dir") else None,
                "started_at": time.time(),
                "thread_started": True,
                # Diagnostics: helps verify we actually scheduled N posts.
                "jobs_count": int(len(jobs or [])),
                "jobs_post_idxs": [int(j.get("post_idx") or 0) for j in (jobs or [])][:200],
            },
        )
    except Exception:
        _csvbatch_set_state(run_id, {"thread_started": True})

    t = threading.Thread(
        target=_csvbatch_worker,
        kwargs={"run_id": run_id, "jobs": jobs, "cfg": cfg},
        daemon=True,
    )
    t.start()


def _run_parallel_gemini_ui(
    *,
    url: str,
    model_choice: str,
    headless: bool,
    user_data_dir: str,
    profile_numbers: list[int] | None,
    executable_path: str,
    launch_stagger_s: int,
    base_image_path: str,
    base_dir: str,
    slug_base: str,
    base_prompt: str,
    num_windows: int,
    # Robustness knobs
    page_default_timeout_ms: int = 30000,
    input_ready_timeout_ms: int = 60000,
    attach_timeout_ms: int = 5000,
    # If internet is down during initial navigation, keep the window open and wait.
    # Recommended for long batches; default is 0 to avoid changing behavior in other tabs.
    offline_wait_timeout_s: int = 0,
    gen_timeout_s: int = 180,
    gen_retry_timeout_s: int = 45,
    max_session_attempts: int = 2,
    # Cooperative STOP support (Tab3)
    stop_path: str | None = None,
    stop_poll_s: float = 2.0,
    # Streamlit UI rendering (must be False in background threads)
    ui: bool = True,
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Parallel run: 1 Chrome profile/window per slot.

    Args:
      num_windows: number of parallel windows (slots) to run.

    Returns:
      items: list[(slot_label, salt_word, saved_path)]
      errors: list[str]
    """

    try:
        num_windows = int(num_windows)
    except Exception:
        num_windows = 4
    num_windows = max(1, min(24, num_windows))

    slot_labels = [f"w{i}" for i in range(1, num_windows + 1)]

    # One random suffix per window (per run) to diversify Gemini outputs
    per_slot_word = {lab: random.choice(RANDOM_WORDS) for lab in slot_labels}

    # Keep backward behavior: add a polite prefix word, cycle through PREFIXES if needed
    final_prompts = [
        f"{PREFIXES[i % len(PREFIXES)]}, {base_prompt} {per_slot_word[slot_labels[i]]}"
        for i in range(len(slot_labels))
    ]

    # NOTE: Stage3 used to clone profiles into tmp_rovodev_pw_profiles_* to avoid
    # running the same profile in parallel. This is very slow on large profiles.
    # If the user provided enough real numbered profiles, we do NOT clone.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    tmp_root = Path(f"tmp_rovodev_pw_profiles_{ts}").resolve()
    download_tmp_root = Path(f"tmp_rovodev_pw_downloads_{ts}_{os.getpid()}").resolve()

    def _ignore_profile(_dirpath: str, names: list[str]):
        skip_exact = {
            "Cache",
            "Code Cache",
            "GPUCache",
            "GrShaderCache",
            "ShaderCache",
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
            # NOTE: do not ignore Service Worker for AI Studio.
            # Missing Service Worker/IndexedDB data can cause intermittent auth/permission issues.
            # if n.lower() in {"service worker", "serviceworker"}:
            #     ignored.append(n)
            #     continue
        return ignored

    max_workers = len(final_prompts)

    # Build profile pool
    requested_numbers = [int(n) for n in (profile_numbers or []) if int(n) > 0]

    profile_pool: queue.Queue[str] = queue.Queue()

    base_profile_dir = _normalize_user_data_dir(user_data_dir)
    if not base_profile_dir:
        raise ValueError("Invalid user-data-dir")

    # Use global banlist across portions (so we don't reuse rate-limited profiles within the same run).
    rate_limited_profiles = _GLOBAL_RATE_LIMITED_PROFILES
    # Profiles that we decided to drop for *this run only* (e.g. repeated WINDOW_CLOSED / 0 images).
    # Unlike rate_limited_profiles, this does not persist between runs.
    dropped_profiles: set[str] = set()

    # Debug: track attempts to find replacement profiles so we can explain
    # why parallelism dropped (written into errors on failure).
    repl_debug: list[str] = []

    # Persist next profile pointer per base_profile_dir.
    # IMPORTANT: reset at the start of each run so we don't skip existing profiles.
    # (Otherwise, after a previous run advanced the global pointer to a large value,
    #  replacements like <base>_14 won't ever be considered.)
    if requested_numbers:
        _GLOBAL_NEXT_PROFILE_NUM[base_profile_dir] = max(requested_numbers) + 1
    next_profile_num = _GLOBAL_NEXT_PROFILE_NUM.get(base_profile_dir, 1)

    _profile_lock = threading.Lock()

    def _try_enqueue_next_profile() -> bool:
        """If <base>_<N> or <base><N> exists for some next N, enqueue it and increment pointer."""
        nonlocal next_profile_num
        if not requested_numbers:
            return False
        with _profile_lock:
            for _ in range(50):
                cur_n = int(next_profile_num)
                cand_paths = _candidate_profile_paths(base_profile_dir, cur_n)
                next_profile_num += 1
                _GLOBAL_NEXT_PROFILE_NUM[base_profile_dir] = int(next_profile_num)
                for cand in cand_paths:
                    try:
                        exists = os.path.isdir(cand)
                        banned = cand in rate_limited_profiles
                        # Keep only a small tail to avoid memory growth.
                        repl_debug.append(f"n={cur_n} exists={int(exists)} banned={int(banned)} path={cand}")
                        if len(repl_debug) > 200:
                            repl_debug[:] = repl_debug[-120:]
                        if exists and (not banned):
                            profile_pool.put(cand)
                            return True
                    except Exception as e:
                        repl_debug.append(f"n={cur_n} err={type(e).__name__} path={cand}")
                        if len(repl_debug) > 200:
                            repl_debug[:] = repl_debug[-120:]
                        continue
        return False

    def _clone_base_into_slot(slot_label: str) -> str:
        """Clone base_profile_dir into tmp_root/<slot_label> and return path."""
        # Lazily create tmp_root only if we actually need cloning.
        tmp_root.mkdir(parents=True, exist_ok=True)
        dst = tmp_root / f"slot_{slot_label}"
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(base_profile_dir, dst, dirs_exist_ok=False, ignore=_ignore_profile)
        return str(dst)

    # Build profile pool
    # requested_numbers already computed above

    def _candidate_profile_paths(base_dir: str, num: int) -> list[str]:
        """Return possible numbered profile paths for base_dir and num.

        Handles both hidden and non-hidden variants of the last path component, e.g.:
        - C:\\path\\.chrome_automation_profile_11
        - C:\\path\\chrome_automation_profile_11
        """
        base_dir = _normalize_user_data_dir(base_dir) or base_dir
        base_path = Path(base_dir)
        last = base_path.name
        # toggle leading dot variant
        if last.startswith("."):
            alt_last = last[1:]
        else:
            alt_last = "." + last
        parent = base_path.parent if str(base_path.parent) != str(base_path) else Path(".")
        # Some users have profiles named both with and without an underscore before the number:
        #   .chrome_automation_profile_15
        #   .chrome_automation_profile16
        p1 = parent / f"{last}_{num}"
        p2 = parent / f"{alt_last}_{num}"
        p3 = parent / f"{last}{num}"
        p4 = parent / f"{alt_last}{num}"
        return [str(p1.resolve()), str(p2.resolve()), str(p3.resolve()), str(p4.resolve())]

    if requested_numbers:
        # Use ONLY specific numbered profiles: <base>_<num>.
        # Important: do NOT clone in this mode (to match Tab1 behavior and avoid heavy disk IO).
        for n in requested_numbers:
            for cand in _candidate_profile_paths(base_profile_dir, n):
                try:
                    if os.path.isdir(cand) and (cand not in rate_limited_profiles):
                        profile_pool.put(cand)
                        break
                except Exception:
                    pass

        # Try to keep requested parallelism even if some configured profiles are banned.
        #
        # Example:
        # - user configured profiles: 9,10
        # - profile 9 becomes rate-limited during a long batch
        #
        # On the next portion/run we *rebuild* the pool. If we only enqueue 9 and 10,
        # we end up with just 10 available -> max_workers collapses to 1 and the whole
        # batch effectively continues in a single window.
        #
        # To preserve parallelism, try to proactively enqueue "next" existing profiles
        # (11,12,...) before reducing max_workers.
        target_workers = int(max_workers)

        # NOTE: this only makes sense when profile_numbers were explicitly provided.
        # We never clone in this mode, because cloning a persistent Chrome profile is
        # heavy and can cause auth/permission flakiness.
        while profile_pool.qsize() < target_workers:
            if not _try_enqueue_next_profile():
                break

        # Limit parallelism to the number of available real profiles.
        available = int(profile_pool.qsize())
        if available <= 0:
            # Fallback: if numbered directories are not present, clone the base profile into tmp slots.
            # This keeps Stage3 usable even when user_data_dir points to a single Chrome profile root.
            for i, n in enumerate(requested_numbers[:max_workers], start=1):
                try:
                    profile_pool.put(_clone_base_into_slot(f"{n}"))
                except Exception:
                    profile_pool.put(_clone_base_into_slot(f"tmp{i}"))
            available = int(profile_pool.qsize())
            if available <= 0:
                raise RuntimeError(
                    "No usable profile directories found for the provided profile numbers. "
                    "Either create <user_data_dir>_<N> folders or clear profile numbers."
                )

        if available < max_workers:
            max_workers = available
            slot_labels = slot_labels[:max_workers]
            final_prompts = final_prompts[:max_workers]
            per_slot_word = {lab: per_slot_word[lab] for lab in slot_labels}
    else:
        # Backward-compatible default: prefer numbered directories next to base profile dir.
        for slot_idx in range(1, max_workers + 1):
            used = False
            for cand in _candidate_profile_paths(base_profile_dir, slot_idx):
                try:
                    if os.path.isdir(cand):
                        profile_pool.put(cand)
                        used = True
                        break
                except Exception:
                    pass
            if not used:
                profile_pool.put(_clone_base_into_slot(str(slot_idx)))

    stop_file = Path(str(stop_path)).resolve() if stop_path else None

    def _stop_requested() -> bool:
        if not stop_file:
            return False
        try:
            return stop_file.exists()
        except Exception:
            return False

    def _run_one(idx: int, prefix: str, fp: str, salt_word: str, stagger_s: int) -> Dict:
        try:
            if stagger_s and idx > 0:
                # sleep in small chunks so STOP can abort quickly
                total = int(stagger_s) * int(idx)
                slept = 0.0
                while slept < total:
                    if _stop_requested():
                        raise RuntimeError("STOP requested")
                    step = min(0.5, total - slept)
                    time.sleep(step)
                    slept += step
        except Exception:
            # note: any exception here means the slot run is aborted
            if _stop_requested():
                return {"prefix": prefix, "salt_word": salt_word, "saved": [], "errors": []}
            pass

        saved: list[str] = []
        errors: list[str] = []
        profile_dir = None
        try:
            profile_dir = profile_pool.get()
            session_attempts = int(max_session_attempts or 1)
            session_attempts = max(1, min(10, session_attempts))
            for session_attempt in range(1, session_attempts + 1):
                profile_pref_backups: list[tuple[Path, bytes]] = []
                trace_path = (
                    Path(base_dir)
                    / "_download_debug"
                    / f"{_safe_filename(prefix)}_attempt{session_attempt}.log"
                )

                def _download_trace(message: str) -> None:
                    """Keep per-window evidence outside the disposable temp folder."""

                    try:
                        trace_path.parent.mkdir(parents=True, exist_ok=True)
                        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        with open(trace_path, "a", encoding="utf-8") as trace_file:
                            trace_file.write(f"{stamp} {message}\n")
                    except Exception:
                        pass

                try:
                    browser_download_dir = download_tmp_root / f"{prefix}_attempt{session_attempt}"
                    browser_download_dir.mkdir(parents=True, exist_ok=True)
                    _download_trace(f"worker download directory: {browser_download_dir}")
                    profile_pref_backups, pref_notes = _set_profile_download_directory_temporarily(
                        profile_dir,
                        str(browser_download_dir),
                    )
                    for pref_note in pref_notes:
                        _download_trace(pref_note)
                    with sync_playwright() as p:
                        ctx = _launch_persistent_ctx_with_retries(
                            p,
                            user_data_dir=profile_dir,
                            headless=headless,
                            executable_path=executable_path or None,
                            downloads_path=str(browser_download_dir),
                        )
                        try:
                            page = ctx.new_page()
                            _download_trace(_pin_chrome_download_directory(page, str(browser_download_dir)))
                            page.set_default_timeout(int(page_default_timeout_ms or 30000))
                            _goto_with_offline_wait(
                                page,
                                url,
                                offline_wait_timeout_s=int(offline_wait_timeout_s or 0),
                                stop_requested_fn=_stop_requested,
                                wait_until="load",
                            )
                            _debug_dom(page)
                            _wait_input_ready(page, timeout_ms=int(input_ready_timeout_ms or 60000))

                            try:
                                _start_new_chat(page)
                            except Exception:
                                pass

                            def _filter_imgs(_imgs: List[Tuple[str, bytes]]) -> List[Tuple[str, bytes]]:
                                """Drop obvious thumbnails/garbage (too small)."""
                                out: List[Tuple[str, bytes]] = []
                                for _mime, _blob in (_imgs or []):
                                    try:
                                        if _blob and len(_blob) >= 25000:
                                            out.append((_mime, _blob))
                                    except Exception:
                                        continue
                                return out

                            imgs: List[Tuple[str, bytes]] = []

                            if base_image_path:
                                try:
                                    import gemini_pw_helpers as _gph
                                    _gph._handle_signed_out_upload_tooltip(page, max_time_s=2.5)
                                except Exception:
                                    pass

                                if not os.path.exists(base_image_path):
                                    raise FileNotFoundError(base_image_path)

                                ok = _attach_image(page, base_image_path, max_time_s=28.0)
                                attached_preview = _wait_image_attached(page, timeout_ms=int(attach_timeout_ms or 5000))
                                if not ok or not attached_preview:
                                    try:
                                        ok2 = _attach_image(page, base_image_path, max_time_s=20.0)
                                        attached_preview = attached_preview or _wait_image_attached(
                                            page, timeout_ms=int(attach_timeout_ms or 5000)
                                        )
                                        ok = ok or ok2
                                    except Exception:
                                        pass

                                if not attached_preview:
                                    raise RuntimeError("Attach timeout / not signed in (upload blocked)")

                            _dismiss_overlays(page)
                            try:
                                if model_choice:
                                    gph._pick_model(page, model_choice)
                            except Exception:
                                pass

                            _type_prompt(page, fp)
                            ok_send = False
                            try:
                                ok_send = bool(_click_send(page))
                            except Exception:
                                ok_send = False
                            if not ok_send:
                                raise RuntimeError("Send/Run did not start generation")

                            # Network/UI-download/page-fetch only (NO screenshots)
                            # We keep STOP responsiveness while the model is still generating by polling
                            # for "images appeared" cheaply, and only then giving the downloader a
                            # sufficiently long window (Gemini helper needs ~12s collection window).
                            imgs = []
                            total_budget = int(gen_timeout_s or 180)
                            poll = float(stop_poll_s or 2.0)
                            poll = max(0.5, min(10.0, poll))

                            started_m = time.monotonic()
                            deadline_m = started_m + float(total_budget)
                            # `gen_retry_timeout_s` used to be displayed in the
                            # UI but was never applied here.  Gemini can render an
                            # image before its Download toolbar becomes clickable,
                            # especially after a UI rollout.  Once an image is
                            # visible, allow a small *download-only* grace window;
                            # successful runs still finish immediately.
                            download_grace_deadline_m = None
                            download_attempts = 0
                            download_retry_s = max(0, int(gen_retry_timeout_s or 0))

                            while time.monotonic() < (download_grace_deadline_m or deadline_m):
                                if _stop_requested():
                                    raise RuntimeError("STOP requested")

                                # Wait for image containers to appear (cheap check).
                                has_imgs = False
                                try:
                                    has_imgs = bool(_has_generated_images(page))
                                except Exception:
                                    has_imgs = False

                                if not has_imgs:
                                    # Still generating: keep STOP responsive.
                                    active_deadline_m = download_grace_deadline_m or deadline_m
                                    time.sleep(min(poll, max(0.5, active_deadline_m - time.monotonic())))
                                    continue

                                if download_grace_deadline_m is None:
                                    if download_retry_s:
                                        # Start a limited download-only grace
                                        # window only after a visible image failed
                                        # to become a saved file.  It is bounded by
                                        # the configured retry value, rather than
                                        # consuming the remaining generation time.
                                        download_grace_deadline_m = (
                                            time.monotonic() + float(download_retry_s)
                                        )

                                # Images are visible in DOM -> give the downloader enough time to collect network
                                # candidates and fetch bytes (otherwise it often returns empty).
                                active_deadline_m = download_grace_deadline_m or deadline_m
                                remaining_s = max(1.0, active_deadline_m - time.monotonic())
                                dl_timeout_s = int(max(20, min(180, remaining_s)))

                                try:
                                    imgs = _wait_and_download_generated_images(
                                        page,
                                        ctx,
                                        timeout_s=dl_timeout_s,
                                        # The Stage3 prompt explicitly requests one
                                        # pin.  Limiting this to one also prevents
                                        # a nested toolbar control from causing a
                                        # duplicate click/save for the same image.
                                        max_images=1,
                                        allow_screenshot_fallback=False,
                                        # Align closer to Stage1: do not use ultra-short request timeouts.
                                        request_timeout_ms=60000,
                                        # The actual Gemini Download button is
                                        # the source of the final original file.
                                        require_browser_download=True,
                                        browser_download_dir=str(browser_download_dir),
                                        native_download_dirs=_native_download_watch_dirs(profile_dir),
                                        download_debug_hook=_download_trace,
                                        # Gemini can ignore the private path and
                                        # write every window's file to Windows
                                        # Downloads.  Serialize only this final
                                        # click+file-confirmation phase so a
                                        # worker never steals another pin.
                                        serialize_native_download_click=True,
                                        native_download_confirmation_timeout_s=20.0,
                                    )
                                except Exception as e:
                                    msg = str(e).lower()
                                    if (
                                        ("target page, context or browser has been closed" in msg)
                                        or ("has been closed" in msg)
                                        or ("target closed" in msg)
                                    ):
                                        raise RuntimeError("WINDOW_CLOSED")
                                    if ("rate limit" in msg) or ("too many requests" in msg):
                                        raise RuntimeError(f"RATE_LIMIT: {e}")
                                    if ("internal error" in msg) or ("an internal error" in msg):
                                        raise RuntimeError(f"UI_ERROR: {e}")
                                    imgs = []

                                imgs = _filter_imgs(imgs)
                                if imgs:
                                    break

                                download_attempts += 1
                                active_deadline_m = download_grace_deadline_m or deadline_m
                                # An explicitly disabled retry keeps the former
                                # two-attempt behavior instead of spending the
                                # remaining generation timeout on retries.
                                if (not download_retry_s) and download_attempts >= 2:
                                    break
                                if time.monotonic() >= active_deadline_m:
                                    break
                                # A short, bounded re-probe is enough for a
                                # late-mounted toolbar and does not resend the
                                # prompt or regenerate the image.
                                time.sleep(min(1.0, max(0.25, active_deadline_m - time.monotonic())))

                            if not imgs:
                                try:
                                    if page.is_closed():
                                        raise RuntimeError("WINDOW_CLOSED")
                                except Exception:
                                    pass
                                raise RuntimeError("No images were collected")

                            slug = _slug(slug_base)
                            for j, (mime, blob) in enumerate(imgs, 1):
                                ext = {
                                    "image/png": "png",
                                    "image/jpeg": "jpg",
                                    "image/webp": "webp",
                                }.get(mime, "bin")
                                pfx_slug = _slug(prefix)
                                # prefix is the slot label (w1..wN), so no need to duplicate idx in filename
                                fname = f"{slug}_{pfx_slug}_{j:02d}.{ext}"
                                fname = _safe_filename(fname)
                                fpath = os.path.join(base_dir, fname)
                                with open(fpath, "wb") as f:
                                    f.write(blob)
                                # Verify the completed browser download was
                                # actually persisted before the window/context is
                                # allowed to close in the finally block below.
                                written_size = os.path.getsize(fpath)
                                if written_size != len(blob) or written_size < 25000:
                                    raise RuntimeError(
                                        f"Saved file verification failed: {os.path.basename(fpath)} "
                                        f"({written_size}/{len(blob)} bytes)"
                                    )
                                saved.append(fpath)
                                gph._log(
                                    f"[download] final result saved: {fpath} ({written_size} bytes)"
                                )
                                _download_trace(
                                    f"final result saved: {fpath} ({written_size} bytes)"
                                )

                            break
                        finally:
                            # Make a best-effort attempt to close the window even on timeouts.
                            # Persistent contexts sometimes fail to close if a page is still mid-navigation.
                            try:
                                page.close()
                            except Exception:
                                pass
                            try:
                                # For persistent contexts, closing the browser is the most reliable way
                                # to ensure the OS window disappears.
                                b = getattr(ctx, "browser", None)
                                if b is not None:
                                    b.close()
                            except Exception:
                                pass
                            try:
                                ctx.close()
                            except Exception:
                                pass
                            for restore_note in _restore_profile_download_directory(profile_pref_backups):
                                _download_trace(restore_note)
                            profile_pref_backups = []
                except Exception as e:
                    if profile_pref_backups:
                        for restore_note in _restore_profile_download_directory(profile_pref_backups):
                            _download_trace(restore_note)
                        profile_pref_backups = []
                    msg = str(e)
                    _download_trace(f"worker attempt failed: {msg}")
                    # Treat STOP as a normal (non-error) early-exit.
                    if "STOP requested" in msg:
                        break

                    # Manual window close should be retryable but NEVER rate-limit-bannable.
                    if "WINDOW_CLOSED" in msg.upper():
                        try:
                            if profile_dir and str(profile_dir) in rate_limited_profiles:
                                rate_limited_profiles.discard(str(profile_dir))
                        except Exception:
                            pass

                    # If we exhausted all attempts and still got 0 images, we need to decide whether
                    # to BAN the profile (persist across portions) or just DROP it for this portion.
                    #
                    # WINDOW_CLOSED is ambiguous: it can be a manual close, but in unattended batches
                    # it often indicates the UI became unusable (daily quota banners, crashes, etc.).
                    # If we keep it as DROP_PROFILE, the profile returns in the next portion and can
                    # create an infinite loop (observed as "stuck" on a single profile number).
                    if (
                        (session_attempt >= session_attempts)
                        and (not saved)
                        and requested_numbers
                        and ("WINDOW_CLOSED" in msg.upper())
                    ):
                        msg = "RATE_LIMIT: window_closed/no-images"

                    if (
                        (session_attempt >= session_attempts)
                        and (not saved)
                        and requested_numbers
                        and (
                            ("НЕ НАЙДЕНО ПОЛЕ ВВОДА" in msg.upper())
                            or ("INPUT FIELD" in msg.upper())
                        )
                    ):
                        msg = "DROP_PROFILE: ui-not-ready/no-images"
                    # If we exhausted all session attempts and still got 0 images due to timeouts,
                    # treat this profile as "rate-limited" for the rest of the run and switch to the next.
                    # This helps long Tab3 batches where some accounts hit a daily limit and start timing out.
                    if (
                        (session_attempt >= session_attempts)
                        and (not saved)
                        and requested_numbers
                        and _is_bannable_profile_error(msg)
                    ):
                        msg = "RATE_LIMIT: timeout/no-images"

                    # On rate limit we also drop this profile and try to switch to a new one.
                    if "RATE_LIMIT:" in msg.upper():
                        # Ban current profile and immediately switch to a fresh one.
                        if profile_dir:
                            rate_limited_profiles.add(str(profile_dir))
                        # Try hard to keep parallelism: attempt to enqueue and acquire a replacement profile.
                        # (E.g. 9..13 configured, 10 got banned -> switch to 14,15,...)
                        new_prof = None
                        for _ in range(15):
                            _try_enqueue_next_profile()
                            try:
                                cand = profile_pool.get(timeout=2)
                            except Exception:
                                cand = None
                            if cand and (str(cand) not in rate_limited_profiles):
                                new_prof = cand
                                break

                        if new_prof and (str(new_prof) not in rate_limited_profiles):
                            profile_dir = new_prof
                            errors.append(f"{prefix}: RATE_LIMIT (timeout/no-images) -> switched profile")
                            continue
                        else:
                            tail = " | ".join(repl_debug[-8:])
                            errors.append(
                                f"{prefix}: RATE_LIMIT and no replacement profiles available "
                                f"(pool_qsize={profile_pool.qsize()}, next_profile_num={next_profile_num}, base_profile_dir={base_profile_dir}; tail={tail})"
                            )
                            break

                    # Drop-profile path (run-scoped), e.g. repeated manual close leading to 0 images.
                    if "DROP_PROFILE:" in msg.upper():
                        if profile_dir:
                            dropped_profiles.add(str(profile_dir))
                        new_prof = None
                        for _ in range(15):
                            _try_enqueue_next_profile()
                            try:
                                cand = profile_pool.get(timeout=2)
                            except Exception:
                                cand = None
                            if cand and (str(cand) not in rate_limited_profiles) and (str(cand) not in dropped_profiles):
                                new_prof = cand
                                break
                        if new_prof:
                            profile_dir = new_prof
                            errors.append(f"{prefix}: DROP_PROFILE -> switched profile")
                            continue
                        errors.append(f"{prefix}: DROP_PROFILE and no replacement profiles available")
                        break

                    if session_attempt < session_attempts and _is_retryable_error(msg):
                        errors.append(f"{prefix}: attempt {session_attempt} failed ({msg}); retrying")
                        continue
                    errors.append(f"{prefix}: {msg}")
                    break
        finally:
            try:
                if profile_dir:
                    # Do NOT return rate-limited profiles back to the pool.
                    if (str(profile_dir) not in rate_limited_profiles) and (str(profile_dir) not in dropped_profiles):
                        profile_pool.put(profile_dir)
            except Exception:
                pass

        return {"prefix": prefix, "salt_word": salt_word, "saved": saved, "errors": errors}

    import concurrent.futures

    status = st.empty() if ui else None
    progress = st.progress(0) if ui else None
    done = 0
    total = len(final_prompts)

    results: List[Dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [
            ex.submit(
                _run_one,
                i,
                slot_labels[i],
                final_prompts[i],
                per_slot_word[slot_labels[i]],
                int(launch_stagger_s),
            )
            for i in range(len(final_prompts))
        ]
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result() or {}
            results.append(r)
            done += 1
            if progress is not None:
                progress.progress(min(1.0, done / max(1, total)))
            if status is not None:
                status.write(f"Готово: {done}/{total}")

    if progress is not None:
        progress.progress(1.0)

    # Cleanup only temporary cloned profiles
    shutil.rmtree(tmp_root, ignore_errors=True)
    shutil.rmtree(download_tmp_root, ignore_errors=True)

    # Collect errors/items
    all_errors: List[str] = []
    for r in results:
        all_errors.extend(r.get("errors") or [])

    items: list[tuple[str, str, str]] = []  # (slot_label, salt_word, path)
    slot_to_res = {r.get("prefix"): r for r in results}
    for lab in slot_labels:
        r = slot_to_res.get(lab) or {}
        sw = r.get("salt_word") or ""
        for p in (r.get("saved") or []):
            items.append((lab, sw, p))

    return items, all_errors


@st.cache_data(show_spinner=False)
def _read_file_bytes_cached(path: str, mtime: float) -> bytes:
    """Read file bytes with caching keyed by mtime."""

    with open(path, "rb") as f:
        return f.read()


@st.cache_data(show_spinner=False)
def _read_as_png_bytes_cached(path: str, mtime: float) -> bytes:
    """Return PNG bytes for an image file; converts non-PNG via PIL.

    Cached by (path, mtime) so Streamlit reruns after clicking download
    do not re-read/re-convert large images.
    """

    ext = (Path(path).suffix or "").lower()
    if ext == ".png":
        return _read_file_bytes_cached(path, mtime)

    img = Image.open(path)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@st.cache_data(show_spinner=False)
def _build_zip_cached(files: tuple[tuple[str, float], ...]) -> bytes:
    """Build a ZIP archive of files. Cached to avoid heavy work on reruns."""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, mtime in files:
            try:
                arcname = Path(path).name
                zf.writestr(arcname, _read_file_bytes_cached(path, mtime))
            except Exception:
                continue
    return buf.getvalue()


def _render_results_block(*, state_prefix: str, title: str = "Последние результаты") -> None:
    """Render results grid + downloads.

    Enhancement: allow selecting a results folder manually and persist it on disk,
    so results can be re-opened after a browser refresh/restart.
    """

    # --- Persisted folder selection (survives browser refresh) ---
    _PERSIST_RESULTS_DIRS_PATH = Path("last_results_dirs.json")

    def _load_persisted_dirs() -> dict:
        try:
            if _PERSIST_RESULTS_DIRS_PATH.exists():
                obj = json.loads(_PERSIST_RESULTS_DIRS_PATH.read_text(encoding="utf-8"))
                return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
        return {}

    def _save_persisted_dirs(obj: dict) -> None:
        try:
            _PERSIST_RESULTS_DIRS_PATH.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            # Best-effort only; UI still works without persistence.
            pass

    def _push_recent_dir(prefix: str, p: str) -> None:
        p = (p or "").strip()
        if not p:
            return
        try:
            # Keep only existing dirs, but don't fail hard if path is network/offline.
            pth = Path(p)
            if not pth.exists() or not pth.is_dir():
                return
        except Exception:
            return

        data = _load_persisted_dirs()
        cur = data.get(prefix)
        if not isinstance(cur, dict):
            cur = {}
        rec = cur.get("recent")
        if not isinstance(rec, list):
            rec = []
        # de-dup + keep newest first
        rec = [x for x in rec if isinstance(x, str) and x != p]
        rec.insert(0, p)
        rec = rec[:30]
        cur["recent"] = rec
        cur["selected"] = p
        data[prefix] = cur
        _save_persisted_dirs(data)

    def _get_persisted_selected(prefix: str) -> str | None:
        data = _load_persisted_dirs()
        cur = data.get(prefix)
        if isinstance(cur, dict):
            sel = (cur.get("selected") or "").strip()
            return sel or None
        return None

    def _get_persisted_recent(prefix: str) -> list[str]:
        data = _load_persisted_dirs()
        cur = data.get(prefix)
        if isinstance(cur, dict):
            rec = cur.get("recent")
            if isinstance(rec, list):
                out: list[str] = []
                for x in rec:
                    if isinstance(x, str) and x.strip():
                        out.append(x.strip())
                return out
        return []

    # 1) take current session_state, 2) fallback to persisted selection
    out_dir = st.session_state.get(f"{state_prefix}_last_base_dir")
    if not out_dir:
        persisted = _get_persisted_selected(state_prefix)
        if persisted:
            st.session_state[f"{state_prefix}_last_base_dir"] = persisted
            out_dir = persisted

    # If still none, show a picker UI (select from known results roots) and exit.
    if not out_dir:
        st.markdown("---")
        st.subheader(title)
        st.caption("Путь к папке результатов не задан. Выберите папку с результатами из списка и нажмите 'Показать'.")

        def _list_result_subdirs(root_dir: Path) -> list[Path]:
            try:
                if not root_dir.exists() or not root_dir.is_dir():
                    return []
                subdirs = [p for p in root_dir.iterdir() if p.is_dir()]
                exts = {".png", ".jpg", ".jpeg", ".webp"}

                def _has_images(d: Path) -> bool:
                    try:
                        for fp in d.rglob("*"):
                            if fp.is_file() and fp.suffix.lower() in exts:
                                return True
                    except Exception:
                        return False
                    return False

                subdirs = [d for d in subdirs if _has_images(d)]
                subdirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return subdirs
            except Exception:
                return []

        userprofile = os.environ.get("USERPROFILE") or ""
        desktop_guess = Path(userprofile) / "Desktop" / "generate automation" / "generated_images"
        roots: list[Path] = []
        try:
            roots.append(desktop_guess)
        except Exception:
            pass
        try:
            roots.append(Path("generated_images").resolve())
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

        recent = _get_persisted_recent(state_prefix)
        opts: list[str] = []
        for x in [*[str(p) for p in subdir_paths], *recent]:
            if isinstance(x, str) and x and x not in opts:
                opts.append(x)

        if not opts:
            st.warning("Не найдено папок с результатами для выбора. Сначала запустите генерацию или укажите, где лежат результаты.")
            return

        picked = st.selectbox(
            f"Папки результатов" + (f" (из {root_selected})" if root_selected else ""),
            options=opts,
            index=0,
            key=f"{state_prefix}_pick_results_dir_empty",
        )

        if st.button("Показать", key=f"{state_prefix}_apply_results_dir_empty"):
            new_dir = (picked or "").strip().strip('"')
            if new_dir and Path(new_dir).exists() and Path(new_dir).is_dir():
                st.session_state[f"{state_prefix}_last_base_dir"] = new_dir
                st.session_state[f"{state_prefix}_last_items"] = []
                st.session_state[f"{state_prefix}_force_rescan"] = True
                _push_recent_dir(state_prefix, new_dir)
                _ui_rerun()
            else:
                st.error("Папка не найдена или это не папка")
        return

    # Keep persisted history in sync with the currently shown directory.
    _push_recent_dir(state_prefix, str(out_dir))

    download_base_name = (st.session_state.get(f"{state_prefix}_download_base_name") or "").strip()

    st.markdown("---")
    st.subheader(title)
    st.code(out_dir)

    # --- Manual folder selection (even after refresh) ---
    with st.expander("Выбрать другую папку результатов", expanded=False):
        def _list_result_subdirs(root_dir: Path) -> list[Path]:
            try:
                if not root_dir.exists() or not root_dir.is_dir():
                    return []
                subdirs = [p for p in root_dir.iterdir() if p.is_dir()]
                exts = {".png", ".jpg", ".jpeg", ".webp"}

                def _has_images(d: Path) -> bool:
                    try:
                        for fp in d.rglob("*"):
                            if fp.is_file() and fp.suffix.lower() in exts:
                                return True
                    except Exception:
                        return False
                    return False

                # Only folders that actually contain images
                subdirs = [d for d in subdirs if _has_images(d)]
                # Newest first
                subdirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return subdirs
            except Exception:
                return []

        # Candidate roots: your Desktop path + local repo folder + parent of current out_dir
        userprofile = os.environ.get("USERPROFILE") or ""
        desktop_guess = Path(userprofile) / "Desktop" / "generate automation" / "generated_images"
        roots: list[Path] = []
        try:
            roots.append(desktop_guess)
        except Exception:
            pass
        try:
            roots.append(Path("generated_images").resolve())
        except Exception:
            pass
        try:
            roots.append(Path(str(out_dir)).resolve().parent)
        except Exception:
            pass

        # Pick first existing root with subdirs
        root_selected: Path | None = None
        subdir_paths: list[Path] = []
        for r in roots:
            subs = _list_result_subdirs(r)
            if subs:
                root_selected = r
                subdir_paths = subs
                break

        if not subdir_paths:
            st.caption("Не нашёл папки с результатами в типовых местах. Откройте папку через проводник и перезапустите, либо скажите где именно лежат результаты — добавлю путь.")
        else:
            # Show recent as a secondary source
            recent = _get_persisted_recent(state_prefix)
            opts: list[str] = []
            # 1) current out_dir 2) subfolders from root 3) recent history
            for x in [str(out_dir), *[str(p) for p in subdir_paths], *recent]:
                if isinstance(x, str) and x and x not in opts:
                    opts.append(x)

            picked = st.selectbox(
                f"Папки результатов" + (f" (из {root_selected})" if root_selected else ""),
                options=opts,
                index=0,
                key=f"{state_prefix}_pick_results_dir",
            )

            colx, coly = st.columns([1, 1])
            with colx:
                apply_btn = st.button("Показать", key=f"{state_prefix}_apply_results_dir")
            with coly:
                rescan_btn = st.button("Пересканировать текущую папку", key=f"{state_prefix}_rescan_results_dir")

            if apply_btn:
                new_dir = (picked or "").strip().strip('"')
                if new_dir and Path(new_dir).exists() and Path(new_dir).is_dir():
                    st.session_state[f"{state_prefix}_last_base_dir"] = new_dir
                    # Force rescan on next render
                    st.session_state[f"{state_prefix}_last_items"] = []
                    st.session_state[f"{state_prefix}_force_rescan"] = True
                    _push_recent_dir(state_prefix, new_dir)
                    _ui_rerun()
                else:
                    st.error("Папка не найдена или это не папка")

            if rescan_btn:
                st.session_state[f"{state_prefix}_force_rescan"] = True
                _ui_rerun()

    if st.button("Открыть папку с результатами", key=f"{state_prefix}_open_results_folder"):
        ok, msg = _open_folder(out_dir)
        if not ok:
            st.error(f"Не удалось открыть папку: {msg}")
        else:
            st.info(f"Открываю: {msg}")

        # Edge-case fix: if generation finished while user was away (opened folder),
        # force a one-time UI refresh so the latest files/grid/errors become visible.
        # Request forced rescan on next rerun (so UI shows ALL files in the folder)
        try:
            st.session_state[f"{state_prefix}_force_rescan"] = True
            st.session_state[f"{state_prefix}_manual_refresh_at"] = time.time()
        except Exception:
            pass
        _ui_rerun()

    errs = st.session_state.get(f"{state_prefix}_last_errors") or []
    if errs:
        st.error("Ошибки:\n" + "\n".join(errs))

    # If user requested manual refresh, force rescan even if we already have some items in memory.
    force_rescan = False
    try:
        force_rescan = bool(st.session_state.pop(f"{state_prefix}_force_rescan", False))
    except Exception:
        force_rescan = False

    items = st.session_state.get(f"{state_prefix}_last_items") or []

    # Disk rescan fallback (useful after refresh/restart OR when user picked a new folder).
    # Originally it was enabled only for csvbatch; we extend it to all prefixes but keep it
    # best-effort and only when needed.
    if force_rescan or (not items):
        try:
            pdir = Path(str(out_dir))
            if pdir.exists() and pdir.is_dir():
                exts = {".png", ".jpg", ".jpeg", ".webp"}
                files = [p for p in pdir.rglob('*') if p.is_file() and p.suffix.lower() in exts]
                files.sort(key=lambda p: (p.stat().st_mtime, p.name))
                if files:
                    items = [("file", "", str(p)) for p in files]
                    st.session_state[f"{state_prefix}_last_items"] = items
        except Exception:
            pass

    if not items:
        # In batch mode we can be in-progress; don't scare the user with a warning.
        if state_prefix == "csvbatch" and (st.session_state.get("csvbatch_running") is True):
            st.info("Пока нет сохранённых картинок (генерация ещё идёт).")
        else:
            st.warning("Нет сохранённых картинок для отображения")
        return

    # One-click download to avoid many reruns (and broken downloads during rerender)
    try:
        files_for_zip: list[tuple[str, float]] = []
        for _pfx, _sw, p in items:
            try:
                mt = float(os.path.getmtime(p))
            except Exception:
                mt = 0.0
            files_for_zip.append((str(p), mt))

        zip_bytes = _build_zip_cached(tuple(files_for_zip))
        zip_name = _safe_filename(f"{Path(out_dir).name}.zip")
        if not zip_name.lower().endswith(".zip"):
            zip_name = zip_name + ".zip"
        st.download_button(
            label="Скачать всё (ZIP)",
            data=zip_bytes,
            file_name=zip_name,
            mime="application/zip",
            key=f"{state_prefix}_dl_all_zip_{out_dir}",
            use_container_width=True,
        )
    except Exception as e:
        st.caption(f"ZIP build error: {e}")

    # Render in a grid; keep UI stable: максимум 4 картинки в одном ряду
    grid_cols = 4
    cols = st.columns(grid_cols)
    for i, (pfx, sw, p) in enumerate(items):
        with cols[i % grid_cols]:
            extra = f" | suffix={sw}" if sw else ""
            st.image(p, caption=f"{pfx}{extra} | {os.path.basename(p)}", use_container_width=True)

            # Deletion is intentionally available only for Tab3 batch results.
            # Its target is constrained to the currently selected result folder,
            # so a stale/session-provided path cannot remove an unrelated file.
            download_area = None
            delete_area = None
            if state_prefix == "csvbatch":
                download_area, delete_area = st.columns(2)

            # Download button: save to browser Downloads with a friendly name based on user input
            try:
                base = download_base_name or os.path.splitext(os.path.basename(p))[0]
                base = _safe_filename(base)
                if not base:
                    base = "image"
                _, ext = os.path.splitext(p)
                ext = (ext or "").lstrip(".").lower()

                try:
                    mt = float(os.path.getmtime(p))
                except Exception:
                    mt = 0.0

                # Use the original saved filename (Tab2-like behavior). If we convert to PNG,
                # keep the same stem but force .png extension.
                if ext == "png":
                    fname = os.path.basename(p)
                else:
                    stem = os.path.splitext(os.path.basename(p))[0]
                    fname = _safe_filename(f"{stem}.png")

                data = _read_as_png_bytes_cached(str(p), mt)

                if download_area is not None:
                    with download_area:
                        st.download_button(
                            label="Скачать",
                            data=data,
                            file_name=fname,
                            mime="image/png",
                            key=f"{state_prefix}_dl_{i}_{os.path.basename(p)}",
                            use_container_width=True,
                        )
                else:
                    st.download_button(
                        label="Скачать",
                        data=data,
                        file_name=fname,
                        mime="image/png",
                        key=f"{state_prefix}_dl_{i}_{os.path.basename(p)}",
                        use_container_width=True,
                    )
            except Exception as e:
                st.caption(f"Download error: {e}")

            if delete_area is not None:
                with delete_area:
                    if st.button(
                        "🗑️ Удалить",
                        key=f"{state_prefix}_delete_{i}_{os.path.basename(p)}",
                        use_container_width=True,
                        help="Удаляет этот файл из текущей папки Batch results.",
                    ):
                        try:
                            result_root = Path(str(out_dir)).expanduser().resolve()
                            target = Path(str(p)).expanduser().resolve()
                            # Protect against accidental deletion outside the
                            # currently selected run directory.
                            target.relative_to(result_root)
                            if not target.is_file():
                                raise FileNotFoundError(str(target))
                            target.unlink()

                            target_key = os.path.normcase(str(target))
                            remaining_items = []
                            for item in items:
                                try:
                                    item_key = os.path.normcase(str(Path(str(item[2])).expanduser().resolve()))
                                except Exception:
                                    item_key = str(item[2])
                                if item_key != target_key:
                                    remaining_items.append(item)
                            st.session_state[f"{state_prefix}_last_items"] = remaining_items
                            st.session_state[f"{state_prefix}_force_rescan"] = True
                            st.success(f"Удалено: {target.name}")
                            _ui_rerun()
                        except ValueError:
                            st.error("Удаление отменено: файл находится вне текущей папки результатов.")
                        except Exception as e:
                            st.error(f"Не удалось удалить файл: {e}")


# ---------------- UI ----------------

# ------------------------ Entrypoint ------------------------
def main() -> None:
    st.set_page_config(page_title="Parallel Gemini (Overlay + Prompt)", layout="wide")
    st.title("Parallel Gemini: overlays + prompt-based pins")

    with st.sidebar:
        st.subheader("Settings")
        url = st.selectbox("URL интерфейса", DEFAULT_URLS, index=0)
        model_choice = st.selectbox("Модель", ["Быстрая", "Думающая"], index=0)
        headless = st.checkbox("Headless режим", value=False)

        num_windows = st.number_input(
            "Параллельных окон",
            min_value=1,
            max_value=24,
            value=4,
            step=1,
            help="Сколько параллельных Chrome-окон/профилей запускать (например 6).",
        )
        st.session_state["ui_num_windows"] = int(num_windows)
        user_data_dir = st.text_input(
            "Путь к базовому профилю (user-data-dir)",
            value=os.path.abspath(".chrome_automation_profile"),
            help=(
                "База для профилей. Если указаны номера профилей ниже, будут использованы папки вида "
                "<base>_10, <base>_11 ...\n"
                "Пример: .chrome_automation_profile (тогда профили: .chrome_automation_profile_10 и т.д.)"
            ),
        )

        profile_numbers_str = st.text_input(
            "Номера профилей (через запятую)",
            value="10,11,12,13",
            help=(
                "Например: 10,11,12,13 откроет <base>_10 .. <base>_13. "
                "Если указано меньше номеров, чем окон — недостающие слоты будут клонированы. "
                "Оставьте пустым для старого режима _1..N (по числу окон)."
            ),
        )
        profile_numbers = _parse_profile_numbers(profile_numbers_str)
        executable_path = st.text_input(
            "Путь к chrome.exe",
            value=r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        )
        st.caption(f"Префиксы: {', '.join(PREFIXES)}")
        st.caption(f"Рандомные суффиксы: {len(RANDOM_WORDS)} шт.")
        launch_stagger_s = st.number_input(
            "Задержка между стартом окон (сек)",
            min_value=0,
            max_value=60,
            value=5,
            step=1,
            help="Помогает, если при одновременном запуске часть окон не открывается из-за конфликтов/таймингов.",
        )


    tab1, tab2, tab3 = st.tabs(
        [
            "1) Overlay existing image (parallel)",
            "2) Generate pin from prompt (10×16)",
            "3) Batch from CSV (context) + manual overlays",
        ]
    )


    # ---------------- Tab 1: existing flow (upload base image, apply overlay) ----------------
    with tab1:
        st.subheader("Text overlay for an uploaded image")

        theme = st.text_input("Тема (пример: 8 Retro Kitchen Accessories)", value="", key="ov_theme")
        uploaded = st.file_uploader(
            "Загрузите базовую картинку",
            type=["png", "jpg", "jpeg", "webp"],
            key="ov_uploaded",
        )

        run_btn = st.button("Generate (parallel)", type="primary", key="ov_run")

        # Init state holders
        if "overlay_last_base_dir" not in st.session_state:
            st.session_state["overlay_last_base_dir"] = None
        if "overlay_last_items" not in st.session_state:
            st.session_state["overlay_last_items"] = []
        if "overlay_last_errors" not in st.session_state:
            st.session_state["overlay_last_errors"] = []

        if run_btn:
            theme_clean = (theme or "").strip()
            if not theme_clean:
                st.error("Введите тему")
                st.stop()
            if uploaded is None:
                st.error("Загрузите картинку")
                st.stop()

            base_image_path = _save_uploaded_file(uploaded)
            if not base_image_path or not os.path.exists(base_image_path):
                st.error("Не удалось сохранить загруженную картинку")
                st.stop()

            base_dir = _get_run_base_dir("overlay_parallel")
            st.session_state["overlay_last_base_dir"] = base_dir
            st.session_state["overlay_download_base_name"] = theme_clean
            st.info(f"Выходная папка: {base_dir}")

            base_prompt = _build_overlay_prompt(theme_clean)

            try:
                items, errors = _run_parallel_gemini_ui(
                    url=url,
                    model_choice=model_choice,
                    headless=headless,
                    user_data_dir=user_data_dir,
                    profile_numbers=profile_numbers,
                    executable_path=executable_path,
                    launch_stagger_s=int(launch_stagger_s),
                    base_image_path=base_image_path,
                    base_dir=base_dir,
                    slug_base=theme_clean,
                    base_prompt=base_prompt,
                    num_windows=int(num_windows),
                )
            except Exception as e:
                st.session_state["overlay_last_items"] = []
                st.session_state["overlay_last_errors"] = [str(e)]
                st.error(str(e))
            else:
                st.session_state["overlay_last_items"] = items
                st.session_state["overlay_last_errors"] = errors
                st.success("Готово")
                st.caption(
                    "Подсказка: для максимальной скорости заранее создайте профили .chrome_automation_profile_1..N "
                    "(где N = число окон) и войдите в Google в каждом. "
                    "Тогда параллельные окна будут запускаться без копирования профиля."
                )

        _render_results_block(state_prefix="overlay")


    # ---------------- Tab 2: prompt-driven 10:16 generation ----------------
    with tab2:
        st.subheader("Generate a Pinterest pin from prompt (no upload)")
        st.caption("10×16 задаётся в тексте промпта; файл 10x16.jpg больше не прикрепляется.")

        site_name = st.text_input(
            "Site name (для фразы 'I have a ... website called ...')",
            value=str(st.session_state.get("gp_site_name") or DEFAULT_SITE_NAME),
            key="gp_site_name",
        )

        text_overlay = st.text_input(
            "Текст оверлея на картинке (например: French Country Aesthetic Ideas)",
            value="",
            key="gp_text_overlay",
        )
        blog_title = st.text_input(
            "Заголовок и часть статьи блога (для контекста)",
            value="",
            key="gp_blog_title",
        )

        base_image_path = ""

        prompt_preview = _build_pin_prompt(text_overlay or "[TEXT OVERLAY]", blog_title or "[BLOG TITLE]", site_name=st.session_state.get("gp_site_name"))
        with st.expander("Prompt preview"):
            st.code(prompt_preview)

        col_gp_btn, col_gp_copy = st.columns([1, 1])
        with col_gp_btn:
            run_btn2 = st.button("Generate pin (parallel)", type="primary", key="gp_run")
        with col_gp_copy:
            ideogram_prompt = _build_ideogram_prompt(
                text_overlay or "[TEXT OVERLAY]",
                blog_title or "[BLOG TITLE]",
                max_chars=1500,
                site_name=st.session_state.get("gp_site_name"),
            )
            _copy_to_clipboard_html(
                text=ideogram_prompt,
                label="Скопировать промпт для ideogram",
                key="gp_copy_ideogram",
            )

        if "prompt_last_base_dir" not in st.session_state:
            st.session_state["prompt_last_base_dir"] = None
        if "prompt_last_items" not in st.session_state:
            st.session_state["prompt_last_items"] = []
        if "prompt_last_errors" not in st.session_state:
            st.session_state["prompt_last_errors"] = []

        if run_btn2:
            t = (text_overlay or "").strip()
            bt = (blog_title or "").strip()
            if not t:
                st.error("Введите текст оверлея")
                st.stop()
            if not bt:
                st.error("Введите заголовок статьи")
                st.stop()
            base_dir2 = _get_run_base_dir("prompt_pin_parallel")
            st.session_state["prompt_last_base_dir"] = base_dir2
            st.session_state["prompt_download_base_name"] = t
            st.info(f"Выходная папка: {base_dir2}")

            base_prompt2 = _build_pin_prompt(t, bt, site_name=st.session_state.get("gp_site_name"))

            try:
                items, errors = _run_parallel_gemini_ui(
                    url=url,
                    model_choice=model_choice,
                    headless=headless,
                    user_data_dir=user_data_dir,
                    profile_numbers=profile_numbers,
                    executable_path=executable_path,
                    launch_stagger_s=int(launch_stagger_s),
                    base_image_path=base_image_path,
                    base_dir=base_dir2,
                    slug_base=t,
                    base_prompt=base_prompt2,
                    num_windows=int(num_windows),
                )
            except Exception as e:
                st.session_state["prompt_last_items"] = []
                st.session_state["prompt_last_errors"] = [str(e)]
                st.error(str(e))
            else:
                st.session_state["prompt_last_items"] = items
                st.session_state["prompt_last_errors"] = errors
                st.success("Готово")

        _render_results_block(state_prefix="prompt")


    # ---------------- Tab 3: batch flow (context from Autosave JSON; overlay text entered in UI) ----------------
    with tab3:
        st.subheader("Batch: take context from Autosave JSON (Tab0), enter overlay texts here")
        st.caption(
            "Источник — autosave JSON файлы из app_unified_streamlit (Tab0: Article Texts). "
            "Для каждого поста вы задаёте 'текст оверлея' и число порций. "
            "Порция = один запуск параллельной генерации (по числу окон в сайдбаре)."
        )

        base_image_path = ""

        st.markdown("#### Источник: Autosave JSON (Tab0 из app_unified_streamlit)")

        autosave_files = _list_unif_text_autosaves()
        last_ptr = Path("autosaves") / "app_unified_streamlit" / "_last_tab0_article_texts_autosave.json"
        last_path = None
        try:
            if last_ptr.exists():
                last_obj = json.loads(last_ptr.read_text(encoding="utf-8"))
                last_path = (last_obj or {}).get("path") or (last_obj or {}).get("last_autosave")
        except Exception:
            last_path = None

        cols_src = st.columns([1, 3, 1])
        with cols_src[0]:
            if st.button("Load LAST", key="csvbatch_load_last_autosave"):
                if last_path and Path(last_path).exists():
                    st.session_state["csvbatch_autosave_main"] = str(last_path)
                    st.session_state["csvbatch_autosave_multi"] = [str(last_path)]
                    _ui_rerun()
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
                _ui_rerun()

        picked_multi = st.multiselect(
            "Выберите autosave JSON файлы (можно несколько — посты будут объединены)",
            options=autosave_files,
            default=([st.session_state.get("csvbatch_autosave_main")] if st.session_state.get("csvbatch_autosave_main") else []),
            key="csvbatch_autosave_multi",
        )

        col_a, col_b = st.columns([2, 1])
        with col_a:
            limit_posts = st.number_input(
                "Сколько постов брать (0 = все)",
                min_value=0,
                value=0,
                step=1,
                key="csvbatch_limit_posts",
            )
        with col_b:
            ctx_chars = st.number_input(
                "Символов контекста (пример: 1200)",
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
            value=1,
            step=1,
            key="csvbatch_default_portions",
        )

        posts: list[dict] = []
        posts_err: str | None = None

        # If user didn't pick anything, fall back to main selection (if any)
        paths = list(picked_multi or [])
        if not paths:
            main = st.session_state.get("csvbatch_autosave_main")
            if main:
                paths = [str(main)]

        merged_results: list[dict] = []
        try:
            for pth in paths:
                res_list, payload = _load_unif_text_results(pth)
                for rr in res_list:
                    if not isinstance(rr, dict):
                        continue
                    # skip errored items
                    if rr.get("error"):
                        continue
                    merged_results.append(rr)
        except Exception as e:
            posts_err = str(e)

        if posts_err:
            st.error(f"Autosave read error: {posts_err}")
            merged_results = []

        # Apply limit_posts
        if int(limit_posts) > 0:
            merged_results = merged_results[: int(limit_posts)]

        # Build posts list compatible with the rest of Tab3
        for rr in merged_results:
            title = (rr.get("title") or "").strip()
            posts.append(
                {
                    # Keep raw text for later extraction
                    "post_text": rr.get("text") or "",
                    "title": title,
                    "post_link": "",
                    "_rr": rr,
                }
            )

        # Build editor table
        editor_rows: list[dict] = []
        for idx, r in enumerate(posts):
            title = (r.get("title") or "").strip()
            if not title:
                title = _guess_post_title(str(r.get("post_text") or ""))
            editor_rows.append(
                {
                    "idx": idx,
                    "title": title,
                    "overlay_text": "",
                    "portions": int(default_portions),
                    "post_link": r.get("post_link") or "",
                }
            )

        st.markdown("#### Настройки для каждого поста")
        if not editor_rows:
            st.info("Пока нет постов (проверьте путь к CSV).")
            edited = []
        else:
            edited = st.data_editor(
                editor_rows,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "idx": st.column_config.NumberColumn("#", disabled=True),
                    "title": st.column_config.TextColumn("Пост (превью)", disabled=True),
                    "overlay_text": st.column_config.TextColumn(
                        "Текст оверлея",
                        help="То, что будет написано на пине (как во 2-й вкладке).",
                    ),
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

        # --- Batch run controls/state ---
        if "csvbatch_run_id" not in st.session_state:
            st.session_state["csvbatch_run_id"] = None
        if "csvbatch_last_run_id" not in st.session_state:
            st.session_state["csvbatch_last_run_id"] = None

        st.markdown("#### Надёжность: таймауты/повторы")
        col_t0, col_t1, col_t2, col_t3, col_t4 = st.columns([1, 1, 1, 1, 1])
        with col_t0:
            page_default_timeout_ms = st.number_input(
                "Default timeout (ms)",
                min_value=5000,
                max_value=300000,
                value=30000,
                step=5000,
                help="Базовый Playwright timeout для ожиданий на странице (page.set_default_timeout).",
                key="csvbatch_page_default_timeout_ms",
            )
        with col_t1:
            input_ready_timeout_ms = st.number_input(
                "Input ready timeout (ms)",
                min_value=5000,
                max_value=300000,
                value=60000,
                step=5000,
                help="Сколько ждать пока поле ввода станет доступным. Если Gemini завис/не прогрузился — будет считаться ошибкой и слот перезапустится.",
                key="csvbatch_input_ready_timeout_ms",
            )
        with col_t2:
            attach_timeout_ms = st.number_input(
                "Legacy attach timeout (ms)",
                min_value=2000,
                max_value=120000,
                value=5000,
                step=1000,
                help="Используется только для режимов, где реально передана картинка.",
                key="csvbatch_attach_timeout_ms",
            )
        with col_t3:
            gen_timeout_s = st.number_input(
                "Generate timeout (s)",
                min_value=10,
                max_value=1200,
                value=180,
                step=10,
                help="Сколько ждать генерацию картинок (основной таймаут).",
                key="csvbatch_gen_timeout_s",
            )
        with col_t4:
            max_session_attempts = st.number_input(
                "Session restarts",
                min_value=1,
                max_value=10,
                value=2,
                step=1,
                help="Сколько раз перезапускать Playwright/Chrome-сессию для одного окна при ошибках/таймаутах.",
                key="csvbatch_max_session_attempts",
            )

        gen_retry_timeout_s = st.number_input(
            "Generate retry timeout (s)",
            min_value=0,
            max_value=600,
            value=45,
            step=5,
            help="Дополнительная короткая попытка скачать картинки, если Gemini их показал, но байты ещё не доступны.",
            key="csvbatch_gen_retry_timeout_s",
        )

        offline_wait_timeout_s = st.number_input(
            "Offline wait (s)",
            min_value=0,
            max_value=3600,
            value=int(st.session_state.get("csvbatch_offline_wait_timeout_s", 90) or 90),
            step=10,
            help="Если интернет пропал в момент открытия окна/навигации к Gemini — не закрывать окно сразу, а подождать пока интернет появится.",
            key="csvbatch_offline_wait_timeout_s",
        )

        # Handle STOP
        if stop_btn3:
            rid = st.session_state.get("csvbatch_run_id")
            out_dir_hint = st.session_state.get("csvbatch_last_base_dir")

            # Create stop-file from UI side (most reliable, independent from in-memory shared state)
            try:
                if rid:
                    sp = Path(f"tmp_rovodev_csvbatch_stop_{rid}").resolve()
                    sp.write_text("stop requested (ui)\n", encoding="utf-8")
                    st.caption(f"STOP-file создан: {sp} (exists={sp.exists()})")
            except Exception as e:
                st.error(f"Не удалось создать STOP-file: {e}")

            if rid:
                _csvbatch_request_stop(str(rid))
                # Immediate UI stop: don't wait for the current portion to finish.
                st.session_state["csvbatch_running"] = False
                st.warning("STOP запрошен. Запуск будет остановлен максимально быстро (текущая порция может оборваться).")
                # Force refresh + rescan to update results block without enabling periodic reruns
                try:
                    st.session_state["csvbatch_force_rescan"] = True
                except Exception:
                    pass
                _ui_rerun()
                try:
                    # Best-effort: force UI to keep showing whatever is already saved on disk
                    if out_dir_hint:
                        st.session_state["csvbatch_last_base_dir"] = out_dir_hint
                except Exception:
                    pass
                _ui_rerun()
            else:
                st.info("Сейчас нет активного запуска")

        if run_btn3:
            if not edited:
                st.error("Нет данных для генерации (пустой список постов)")
                st.stop()

            # Validate rows
            jobs: list[dict] = []
            skipped_empty_overlay = 0
            for row in edited:
                i = int(row.get("idx", 0))
                overlay_text = (row.get("overlay_text") or "").strip()
                portions = int(row.get("portions") or 0)
                if portions <= 0:
                    portions = 1
                # If overlay text is empty, just skip this post and continue.
                if not overlay_text:
                    skipped_empty_overlay += 1
                    continue
                if i < 0 or i >= len(posts):
                    continue

                src = posts[i]
                rr = src.get("_rr") if isinstance(src, dict) else None
                if isinstance(rr, dict):
                    ctx = _unif_text_item_to_context(rr, target_chars=int(ctx_chars))
                else:
                    ctx = _extract_post_context(src.get("post_text") or "", target_chars=int(ctx_chars))
                jobs.append(
                    {
                        "post_idx": i,
                        "title": row.get("title") or f"post{i+1}",
                        "overlay_text": overlay_text,
                        "portions": max(1, portions),
                        "context": ctx,
                    }
                )

            if skipped_empty_overlay:
                st.info(f"Пропущено постов с пустым 'Текст оверлея': {skipped_empty_overlay}")

            if not jobs:
                st.error("Нечего генерировать: заполните 'Текст оверлея' хотя бы для одного поста")
                st.stop()

            total_runs = sum(int(j["portions"]) for j in jobs)
            base_dir3 = _get_run_base_dir("csv_batch_pin_parallel")

            # New run id and start worker
            # Use microseconds to avoid run_id collisions (can otherwise reuse stale STOP-file).
            run_id = datetime.now().strftime("csvbatch_%Y%m%d_%H%M%S_%f")
            st.session_state["csvbatch_run_id"] = run_id
            st.session_state["csvbatch_last_run_id"] = run_id
            st.session_state["csvbatch_last_base_dir"] = base_dir3
            st.session_state["csvbatch_download_base_name"] = "batch"
            st.session_state["csvbatch_last_items"] = []
            st.session_state["csvbatch_last_errors"] = []

            cfg = {
                "url": url,
                "model_choice": model_choice,
                "headless": headless,
                "user_data_dir": user_data_dir,
                "profile_numbers": profile_numbers,
                "executable_path": executable_path,
                "launch_stagger_s": int(launch_stagger_s),
                "base_image_path": base_image_path,
                "base_dir": base_dir3,
                "num_windows": int(num_windows),
                "total_runs": int(total_runs),
                "page_default_timeout_ms": int(page_default_timeout_ms),
                "input_ready_timeout_ms": int(input_ready_timeout_ms),
                "attach_timeout_ms": int(attach_timeout_ms),
                "offline_wait_timeout_s": int(offline_wait_timeout_s),
                "gen_timeout_s": int(gen_timeout_s),
                "gen_retry_timeout_s": int(gen_retry_timeout_s),
                "max_session_attempts": int(max_session_attempts),
            }

            _csvbatch_start(run_id=run_id, jobs=jobs, cfg=cfg)
            st.info(f"Стартовали batch в фоне. Run ID: {run_id}")
            st.info(f"Выходная папка: {base_dir3}")
            _ui_rerun()

        # --- Live progress from worker state ---
        rid = st.session_state.get("csvbatch_run_id") or st.session_state.get("csvbatch_last_run_id")

        # If still empty, try to auto-pick the latest run from in-memory shared state.
        if not rid:
            try:
                with _CSVBATCH_LOCK:
                    if _CSVBATCH_RUNS:
                        # pick run with max(started_at)
                        rid = max(
                            _CSVBATCH_RUNS.keys(),
                            key=lambda k: float((_CSVBATCH_RUNS.get(k) or {}).get("started_at") or 0.0),
                        )
                        st.session_state["csvbatch_last_run_id"] = rid
            except Exception:
                pass

        run_state = _csvbatch_get_state(str(rid)) if rid else {}

        if run_state:
            running = bool(run_state.get("running"))
            st.session_state["csvbatch_running"] = running

            # Always sync last-known results to session_state so they remain visible after stop/finish.
            try:
                if run_state.get("base_dir"):
                    st.session_state["csvbatch_last_base_dir"] = run_state.get("base_dir")
                st.session_state["csvbatch_last_items"] = list(run_state.get("items") or [])
                st.session_state["csvbatch_last_errors"] = list(run_state.get("errors") or [])
            except Exception:
                pass
            stopped = bool(run_state.get("stopped"))
            finished = bool(run_state.get("finished"))
            done_runs = int(run_state.get("done_runs") or 0)
            total_runs = int(run_state.get("total_runs") or 0)

            # Keep session_state results in sync so _render_results_block shows partials
            st.session_state["csvbatch_last_base_dir"] = run_state.get("base_dir") or st.session_state.get("csvbatch_last_base_dir")
            st.session_state["csvbatch_last_items"] = run_state.get("items") or []
            st.session_state["csvbatch_last_errors"] = run_state.get("errors") or []

            st.markdown("#### Статус запуска")
            st.progress(0.0 if total_runs <= 0 else min(1.0, done_runs / max(1, total_runs)))
            st.write(f"Прогресс: {done_runs}/{total_runs}")

            # Debug panel (helps verify STOP flag/file)
            with st.expander("Debug (batch state)", expanded=False):
                st.write({
                    "run_id": rid,
                    "running": run_state.get("running"),
                    "stop_requested": run_state.get("stop_requested"),
                    "stopped": run_state.get("stopped"),
                    "finished": run_state.get("finished"),
                    "base_dir": run_state.get("base_dir"),
                    "stop_file": run_state.get("stop_file"),
                })
                try:
                    if rid:
                        sp = Path(f"tmp_rovodev_csvbatch_stop_{rid}").resolve()
                        st.caption(f"STOP-file exists: {sp.exists()} ({sp})")
                except Exception as e:
                    st.caption(f"STOP-file check error: {e}")

            cur = run_state.get("current") or {}
            try:
                if cur:
                    st.caption(
                        f"Текущий шаг: post#{int(cur.get('post_idx', 0)) + 1} | порция {cur.get('portion')}/{cur.get('portions')} | overlay='{cur.get('overlay_text')}'"
                    )
            except Exception:
                pass

            if running:
                st.info("Запуск выполняется... (можно нажать STOP)")

                # Auto-refresh UI while running (best-effort)
                did_autorefresh = False
                try:
                    if hasattr(st, "autorefresh"):
                        st.autorefresh(interval=2000, key="csvbatch_autorefresh")
                        did_autorefresh = True
                except Exception:
                    did_autorefresh = False

                if not did_autorefresh:
                    if st.button("Обновить статус", key="csvbatch_manual_refresh"):
                        _ui_rerun()
            elif finished:
                st.success("Batch завершён")
            elif stopped:
                st.warning("Batch остановлен по STOP")
            else:
                st.info("Batch не запущен")

        _render_results_block(state_prefix="csvbatch", title="Batch results")


if __name__ == "__main__":
    main()
