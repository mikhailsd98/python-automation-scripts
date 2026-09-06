# Streamlit + Selenium: Gemini UI image generation (no API)
# Usage: streamlit run gemini_selenium_streamlit.py

import os
import io
import time
import base64
import tempfile
from typing import List, Optional, Tuple

import streamlit as st

# Selenium imports
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementNotInteractableException
from webdriver_manager.chrome import ChromeDriverManager


def _log(msg: str):
    st.write(f"[selenium] {msg}")


def _build_driver(headless: bool = False, profile: Optional[str] = None, profile_dir: Optional[str] = None, chrome_binary: Optional[str] = None, yandex: bool = False, yandex_binary: Optional[str] = None, driver_path: Optional[str] = None) -> webdriver.Chrome:
    options = ChromeOptions()
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--start-maximized")
    options.add_argument("--lang=ru-RU")
    options.add_experimental_option("excludeSwitches", ["enable-automation"]) 
    options.add_experimental_option("useAutomationExtension", False)

    # Выбор бинарника: если yandex=True — используем yandex_binary, иначе chrome_binary
    if yandex:
        if yandex_binary:
            options.binary_location = yandex_binary
    else:
        if chrome_binary:
            options.binary_location = chrome_binary

    if profile:
        options.add_argument(f"--user-data-dir={profile}")
    if profile_dir:
        options.add_argument(f"--profile-directory={profile_dir}")

    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1600,1200")

    from selenium.webdriver.chrome.service import Service
    # Если указан прямой путь к драйверу — используем его, иначе webdriver-manager
    if driver_path:
        service = Service(executable_path=driver_path)
    else:
        service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
    except Exception:
        pass
    return driver


def _wait_for_input_area(driver: webdriver.Chrome, timeout: int = 60) -> None:
    wait = WebDriverWait(driver, timeout)
    selectors = [
        (By.CSS_SELECTOR, "div.ql-editor.textarea.new-input-ui[contenteditable='true']"),
        (By.CSS_SELECTOR, "div.ql-editor[contenteditable='true']"),
        (By.CSS_SELECTOR, "[contenteditable='true'][role='textbox']"),
    ]
    last = None
    for by, sel in selectors:
        try:
            wait.until(EC.presence_of_element_located((by, sel)))
            wait.until(EC.element_to_be_clickable((by, sel)))
            return
        except Exception as e:  # noqa: BLE001
            last = e
    raise TimeoutException(f"Не найдено поле ввода. Последняя ошибка: {last}")


def _find_editor(driver: webdriver.Chrome):
    els = driver.find_elements(By.CSS_SELECTOR, "div.ql-editor.textarea.new-input-ui[contenteditable='true']")
    if not els:
        els = driver.find_elements(By.CSS_SELECTOR, "div.ql-editor[contenteditable='true']")
    if not els:
        els = driver.find_elements(By.CSS_SELECTOR, "[contenteditable='true'][role='textbox']")
    return els[0] if els else None


def _type_prompt(driver: webdriver.Chrome, prompt: str):
    """Insert prompt into Gemini editor.

    IMPORTANT: sending very long strings via send_keys() into contenteditable (Quill) can drop chunks.
    Prefer DOM assignment + input events.
    """
    el = _find_editor(driver)
    if el is None:
        raise RuntimeError("Поле ввода не найдено")

    try:
        el.click()
    except Exception:
        pass

    # Clear existing
    try:
        for _ in range(2):
            el.send_keys(Keys.CONTROL, 'a')
            el.send_keys(Keys.DELETE)
    except Exception:
        pass

    # Best effort: DOM set for contenteditable
    try:
        driver.execute_script(
            """
            const el = arguments[0];
            const text = arguments[1];
            try { el.focus(); } catch(e) {}
            try { el.textContent = text; } catch(e) { try { el.innerText = text; } catch(e2) {} }
            try { el.dispatchEvent(new InputEvent('input', { bubbles: true })); } catch(e) {
              try { const ev = document.createEvent('Event'); ev.initEvent('input', true, true); el.dispatchEvent(ev); } catch(e2) {}
            }
            try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch(e) {}
            """,
            el,
            prompt or "",
        )
        return
    except Exception:
        # Fallback to send_keys (may truncate on huge prompts, but better than failing)
        el.send_keys(prompt)


def _upload_image(driver: webdriver.Chrome, image_path: str, timeout: int = 30):
    if not os.path.isfile(image_path):
        raise FileNotFoundError(image_path)
    wait = WebDriverWait(driver, timeout)

    # Try direct input[type=file]
    inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
    for inp in inputs:
        try:
            driver.execute_script("arguments[0].removeAttribute('hidden'); arguments[0].style.display='block'; arguments[0].style.visibility='visible'; arguments[0].style.opacity='1'; arguments[0].style.height='1px';", inp)
            inp.send_keys(os.path.abspath(image_path))
            _wait_image_attached(driver, timeout=timeout)
            return
        except Exception:
            pass

    # Try clicking upload button
    upload_btn_sels = [
        "button[aria-label='Открыть меню загрузки файлов']",
        "button[aria-label='Open file upload menu']",
        ".upload-card-button.open",
        "button[data-test-id='hidden-local-image-upload-button']",
    ]
    for sel in upload_btn_sels:
        btns = driver.find_elements(By.CSS_SELECTOR, sel)
        if not btns:
            continue
        try:
            btns[0].click()
            time.sleep(1)
            # retry inputs
            inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
            for inp in inputs:
                try:
                    driver.execute_script("arguments[0].removeAttribute('hidden'); arguments[0].style.display='block'; arguments[0].style.visibility='visible'; arguments[0].style.opacity='1'; arguments[0].style.height='1px';", inp)
                    inp.send_keys(os.path.abspath(image_path))
                    _wait_image_attached(driver, timeout=timeout)
                    return
                except Exception:
                    pass
        except Exception:
            pass
    raise RuntimeError("Не удалось загрузить изображение в Gemini UI")


def _wait_image_attached(driver: webdriver.Chrome, timeout: int = 30):
    end = time.time() + timeout
    sels = [
        "img[alt*='attachment']",
        "div[class*='attachment'] img",
        "div[class*='preview'] img",
        "img",
    ]
    while time.time() < end:
        for sel in sels:
            if driver.find_elements(By.CSS_SELECTOR, sel):
                return
        time.sleep(0.5)


def _click_send(driver: webdriver.Chrome, timeout: int = 20):
    wait = WebDriverWait(driver, timeout)
    candidates = [
        (By.CSS_SELECTOR, "button[aria-label='Отправить сообщение']"),
        (By.CSS_SELECTOR, "button[aria-label='Send message']"),
        (By.CSS_SELECTOR, "button .mat-icon[fonticon='send']"),
        (By.CSS_SELECTOR, ".send-button"),
    ]
    for by, sel in candidates:
        try:
            btn = wait.until(EC.presence_of_element_located((by, sel)))
            if btn.get_attribute("aria-disabled") == "true":
                continue
            try:
                btn.click()
                return
            except Exception:
                continue
        except TimeoutException:
            continue
    # Fallback: Enter in editor
    el = _find_editor(driver)
    if el is None:
        raise RuntimeError("Не удалось отправить запрос: не найдено поле редактора")
    el.send_keys(Keys.ENTER)


ess_response_sels = [
    "conversation-turn",
    "response-container",
    ".mdc-card",
    "div[class*='response']",
]


def _wait_for_new_response(driver: webdriver.Chrome, old_count: int, timeout: int = 120):
    end = time.time() + timeout
    while time.time() < end:
        count = 0
        for sel in ess_response_sels:
            count += len(driver.find_elements(By.CSS_SELECTOR, sel))
        if count > old_count:
            return
        time.sleep(0.75)


def _collect_images_from_last_turn(driver: webdriver.Chrome, max_images: int = 4) -> List[Tuple[str, bytes]]:
    # Find last response-like container
    containers = []
    for sel in ess_response_sels:
        containers.extend(driver.find_elements(By.CSS_SELECTOR, sel))
    if not containers:
        return []
    last = containers[-1]

    imgs = last.find_elements(By.CSS_SELECTOR, "img")
    results: List[Tuple[str, bytes]] = []
    seen = 0
    for im in imgs:
        if seen >= max_images:
            break
        try:
            src = im.get_attribute("src") or ""
            if src.startswith("data:image/"):
                # Inline data URL, decode
                header, b64 = src.split(",", 1)
                mime = header.split(":", 1)[1].split(";")[0]
                data = base64.b64decode(b64)
                results.append((mime, data))
                seen += 1
            elif src.startswith("http"):
                # Cross-origin and needs auth; fallback to element screenshot
                data = im.screenshot_as_png
                results.append(("image/png", data))
                seen += 1
            else:
                # blob: or empty -> fallback to screenshot
                data = im.screenshot_as_png
                results.append(("image/png", data))
                seen += 1
        except Exception:
            # fallback to screenshot
            try:
                data = im.screenshot_as_png
                results.append(("image/png", data))
                seen += 1
            except Exception:
                pass
    return results


st.set_page_config(page_title="Gemini (Selenium UI)", layout="wide")
st.title("Gemini генерация через браузер (Selenium)")

DEFAULT_URLS = [
    "https://gemini.google.com/app",
    "https://aistudio.google.com/app",
    "https://aistudio.google.com/prompts/new_chat?model=gemini-2.5-flash-image",
]
BASE_IMAGE_PATH = "10x16.jpg"  # та самая белая картинка, как в API‑версии

# Controls
url = st.selectbox("URL интерфейса", DEFAULT_URLS, index=0)
headless = st.checkbox("Headless режим (без окна браузера)", value=False)
use_auto_profile = st.checkbox("Использовать отдельный профиль для автоматики (рекомендуется)", value=True, help="Создаёт/использует профиль в папке проекта ./.chrome_automation_profile — не конфликтует с вашим основным Chrome")
profile = st.text_input("Путь к профилю Chrome (user-data-dir), например C:/Users/USER/AppData/Local/Google/Chrome/User Data", value=r"C:\Users\Пользователь\AppData\Local\Google\Chrome\User Data")
profile_dir = st.text_input("Имя каталога профиля (profile-directory), например Default, Profile 1, Profile 2", value="Default")
use_yandex = st.checkbox("Открывать в Yandex Browser", value=True)
chrome_binary = st.text_input("Путь к исполняемому файлу Chrome (опционально)")
yandex_binary = st.text_input("Путь к исполняемому файлу Yandex Browser (обычно C:/Users/USER/AppData/Local/Yandex/YandexBrowser/Application/browser.exe)", value=r"C:\Users\Пользователь\AppData\Local\Yandex\YandexBrowser\Application\browser.exe")
driver_path = st.text_input("Путь к chromedriver (или yandexdriver) (опционально)")

# Динамические промпты (как в API скрипте)
if "prompts" not in st.session_state:
    st.session_state.prompts = [""]

st.markdown("**Промпты**")
new_prompts = []
for i, val in enumerate(st.session_state.prompts):
    new_val = st.text_input(f"Промпт #{i+1}", value=val, key=f"prompt_{i}")
    new_prompts.append(new_val)

col_add, col_rem = st.columns([1, 1])
with col_add:
    if st.button("+ Добавить поле"):
        st.session_state.prompts.append("")
        st.rerun()
with col_rem:
    if len(st.session_state.prompts) > 1 and st.button("− Убрать последнее поле"):
        st.session_state.prompts = st.session_state.prompts[:-1]
        st.rerun()

# Обновляем список промптов из полей ввода
st.session_state.prompts = new_prompts

# Показываем белую картинку в сайдбаре, как в API‑версии
with st.sidebar:
    st.markdown("### Базовое изображение (белое)")
    if os.path.exists(BASE_IMAGE_PATH):
        st.image(BASE_IMAGE_PATH, caption=BASE_IMAGE_PATH, use_container_width=True)
    else:
        st.error(f"Базовый файл не найден: {BASE_IMAGE_PATH}")
    st.caption("Эта картинка будет автоматически загружаться перед каждым промптом.")

colA, colB = st.columns([1,1])
with colA:
    open_btn = st.button("1) Открыть Gemini", type="secondary")
with colB:
    go_btn = st.button("2) Сгенерировать все промпты", type="primary")

if "driver" not in st.session_state:
    st.session_state.driver = None
    st.session_state.opened = False
    st.session_state.prev_resp_count = 0

# Step 1: Open
if open_btn:
    try:
        if st.session_state.driver is None:
            # Выбор профиля: отдельный автоматический профиль в папке проекта или пользовательский
            final_user_data_dir = profile or None
            final_profile_dir = profile_dir or None
            if use_auto_profile:
                final_user_data_dir = os.path.abspath(".chrome_automation_profile")
                final_profile_dir = None  # пусть Chrome создаст профиль по умолчанию внутри этого каталога
                os.makedirs(final_user_data_dir, exist_ok=True)
            st.session_state.driver = _build_driver(headless=headless, profile=final_user_data_dir, profile_dir=final_profile_dir, chrome_binary=chrome_binary or None, yandex=use_yandex, yandex_binary=yandex_binary or None, driver_path=driver_path or None)
        drv = st.session_state.driver
        drv.get(url)
        time.sleep(2)
        st.session_state.opened = True
        # Try to detect input area early (if already logged in)
        try:
            _wait_for_input_area(drv, timeout=10)
            st.success("Поле ввода найдено. Можно сразу нажимать '2) Сгенерировать все промпты'.")
        except Exception:
            st.info("Если требуется — войдите в аккаунт Google в открывшемся окне, затем нажмите '2) Сгенерировать все промпты'.")
        # Count existing response containers
        cnt = 0
        for sel in ess_response_sels:
            cnt += len(drv.find_elements(By.CSS_SELECTOR, sel))
        st.session_state.prev_resp_count = cnt
    except Exception as e:
        st.error(f"Ошибка при открытии: {e}")

# Step 2: Automate and fetch for each prompt
if go_btn:
    if st.session_state.driver is None:
        st.warning("Сначала нажмите '1) Открыть Gemini'.")
    else:
        drv = st.session_state.driver
        try:
            _wait_for_input_area(drv, timeout=60)

            # Собираем финальные промпты с пре- и постамбулой как в API-версии
            pre = "Change the white image using this Prompt: "
            post = " DO NOT LEAVE BLANK WHITE SPACE, THIS IS IMPORTANT "
            final_prompts = [f"{pre}{(p or '').strip()}{post}" for p in st.session_state.prompts if (p or '').strip()]

            results = []  # (prompt_text, [(mime, bytes)], error)
            for idx, fp in enumerate(final_prompts, 1):
                # Всегда загружаем белую картинку перед отправкой каждого промпта
                try:
                    _upload_image(drv, BASE_IMAGE_PATH)
                    _log(f"[{idx}/{len(final_prompts)}] Белая картинка загружена")
                except Exception as e:
                    _log(f"Не удалось загрузить белую картинку: {e}")

                # Вводим промпт и отправляем
                _type_prompt(drv, fp)

                # Засекаем старое число ответов, отправляем
                old = 0
                for sel in ess_response_sels:
                    old += len(drv.find_elements(By.CSS_SELECTOR, sel))
                _click_send(drv)

                # Ждем новый ответ
                try:
                    _wait_for_new_response(drv, old_count=old, timeout=180)
                except Exception:
                    pass

                # Собираем изображения из последнего ответа
                imgs = _collect_images_from_last_turn(drv, max_images=6)
                if not imgs:
                    results.append((fp, [], "В ответе не найдены изображения (возможно, текстовый ответ)"))
                else:
                    results.append((fp, imgs, None))

            # Выводим результаты
            st.markdown("---")
            for i, (pt, images, err) in enumerate(results, 1):
                with st.expander(f"Промпт #{i}", expanded=True):
                    st.code(pt)
                    if images:
                        cols = st.columns(3)
                        for j, (mime, blob) in enumerate(images):
                            with cols[j % 3]:
                                st.image(io.BytesIO(blob), caption=f"Image {j+1}")
                                fname = f"gemini_ui_{i}_{j+1}." + ("png" if mime == "image/png" else ("jpg" if mime == "image/jpeg" else "bin"))
                                st.download_button("Скачать", data=blob, file_name=fname, mime=mime or "application/octet-stream")
                    else:
                        st.warning("Не удалось получить изображение для этого промпта.")
                        if err:
                            st.caption(f"Ошибка: {err}")
        except Exception as e:
            st.error(f"Ошибка автоматизации: {e}")

st.markdown("---")
st.caption("Как в API‑версии: промпты с "+" и белая картинка 10x16.jpg отправляются автоматически. Укажите профиль Chrome, чтобы не логиниться каждый раз.")
