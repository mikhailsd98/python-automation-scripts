# Helper functions extracted from gemini_playwright_streamlit.py (moved verbatim)
import os
import time
import random
import tempfile
import base64  # may not be needed for all, kept for parity
import re
from typing import List, Tuple, Optional

import streamlit as st
from playwright.sync_api import TimeoutError as PWTimeout

# Streamlit UI calls (st.write, etc.) must NOT be executed from background threads.
# In batch/parallel runs we often call these helpers from ThreadPoolExecutor workers.
# When there is no ScriptRunContext, Streamlit prints noisy warnings and may stall.
try:
    from streamlit.runtime.scriptrunner import get_script_run_ctx  # type: ignore
except Exception:  # pragma: no cover
    get_script_run_ctx = None  # type: ignore

# Keep local flags/constants used inside helpers
verbose_logs = False
BASE_IMAGE_PATH = "10x16.jpg"


def _upgrade_gphotos_url(url: str) -> str:
    """Normalize Google Photos dl URL to high-res, uncropped variant.
    - Remove crop flags (e.g., "-c")
    - Replace any s<number> or w<h>-h<w> style with a large size (s4096)
    - Prefer JPEG render flag (-rj)
    """
    try:
        import re as _re
        u = url
        # Gemini may return different googleusercontent URL shapes (not always /gg-dl/).
        # If it's not a googleusercontent URL, leave untouched.
        if "googleusercontent.com" not in u:
            return u
        if "?" in u:
            u = u.split("?", 1)[0]
        u = _re.sub(r"-c(?=[^/]*$)", "", u)
        u = _re.sub(r"w\d+-h\d+-", "s4096-", u)
        def _repl_size(m):
            tail = m.group(2) or ""
            if tail and not tail.startswith("-"):
                tail = "-" + tail
            tail = tail.replace("-c", "")
            return f"s4096{tail}"
        u = _re.sub(r"s(\d+)([^/]*?)", _repl_size, u)
        if "-rj" not in u:
            u = _re.sub(r"(s\d+[^/]*?)", lambda m: (m.group(1) + "-rj") if "-rj" not in m.group(1) else m.group(1), u, count=1)
        return u
    except Exception:
        return url


def _log(msg: str, *, force: bool = False):
    """Log helper.

    In Streamlit UI thread, logs go to st.write().
    In background threads (no ScriptRunContext), fall back to stdout to avoid
    "missing ScriptRunContext" warnings and hangs.
    """

    noisy_prefixes = ("[debug]", "[DOM]", "[net]", "[window]", "[menu]", "[click]", "[dom-extract]")
    noisy_substrings = (
        "Проба селектора изображений",
        "Найдено контейнеров по селектору",
        "В последнем ответе",
        "переключ",
        "Нашёл кнопку 'Скачать'",
    )
    is_noisy = (msg or "").startswith(noisy_prefixes) or any(s in (msg or "") for s in noisy_substrings)

    def _emit(line: str) -> None:
        # If running outside Streamlit (or in background thread), avoid st.*
        try:
            if get_script_run_ctx is None or get_script_run_ctx() is None:
                print(line, flush=True)
                return
        except Exception:
            print(line, flush=True)
            return

        try:
            st.write(line)
        except Exception:
            # last resort
            print(line, flush=True)

    if force or not is_noisy:
        _emit(f"[playwright] {msg}")
    else:
        if verbose_logs:
            _emit(f"[playwright] {msg}")


# --- Human-like delay helpers ---
_def_min_typing_ms = (55, 120)  # per-char delay range

def _hsleep(a: float = 0.4, b: float = 1.2):
    try:
        time.sleep(random.uniform(a, b))
    except Exception:
        pass

def _hthink(a: float = 0.8, b: float = 2.2):
    _hsleep(a, b)

def _maybe_pause(p: float = 0.22):
    if random.random() < p:
        _hsleep(0.4, 1.4)


def _debug_dom(page):
    try:
        _log("[debug] ===== DOM reachability =====")
        try:
            _log(f"[debug] page.url = {page.url}")
        except Exception:
            pass
        try:
            frames = page.frames
            _log(f"[debug] frames: {len(frames)}")
            for i, fr in enumerate(frames):
                try:
                    _log(f"[debug] frame[{i}]: url={fr.url}")
                    for sel in [
                        'chat-app',
                        '.presented-response-container',
                        '.response-container-content',
                        'structured-content-container',
                        '.attachment-container.generated-images',
                        'single-image',
                        "button[data-test-id*='download']",
                        'img', 'picture img', 'canvas'
                    ]:
                        try:
                            c1 = fr.locator(sel).count()
                        except Exception:
                            c1 = -1
                        try:
                            c2 = fr.locator(f"pierce={sel}").count()
                        except Exception:
                            c2 = -1
                        _log(f"[debug]  sel='{sel}': normal={c1} | piercing={c2}")
                except Exception as e:
                    _log(f"[debug]  frame[{i}] error: {e}")
        except Exception as e:
            _log(f"[debug] frames error: {e}")
        try:
            stats = page.evaluate("() => {\n  function walk(n, acc){\n    if(n.nodeType!==1) return;\n    const el = n;\n    const tn = el.tagName;\n    if(tn==='IMG') acc.img++;\n    if(tn==='CANVAS') acc.canvas++;\n    if(tn==='PICTURE') acc.picture++;\n    if(tn==='BUTTON' && el.getAttribute('data-test-id') && el.getAttribute('data-test-id').includes('download')) acc.dl++;\n    for(const ch of el.children) walk(ch, acc);\n    if(el.shadowRoot){\n      for(const ch of el.shadowRoot.children) walk(ch, acc);\n    }\n  }\n  const acc={img:0,canvas:0,picture:0,dl:0};\n  walk(document.documentElement, acc);\n  return acc;\n}")
            _log(f"[debug] deep-walk stats: {stats}")
        except Exception as e:
            _log(f"[debug] deep-walk error: {e}")
        _log("[debug] ===== end =====")
    except Exception as e:
        _log(f"[debug] error: {e}")


def _ensure_tmp_file(uploaded) -> Optional[str]:
    if uploaded is None:
        return None
    suffix = os.path.splitext(uploaded.name)[1] or ".jpg"
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(uploaded.getbuffer())
    return path


def _wait_image_attached(page, timeout_ms: int = 3000) -> bool:
    try:
        targets = [
            "img[src^='blob:']",
            "img[src^='data:image']",
            "[data-testid*='attachment' i]",
            "[data-test-id*='attachment' i]",
            "[aria-label*='прикрепл' i]",
            "[aria-label*='attached' i]",
            "[aria-label*='image' i]",
        ]
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            for sel in targets:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    return True
            time.sleep(0.2)
    except Exception:
        pass
    return False


def _handle_signed_out_upload_tooltip(
    page,
    *,
    max_time_s: float = 6.0,
) -> bool:
    """Detect the "signed out" tooltip near uploader and click the Sign in button.

    Gemini / AI Studio can show a tooltip like:
      - class: uploader-signed-out-tooltip
      - text: "Чтобы загрузить файлы, войдите в аккаунт."

    If it stays, image attach will never work and callers may hang waiting for previews.

    Returns True if we detected the signed-out UI and attempted to click a sign-in action.
    """

    handled = False
    deadline = time.time() + max(0.5, float(max_time_s))

    # Known/observed selectors
    tooltip_sels = [
        ".uploader-signed-out-tooltip",
        "div.uploader-signed-out-tooltip",
    ]

    # Text patterns (RU/EN)
    txt_pats = [
        re.compile(r"войдите\s+в\s+аккаунт", re.IGNORECASE),
        re.compile(r"чтобы\s+загрузить\s+файл", re.IGNORECASE),
        re.compile(r"sign\s+in\s+to\s+upload", re.IGNORECASE),
        re.compile(r"to\s+upload\s+files,?\s+sign\s+in", re.IGNORECASE),
    ]

    sign_btn_sels = [
        "button.sign-in-button",
        "button:has-text('Войти')",
        "button:has-text('Sign in')",
        "a:has-text('Войти')",
        "a:has-text('Sign in')",
    ]

    def _tooltip_visible() -> bool:
        # 1) Prefer class-based
        for sel in tooltip_sels:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    return True
            except Exception:
                pass
        # 2) Fallback to text-based (visible)
        try:
            # Use locator to avoid full DOM dump
            for pat in txt_pats:
                loc = page.locator("text=/" + pat.pattern + "/i").first
                if loc and loc.count():
                    try:
                        if loc.is_visible():
                            return True
                    except Exception:
                        # If count() succeeded but is_visible fails, still treat as visible-ish
                        return True
        except Exception:
            pass
        return False

    def _click_best_effort(loc) -> bool:
        if not loc:
            return False
        try:
            try:
                loc.scroll_into_view_if_needed()
            except Exception:
                pass
        except Exception:
            pass
        # Try normal/force/js
        for mode in ("normal", "force"):
            try:
                if mode == "normal":
                    loc.click(timeout=1200)
                else:
                    loc.click(timeout=1200, force=True)
                return True
            except Exception:
                continue
        try:
            page.evaluate("el => el.click()", loc)
            return True
        except Exception:
            return False

    while time.time() < deadline:
        if not _tooltip_visible():
            return handled

        handled = True
        # Try click sign-in action
        clicked = False
        for sel in sign_btn_sels:
            try:
                btn = page.locator(sel).first
                if btn and btn.count() > 0:
                    try:
                        if not btn.is_visible():
                            continue
                    except Exception:
                        pass
                    _log("[auth] Signed-out uploader tooltip detected; clicking 'Sign in'...", force=True)
                    try:
                        _click_best_effort(btn)
                    except Exception:
                        pass
                    clicked = True
                    break
            except Exception:
                continue

        # After click, give UI a moment (login page may open).
        try:
            page.wait_for_timeout(350)
        except Exception:
            time.sleep(0.35)

        # If tooltip disappeared, done.
        if not _tooltip_visible():
            return True

        # If we couldn't click, don't spin too long.
        if not clicked:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            try:
                page.wait_for_timeout(250)
            except Exception:
                time.sleep(0.25)

        time.sleep(0.25)

    return handled


def _attach_image(page, image_path: str, *, max_time_s: float = 28.0):
    """Attach an image file to Gemini/AI Studio UI.

    max_time_s is a hard budget to avoid getting stuck forever on UI edge-cases
    (e.g. signed-out uploader tooltip).
    """

    start_ts = time.time()
    try:
        # If uploader is signed out, try to click Sign in right away.
        try:
            _handle_signed_out_upload_tooltip(page, max_time_s=2.5)
        except Exception:
            pass

        # If the upload disclaimer dialog blocks UI, handle it BEFORE attempting to open upload menu.
        # Here we allow a reload as a last resort because we haven't sent the prompt yet.
        try:
            _handle_upload_image_disclaimer_dialog(page, max_time_s=8.0, allow_reload=True)
        except Exception:
            pass

        # Ensure overlays are closed before interacting with upload menu
        try:
            for _ in range(2):
                page.keyboard.press("Escape")
                time.sleep(0.05)
        except Exception:
            pass

        plus_btn_sels = [
            # NOTE: Signed-out tooltip can appear near the input/uploader.
            # We handle it before interacting and also after opening the upload menu.

            # AI Studio
            "button[data-test-id='add-media-button']",
            "button[aria-label='Insert images or files']",
            # Classic Gemini
            "button[aria-label='Открыть меню загрузки файлов']",
            "button[aria-label='Open file upload menu']",
            ".upload-card-button.open",
            "button[aria-label*='загруз']",
            "button[aria-label*='upload' i]",
        ]
        menu_item_texts = [
            "Загрузить файлы", "Загрузить файл", "Загрузить",
            "Upload files", "Upload file", "Upload",
        ]
        # Up to 3 open/close cycles in case menu gets stuck open without handling clicks
        for psel in plus_btn_sels:
            # Hard time budget guard
            if (time.time() - start_ts) > float(max_time_s):
                _log("[upload] attach max_time_s budget exceeded", force=True)
                return False

            btn = page.query_selector(psel)
            if not btn:
                continue
            for cycle in range(3):
                try:
                    try:
                        btn.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    btn.click(force=True)
                    # Small wait for menu to render
                    page.wait_for_timeout(200)

                    # Signed-out tooltip can show after opening the uploader menu.
                    try:
                        _handle_signed_out_upload_tooltip(page, max_time_s=2.0)
                    except Exception:
                        pass

                    # If we're still signed out, attaching will never succeed.
                    # Bail early so callers can treat it as a retryable/bannable failure.
                    try:
                        if page.query_selector('.uploader-signed-out-tooltip'):
                            _log("[auth] Still signed out after sign-in click; abort attach", force=True)
                            return False
                    except Exception:
                        pass

                    # The disclaimer dialog can appear right after clicking the upload button.
                    # Handle it quickly (no reload here; we are mid-interaction).
                    try:
                        _handle_upload_image_disclaimer_dialog(page, max_time_s=2.0, allow_reload=False)
                    except Exception:
                        pass
                    # AI Studio: the menu item contains a real <input type=file>.
                    # Prefer direct set_input_files (more reliable than file chooser events).
                    try:
                        file_inp = page.locator(
                            "input[data-test-upload-file-input], "
                            ".upload-file-menu-item input[type='file'], "
                            ".mat-mdc-menu-content input[type='file']"
                        ).first
                        if file_inp and file_inp.count() > 0:
                            try:
                                file_inp.set_input_files(image_path)
                                try:
                                    page.keyboard.press("Escape")
                                except Exception:
                                    pass
                                return True
                            except Exception:
                                pass
                    except Exception:
                        pass

                    # Fallback: click explicit text variants and use file chooser expectation
                    text_variants = [
                        "Загрузить файлы", "Загрузить файл", "Загрузить",
                        "Upload files", "Upload file", "Upload",
                    ]
                    for tv in text_variants:
                        try:
                            with page.expect_file_chooser(timeout=2500) as fc_info:
                                page.locator(
                                    f"button:has-text('{tv}'), .mat-mdc-menu-item:has-text('{tv}'), .mdc-list-item:has-text('{tv}'), [role='menuitem']:has-text('{tv}')"
                                ).first.click(force=True)
                            fc = fc_info.value
                            fc.set_files(image_path)
                            try:
                                page.keyboard.press("Escape")
                            except Exception:
                                pass
                            return True
                        except Exception:
                            continue
                    # If above failed, try scanning visible menu items and click by contains
                    candidates = page.query_selector_all("button[role='menuitem'], .mat-mdc-menu-item, .mdc-list-item, [role='menuitem']")
                    for item in candidates:
                        try:
                            if not item.is_visible():
                                continue
                        except Exception:
                            pass
                        try:
                            text = (item.inner_text() or "").strip().lower()
                        except Exception:
                            text = ""
                        if not text:
                            continue
                        for want in menu_item_texts:
                            if want.lower() in text:
                                try:
                                    with page.expect_file_chooser(timeout=3000) as fc_info:
                                        item.click(force=True)
                                    fc = fc_info.value
                                    fc.set_files(image_path)
                                    try:
                                        page.keyboard.press("Escape")
                                    except Exception:
                                        pass
                                    return True
                                except Exception:
                                    # Try JS click as a last attempt on this item
                                    try:
                                        with page.expect_file_chooser(timeout=3000) as fc_info:
                                            page.evaluate("el => el.click()", item)
                                        fc = fc_info.value
                                        fc.set_files(image_path)
                                        try:
                                            page.keyboard.press("Escape")
                                        except Exception:
                                            pass
                                        return True
                                    except Exception:
                                        continue
                    # If we reach here, the menu likely ignored clicks — close and retry
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(150)
                    except Exception:
                        pass
                except Exception:
                    # Try to recover by closing menu if any
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                    page.wait_for_timeout(150)
            # end cycles
        # Hidden triggers (bypass menu entirely)
        hidden_triggers = [
            "button[data-test-id='hidden-local-image-upload-button']",
            "button[data-test-id='hidden-local-file-upload-button']",
            "[data-test-id*='hidden-local'][data-test-id*='upload']",
        ]
        for sel in hidden_triggers:
            trg = page.query_selector(sel)
            if not trg:
                continue
            try:
                with page.expect_file_chooser(timeout=3000) as fc_info:
                    trg.click(force=True)
                fc = fc_info.value
                fc.set_files(image_path)
                return True
            except Exception:
                pass
        # Direct input[type=file] fallbacks
        preferred = page.query_selector_all("input[type='file'][accept*='image']")
        for inp in preferred:
            try:
                inp.set_input_files(image_path)
                return True
            except Exception:
                pass
        any_inputs = page.query_selector_all("input[type='file']")
        for inp in any_inputs:
            try:
                inp.set_input_files(image_path)
                return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _find_editor(page):
    """Find the prompt input element.

    Supports:
      - Classic Gemini UI (contenteditable Quill)
      - Google AI Studio new_chat (Angular textarea[formcontrolname='promptText'])
    """

    sels = [
        # AI Studio (new_chat)
        "textarea[formcontrolname='promptText']",
        "textarea[aria-label='Enter a prompt']",
        ".prompt-box-container textarea",
        # Classic Gemini
        "div.ql-editor.textarea.new-input-ui[contenteditable='true']",
        "div.ql-editor[contenteditable='true']",
        "[contenteditable='true'][role='textbox']",
    ]
    for sel in sels:
        try:
            el = page.query_selector(sel)
        except Exception:
            el = None
        if el:
            return el
    return None


def _handle_upload_image_disclaimer_dialog(
    page,
    *,
    max_time_s: float = 6.0,
    allow_reload: bool = False,
) -> bool:
    """Best-effort handler for Gemini UI upload-image disclaimer dialog.

    Sometimes Gemini/AI Studio shows a modal dialog (Angular Material) like:
      - "Создание контента на основе изображений и файлов"
      - buttons with data-test-id: upload-image-agree-button / upload-image-cancel-button

    If this dialog stays open, automation can deadlock because clicks/typing are blocked.

    Returns True if we detected the dialog and attempted to handle it.
    """

    handled_any = False
    deadline = time.time() + max(0.5, float(max_time_s))

    agree_sel = "button[data-test-id='upload-image-agree-button']"
    cancel_sel = "button[data-test-id='upload-image-cancel-button']"
    dialog_sel = "mat-dialog-container, .mat-mdc-dialog-container, .cdk-dialog-container, [role='dialog']"

    def _is_dialog_visible() -> bool:
        try:
            b = page.query_selector(agree_sel) or page.query_selector(cancel_sel)
            if b and b.is_visible():
                return True
        except Exception:
            pass
        try:
            dlg = page.query_selector(dialog_sel)
            if dlg and dlg.is_visible():
                # Heuristic: only treat it as a blocker if it contains the known heading.
                try:
                    t = (dlg.inner_text() or "")
                except Exception:
                    t = ""
                if ("создание контента" in t.lower()) or ("upload" in t.lower() and "image" in t.lower()):
                    return True
        except Exception:
            pass
        return False

    def _click_best_effort(el) -> bool:
        if not el:
            return False
        try:
            el.scroll_into_view_if_needed()
        except Exception:
            pass
        for mode in ("normal", "force", "js"):
            try:
                if mode == "normal":
                    el.click(timeout=900)
                elif mode == "force":
                    el.click(force=True, timeout=900)
                else:
                    page.evaluate("el => el.click()", el)
                return True
            except Exception:
                continue
        return False

    while time.time() < deadline:
        try:
            # 1) Prefer stable data-test-id click
            btn = None
            try:
                btn = page.query_selector(agree_sel)
            except Exception:
                btn = None

            if btn and btn.is_visible():
                handled_any = True
                _log("[overlay] Upload-image disclaimer detected; clicking 'Принять/Agree'...", force=True)
                _click_best_effort(btn)
                try:
                    page.wait_for_selector(agree_sel, state="detached", timeout=1800)
                except Exception:
                    try:
                        page.wait_for_timeout(150)
                    except Exception:
                        time.sleep(0.15)
                # loop again until dialog disappears
                if not _is_dialog_visible():
                    return True

            # 2) Fallback: click a button by text inside any dialog container
            if _is_dialog_visible():
                handled_any = True
                try:
                    loc = page.locator(
                        f"{dialog_sel} button"
                    ).filter(has_text=re.compile(r"(принять|agree|accept)", re.IGNORECASE)).first
                    if loc and loc.count() > 0:
                        try:
                            loc.click(timeout=900)
                        except Exception:
                            try:
                                loc.click(force=True, timeout=900)
                            except Exception:
                                pass
                except Exception:
                    pass

                # 3) Try ESC and backdrop click
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                try:
                    page.locator(".cdk-overlay-backdrop").first.click(timeout=600, force=True)
                except Exception:
                    pass

                # Give UI a moment
                try:
                    page.wait_for_timeout(220)
                except Exception:
                    time.sleep(0.22)

                if not _is_dialog_visible():
                    return True

        except Exception:
            pass

        time.sleep(0.2)

    # Last resort: reload (ONLY when explicitly allowed by caller)
    if allow_reload and _is_dialog_visible():
        handled_any = True
        _log("[overlay] Disclaimer dialog seems stuck; reloading page as a last resort...", force=True)
        try:
            page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            try:
                page.reload(timeout=30000)
            except Exception:
                pass
        try:
            # After reload, make sure input is reachable again.
            _wait_input_ready(page, timeout_ms=60000)
        except Exception:
            pass
        return True

    return handled_any


def _dismiss_overlays(page):
    # IMPORTANT: handle blocking upload disclaimer first.
    try:
        _handle_upload_image_disclaimer_dialog(page, max_time_s=2.5, allow_reload=False)
    except Exception:
        pass

    try:
        for _ in range(2):
            page.keyboard.press("Escape")
            time.sleep(0.05)
    except Exception:
        pass
    try:
        locs = [
            ".text-input-field_textarea-inner",
            "div.ql-editor.textarea.new-input-ui[contenteditable='true']",
            "div.ql-editor[contenteditable='true']",
            "[contenteditable='true'][role='textbox']",
            # AI Studio textarea
            "textarea[formcontrolname='promptText']",
            "textarea[aria-label='Enter a prompt']",
        ]
        for sel in locs:
            el = page.query_selector(sel)
            if el:
                try:
                    el.click()
                    break
                except Exception:
                    continue
    except Exception:
        pass


def _wait_input_ready(page, timeout_ms: int = 60000):
    # Keep selectors in sync with _find_editor()
    sels = [
        "textarea[formcontrolname='promptText']",
        "textarea[aria-label='Enter a prompt']",
        ".prompt-box-container textarea",
        "div.ql-editor.textarea.new-input-ui[contenteditable='true']",
        "div.ql-editor[contenteditable='true']",
        "[contenteditable='true'][role='textbox']",
    ]
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        for sel in sels:
            try:
                el = page.query_selector(sel)
            except Exception:
                el = None
            if el:
                return True
        time.sleep(0.3)
    raise PWTimeout("Не найдено поле ввода в отведённое время")


def _start_new_chat(page, timeout_ms: int = 15000) -> bool:
    try:
        btn = page.query_selector("button[data-test-id='new-chat-button']") or page.query_selector("[data-test-id='new-chat-button']")
        if btn:
            try:
                btn.click()
            except Exception:
                pass
            time.sleep(0.5)
            try:
                _wait_input_ready(page, timeout_ms=timeout_ms)
            except Exception:
                pass
            return True
    except Exception:
        pass
    return False


def _type_prompt(page, prompt: str):
    """Type/paste prompt into Gemini editor.

    IMPORTANT: Gemini's editor is typically a contenteditable (Quill).
    Using ElementHandle.type() can be extremely slow for long prompts and may
    hit the default 30s timeout, which in the parallel runner triggers a full
    browser/context restart.

    Strategy:
      1) Focus editor and clear existing content
      2) Prefer fast insertion via keyboard.insert_text (not per-char type)
      3) Fallback to direct DOM assignment + input/change events
      4) Last resort: ElementHandle.type with a higher timeout
    """

    prompt = "" if prompt is None else str(prompt)

    el = _find_editor(page)
    if not el:
        raise RuntimeError("Поле ввода не найдено")

    # Focus editor
    try:
        el.click()
    except Exception:
        try:
            page.evaluate("el => el.focus()", el)
        except Exception:
            pass

    # Clear existing content
    # IMPORTANT: do it ONLY once.
    # Multiple clears contribute to the "several wipes" effect when tabs rerender.
    # We rely on _clear_editor() right before inserting the prompt.

    def _read_editor_text() -> str:
        try:
            return page.evaluate(
                """(el) => {
                    try { return (el.innerText || el.textContent || '').trim(); }
                    catch(e){ return ''; }
                }""",
                el,
            ) or ""
        except Exception:
            return ""

    def _set_editor_text(text: str) -> bool:
        try:
            page.evaluate(
                """
(el, text) => {
  try { el.focus(); } catch(e) {}
  // Prefer textContent (keeps it plain)
  try { el.textContent = text; } catch(e) {
    try { el.innerText = text; } catch(e2) {}
  }
  // Dispatch common events so frameworks notice the change
  try { el.dispatchEvent(new InputEvent('input', { bubbles: true })); } catch(e) {
    try { const ev = document.createEvent('Event'); ev.initEvent('input', true, true); el.dispatchEvent(ev); } catch(e2) {}
  }
  try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch(e) {}
}
                """,
                el,
                text,
            )
            return True
        except Exception:
            return False

    def _clear_editor():
        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            return
        except Exception:
            pass
        try:
            _set_editor_text("")
        except Exception:
            pass

    def _text_matches(expected: str, actual: str) -> bool:
        """Fast-ish match with whitespace normalization.

        Reading very large editor text can be expensive; callers should avoid calling this
        in a tight loop. We keep it permissive to handle Gemini's whitespace normalization.
        """
        if expected == actual:
            return True
        if not actual:
            return False
        # Quick accept: expected content is fully present
        if expected in actual:
            return True
        # Normalize whitespace and compare
        ne = re.sub(r"\s+", " ", expected).strip()
        na = re.sub(r"\s+", " ", actual).strip()
        return ne == na or (ne and ne in na)

    def _fast_verify(expected: str) -> bool:
        """Verify editor content without pulling full text back to Python.

        We need to catch a tricky failure mode you reported: when inserting long text into Gemini's
        Quill/contenteditable, a middle chunk can drop out and later appear appended at the end.
        That keeps prefix+suffix and near-total length, but the text order is corrupted.

        Verification strategy (in browser, after whitespace normalization):
          - length close enough (>= ~95% with tolerance)
          - prefix and suffix present
          - multiple interior checkpoints present AND in correct order
        """
        try:
            n = len(expected)
            if n == 0:
                return True

            # windows
            pre = expected[:220]
            mid1 = expected[n//3 : min(n//3 + 180, n)] if n > 600 else ""
            mid2 = expected[(2*n)//3 : min((2*n)//3 + 180, n)] if n > 900 else ""
            suf = expected[-220:] if n > 220 else expected

            tol = max(120, int(n * 0.05))  # 5% or at least 120 chars
            min_len = max(0, n - tol)

            return bool(
                page.evaluate(
                    """(el, pre, mid1, mid2, suf, minLen) => {
                        try {
                          const tRaw = (el.innerText || el.textContent || '');
                          if (!tRaw) return false;
                          const t = tRaw.replace(/\\s+/g,' ').trim();
                          if (t.length < minLen) return false;

                          function norm(x){ return (x||'').replace(/\\s+/g,' ').trim(); }
                          const a = norm(pre), b = norm(mid1), c = norm(mid2), d = norm(suf);

                          // find indices; require increasing order
                          let idxA = a ? t.indexOf(a) : 0;
                          if (a && idxA < 0) return false;
                          let pos = (idxA >= 0 ? idxA + a.length : 0);

                          let idxB = b ? t.indexOf(b, pos) : pos;
                          if (b && idxB < 0) return false;
                          pos = (idxB >= 0 ? idxB + b.length : pos);

                          let idxC = c ? t.indexOf(c, pos) : pos;
                          if (c && idxC < 0) return false;
                          pos = (idxC >= 0 ? idxC + c.length : pos);

                          let idxD = d ? t.indexOf(d, pos) : pos;
                          if (d && idxD < 0) return false;

                          return true;
                        } catch(e){ return false; }
                    }""",
                    el,
                    pre,
                    mid1,
                    mid2,
                    suf,
                    min_len,
                )
            )
        except Exception:
            return False

    # NOTE: clipboard-based insertion removed for stability (can be blocked and can trigger re-render wipes).

    # IMPORTANT STABILITY NOTE (Gemini/Quill):
    # Direct DOM assignment and clipboard APIs can be flaky depending on account/permissions
    # and may cause the text to flash and then disappear (UI re-render wipes it).
    # For robustness we prefer Playwright's native keyboard.insert_text which behaves like real input.

    def _insert_via_dom(text: str) -> bool:
        """One-shot paste-like insertion for long prompts.

        Uses in-page APIs to avoid Playwright keyboard limitations on very long strings.
        No retries, no verify.
        """
        try:
            return bool(
                page.evaluate(
                    """(el, text) => {
                        try {
                          el.focus();

                          // Select all existing content
                          const sel = window.getSelection();
                          if (sel) {
                            sel.removeAllRanges();
                            const r = document.createRange();
                            r.selectNodeContents(el);
                            sel.addRange(r);
                          }

                          // Try execCommand insertText (usually triggers Quill listeners)
                          let ok = false;
                          try { ok = document.execCommand('insertText', false, text); } catch(e) { ok = false; }

                          if (!ok) {
                            // Fallback: direct textContent
                            try { el.textContent = text; } catch(e) { try { el.innerText = text; } catch(e2) {} }
                          }

                          // Notify frameworks
                          try { el.dispatchEvent(new InputEvent('input', { bubbles: true })); } catch(e) {
                            try { const ev = document.createEvent('Event'); ev.initEvent('input', true, true); el.dispatchEvent(ev); } catch(e2) {}
                          }
                          try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch(e) {}

                          return true;
                        } catch(e){ return false; }
                    }""",
                    el,
                    text,
                )
            )
        except Exception:
            return False

    # Insert ONCE and stop. No verify, no retries, no per-character typing.
    # For long prompts we prefer DOM insertion to avoid truncation in Quill.
    try:
        _clear_editor()
        if len(prompt) >= 800:
            ok = _insert_via_dom(prompt)
            if not ok:
                # last fallback: still one-shot
                page.keyboard.insert_text(prompt)
        else:
            page.keyboard.insert_text(prompt)

        try:
            page.wait_for_timeout(120)
        except Exception:
            pass
        return
    except Exception as e:
        _log(f"[input] ERROR: failed to insert prompt: {e}", force=True)
        raise


def _pick_model(page, label: str, timeout_ms: int = 5000) -> bool:
    if not label:
        return False
    try:
        _log(f"[menu] Выбираю модель: {label}", force=True)
        _lbl = label.strip().lower()
        want_fast = _lbl.startswith("быст") or _lbl.startswith("fast")
        want_think = _lbl.startswith("дума") or ("reason" in _lbl) or ("advanced" in _lbl) or ("think" in _lbl)
        want_pro = ("nano banana pro" in _lbl) or ("banana pro" in _lbl) or (_lbl == "pro") or (" pro" in _lbl) or _lbl.endswith("pro")

        menu_sel = (
            ".cdk-overlay-pane gem-menu[role='menu'], "
            ".cdk-overlay-pane [data-test-id='gem-mode-menu'], "
            ".cdk-overlay-pane .popover-menu, "
            ".cdk-overlay-pane .mat-mdc-menu-panel[role='menu'], "
            ".cdk-overlay-pane .mat-mdc-menu-panel"
        )

        def _menu_is_open() -> bool:
            try:
                loc = page.locator(menu_sel).first
                return bool(loc and loc.count() and loc.is_visible())
            except Exception:
                return False

        def _open_menu() -> bool:
            if _menu_is_open():
                return True
            triggers = [
                "[data-test-id='bard-mode-menu-button']",
                "[data-test-id='bard-mode-menu-button'] button",
                "button.input-area-switch",
                "bard-mode-switcher button[aria-haspopup='true']",
            ]
            for sel in triggers:
                try:
                    el = page.locator(sel).first
                    if not el or el.count() == 0:
                        continue
                    try:
                        el.scroll_into_view_if_needed(timeout=700)
                    except Exception:
                        pass
                    try:
                        el.click(timeout=1200)
                    except Exception:
                        el.click(force=True, timeout=1200)
                    page.wait_for_selector(menu_sel, state="visible", timeout=1800)
                    _log("[menu] Меню моделей открыто")
                    return True
                except Exception:
                    continue
            return _menu_is_open()

        def _click_menu_item(labels: list[str], *, exclude: list[str] | None = None) -> tuple[bool, str]:
            try:
                return tuple(page.evaluate(
                    """({labels, exclude}) => {
                      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                      const wanted = (labels || []).map(norm).filter(Boolean);
                      const banned = (exclude || []).map(norm).filter(Boolean);
                      const visible = (el) => {
                        if (!el) return false;
                        const st = window.getComputedStyle(el);
                        const r = el.getBoundingClientRect();
                        return st && st.visibility !== 'hidden' && st.display !== 'none' && r.width > 0 && r.height > 0;
                      };
                      const menus = Array.from(document.querySelectorAll(
                        '.cdk-overlay-pane gem-menu[role="menu"], .cdk-overlay-pane [role="menu"], .cdk-overlay-pane .popover-menu, .cdk-overlay-pane .mat-mdc-menu-panel'
                      )).filter(visible);
                      const root = menus.length ? menus[menus.length - 1] : document;
                      const items = Array.from(root.querySelectorAll(
                        'gem-menu-item[role="menuitem"], [role="menuitem"], [role="menuitemradio"], .mat-mdc-menu-item, button'
                      )).filter(visible);
                      for (const item of items) {
                        const labelEl = item.querySelector('.label');
                        const labelText = norm(labelEl ? labelEl.textContent : '');
                        const fullText = norm(item.innerText || item.textContent || '');
                        const hay = labelText || fullText;
                        if (!hay) continue;
                        if (banned.some((b) => hay.includes(b))) continue;
                        const hit = wanted.some((w) => labelText === w || hay.includes(w));
                        if (!hit) continue;
                        item.scrollIntoView({block: 'center', inline: 'nearest'});
                        item.click();
                        return [true, hay];
                      }
                      return [false, ''];
                    }""",
                    {"labels": labels, "exclude": exclude or []},
                ))
            except Exception:
                return (False, "")

        def _set_extended_reasoning() -> bool:
            if not _open_menu():
                return False
            clicked_level, level_txt = _click_menu_item(["Расширенный", "Advanced", "Extended"])
            if clicked_level:
                _log(f"[menu] Уровень рассуждений выбран: {level_txt}", force=True)
                return True
            opened_reasoning, txt = _click_menu_item(
                ["Уровень рассуждений", "Reasoning level", "Thinking level"]
            )
            if not opened_reasoning:
                return False
            try:
                page.wait_for_timeout(180)
            except Exception:
                pass
            clicked_level, level_txt = _click_menu_item(["Расширенный", "Advanced", "Extended"])
            if clicked_level:
                _log(f"[menu] Уровень рассуждений выбран: {level_txt or txt}", force=True)
            return bool(clicked_level)

        if not _open_menu():
            _log("[menu] Не удалось открыть меню моделей")
            return False

        clicked = False
        if want_fast:
            clicked, txt = _click_menu_item(
                ["3.5 Flash", "Flash", "Быстрая", "Fast"],
                exclude=["3.1 Flash-Lite", "Flash-Lite", "Flash Lite", "Lite"],
            )
            if clicked:
                _log(f"[menu] Модель выбрана: {txt}", force=True)
        else:
            # Old "Думающая" and "Nano Banana Pro" now map to 3.1 Pro + Расширенный.
            clicked, txt = _click_menu_item(
                ["3.1 Pro", "Nano Banana Pro", "Banana Pro", "Думающая", "Pro"],
                exclude=["Flash", "Lite"],
            )
            if clicked:
                _log(f"[menu] Модель выбрана: {txt}", force=True)
            if want_pro or want_think or clicked:
                _set_extended_reasoning()

        # Verify selection by reading trigger text
        try:
            page.wait_for_timeout(300)
            trig = page.query_selector("[data-test-id='bard-mode-menu-button']") or page.query_selector("button.input-area-switch")
            picked_txt = ""
            if trig:
                try:
                    picked_txt = (trig.inner_text() or "").strip().lower()
                except Exception:
                    picked_txt = ""
            _log(f"[menu] Текущая подпись кнопки: '{picked_txt}'")
            if want_fast and (("flash" in picked_txt and "lite" not in picked_txt) or "быстр" in picked_txt or "fast" in picked_txt):
                return True
            if (want_pro or want_think) and ("pro" in picked_txt):
                return True
            _log("[menu] Похоже, переключение не применилось (нет доступа или DOM изменился)")
        except Exception:
            pass

        # Close overlay if still open
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return clicked
    except Exception:
        return False


def _click_send(page, *, timeout_ms: int = 2500) -> bool:
    """Click Gemini "Send" reliably.

    Failure mode we want to reduce:
    - prompt text is inserted, but the UI doesn't actually submit (no new turn, no generation),
      and the worker then waits until timeout.

    We therefore do a small, safe retry loop and (crucially) verify that submission has STARTED.

    Success signals (any of):
    - editor becomes empty (Gemini cleared the input)
    - a "stop generating" button appears
    - the number of response containers increases

    Notes:
    - We keep this conservative (short timeouts) because bulk runners open many tabs.
    - We only press Enter as a fallback AND only when the editor is focused.
    """

    deadline = time.time() + (max(250, int(timeout_ms)) / 1000.0)

    send_sels = [
        # AI Studio new_chat
        "ms-run-button button[type='submit']",
        "button:has-text('Run')",
        # Classic Gemini
        "button[aria-label='Отправить сообщение']",
        "button[aria-label='Send message']",
        # Sometimes icon is inside a button; click closest button.
        "button:has(.mat-icon[fonticon='send'])",
        "button .mat-icon[fonticon='send']",
        ".send-button",
        "button[type='submit']",
        "[data-test-id*='send' i]",
    ]

    stop_sels = [
        "button[aria-label*='Stop' i]",
        "button[aria-label*='Останов' i]",
        "button[data-test-id*='stop' i]",
        "[data-test-id*='stop-generating' i]",
    ]

    response_sels = [
        ".presented-response-container",
        ".response-container-content",
        "structured-content-container",
        ".response-container",
        "div[class*='response']",
        "model-response",
        "[data-test-id*='model-response']",
    ]

    def _count_responses() -> int:
        try:
            total = 0
            for sel in response_sels:
                try:
                    total += int(page.locator(sel).count())
                except Exception:
                    pass
            return int(total)
        except Exception:
            return 0

    def _editor_len(ed) -> int:
        try:
            return int(
                page.evaluate(
                    """(el) => {
                        try {
                          const t = (el.innerText || el.textContent || '');
                          return (t || '').trim().length;
                        } catch(e){ return -1; }
                    }""",
                    ed,
                )
            )
        except Exception:
            return -1

    def _has_stop_button() -> bool:
        for sel in stop_sels:
            try:
                loc = page.locator(sel).first
                if loc and loc.count():
                    try:
                        if loc.is_visible():
                            return True
                    except Exception:
                        return True
            except Exception:
                continue
        return False

    def _try_click_send_once() -> bool:
        for sel in send_sels:
            el = None
            try:
                el = page.query_selector(sel)
            except Exception:
                el = None
            if not el:
                continue

            try:
                # If selector matched the icon, climb to the button
                try:
                    tag = (el.evaluate("e => e.tagName") or "").lower()
                except Exception:
                    tag = ""
                if tag != "button":
                    try:
                        btn = el.evaluate_handle("e => e.closest('button')")
                        if btn:
                            el = btn
                    except Exception:
                        pass

                try:
                    if el.get_attribute("aria-disabled") == "true":
                        continue
                except Exception:
                    pass

                # Prefer normal click, then force, then JS click
                try:
                    el.click(timeout=800)
                    return True
                except Exception:
                    pass
                try:
                    el.click(force=True, timeout=800)
                    return True
                except Exception:
                    pass
                try:
                    page.evaluate("el => el.click()", el)
                    return True
                except Exception:
                    pass
            except Exception:
                continue

        return False

    # Snapshot before send
    ed0 = None
    try:
        ed0 = _find_editor(page)
    except Exception:
        ed0 = None
    before_len = _editor_len(ed0) if ed0 else -1
    before_cnt = _count_responses()

    def _submitted_started(ed, before_cnt0: int) -> bool:
        # 1) Stop button appears
        if _has_stop_button():
            return True
        # 2) Responses increased
        try:
            if _count_responses() > int(before_cnt0):
                return True
        except Exception:
            pass
        # 3) Editor cleared
        try:
            if ed is not None and _editor_len(ed) == 0:
                return True
        except Exception:
            pass
        return False

    # We do a few fast attempts before giving up.
    # Keep total time bounded by deadline.
    attempt = 0
    while time.time() < deadline:
        attempt += 1

        # Overlays/popups can steal clicks.
        try:
            _dismiss_overlays(page)
        except Exception:
            pass

        clicked = _try_click_send_once()
        if not clicked:
            # Safe Enter fallback (ONLY when editor is focused)
            try:
                ed = _find_editor(page)
                if ed:
                    try:
                        page.evaluate("el => el.focus()", ed)
                    except Exception:
                        pass
                    is_active = True
                    try:
                        is_active = bool(page.evaluate("el => document.activeElement === el", ed))
                    except Exception:
                        is_active = True
                    if is_active:
                        try:
                            page.keyboard.press("Enter")
                            clicked = True
                        except Exception:
                            clicked = False
            except Exception:
                clicked = False

        if clicked:
            # Give the UI a moment to react
            try:
                page.wait_for_timeout(140)
            except Exception:
                time.sleep(0.14)

            # Verify submission started; if not, keep trying until deadline.
            ed = None
            try:
                ed = _find_editor(page)
            except Exception:
                ed = None
            if _submitted_started(ed, before_cnt):
                return True

        # If prompt was empty before_len==0, there's nothing to send.
        if before_len == 0:
            return False

        # small backoff
        try:
            page.wait_for_timeout(120 + min(220, attempt * 40))
        except Exception:
            time.sleep(0.12)

    return False
