# Unified Streamlit interface: Gemini (Playwright) → Photoshop watermark removal → WebP conversion
# Usage: streamlit run app_unified_streamlit.py

import os
import sys
import subprocess
import shutil
import queue
from datetime import datetime
from pathlib import Path
from typing import List

import streamlit as st
import streamlit.components.v1 as components
import requests

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

# Default URLs (same as original UI)
DEFAULT_URLS = [
    "https://gemini.google.com/app",
    "https://aistudio.google.com/app",
    "https://aistudio.google.com/prompts/new_chat?model=gemini-2.5-flash-image",
]
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

st.set_page_config(page_title="Unified: Gemini → Photoshop → WebP", layout="wide")
st.title("Unified workflow: Gemini (Playwright) → Photoshop fill → WebP")

# Глобальная настройка корня генераций (по умолчанию — локальная папка). Можно указать абсолютный путь на диске.
if "unif_base_root" not in st.session_state:
    # Попробуем использовать Desktop path из Windows-профиля пользователя, если доступен
    try:
        default_root = str((_PathAlias("generate automation")).resolve())
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


tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "1) Gemini Generate (Playwright)",
    "2) Gemini Generate (Nano Banana Pro / multi-window)",
    "3) Remove watermark in Photoshop",
    "4) Normalize Pins to 640×1024 (crop)",
    "5) Convert to WebP",
    "6) Regenerate Titles & Descriptions (Gemini API)",
    "7) Regenerate Product Images (Gemini UI)",
])

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
    resolved_user_data_dir = os.path.abspath(os.path.expanduser(user_data_dir))
    st.caption("Подсказка: по умолчанию используется .chrome_automation_profile. Можете дописать _1 (например, .chrome_automation_profile_1) для второго аккаунта.")
    st.caption(f"Реально используется: {resolved_user_data_dir}")
    # Используем абсолютный путь дальше по коду
    user_data_dir = resolved_user_data_dir
    executable_path = st.text_input("Путь к chrome.exe (по умолчанию C:/Program Files/Google/Chrome/Application/chrome.exe)", value=r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", key="unif_exe_path")
    use_cdp = st.checkbox("Запуск и подключение по CDP (рекомендуется)", value=True, key="unif_use_cdp")
    # Поддержка обновления CDP URL через временное состояние
    if "_tmp_new_cdp" in st.session_state:
        st.session_state["unif_cdp_url"] = st.session_state.pop("_tmp_new_cdp")
    cdp_url = st.text_input("CDP URL", value=st.session_state.get("unif_cdp_url", "http://127.0.0.1:9222"), key="unif_cdp_url")


    # Multiprompt UI
    if "unif_pw_prompts" not in st.session_state:
        st.session_state.unif_pw_prompts = [""]

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

    with st.sidebar:
        st.markdown("### Базовое изображение (белое)")
        if os.path.exists(BASE_IMAGE_PATH):
            st.image(BASE_IMAGE_PATH, caption=BASE_IMAGE_PATH, use_container_width=True)
        else:
            st.error(f"Базовый файл не найден: {BASE_IMAGE_PATH}")
        # Блок перегенерации как в оригинале — использует session_state
        st.markdown("---")
        st.markdown("#### Перегенерация")
        # Показываем сохранённые файлы из последней генерации
        saved_paths = st.session_state.get("unif_saved_paths", [])
        base_dir = st.session_state.get("unif_last_base_dir", _get_run_base_dir())
        final_prompts = st.session_state.get("unif_final_prompts", [])
        if saved_paths:
            import re as _re
            by_prompt = {}
            for p in saved_paths:
                m = _re.match(r"^(\d{2})_\d{2}_(?:re\d+_)?(.+)\.(?:png|jpg|jpeg|bin)$", os.path.basename(p))
                if m:
                    idx = int(m.group(1))
                    by_prompt.setdefault(idx, []).append(p)
            for idx in sorted(by_prompt.keys()):
                st.caption(f"Промпт #{idx}")
                # Кнопка рядом с каждой группой
                if st.button(f"Пересоздать #{idx}", key=f"regen_side_{idx}"):
                    try:
                        # Определим актуальную папку по уже сохранённым файлам этого промпта, если возможно
                        _saved = st.session_state.get("unif_saved_paths", [])
                        import re as _re
                        _dirs = [os.path.dirname(p) for p in _saved if _extract_prompt_idx(p) == idx]
                        base_dir = (_dirs[0] if _dirs else base_dir)
                        playwright = sync_playwright().start()
                        if st.session_state.get("unif_use_cdp", True):
                            def _is_cdp_up(url: str) -> bool:
                                try:
                                    with urllib.request.urlopen(url + "/json/version", timeout=1) as resp:
                                        return resp.status == 200
                                except Exception:
                                    return False
                            cdp_url = st.session_state.get("unif_cdp_url", "http://127.0.0.1:9222")
                            executable_path = st.session_state.get("unif_exe_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
                            user_data_dir = st.session_state.get("unif_user_data_dir", r"C:\\temp\\chrome-debug")
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
                            page = ctx.pages[0] if ctx.pages else ctx.new_page()
                            page.set_default_timeout(30000)
                        else:
                            headless = st.session_state.get("unif_headless", False)
                            use_auto_profile = st.session_state.get("unif_auto_profile", True)
                            executable_path = st.session_state.get("unif_exe_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
                            user_data_dir = st.session_state.get("unif_user_data_dir", r"C:\\temp\\chrome-debug")
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
                        url = st.session_state.get("unif_url", DEFAULT_URLS[0])
                        page.goto(url, wait_until="load")
                        _debug_dom(page)
                        # Выберем текст промпта из сохранённого списка
                        if 1 <= idx <= len(final_prompts):
                            pt = final_prompts[idx-1]
                        else:
                            pt = final_prompts[0] if final_prompts else ""
                        imgs, new_saved = _regenerate_prompt(page, ctx, pt, idx, base_dir, max_images=1, attach_base=True, base_image_path=BASE_IMAGE_PATH)
                        if new_saved:
                            # Remove old files for this prompt index, keep only newly generated ones
                            try:
                                deleted = _cleanup_old_prompt_files(os.path.dirname(new_saved[0]), idx, new_saved)
                            except Exception:
                                deleted = 0
                            # Update session state's saved paths: drop old for idx, add new
                            cur = st.session_state.get("unif_saved_paths", [])
                            import re as _re
                            updated = []
                            for p in cur:
                                _idxp = _extract_prompt_idx(p)
                                if _idxp == idx:
                                    continue
                                updated.append(p)
                            updated.extend(list(new_saved))
                            st.session_state.unif_saved_paths = updated
                            st.success(f"Перегенерация завершена: {len(new_saved)} файлов. Удалено старых: {deleted}")
                            st.rerun()
                        else:
                            st.warning("Перегенерация не вернула новых файлов")
                    except Exception as e:
                        st.error(f"Ошибка при перегенерации: {e}")
        else:
            st.caption("Пока нет сохранённых изображений для перегенерации")

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

    # Open/connect
    if open_btn:
        try:
            if use_auto_profile:
                os.makedirs(user_data_dir, exist_ok=True)
            playwright = sync_playwright().start()
            if use_cdp:
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
                page.goto(url, wait_until="load")
                _debug_dom(page)
                st.session_state.unif_pw_ctx = ctx
                st.session_state.unif_pw_browser = playwright
                st.session_state.unif_pw_page = page
            else:
                browser = playwright.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir if use_auto_profile else None,
                    headless=headless,
                    channel='chrome',
                    executable_path=executable_path or None,
                    args=["--lang=ru-RU"],
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
            playwright = sync_playwright().start()
            if use_cdp:
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
            # Persist handles in session_state for later 'Пересоздать'
            st.session_state.unif_pw_ctx = ctx
            st.session_state.unif_pw_page = page
            st.session_state.unif_pw_browser = playwright
            page.goto(url, wait_until="load")
            _debug_dom(page)
            _wait_input_ready(page, timeout_ms=60000)

            pre = "Change the white 10:16 ratio image using this Prompt: "
            post = " DO NOT LEAVE BLANK WHITE SPACE, THIS IS IMPORTANT "
            final_prompts = [f"{pre}{(p or '').strip()}{post}" for p in st.session_state.unif_pw_prompts if (p or '').strip()]

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
                    _type_prompt(page, fp)
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
                st.session_state.unif_saved_paths = _unique_saved
                st.session_state.unif_last_base_dir = str(_PathAlias(base_dir).resolve())
                st.session_state.unif_final_prompts = final_prompts
                # Обновляем дефолт для Photoshop вкладки, чтобы она показывала именно текущую генерацию
                try:
                    st.session_state.ps_src_dir = st.session_state.unif_last_base_dir
                except Exception:
                    pass
            else:
                st.warning("Не удалось сохранить изображения. Проверьте логин/генерацию.")
        except Exception as e:
            st.error(f"Ошибка генерации: {e}")

    # Всегда показываем панель результатов, если есть данные в session_state
    saved_paths = st.session_state.get("unif_saved_paths", [])
    base_dir = st.session_state.get("unif_last_base_dir", _get_run_base_dir())
    if saved_paths:
        with st.expander("Показать сохранённые файлы (кликните для раскрытия)", expanded=True):
            import re as _re
            by_prompt = {}
            # Универсальная группировка: новый формат {idx}_{slug}_{vv}.{ext} и старый {ii}_{vv}_{reN_}{slug}.{ext}
            for p in saved_paths:
                b = os.path.basename(p)
                m = _re.match(r"^(\d+)_((?:re\d+_)?[^.]+)_(\d{2})(?:_filled)?\.(?:png|jpg|jpeg|bin|webp)$", b, flags=_re.IGNORECASE)
                if m:
                    _idx = int(m.group(1))
                    by_prompt.setdefault(_idx, []).append(p)
                    continue
                m2 = _re.match(r"^(\d{2})_\d{2}_(?:re\d+_)?(.+)\.(?:png|jpg|jpeg|bin)$", b, flags=_re.IGNORECASE)
                if m2:
                    _idx = int(m2.group(1))
                    by_prompt.setdefault(_idx, []).append(p)
                else:
                    # Если индекс не извлекается, складываем в группу 0, чтобы всё равно показать
                    by_prompt.setdefault(0, []).append(p)
            for idx in sorted(by_prompt.keys()):
                st.write(f"Промпт #{idx}")
                # вывод в 4 колонки (примерно четверть ширины на изображение)
                cols = st.columns(4)
                for i, pth in enumerate(sorted(by_prompt[idx])):
                    with cols[i % 4]:
                        try:
                            st.image(pth, caption=os.path.basename(pth), use_container_width=True)
                        except Exception:
                            st.write(os.path.basename(pth))
                # Кнопка перегенерации под группой (как в оригинальном UI)
                if st.button(f"Пересоздать промпт #{idx}", key=f"regen_inline_{idx}"):
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
                            cur = st.session_state.get("unif_saved_paths", [])
                            import re as _re
                            new_list = []
                            for p in cur:
                                _idxp = _extract_prompt_idx(p)
                                if _idxp == idx:
                                    continue
                                new_list.append(p)
                            new_list.extend(list(new_saved))
                            st.session_state.unif_saved_paths = new_list
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
        tasks_new.append({"prompt": pval, "user_data_dir": os.path.abspath(os.path.expanduser(ud))})
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
        value=5,
        step=1,
        help="Сколько окон/профилей запускать одновременно. Для параллельного режима нужно выключить 'Оставить окна открытыми'.",
        key="unif_nbp_parallelism",
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
                "user_data_dir": os.path.abspath(os.path.expanduser((t.get("user_data_dir") or "").strip())),
            }
            for t in (st.session_state.get("unif_nbp_tasks") or [])
        ]
        url_snapshot = st.session_state.get("unif_nbp_url", DEFAULT_URLS[0])
        headless_snapshot = bool(st.session_state.get("unif_nbp_headless", False))
        exe_snapshot = st.session_state.get("unif_nbp_exe_path") or None
        keep_open_snapshot = bool(st.session_state.get("unif_nbp_keep_open", True))

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

                    p = _sp().start()
                    st.session_state.unif_nbp_keepalive.append(p)

                    for idx, item in enumerate(tasks_snapshot, 1):
                        prompt_raw = item["prompt"]
                        udir = (item.get("user_data_dir") or "").strip() or None
                        status.write(f"Окно #{idx}: профиль={udir or '(без профиля)'}")

                        if udir:
                            try:
                                os.makedirs(udir, exist_ok=True)
                            except Exception:
                                pass

                        ctx = p.chromium.launch_persistent_context(
                            user_data_dir=udir,
                            headless=headless_snapshot,
                            channel="chrome",
                            executable_path=exe_snapshot,
                            args=["--lang=ru-RU"],
                        )
                        st.session_state.unif_nbp_keepalive.append(ctx)

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

                            pre = "Change the white 10:16 ratio image using this Prompt: "
                            post = " DO NOT LEAVE BLANK WHITE SPACE, THIS IS IMPORTANT "
                            final_prompt = f"{pre}{prompt_raw}{post}"

                            _type_prompt(page, final_prompt)
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
                            prefix_len = len(str(idx)) + 1
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
                                fname = f"{idx}_{slug}_{j:02d}.{ext}"
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

                    def _run_one(task_idx: int, prompt_raw: str, udir: str | None):
                        # No Streamlit calls here.
                        local_result = {"idx": task_idx, "saved": [], "errors": []}
                        try:
                            if udir:
                                try:
                                    os.makedirs(udir, exist_ok=True)
                                except Exception:
                                    pass

                            with _sp() as p:
                                ctx = p.chromium.launch_persistent_context(
                                    user_data_dir=udir,
                                    headless=headless_snapshot,
                                    channel="chrome",
                                    executable_path=exe_snapshot,
                                    args=["--lang=ru-RU"],
                                )
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

                                    pre = "Change the white 10:16 ratio image using this Prompt: "
                                    post = " DO NOT LEAVE BLANK WHITE SPACE, THIS IS IMPORTANT "
                                    final_prompt = f"{pre}{prompt_raw}{post}"

                                    _type_prompt(page, final_prompt)
                                    _click_send(page)

                                    imgs = _wait_and_download_generated_images(page, ctx, timeout_s=150, max_images=6)
                                    if not imgs and _has_generated_images(page):
                                        imgs = _wait_and_download_generated_images(page, ctx, timeout_s=20, max_images=6)

                                    import re as _re
                                    import hashlib as _hashlib

                                    base_slug = _re.sub(r"[^a-zA-Z0-9_-]+", "_", prompt_raw)
                                    base_slug = _re.sub(r"_+", "_", base_slug).strip("_")
                                    if not base_slug:
                                        base_slug = f"prompt_{task_idx}"

                                    MAX_BASENAME = 110
                                    prefix_len = len(str(task_idx)) + 1
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
                                        fname = f"{task_idx}_{slug}_{j:02d}.{ext}"
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
                        return local_result

                    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                        futs = []
                        for idx, item in enumerate(tasks_snapshot, 1):
                            prompt_raw = item["prompt"]
                            udir = (item.get("user_data_dir") or "").strip() or None
                            futs.append(ex.submit(_run_one, idx, prompt_raw, udir))

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
                st.session_state.unif_saved_paths = list(dict.fromkeys(result["saved"]))
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
    saved_paths = st.session_state.get("unif_saved_paths", [])
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
        saved_paths = st.session_state.get("unif_saved_paths", [])
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
        "AIzaSyA-pBPECS91lPpG_TfS82i5jlRj2LPbcLU",
        "AIzaSyC3ZZlvgw67VS9bYjBuoNWeTRilzH9EpVc",
        "AIzaSyBCyPgGyGyOD2iBGwnhWY4GtGr__fJZ_sk",
        "AIzaSyCn41iq0IcG-sPV27hHZQVtNTNYDleDnFs",
        "AIzaSyAf9xq74b40OMAFx2tmjzSxIFnlDBfXmlI",
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
                        browser = p.chromium.launch_persistent_context(
                            user_data_dir=regen_user_data_dir if regen_use_auto_profile else None,
                            headless=regen_headless,
                            channel='chrome',
                            executable_path=regen_executable_path or None,
                            args=["--lang=ru-RU"],
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
                            try:
                                _click_send(page)
                            except Exception:
                                # Fallback: иногда Enter работает как send
                                try:
                                    page.keyboard.press("Enter")
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
