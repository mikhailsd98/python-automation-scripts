# -*- coding: utf-8 -*-
"""Streamlit app: generate images via Gemini UI (Playwright) using uploaded images.

Flow (mirrors app_streamlit.py at high level):
1) Upload images
2) Call Gemini API (via generate_pinterest_texts.generate_keywords_and_image_prompt) to get keywords + a base prompt
3) Build a Gemini UI prompt tailored for attached 10x16 base image
4) Open Gemini UI in a browser (Playwright), attach 10x16 and send the prompt
5) Download generated images and save to a per-input folder

Run:
  pip install streamlit playwright
  playwright install chromium
  streamlit run app_streamlit_gemini.py
"""

import os
import sys
import time
import json
import tempfile
import traceback
import shutil
import base64
import csv
import io
import re
import queue
import concurrent.futures
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Any

import requests
import streamlit as st

# Windows: Playwright uses subprocess; Proactor loop is safer in Streamlit
import asyncio
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

import generate_pinterest_texts as gptx
import pinterest_csv_helpers as pch


# -------------------------
# Pinterest CSV helpers (ported from pinterest_post_texts_streamlit.py)
# -------------------------

def _load_board_names(path: str = "board_names.txt") -> list[str]:
    """Load Pinterest board names from a text file (one board per line)."""
    try:
        if not os.path.exists(path):
            return [""]

        raw_lines = open(path, "r", encoding="utf-8", errors="ignore").read().splitlines()

        def norm(s: str) -> str:
            s = (s or "").replace("\u00A0", " ")  # NBSP -> space
            s = " ".join(s.strip().split())  # collapse whitespace
            return s

        names = [norm(ln) for ln in raw_lines if norm(ln)]

        seen: set[str] = set()
        out: list[str] = [""]
        for n in names:
            if n not in seen:
                out.append(n)
                seen.add(n)
        return out
    except Exception:
        return [""]


def _imgbb_upload_bytes(image_bytes: bytes, api_key: str, filename: str | None = None, timeout_sec: int = 90) -> str:
    """Upload image bytes to imgbb and return hosted image URL."""
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("IMGBB API key is empty")

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data = {"key": api_key, "image": b64}

    if filename:
        base = os.path.splitext(os.path.basename(filename))[0]
        if base:
            data["name"] = base

    resp = requests.post("https://api.imgbb.com/1/upload", data=data, timeout=timeout_sec)
    resp.raise_for_status()
    j = resp.json() or {}
    if not isinstance(j, dict) or not j.get("success"):
        err = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else None
        raise RuntimeError(f"imgbb upload failed: {err or 'unknown error'}")

    d = j.get("data") or {}
    url = d.get("url") or d.get("display_url")
    if not url:
        raise RuntimeError("imgbb upload failed: response has no url")
    return str(url)


def _build_description(pin: dict[str, Any], include_hashtags: bool) -> str:
    desc = (pin.get("description") or "").strip()

    ht = pin.get("hashtags")
    hashtags_str = ""
    if isinstance(ht, str):
        hashtags_str = ht.strip()
    elif isinstance(ht, list):
        # Normalize list -> space separated string with leading '#'
        cleaned: list[str] = []
        for h in ht:
            s = str(h or "").strip()
            if not s:
                continue
            cleaned.append(s if s.startswith("#") else f"#{s.lstrip('#')}")
        hashtags_str = " ".join(cleaned).strip()

    if include_hashtags and hashtags_str:
        return (desc + "\n\n" + hashtags_str).strip()
    return desc


def _extract_first_title(pin: dict[str, Any]) -> str:
    # Primary expected format from generate_pinterest_assets(): title_options: list[str]
    titles = pin.get("title_options")
    if isinstance(titles, list) and titles:
        return str(titles[0] or "").strip()

    # Some older/alternate payloads might use a single title
    t = pin.get("title")
    if isinstance(t, str) and t.strip():
        return t.strip()

    # fallback: if only raw exists, keep it empty (user can fill in editor)
    return ""


def _parse_pinterest_raw_text(raw: str) -> dict[str, Any]:
    """Parse non-JSON Gemini Pinterest response into structured fields.

    Gemini sometimes ignores the "strict JSON" instruction and returns Markdown like:

    - "## Title Options:" then a list of keywords and/or real title variants
    - "**Description (approx. 600 characters):**" then a paragraph
    - "**Hashtags (approx. 200 characters):**" then hashtags

    This parser is deliberately tolerant:
    - understands Markdown headings (#/##/###), bold markers (**...**), bullets and numbered lists
    - extracts:
        - title_options: list[str] (prefers long variants like "Style | ...")
        - description: str (first meaningful paragraph)
        - hashtags: str (space-separated #tags)
    """

    txt = (raw or "").strip()
    if not txt:
        return {}

    # Normalize newlines
    t = txt.replace("\r\n", "\n")

    def _strip_markdown_emphasis(s: str) -> str:
        """Remove common Markdown emphasis markers while keeping the text.

        We only want plain text in UI/CSV, even if Gemini returns **bold** or *italic*.
        """
        s = (s or "")
        # remove bold/italic markers (best-effort, non-recursive)
        s = s.replace("**", "")
        # keep single '*' only when it acts as emphasis marker
        s = s.replace("*", "")
        return s

    def _clean_heading(s: str) -> str:
        s = (s or "").strip()
        s = re.sub(r"^#{1,6}\s*", "", s)  # remove markdown heading markers
        s = _strip_markdown_emphasis(s)
        s = s.strip()
        return s

    def _strip_list_prefix(s: str) -> str:
        s = (s or "").strip()
        s = _strip_markdown_emphasis(s)
        s = s.strip()
        # bullets: -, *, • (after emphasis stripping we still keep '-'/'•')
        s = re.sub(r"^[\-•\u2022]+\s+", "", s)
        # numbered: 1. / 1) / 1 - / 1:
        s = re.sub(r"^\d+\s*[\.\)\-:]\s+", "", s)
        return s.strip()

    # Split into rough sections by scanning headings line-by-line.
    lines = t.split("\n")
    cur: str | None = None  # 'title'|'desc'|'hashtags'
    buf: dict[str, list[str]] = {"title": [], "desc": [], "hashtags": []}

    for ln in lines:
        raw_ln = ln
        ln = (ln or "").rstrip()

        if not ln.strip():
            # keep paragraph breaks for description
            if cur == "desc":
                buf["desc"].append("")
            continue

        head = _clean_heading(ln)
        head_low = head.lower()

        # Headings / labels detection (tolerant)
        if re.match(r"^title(\s+options)?\b", head_low):
            cur = "title"
            continue
        if re.match(r"^description\b", head_low) or head_low.startswith("description and"):
            cur = "desc"
            continue
        if re.match(r"^hashtags\b", head_low):
            cur = "hashtags"
            continue

        # Also handle label lines with trailing ':' and additional text
        if "description" in head_low and head_low.endswith(":"):
            cur = "desc"
            continue
        if "hashtags" in head_low and head_low.endswith(":"):
            cur = "hashtags"
            continue
        if "title" in head_low and head_low.endswith(":"):
            cur = "title"
            continue

        if cur is None:
            # before any recognized section - ignore
            continue

        buf[cur].append(raw_ln)

    out: dict[str, Any] = {}

    # ---- Titles ----
    title_lines = [_strip_list_prefix(_clean_heading(x)) for x in buf["title"]]
    title_lines = [x for x in title_lines if x]

    # Filter out obvious non-title commentary lines
    title_lines = [x for x in title_lines if not x.lower().startswith(("here are", "explanation"))]

    # Prefer real title variants (usually longer and often contain '|')
    long_titles = [x for x in title_lines if ("|" in x) or (len(x.split()) >= 5)]
    titles = long_titles or title_lines

    # De-dup while preserving order
    seen_t: set[str] = set()
    dedup_titles: list[str] = []
    for x in titles:
        x = _strip_markdown_emphasis(x).strip()
        key = " ".join(x.split())
        if key and key not in seen_t:
            dedup_titles.append(x.strip())
            seen_t.add(key)

    if dedup_titles:
        out["title_options"] = dedup_titles

    # ---- Description ----
    desc_text = "\n".join(buf["desc"]).strip()
    if desc_text:
        # take the first meaningful paragraph
        paras = [p.strip() for p in re.split(r"\n\s*\n", desc_text) if p.strip()]
        if paras:
            chosen = ""
            for p in paras:
                p_clean = _clean_heading(p)
                if p_clean.lower().startswith(("hashtags", "title", "explanation")):
                    continue
                chosen = p_clean
                break
            if not chosen:
                chosen = _clean_heading(paras[0])
            if chosen:
                out["description"] = _strip_markdown_emphasis(chosen).strip()

    # ---- Hashtags ----
    ht_text = "\n".join(buf["hashtags"]).strip()
    if ht_text:
        # Extract #tags from any text
        tags = re.findall(r"#[_A-Za-z0-9]+", ht_text)
        if tags:
            out["hashtags"] = " ".join(tags)
        else:
            # Fallback: normalize space/comma separated tokens into #tags
            flat = ht_text.replace("\n", " ")
            parts = [p.strip() for p in re.split(r"[\s,]+", flat) if p.strip()]
            cleaned: list[str] = []
            for p in parts:
                if p.startswith("#"):
                    cleaned.append(p)
                else:
                    cleaned.append(f"#{p.lstrip('#')}")
            if cleaned:
                out["hashtags"] = " ".join(cleaned)

    return out


def _normalize_pinterest_pin(pin: Any) -> dict[str, Any]:
    """Best-effort normalize pin dict.

    Why this exists:
    - generate_pinterest_assets() returns {description/hashtags/title_options/...} but values can be None
      if the model used different key names (e.g. "Description" instead of "description").
    - It also stores the original model output in `_raw`.

    We recover/standardize fields from `_raw` when needed and do some key aliasing.
    """
    if not isinstance(pin, dict):
        return {}

    def _is_blank(v: Any) -> bool:
        if v is None:
            return True
        if isinstance(v, str) and not v.strip():
            return True
        if isinstance(v, list) and len(v) == 0:
            return True
        return False

    # Helper: merge aliases from a parsed dict into our canonical schema.
    def _merge_from(src: dict[str, Any], dst: dict[str, Any]) -> dict[str, Any]:
        # canonical -> aliases
        aliases: dict[str, list[str]] = {
            "description": ["description", "Description", "desc", "Desc"],
            "hashtags": ["hashtags", "Hashtags", "hashTags", "hash_tags", "tags", "Tags"],
            "three_word_keywords": [
                "three_word_keywords",
                "threeWordKeywords",
                "three_word_keys",
                "keywords",
                "Keywords",
            ],
            "title_options": [
                "title_options",
                "titleOptions",
                "titles",
                "Titles",
                "title_variants",
                "titleVariants",
            ],
            "title": ["title", "Title"],
        }

        for canon, keys in aliases.items():
            cur = dst.get(canon)
            if not _is_blank(cur):
                continue
            for k in keys:
                if k in src and not _is_blank(src.get(k)):
                    dst[canon] = src.get(k)
                    break

        return dst

    out = dict(pin)

    # If we already have non-empty structured fields -> ok.
    has_any = any(k in out for k in ("description", "hashtags", "title_options", "three_word_keywords", "title"))
    has_values = any(
        not _is_blank(out.get(k)) for k in ("description", "hashtags", "title_options", "three_word_keywords", "title")
    )

    raw = out.get("_raw")
    if isinstance(raw, str) and raw.strip() and (not has_any or not has_values):
        txt = raw.strip()

        # Strip ```json ... ``` fences if present
        if txt.startswith("```"):
            txt = re.sub(r"^```(?:json)?\s*", "", txt, flags=re.IGNORECASE)
            txt = re.sub(r"\s*```\s*$", "", txt)

        parsed: dict[str, Any] | None = None

        # Try direct JSON
        try:
            j = json.loads(txt)
            if isinstance(j, dict):
                parsed = j
        except Exception:
            parsed = None

        # Try first JSON object inside text
        if parsed is None:
            m = re.search(r"\{[\s\S]*\}", txt)
            if m:
                try:
                    j = json.loads(m.group(0))
                    if isinstance(j, dict):
                        parsed = j
                except Exception:
                    parsed = None

        if parsed is not None:
            out = _merge_from(parsed, out)
        else:
            # Non-JSON response: try to parse common "Title Options / Description / Hashtags" format
            out = _merge_from(_parse_pinterest_raw_text(raw), out)

    # Also handle the case where generate_pinterest_assets returned parsed JSON but used non-canonical keys
    # (e.g. TitleOptions). This merges from itself.
    out = _merge_from(out, out)

    # If still blank but has _raw, attempt non-JSON parsing as a final fallback.
    raw2 = out.get("_raw")
    if isinstance(raw2, str) and raw2.strip():
        if _is_blank(out.get("description")) and _is_blank(out.get("title_options")):
            out = _merge_from(_parse_pinterest_raw_text(raw2), out)

    return out

# Reuse stable Gemini UI automation helpers
import gemini_pw_helpers as gph
from gemini_playwright_streamlit import (
    DEFAULT_URLS,
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
)

from playwright.sync_api import sync_playwright


def _ensure_dir(p: str) -> str:
    Path(p).mkdir(parents=True, exist_ok=True)
    return str(Path(p).resolve())


def _create_generation_root(parent: str = "generated_images_gemini") -> str:
    """Create date-based output folder like app_streamlit.py."""
    date_str = time.strftime("%Y-%m-%d")
    base = Path(parent).resolve()
    root = base / date_str
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        return str(root)
    i = 2
    while True:
        cand = base / f"{date_str}_{i}"
        if not cand.exists():
            cand.mkdir(parents=True, exist_ok=True)
            return str(cand)
        i += 1


def _slug(s: str) -> str:
    import re

    s = (s or "").strip()
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "image"


def _parse_profile_numbers(raw: str) -> list[int]:
    """Parse comma-separated profile numbers like '10,11,12'."""
    raw = (raw or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    nums: list[int] = []
    for p in parts:
        if not p.isdigit():
            raise ValueError("profile numbers must be comma-separated integers")
        nums.append(int(p))
    # Keep order, remove duplicates
    out: list[int] = []
    seen: set[int] = set()
    for n in nums:
        if n not in seen:
            out.append(n)
            seen.add(n)
    return out


def _resolve_profile_pool(base_profile_dir: str, numbers_csv: str) -> list[str]:
    """Resolve Chrome profile directories.

    Why:
    - In Stage3 we only use *real* existing profile directories (or clones) to avoid flaky behavior.
    - Creating an empty `<base>_N` directory often leads to a fresh logged-out Chrome profile,
      which then fails in Gemini UI and can look like "hangs"/"crashes".

    Behavior:
    - If `numbers_csv` is empty -> use the base dir (create it if missing).
    - If numbers are provided -> search for existing numbered profile dirs near the base path.
      Supports common naming variants:
        - `<base>_11` and `<base>11`
        - both with and without a leading dot in the last path component

      If none found -> raise a clear error.
    """

    base = os.path.abspath(os.path.expanduser((base_profile_dir or "").strip().strip('"').strip("'")))
    nums = _parse_profile_numbers(numbers_csv)

    def _candidate_profile_paths(base_dir: str, num: int) -> list[str]:
        base_path = Path(base_dir)
        last = base_path.name
        alt_last = last[1:] if last.startswith(".") else ("." + last)
        parent = base_path.parent if str(base_path.parent) != str(base_path) else Path(".")
        return [
            str((parent / f"{last}_{num}").resolve()),
            str((parent / f"{alt_last}_{num}").resolve()),
            str((parent / f"{last}{num}").resolve()),
            str((parent / f"{alt_last}{num}").resolve()),
        ]

    if not nums:
        # Base dir is allowed to be created (it can still be a valid persistent profile).
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            pass
        return [base]

    out: list[str] = []
    seen: set[str] = set()
    for n in nums:
        picked: str | None = None
        for cand in _candidate_profile_paths(base, n):
            try:
                if os.path.isdir(cand):
                    picked = cand
                    break
            except Exception:
                continue
        if picked and picked not in seen:
            out.append(picked)
            seen.add(picked)

    if not out:
        raise RuntimeError(
            "No usable numbered Chrome profiles found. "
            "Create existing folders like '<user-data-dir>_10', '<user-data-dir>_11' (or without dot), "
            "or clear the profile numbers field."
        )

    return out


def _build_gemini_ui_prompt(*, keywords_prompt: str) -> str:
    """Convert the base image_prompt (keywords-based) into a Gemini UI instruction.

    In Gemini UI we also attach a white 10x16 image (BASE_IMAGE_PATH).
    So the prompt must explicitly instruct to modify the attached white 10x16 image.
    """

    core = (keywords_prompt or "").strip()
    # If upstream already provides a very long instruction, keep it.
    # Otherwise wrap it in the same pattern used by other apps in this repo.
    pre = "Change the white 10:16 ratio image using this Prompt: "
    post = " DO NOT LEAVE BLANK WHITE SPACE, THIS IS IMPORTANT"

    # Avoid double-wrapping
    low = core.lower()
    if "change the white" in low and "10" in low and "ratio" in low:
        return core

    return f"{pre}{core}{post}".strip()


def _launch_persistent_ctx_with_retries(
    pw,
    *,
    user_data_dir: str | None,
    headless: bool,
    executable_path: str | None,
    downloads_path: str | None = None,
    attempts: int = 5,
    allow_create_profile_dir: bool = True,
):
    """Small, robust wrapper around launch_persistent_context.

    Copied in spirit from app_parallel_overlay_streamlit.py: clears stale lock files and retries.
    """

    # Normalize user-data-dir (and fix a common Windows copy/paste typo)
    def _normalize_user_data_dir_local(p: str | None) -> str | None:
        if not p:
            return None
        s = str(p).strip().strip('"').strip("'")
        if not s:
            return None
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

    norm = _normalize_user_data_dir_local(user_data_dir)
    norm_downloads = os.path.abspath(downloads_path) if downloads_path else None

    if norm and bool(allow_create_profile_dir):
        os.makedirs(norm, exist_ok=True)
    if norm_downloads:
        os.makedirs(norm_downloads, exist_ok=True)

    def _cleanup_profile_locks(path: str | None) -> None:
        if not path:
            return
        p = Path(path)
        if not p.exists() or not p.is_dir():
            return
        for name in ["SingletonLock", "SingletonCookie", "SingletonSocket", "LOCK", "lockfile", "DevToolsActivePort"]:
            fp = p / name
            try:
                if fp.exists():
                    fp.unlink()
            except Exception:
                pass

    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _cleanup_profile_locks(norm)
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=norm,
                headless=headless,
                # Keep download objects alive until the helper confirms the
                # actual completed image file, rather than a UI preview.
                accept_downloads=True,
                # This is a private directory for one session. Gemini can
                # still bypass it, which is handled by the profile/CDP setup
                # and native file watcher below.
                downloads_path=norm_downloads,
                channel="chrome",
                executable_path=executable_path or None,
                # Align with Stage3: reduce automation fingerprints (can affect AI Studio/Gemini stability)
                ignore_default_args=["--enable-automation"],
                args=[
                    "--lang=ru-RU",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                    "--disable-gpu-compositing",
                    "--disable-blink-features=AutomationControlled",
                ],
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
            last_err = RuntimeError(f"launch_persistent_context failed (attempt {attempt}/{attempts}): {e}")
            time.sleep(0.7 + 0.6 * attempt)

    raise last_err or RuntimeError("Failed to launch persistent context")


def _set_profile_download_directory_temporarily(
    profile_dir: str | None, download_dir: str
) -> tuple[list[tuple[Path, bytes]], list[str]]:
    """Temporarily point Chrome's persistent profile at one private folder.

    Gemini's Download action occasionally bypasses Playwright's
    ``downloads_path`` and follows the profile preference instead.  The raw
    Preferences bytes are restored after the session, so this change never
    becomes a permanent user setting.
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

            tmp_path = pref_path.with_name(f"{pref_path.name}.gemui-download-tmp")
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
    """Restore the profile Preferences bytes saved before download pinning."""

    notes: list[str] = []
    for pref_path, original in backups or []:
        try:
            tmp_path = pref_path.with_name(f"{pref_path.name}.gemui-restore-tmp")
            tmp_path.write_bytes(original)
            os.replace(str(tmp_path), str(pref_path))
            notes.append(f"profile preference restored: {pref_path}")
        except Exception as e:
            notes.append(f"profile preference restore failed: {pref_path}: {e}")
    return notes


def _native_download_watch_dirs(profile_dir: str | None) -> list[str]:
    """Return Chrome's configured destination plus the Windows Downloads fallback."""

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
                    pass
                break
    except Exception:
        pass

    try:
        candidates.append(str(Path.home() / "Downloads"))
    except Exception:
        pass

    out: list[str] = []
    for candidate in candidates:
        try:
            path = str(Path(candidate).resolve())
            if Path(path).is_dir() and path not in out:
                out.append(path)
        except Exception:
            continue
    return out


def _pin_chrome_download_directory(page, download_dir: str) -> str:
    """Set Chrome's live native download path through CDP, if supported."""

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
                    {"behavior": "allow", "downloadPath": target, "eventsEnabled": True},
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


def _run_gemini_ui_generation(
    *,
    url: str,
    model_choice: str,
    headless: bool,
    user_data_dir: str,
    executable_path: str | None,
    base_image_path: str,
    prompt: str,
    timeout_s: int,
    max_images: int,
    gen_retry_timeout_s: int = 20,
    max_session_attempts: int = 2,
    debug_dom: bool = False,
    download_debug_dir: str | None = None,
    download_debug_label: str | None = None,
) -> List[Tuple[str, bytes]]:
    """Open Gemini UI, attach base image, send prompt, download images.

    Changes vs older implementation:
    - Avoid unconditional `_debug_dom()` (can be very slow/hangy on some Gemini Pro variants).
    - Require that `_click_send()` actually starts generation; retry click once.
    - Retry the whole Playwright session on the SAME profile a couple of times (Tab1-like resilience).
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

    last_err: Exception | None = None

    for session_attempt in range(1, max(1, int(max_session_attempts)) + 1):
        profile_pref_backups: list[tuple[Path, bytes]] = []
        browser_download_dir: str | None = None
        trace_path: Path | None = None
        if download_debug_dir:
            try:
                trace_root = Path(download_debug_dir).resolve()
                trace_root.mkdir(parents=True, exist_ok=True)
                label = _slug(download_debug_label or "gemini")
                profile_label = _slug(Path(user_data_dir).name or "profile")
                trace_path = trace_root / f"{label}_{profile_label}_attempt{session_attempt}.log"
            except Exception:
                trace_path = None

        def _download_trace(message: str) -> None:
            if trace_path is None:
                return
            try:
                stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                with open(trace_path, "a", encoding="utf-8") as trace_file:
                    trace_file.write(f"{stamp} {message}\n")
            except Exception:
                pass

        try:
            # Use an OS-temp folder per session, never the user's Downloads.
            # It is removed after the confirmed image bytes have been returned.
            browser_download_dir = tempfile.mkdtemp(prefix="gemui_pw_download_")
            _download_trace(f"worker download directory: {browser_download_dir}")
            profile_pref_backups, pref_notes = _set_profile_download_directory_temporarily(
                user_data_dir,
                browser_download_dir,
            )
            for note in pref_notes:
                _download_trace(note)

            with sync_playwright() as p:
                # IMPORTANT: do not silently create empty profile dirs here.
                # If the profile dir does not exist, it's better to fail fast than to create
                # a fresh logged-out profile that then fails inside Gemini UI.
                ctx = _launch_persistent_ctx_with_retries(
                    p,
                    user_data_dir=user_data_dir,
                    headless=headless,
                    executable_path=executable_path,
                    downloads_path=browser_download_dir,
                    allow_create_profile_dir=False,
                    attempts=6,
                )
                try:
                    # IMPORTANT (align with Stage3): always open a fresh page.
                    # Reusing ctx.pages[0] can pick up an old tab with stale UI state, banners,
                    # or an interrupted previous generation, which increases UI errors.
                    page = ctx.new_page()
                    _download_trace(_pin_chrome_download_directory(page, browser_download_dir))
                    page.set_default_timeout(30000)

                    # Ensure Gemini page (do not always navigate if already there)
                    try:
                        cur_url = page.url or ""
                    except Exception:
                        cur_url = ""
                    if ("gemini.google.com" not in cur_url) and ("aistudio.google.com" not in cur_url):
                        try:
                            page.goto(url, wait_until="load")
                        except Exception:
                            try:
                                page.goto(url, wait_until="domcontentloaded")
                            except Exception:
                                pass

                    if bool(debug_dom):
                        _debug_dom(page)

                    _wait_input_ready(page, timeout_ms=60000)
                    _dismiss_overlays(page)

                    # -----------------
                    # Phase 1: prepare + send (retry only BEFORE successful send)
                    # -----------------
                    prompt_sent = False
                    last_send_err: Exception | None = None
                    for send_attempt in range(2):
                        try:
                            try:
                                _start_new_chat(page)
                            except Exception:
                                pass

                            # Attach base image (10x16)
                            ok = _attach_image(page, base_image_path)
                            attached = _wait_image_attached(page, timeout_ms=8000)
                            if not ok or not attached:
                                # Retry once
                                ok = _attach_image(page, base_image_path)
                                attached = _wait_image_attached(page, timeout_ms=8000)

                            _dismiss_overlays(page)

                            # Model mapping:
                            # - "Быстрая" -> fast mode
                            # - "Pro" -> pick the same model as in app_unified_streamlit.py (Tab 2 / Nano Banana Pro)
                            # Backward-compat: if older UI/state still passes "Думающая", keep it working.
                            mc = (model_choice or "").strip()
                            if mc.lower() in {"pro", "nano banana pro"}:
                                mc = "Nano Banana Pro"
                            elif mc.lower().startswith("дума"):
                                mc = "Думающая"

                            try:
                                if mc:
                                    gph._pick_model(page, mc)
                            except Exception:
                                pass

                            _type_prompt(page, prompt)

                            ok_send = False
                            try:
                                ok_send = bool(_click_send(page))
                            except Exception:
                                ok_send = False

                            if not ok_send:
                                time.sleep(0.4)
                                try:
                                    ok_send = bool(_click_send(page))
                                except Exception:
                                    ok_send = False

                            if not ok_send:
                                raise RuntimeError("Send/Run did not start generation")

                            prompt_sent = True
                            break
                        except Exception as e:
                            last_send_err = e
                            time.sleep(0.6 + 0.2 * send_attempt)
                            continue

                    if not prompt_sent:
                        raise RuntimeError(f"Failed to send prompt: {last_send_err}")

                    # -----------------
                    # Phase 2: wait + download
                    # -----------------
                    # Match the robust Tab1/Tab3 path: only a fully completed
                    # native browser download counts as success.  The helper
                    # watches the private folder and, as a guarded fallback,
                    # any profile/system destination Gemini may have ignored.
                    # A second click on Gemini's Download control can cancel
                    # the still-active first transfer.  Give that one click a
                    # long confirmation window instead of re-entering the
                    # downloader and clicking again.
                    confirmation_timeout_s = max(60.0, float(gen_retry_timeout_s or 0))
                    _download_trace(
                        f"[download] one-click confirmation window: {confirmation_timeout_s:.0f}s"
                    )
                    imgs = _wait_and_download_generated_images(
                        page,
                        ctx,
                        timeout_s=int(timeout_s),
                        max_images=int(max_images),
                        require_browser_download=True,
                        browser_download_dir=browser_download_dir,
                        native_download_dirs=_native_download_watch_dirs(user_data_dir),
                        serialize_native_download_click=True,
                        download_debug_hook=_download_trace,
                        native_download_confirmation_timeout_s=confirmation_timeout_s,
                    )
                    return imgs
                finally:
                    try:
                        ctx.close()
                    except Exception:
                        pass
        except Exception as e:
            last_err = e
            _download_trace(f"generation session failed: {type(e).__name__}: {e}")
            if session_attempt < int(max_session_attempts) and _is_retryable_error(str(e)):
                time.sleep(0.8 * session_attempt)
                continue
            raise
        finally:
            for note in _restore_profile_download_directory(profile_pref_backups):
                _download_trace(note)
            if browser_download_dir:
                try:
                    shutil.rmtree(browser_download_dir, ignore_errors=True)
                except Exception:
                    pass

    raise last_err or RuntimeError("Generation failed")


def _keywords_to_filename_stub(keywords_30: str | None, *, max_words: int = 5, max_len: int = 60) -> str:
    """Take keywords_30 string and return a SHORT safe stub for filenames.

    Windows can throw OSError 22 / path errors if the filename becomes too long.
    Keep this stub short and ASCII-safe.
    """
    if not keywords_30:
        return "keywords"
    # Try splitting by comma first, fallback to whitespace
    parts = [p.strip() for p in str(keywords_30).split(",") if p.strip()]
    if len(parts) < 2:
        parts = [p.strip() for p in str(keywords_30).split() if p.strip()]
    parts = parts[:max_words]
    stub = _slug("_".join(parts))
    if len(stub) > max_len:
        stub = stub[:max_len].rstrip("_")
    return stub or "keywords"


def _unique_path(out_dir: str, filename: str) -> str:
    """Avoid overwriting files by appending _2, _3..."""
    out = Path(out_dir)
    base = out / filename
    if not base.exists():
        return str(base)
    stem = base.stem
    suffix = base.suffix
    for k in range(2, 9999):
        cand = out / f"{stem}_{k}{suffix}"
        if not cand.exists():
            return str(cand)
    return str(out / f"{stem}_{int(time.time())}{suffix}")


def _save_images(blobs: List[Tuple[str, bytes]], out_dir: str, name_stub: str) -> List[str]:
    """Save images into a single folder.

    Filenames look like:
      1_<keywords>_<hash>.png
      2_<keywords>_<hash>.png

    If Windows rejects the path/filename (OSError 22), fallback to a very short name.
    """
    import hashlib

    saved: List[str] = []
    # Hash based on name_stub to keep stable, short uniqueness
    h = hashlib.sha1((name_stub or "").encode("utf-8", errors="ignore")).hexdigest()[:8]
    safe_stub = (name_stub or "keywords")

    for i, (mime, data) in enumerate(blobs, 1):
        ext = "png" if mime == "image/png" else ("jpg" if mime == "image/jpeg" else "bin")

        # Primary filename (short + hashed)
        fname = f"{i}_{safe_stub}_{h}.{ext}"
        path = _unique_path(out_dir, fname)

        try:
            with open(path, "wb") as f:
                f.write(data)
            saved.append(path)
            continue
        except OSError as e1:
            # Fallback: super short name (still unique via hash)
            short_fname = f"{i}_{h}.{ext}"
            short_path = _unique_path(out_dir, short_fname)
            try:
                with open(short_path, "wb") as f:
                    f.write(data)
                saved.append(short_path)
            except OSError as e2:
                # Bubble up a clearer error; this is not a "profile" problem
                raise OSError(f"Failed to save image to '{out_dir}'. Primary error: {e1}. Fallback error: {e2}")

    return saved


st.set_page_config(page_title="Gemini UI Image Generator", layout="wide")
st.title("Gemini UI Image Generator (from uploaded images)")
st.caption(
    "Загрузите картинки. Для каждой будет вызван Gemini API для ключевых, затем соберётся prompt и будет выполнена генерация через интерфейс Gemini (Playwright)."
)

# Pinterest CSV export settings
with st.expander("Pinterest CSV export settings", expanded=False):
    include_hashtags = st.checkbox("Добавлять hashtags в Description", value=True, key="gemui_include_hashtags")
    imgbb_default = os.getenv("IMGBB_API_KEY") or "ec09b2c0c2df893c2ba9650c4d577975"
    imgbb_api_key = st.text_input(
        "IMGBB API key (для загрузки картинок и заполнения Media URL)",
        value=imgbb_default,
        type="password",
        key="gemui_imgbb_api_key",
    )
    st.caption("Если не хотите грузить в imgbb, можете оставить ключ пустым и заполнить Media URL вручную в таблице.")

with st.sidebar:
    st.subheader("Gemini UI settings")
    url = st.selectbox("URL интерфейса", DEFAULT_URLS, index=0, key="gemui_url")
    # В unified (Nano Banana Pro) используется модель "Nano Banana Pro".
    # Здесь делаем то же самое: вместо "Думающая" даём выбор "Pro".
    model_choice = st.selectbox("Модель", ["Быстрая", "Pro"], index=0, key="gemui_model_choice")
    headless = st.checkbox("Headless режим", value=False, key="gemui_headless")
    user_data_dir = st.text_input(
        "Базовый профиль (user-data-dir)",
        value=os.path.abspath(".chrome_automation_profile"),
        help="Базовая папка профиля. Если укажете номера ниже, будут использованы <база>_N.",
        key="gemui_user_data_dir",
    )
    profile_numbers_csv = st.text_input(
        "Номера профилей (через запятую)",
        value="30,31,32,33,34,35,36,37,38",
        help="Напр.: 10,11,12,13. Для каждого нового prompt будет браться следующий профиль по кругу: <база>_10 → _11 → ... → снова _10.",
        key="gemui_profile_numbers",
    )
    chrome_exe = st.text_input(
        "Путь к chrome.exe (опционально)",
        value=r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" if sys.platform.startswith("win") else "",
        key="gemui_chrome_exe",
    )
    max_images = st.number_input(
        "Максимум изображений на prompt",
        min_value=1,
        max_value=6,
        value=int(st.session_state.get("gemui_max_images") or 1),
        step=1,
        key="gemui_max_images",
    )
    timeout_s = st.number_input(
        "Таймаут ожидания (сек)",
        min_value=30,
        max_value=600,
        value=int(st.session_state.get("gemui_timeout_s") or 110),
        step=10,
        key="gemui_timeout_s",
    )

    st.markdown("---")
    st.subheader("Parallel")
    enable_parallel = st.checkbox(
        "Включить параллельную генерацию",
        value=bool(st.session_state.get("gemui_enable_parallel", True)),
        key="gemui_enable_parallel",
    )
    parallelism = st.number_input(
        "Параллельно окон (как во вкладке 1)",
        min_value=1,
        max_value=12,
        value=int(st.session_state.get("gemui_parallelism") or 4),
        step=1,
        key="gemui_parallelism",
    )
    profile_attempts_parallel = st.number_input(
        "Сколько профилей пробовать на 1 картинку (parallel)",
        min_value=1,
        max_value=50,
        value=int(st.session_state.get("gemui_profile_attempts_parallel") or 2),
        step=1,
        key="gemui_profile_attempts_parallel",
        help="В sequential режиме перебираются все профили. В parallel лучше держать число небольшим.",
    )

    # Download robustness: the first native Download click must be allowed to
    # complete; clicking it again makes Gemini abort/restart the transfer.
    try:
        if int(st.session_state.get("gemui_gen_retry_timeout_s") or 0) < 60:
            st.session_state["gemui_gen_retry_timeout_s"] = 60
    except Exception:
        pass
    gen_retry_timeout_s = st.number_input(
        "Ожидание первой попытки Download (сек)",
        min_value=60,
        max_value=600,
        value=int(st.session_state.get("gemui_gen_retry_timeout_s") or 60),
        step=5,
        key="gemui_gen_retry_timeout_s",
        help="После клика Download скрипт ждёт завершения именно этой загрузки и не нажимает кнопку повторно.",
    )
    max_session_attempts = st.number_input(
        "Max session attempts (на один профиль)",
        min_value=1,
        max_value=5,
        value=int(st.session_state.get("gemui_max_session_attempts") or 2),
        step=1,
        key="gemui_max_session_attempts",
        help="Как в Tab1: если Playwright-сессия упала/закрылась, пробуем заново на том же профиле, прежде чем менять профиль.",
    )
    debug_dom = st.checkbox(
        "Debug DOM (очень медленно / может подвисать на Pro)",
        value=bool(st.session_state.get("gemui_debug_dom", False)),
        key="gemui_debug_dom",
    )

    st.markdown("---")
    st.subheader("Base image")
    base_image_path = st.text_input(
        "Путь к белой 10x16 картинке",
        value=BASE_IMAGE_PATH,
        help="По умолчанию: 10x16.jpg",
        key="gemui_base_image_path",
    )
    if os.path.exists(base_image_path):
        st.image(base_image_path, caption=os.path.basename(base_image_path), use_container_width=True)
    else:
        st.error(f"Не найден base image: {base_image_path}")

# Global scheduling setting (applies to Pinterest CSV schedule)
st.session_state.setdefault("pins_per_day", 10)

pins_per_day = st.number_input(
    "Максимум пинов в день (auto schedule)",
    min_value=1,
    max_value=100,
    value=int(st.session_state.get("pins_per_day") or 10),
    step=1,
    help="Сколько пинов максимум ставить на один день. Остальные переносятся на следующие дни.",
    key="gemui_pins_per_day",
)
st.session_state["pins_per_day"] = int(pins_per_day)

uploaded = st.file_uploader(
    "Перетащите сюда исходные изображения (jpg/png/webp) — можно несколько",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
    key="stage4_uploaded_files",
)

# NOTE: use namespaced key to avoid collisions when embedded in other Streamlit apps
if "gemui_results" not in st.session_state:
    st.session_state.gemui_results = []

# Unified pipeline uses this flag to disable its autorefresh while Stage4 runs.
# Safe in standalone mode too.
st.session_state.setdefault("stage4_running", False)

# Pinterest CSV export state
st.session_state.setdefault("pinterest_image_urls", {})  # {local_image_path -> hosted_url}
st.session_state.setdefault("pinterest_export_rows", [])
st.session_state.setdefault("pinterest_export_sig", None)
st.session_state.setdefault("pinterest_export_version", 0)

# Pinterest two-step flow state (like app_streamlit.py)
st.session_state.setdefault("pinterest_selection", {})  # {image_path -> bool}
st.session_state.setdefault("pinterest_results", {})    # {image_path -> normalized pin_dict}
st.session_state.setdefault("pinterest_results_raw", {})  # {image_path -> raw pin_dict from generate_pinterest_assets}

colA, colB = st.columns([1, 1])
# Unified pipeline can trigger this stage without clicking the button
_autorun = bool(st.session_state.get("stage4_autorun"))
if _autorun:
    # clear flag so it doesn't rerun forever
    st.session_state["stage4_autorun"] = False

with colA:
    run_btn = st.button("Сгенерировать", type="primary", disabled=not bool(uploaded), key="gemui_generate")

# If autorun was requested AND we have uploads, treat as clicked.
if _autorun and uploaded:
    run_btn = True
with colB:
    clear_btn = st.button("Очистить результаты", key="gemui_clear_results")
    if clear_btn:
        st.session_state.gemui_results = []
        st.rerun()

if run_btn:
    st.session_state["stage4_running"] = True

    # Acknowledge actual Stage4 start for the unified pipeline.
    # Unified app sets unified_current_stage3_run_id when Stage3 is finished.
    try:
        rid = str(st.session_state.get("unified_current_stage3_run_id") or "").strip()
        if rid:
            st.session_state["unified_stage4_started_for_stage3_run_id"] = rid
    except Exception:
        pass

    if not os.path.exists(base_image_path):
        st.session_state["stage4_running"] = False
        st.error("Base image 10x16 не найден. Исправьте путь в сайдбаре.")
        st.stop()

    out_root = _create_generation_root("generated_images_gemini")
    st.info(f"Папка вывода: {out_root}")

    results = []
    # Prepare round-robin profile pool
    try:
        profile_pool = _resolve_profile_pool(user_data_dir, profile_numbers_csv)
    except ValueError:
        st.session_state["stage4_running"] = False
        st.error("Ошибка: номера профилей должны быть числами через запятую (например: 10,11,12)")
        st.stop()
    except Exception as e:
        st.session_state["stage4_running"] = False
        st.error(f"Не удалось подготовить пул профилей: {type(e).__name__}: {e}")
        st.stop()

    st.caption(f"Профилей в пуле: {len(profile_pool)}")

    with st.spinner("Генерация..."):
        use_parallel = bool(enable_parallel) and int(parallelism) > 1

        if use_parallel:
            total_items = len(uploaded)

            # `parallelism` means how many browser windows we want at once,
            # but we still cannot exceed available profiles.
            max_workers_global = max(1, min(int(parallelism), len(profile_pool)))
            if max_workers_global < int(parallelism):
                st.warning(
                    f"Parallel ограничен: workers={max_workers_global} (профилей={len(profile_pool)}, parallelism={int(parallelism)})"
                )

            profile_q: queue.Queue[str] = queue.Queue()
            for pdir in profile_pool:
                profile_q.put(pdir)

            # How long to wait for a free profile before declaring a deadlock.
            profile_get_timeout_s = max(5.0, float(st.session_state.get("gemui_profile_get_timeout_s") or 90.0))

            overall = st.progress(0, text=f"Генерация (batch): 0/{total_items}")
            status = st.empty()
            done = 0

            def _build_prompt_item(idx: int, up) -> dict[str, Any]:
                suffix = Path(up.name).suffix or ".jpg"
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(up.getbuffer())
                    tmp_path = tmp.name

                try:
                    kw = gptx.generate_keywords_and_image_prompt(tmp_path)
                    prompt_base = (kw or {}).get("image_prompt") or ""
                    final_prompt = _build_gemini_ui_prompt(keywords_prompt=prompt_base)
                    name_stub = _keywords_to_filename_stub((kw or {}).get("keywords_30"), max_words=5, max_len=60)

                    return {
                        "idx": idx,
                        "input_name": up.name,
                        "kw": kw,
                        "prompt_base": prompt_base,
                        "final_prompt": final_prompt,
                        "name_stub": name_stub,
                    }
                finally:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

            def _worker(item: dict[str, Any]) -> dict[str, Any]:
                idx_local = int(item.get("idx") or 0)
                input_name = str(item.get("input_name") or "")
                kw = item.get("kw") or {}
                prompt_base = str(item.get("prompt_base") or "")
                final_prompt = str(item.get("final_prompt") or "")
                name_stub = str(item.get("name_stub") or "keywords")

                # Save into the common output folder (no per-item wrapper directory).
                # To avoid name collisions, prefix filenames via name_stub.
                item_out_dir = out_root

                saved: list[str] = []
                profile_used: str | None = None
                last_exc: Exception | None = None
                last_trace: str | None = None

                tries = max(1, min(int(profile_attempts_parallel), len(profile_pool)))
                for _ in range(tries):
                    prof_dir: str | None = None
                    try:
                        prof_dir = profile_q.get(timeout=float(profile_get_timeout_s))
                    except Exception as e_get:
                        last_exc = RuntimeError(
                            f"Timeout waiting for a free profile (>{profile_get_timeout_s}s). "
                            f"Profiles in pool: {len(profile_pool)}. Error: {e_get}"
                        )
                        last_trace = traceback.format_exc()
                        break

                    try:
                        imgs = _run_gemini_ui_generation(
                            url=url,
                            model_choice=model_choice,
                            headless=bool(headless),
                            user_data_dir=prof_dir,
                            executable_path=(chrome_exe.strip() or None),
                            base_image_path=base_image_path,
                            prompt=final_prompt,
                            timeout_s=int(timeout_s),
                            max_images=int(max_images),
                            gen_retry_timeout_s=int(gen_retry_timeout_s),
                            max_session_attempts=int(max_session_attempts),
                            debug_dom=bool(debug_dom),
                            download_debug_dir=str(Path(out_root) / "_download_debug"),
                            download_debug_label=f"{idx_local:03d}_{name_stub}",
                        )

                        if not imgs:
                            raise RuntimeError("No images were downloaded from Gemini UI")

                        name_stub2 = _slug(f"{idx_local:03d}_{name_stub}")
                        saved = _save_images(imgs, item_out_dir, name_stub=name_stub2)
                        if not saved:
                            raise RuntimeError("Images downloaded but nothing was saved")

                        profile_used = prof_dir
                        break
                    except Exception as e:
                        last_exc = e
                        last_trace = traceback.format_exc()
                    finally:
                        if prof_dir:
                            try:
                                profile_q.put(prof_dir)
                            except Exception:
                                pass

                if profile_used and saved:
                    return {
                        "input_name": input_name,
                        "keywords_30": (kw or {}).get("keywords_30"),
                        "image_prompt": prompt_base,
                        "final_prompt": final_prompt,
                        "out_dir": item_out_dir,
                        "saved": saved,
                        "profile_used": os.path.basename(profile_used),
                        "error": None,
                    }

                return {
                    "input_name": input_name,
                    "keywords_30": (kw or {}).get("keywords_30"),
                    "image_prompt": prompt_base,
                    "final_prompt": final_prompt,
                    "out_dir": None,
                    "saved": [],
                    "profile_used": None,
                    "error": f"{type(last_exc).__name__}: {last_exc}" if last_exc else "Unknown error",
                    "trace": last_trace,
                }

            batch_size = max_workers_global
            total_batches = (total_items + batch_size - 1) // batch_size

            for batch_start in range(0, total_items, batch_size):
                batch = uploaded[batch_start : batch_start + batch_size]
                batch_no = (batch_start // batch_size) + 1

                status.write(f"Batch {batch_no}/{total_batches}: подготовка prompts...")

                prepared_batch: list[dict[str, Any]] = []
                for idx, up in enumerate(batch, batch_start + 1):
                    try:
                        prepared_batch.append(_build_prompt_item(idx, up))
                    except Exception as e:
                        results.append(
                            {
                                "input_name": up.name,
                                "keywords_30": None,
                                "image_prompt": None,
                                "final_prompt": None,
                                "out_dir": None,
                                "saved": [],
                                "profile_used": None,
                                "error": f"{type(e).__name__}: {e}",
                                "trace": traceback.format_exc(),
                            }
                        )
                        done += 1
                        overall.progress(int(done / max(1, total_items) * 100), text=f"Генерация (batch): {done}/{total_items}")
                        st.error(f"[{idx}/{total_items}] {up.name} | ошибка на этапе API/prompt: {e}")

                if not prepared_batch:
                    continue

                status.write(f"Batch {batch_no}/{total_batches}: генерация картинок (parallel={batch_size})...")

                max_workers_batch = max(1, min(batch_size, len(prepared_batch)))
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers_batch) as ex:
                    futs = [ex.submit(_worker, it) for it in prepared_batch]
                    for fut in concurrent.futures.as_completed(futs):
                        try:
                            r = fut.result() or {}
                        except Exception as e_fut:
                            r = {
                                "input_name": "<parallel_worker>",
                                "keywords_30": None,
                                "image_prompt": None,
                                "final_prompt": None,
                                "out_dir": None,
                                "saved": [],
                                "profile_used": None,
                                "error": f"{type(e_fut).__name__}: {e_fut}",
                                "trace": traceback.format_exc(),
                            }
                        results.append(r)
                        done += 1
                        overall.progress(int(done / max(1, total_items) * 100), text=f"Генерация (batch): {done}/{total_items}")

            overall.empty()
            status.empty()

        else:
            # -----------------
            # Sequential mode: preserve original behavior
            # -----------------
            prepared: list[dict[str, Any]] = []
            prep = st.progress(0, text="Подготовка prompts...")

            for idx, up in enumerate(uploaded, 1):
                suffix = Path(up.name).suffix or ".jpg"
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(up.getbuffer())
                    tmp_path = tmp.name

                try:
                    kw = gptx.generate_keywords_and_image_prompt(tmp_path)
                    prompt_base = (kw or {}).get("image_prompt") or ""
                    final_prompt = _build_gemini_ui_prompt(keywords_prompt=prompt_base)
                    name_stub = _keywords_to_filename_stub((kw or {}).get("keywords_30"), max_words=5, max_len=60)

                    prepared.append(
                        {
                            "idx": idx,
                            "input_name": up.name,
                            "kw": kw,
                            "prompt_base": prompt_base,
                            "final_prompt": final_prompt,
                            "name_stub": name_stub,
                        }
                    )
                except Exception as e:
                    results.append(
                        {
                            "input_name": up.name,
                            "keywords_30": None,
                            "image_prompt": None,
                            "final_prompt": None,
                            "out_dir": None,
                            "saved": [],
                            "profile_used": None,
                            "error": f"{type(e).__name__}: {e}",
                            "trace": traceback.format_exc(),
                        }
                    )
                    st.error(f"[{idx}/{len(uploaded)}] {up.name} | ошибка на этапе API/prompt: {e}")
                finally:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

                prep.progress(int(idx / max(1, len(uploaded)) * 100), text=f"Подготовка prompts... {idx}/{len(uploaded)}")

            prep.empty()

            if not prepared:
                st.warning("Нет задач для генерации (все упали на этапе подготовки prompts)")
                # Let the stage finish gracefully; results will be empty
            else:
                # Sequential mode: round-robin and full profile fallback
                profile_cursor = 0
                for item in prepared:
                    idx_local = int(item.get("idx") or 0)
                    input_name = str(item.get("input_name") or "")
                    kw = item.get("kw") or {}
                    prompt_base = str(item.get("prompt_base") or "")
                    final_prompt = str(item.get("final_prompt") or "")
                    name_stub = str(item.get("name_stub") or "keywords")

                    item_out_dir = out_root

                    saved: list[str] = []
                    profile_used: str | None = None
                    last_exc: Exception | None = None
                    last_trace: str | None = None

                    for attempt in range(len(profile_pool)):
                        prof_dir = profile_pool[(profile_cursor + attempt) % len(profile_pool)]
                        st.write(
                            f"[{idx_local}/{len(uploaded)}] {input_name} | попытка {attempt + 1}/{len(profile_pool)} | профиль: {os.path.basename(prof_dir)}"
                        )
                        try:
                            imgs = _run_gemini_ui_generation(
                                url=url,
                                model_choice=model_choice,
                                headless=bool(headless),
                                user_data_dir=prof_dir,
                                executable_path=(chrome_exe.strip() or None),
                                base_image_path=base_image_path,
                                prompt=final_prompt,
                                timeout_s=int(timeout_s),
                                max_images=int(max_images),
                                gen_retry_timeout_s=int(gen_retry_timeout_s),
                                max_session_attempts=int(max_session_attempts),
                                debug_dom=bool(debug_dom),
                                download_debug_dir=str(Path(out_root) / "_download_debug"),
                                download_debug_label=f"{idx_local:03d}_{name_stub}",
                            )

                            if not imgs:
                                raise RuntimeError("No images were downloaded from Gemini UI")

                            saved = _save_images(imgs, item_out_dir, name_stub=name_stub)
                            if not saved:
                                raise RuntimeError("Images downloaded but nothing was saved")

                            profile_used = prof_dir
                            profile_cursor = (profile_cursor + attempt + 1) % len(profile_pool)
                            break
                        except Exception as e:
                            last_exc = e
                            last_trace = traceback.format_exc()
                            st.warning(f"Профиль не подошёл, пробую следующий. Причина: {type(e).__name__}: {e}")

                    if profile_used and saved:
                        results.append(
                            {
                                "input_name": input_name,
                                "keywords_30": (kw or {}).get("keywords_30"),
                                "image_prompt": prompt_base,
                                "final_prompt": final_prompt,
                                "out_dir": out_root,
                                "saved": saved,
                                "profile_used": os.path.basename(profile_used),
                                "error": None,
                            }
                        )
                        st.success(f"Сохранено: {len(saved)} (профиль {os.path.basename(profile_used)})")
                    else:
                        results.append(
                            {
                                "input_name": input_name,
                                "keywords_30": (kw or {}).get("keywords_30"),
                                "image_prompt": prompt_base,
                                "final_prompt": final_prompt,
                                "out_dir": out_root,
                                "saved": [],
                                "profile_used": None,
                                "error": f"{type(last_exc).__name__}: {last_exc}" if last_exc else "Unknown error",
                                "trace": last_trace,
                            }
                        )
                        st.error(
                            f"Не удалось сгенерировать даже после перебора всех профилей ({len(profile_pool)}). Последняя ошибка: {last_exc}"
                        )
    st.session_state.gemui_results = results
    st.session_state["stage4_running"] = False

# Render results
if st.session_state.gemui_results:
    st.markdown("---")
    st.subheader("Результаты")

    for r in st.session_state.gemui_results:
        with st.expander(r.get("input_name") or "item", expanded=False):
            if r.get("error"):
                st.error(r["error"])
                if r.get("trace"):
                    st.code(r["trace"], language=None)
                continue

            left, right = st.columns([1, 1])
            with left:
                st.markdown("**keywords_30**")
                st.text_area("", r.get("keywords_30") or "", height=140)
                st.markdown("**image_prompt (from API)**")
                st.text_area("", r.get("image_prompt") or "", height=100)
                st.markdown("**final_prompt (sent to Gemini UI)**")
                st.text_area("", r.get("final_prompt") or "", height=140)

            with right:
                prof_used = r.get('profile_used')
                if prof_used:
                    st.markdown(f"**Profile used:** `{prof_used}`")
                st.markdown(f"**Output dir:** `{r.get('out_dir')}`")
                saved = r.get("saved") or []
                if saved:
                    cols = st.columns(3)
                    for i, p in enumerate(saved):
                        with cols[i % 3]:
                            try:
                                st.image(p, caption=os.path.basename(p), use_container_width=True)
                            except Exception:
                                st.write(os.path.basename(p))
                else:
                    st.info("Нет сохранённых картинок")

    # -------------------------
    # Pinterest: step 1) select images and generate pin texts
    # -------------------------
    st.markdown("---")
    st.subheader("Pinterest: генерация текстовых данных")

    ok_items = [r for r in st.session_state.gemui_results if not r.get("error")]
    all_images: list[str] = []
    for it in ok_items:
        all_images.extend([p for p in (it.get("saved") or []) if isinstance(p, str) and p])
    # de-dup while keeping order
    seen_img: set[str] = set()
    all_images = [p for p in all_images if not (p in seen_img or seen_img.add(p))]

    if not all_images:
        st.info("Нет сохранённых картинок для Pinterest.")
    else:
        st.caption("Отметьте изображения, для которых нужно сгенерировать Title/Description/Hashtags.")
        cols = st.columns(3)
        for i, p in enumerate(all_images):
            with cols[i % 3]:
                try:
                    st.image(p, caption=os.path.basename(p), use_container_width=True)
                except Exception:
                    st.write(os.path.basename(p))
                key = f"pin_sel_{abs(hash(p))}"
                # init checkbox state from selection map
                if key not in st.session_state:
                    st.session_state[key] = bool(st.session_state.pinterest_selection.get(p, False))
                checked = st.checkbox("Выбрать для Pinterest", key=key)
                st.session_state.pinterest_selection[p] = bool(checked)

        pin_selected = [p for p, v in (st.session_state.get("pinterest_selection") or {}).items() if v]
        debug_pins = st.checkbox("Debug: показывать распарсенный pin JSON", value=False, key="gemui_debug_pins")
        if pin_selected:
            st.info(f"Выбрано для Pinterest: {len(pin_selected)}")
            if st.button(
                f"Сгенерировать Pinterest данные ({len(pin_selected)})",
                type="primary",
                key="gemui_generate_pinterest",
            ):
                with st.spinner("Генерация Pinterest данных..."):
                    pin_results: dict[str, Any] = dict(st.session_state.get("pinterest_results") or {})
                    pin_results_raw: dict[str, Any] = dict(st.session_state.get("pinterest_results_raw") or {})
                    for idx2, img_path in enumerate(pin_selected, 1):
                        try:
                            pin_raw = gptx.generate_pinterest_assets(img_path)
                            pin = _normalize_pinterest_pin(pin_raw)
                            pin_results_raw[img_path] = pin_raw
                            pin_results[img_path] = pin
                            st.success(f"[{idx2}/{len(pin_selected)}] Готово: {os.path.basename(img_path)}")
                        except Exception as e:
                            st.error(f"[{idx2}/{len(pin_selected)}] Ошибка: {os.path.basename(img_path)} | {e}")
                    st.session_state.pinterest_results_raw = pin_results_raw
                    st.session_state.pinterest_results = pin_results

                    # Show extracted Title/Description + RAW right away
                    with st.expander("Pinterest тексты (preview)", expanded=True):
                        for k in pin_selected:
                            raw_obj = pin_results_raw.get(k)
                            pin_norm = _normalize_pinterest_pin(pin_results.get(k))

                            st.markdown(f"**{os.path.basename(k)}**")
                            st.write("**Title:**", _extract_first_title(pin_norm) or "(empty)")
                            st.write("**Description:**")
                            st.text_area(
                                "",
                                _build_description(pin_norm, include_hashtags=bool(include_hashtags)) or "",
                                height=140,
                                key=f"pin_desc_prev_{abs(hash(k))}",
                            )

                            with st.expander("RAW from generate_pinterest_assets()", expanded=False):
                                try:
                                    st.code(json.dumps(raw_obj, ensure_ascii=False, indent=2), language="json")
                                except Exception:
                                    st.write(raw_obj)

                                # extra hints
                                if isinstance(raw_obj, dict):
                                    if raw_obj.get("_raw"):
                                        st.caption(f"_raw length: {len(str(raw_obj.get('_raw')))}")
                                    if all(raw_obj.get(x) is None for x in ["description", "hashtags", "three_word_keywords", "title_options"]):
                                        st.warning("RAW dict содержит только None-поля. Это означает: либо Gemini вернул non-JSON (и тогда должен быть _raw), либо call_gemini_with_image вернул None (block/ошибка).")

                            st.markdown("---")

                    if debug_pins and pin_results:
                        with st.expander("Debug: Pinterest pin_results (первые 2)", expanded=False):
                            shown = 0
                            for k, v in pin_results.items():
                                st.markdown(f"**{os.path.basename(k)}**")
                                st.code(json.dumps(v, ensure_ascii=False, indent=2), language="json")
                                shown += 1
                                if shown >= 2:
                                    break

                    # Force rebuild of export table & data_editor widget state
                    st.session_state.pinterest_export_sig = None
                    st.session_state.pinterest_export_rows = []
                    st.session_state.pinterest_export_version = int(st.session_state.get("pinterest_export_version") or 0) + 1
        else:
            st.warning("Ничего не выбрано для Pinterest.")

    # -------------------------
    # Pinterest: step 2) build CSV from generated pin texts
    # -------------------------
    pin_map: dict[str, Any] = dict(st.session_state.get("pinterest_results") or {})
    if pin_map:
        st.markdown("---")
        st.subheader("Pinterest bulk CSV")

        def _pin_content_sig(pin: object) -> tuple:
            if not isinstance(pin, dict):
                return tuple()
            titles = pin.get("title_options")
            if isinstance(titles, list):
                titles_t = tuple(str(x or "") for x in titles)
            else:
                titles_t = (str(titles or ""),) if titles else tuple()

            three = pin.get("three_word_keywords")
            if isinstance(three, list):
                three_t = tuple(str(x or "") for x in three)
            else:
                three_t = (str(three or ""),) if three else tuple()

            return (
                str(pin.get("description") or ""),
                str(pin.get("hashtags") or ""),
                titles_t,
                three_t,
            )

        pins_sig = tuple((p, _pin_content_sig(pin_map.get(p))) for p in sorted(pin_map.keys()))
        export_sig = (
            tuple(sorted(pin_map.keys())),
            bool(include_hashtags),
            int(st.session_state.get("pins_per_day") or 10),
            pins_sig,
        )

        # Rebuild default export table if pin set / settings / generated content changed
        if st.session_state.get("pinterest_export_sig") != export_sig:
            schedule = pch.build_publish_schedule_iso(
                len(pin_map),
                max_per_day=int(st.session_state.get("pins_per_day") or 10),
            )
            st.session_state.pinterest_export_rows = pch.build_default_export_rows(
                pin_map,
                include_hashtags=bool(include_hashtags),
                publish_schedule=schedule,
            )
            st.session_state.pinterest_export_sig = export_sig

        board_options = pch.load_board_names()
        editor_version = int(st.session_state.get("pinterest_export_version") or 0)
        editor_key = f"pinterest_export_editor_{abs(hash(export_sig))}_v{editor_version}"

        edited_export = st.data_editor(
            st.session_state.pinterest_export_rows,
            key=editor_key,
            use_container_width=True,
            hide_index=True,
            column_config={
                "local_image_path": st.column_config.TextColumn("local_image_path", disabled=True, width="large"),
                "Title": st.column_config.TextColumn("Title", width="large"),
                "Media URL": st.column_config.TextColumn("Media URL", width="large"),
                "Pinterest board": st.column_config.SelectboxColumn(
                    "Pinterest board",
                    options=board_options,
                    help="Выберите board из списка (можно начать печатать).",
                    width="medium",
                ),
                "Thumbnail": st.column_config.TextColumn("Thumbnail", width="medium"),
                "Description": st.column_config.TextColumn("Description", width="large"),
                "Link": st.column_config.TextColumn("Link", width="large"),
                "Publish date": st.column_config.TextColumn("Publish date", width="medium"),
                "Keywords": st.column_config.TextColumn("Keywords", width="large"),
            },
        )

        def _to_records(val: Any) -> list[dict[str, Any]]:
            if val is None:
                return []
            if hasattr(val, "to_dict"):
                try:
                    return list(val.to_dict("records"))
                except Exception:
                    pass
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
            return []

        st.session_state.pinterest_export_rows_live = _to_records(edited_export)
        export_disabled = not bool(st.session_state.get("pinterest_export_rows_live") or st.session_state.pinterest_export_rows)

        if st.button(
            "Сгенерировать CSV (и при необходимости загрузить картинки в imgbb)",
            disabled=export_disabled,
            key="gemui_generate_csv",
        ):
            export_rows = (
                st.session_state.get("pinterest_export_rows_live")
                or st.session_state.get("pinterest_export_rows")
                or []
            )

            # Upload images (dedupe)
            urls: dict[str, str] = dict(st.session_state.get("pinterest_image_urls") or {})

            need_paths = [str(r.get("local_image_path") or "").strip() for r in export_rows]
            need_paths = [p for p in need_paths if p]

            missing_files = [p for p in need_paths if not os.path.exists(p)]
            if missing_files:
                st.error("Не найдены локальные файлы картинок (проверьте пути):\n- " + "\n- ".join(missing_files[:10]))
                st.stop()

            to_upload = [p for p in sorted(set(need_paths)) if (p not in urls)]
            if to_upload:
                if not (imgbb_api_key or "").strip():
                    st.error("Нужен IMGBB API key для автозаполнения Media URL (или заполните Media URL вручную в таблице).")
                    st.stop()

                prog = st.progress(0.0)
                with st.spinner(f"Загрузка в imgbb: {len(to_upload)} шт"):
                    for idx2, p in enumerate(to_upload, 1):
                        try:
                            img_bytes = open(p, "rb").read()
                            url_hosted = _imgbb_upload_bytes(img_bytes, api_key=imgbb_api_key, filename=os.path.basename(p))
                        except Exception as e:
                            st.error(f"Не удалось загрузить {os.path.basename(p)} в imgbb: {e}")
                            st.stop()
                        urls[p] = url_hosted
                        prog.progress(idx2 / max(1, len(to_upload)))

            st.session_state.pinterest_image_urls = urls

            # Fill Media URL if blank
            pch.fill_media_urls_from_cache(export_rows, urls_cache=urls)

            st.session_state.pinterest_export_rows = export_rows

            out_bytes = pch.build_pinterest_bulk_csv_bytes(export_rows)
            wrote = len(export_rows)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            st.success(f"Pinterest CSV сформирован: {wrote} строк")
            st.download_button(
                "Скачать Pinterest CSV",
                data=out_bytes,
                file_name=f"pinterest_bulk_{ts}.csv",
                mime="text/csv; charset=utf-8",
            )
    else:
        st.info("Сначала сгенерируйте Pinterest данные (шаг 1), затем появится экспорт CSV.")

    # Download JSON summary
    try:
        blob = json.dumps(st.session_state.gemui_results, ensure_ascii=False, indent=2).encode("utf-8")
        st.download_button("Скачать results.json", data=blob, file_name="results_gemini_ui.json", mime="application/json")
    except Exception:
        pass
else:
    st.info("Загрузите файлы и нажмите 'Сгенерировать'.")
