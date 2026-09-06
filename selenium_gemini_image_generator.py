"""
Selenium Gemini Image Generator

Overview
- Opens the Gemini web interface in a real Chrome browser
- Lets you log in manually (for security and 2FA)
- Inserts your prompt into the input field
- Uploads an image file into the chat
- Submits the request to generate an answer/image

Usage
  python selenium_gemini_image_generator.py --image path/to/image.jpg --prompt "Твой промпт тут" \
      [--url https://gemini.google.com/app] [--headless] [--profile /path/to/chrome/profile]

Notes
- You must log into your Google account manually when the browser opens. The script will pause until
  you press Enter in the terminal.
- The Gemini web UI frequently changes. This script tries multiple strategies/selectors and retries.
- Requires: pip install selenium webdriver-manager

"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional, List

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementNotInteractableException
from webdriver_manager.chrome import ChromeDriverManager


DEFAULT_URLS: List[str] = [
    "https://gemini.google.com/app",
    "https://aistudio.google.com/app",
]


def _log(msg: str) -> None:
    print(f"[gemini-selenium] {msg}")


def build_driver(headless: bool = False, profile: Optional[str] = None) -> webdriver.Chrome:
    options = ChromeOptions()
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--start-maximized")
    options.add_argument("--lang=ru-RU")
    # Reduce automation flags
    options.add_experimental_option("excludeSwitches", ["enable-automation"]) 
    options.add_experimental_option("useAutomationExtension", False)

    if profile:
        # Use an existing Chrome profile to avoid repeated logins (optional)
        options.add_argument(f"--user-data-dir={profile}")

    if headless:
        options.add_argument("--headless=new")
        # Fixed window size for headless to avoid layout quirks
        options.add_argument("--window-size=1600,1200")

    from selenium.webdriver.chrome.service import Service
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    # Stealth-ish tweaks
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
    except Exception:
        pass
    return driver


def wait_for_gemini_ready(driver: webdriver.Chrome, timeout: int = 60) -> None:
    """Wait until the input area is present and interactive."""
    wait = WebDriverWait(driver, timeout)
    # Try multiple known selectors for the editable input area
    selectors = [
        # Common Quill editor container
        (By.CSS_SELECTOR, "div.ql-editor.textarea.new-input-ui[contenteditable='true']"),
        (By.CSS_SELECTOR, "div.ql-editor[contenteditable='true']"),
        # Fallback: any contenteditable text area
        (By.CSS_SELECTOR, "[contenteditable='true'][role='textbox']"),
    ]
    last_exc = None
    for by, sel in selectors:
        try:
            wait.until(EC.presence_of_element_located((by, sel)))
            wait.until(EC.element_to_be_clickable((by, sel)))
            _log(f"Input area ready using selector: {sel}")
            return
        except Exception as e:  # noqa: BLE001
            last_exc = e
    raise TimeoutException(f"Gemini input area did not appear; last error: {last_exc}")


def find_editable_input(driver: webdriver.Chrome) -> Optional[webdriver.remote.webelement.WebElement]:
    candidates = driver.find_elements(By.CSS_SELECTOR, "div.ql-editor.textarea.new-input-ui[contenteditable='true']")
    if not candidates:
        candidates = driver.find_elements(By.CSS_SELECTOR, "div.ql-editor[contenteditable='true']")
    if not candidates:
        candidates = driver.find_elements(By.CSS_SELECTOR, "[contenteditable='true'][role='textbox']")
    return candidates[0] if candidates else None


def type_prompt(driver: webdriver.Chrome, prompt: str) -> None:
    el = find_editable_input(driver)
    if el is None:
        raise RuntimeError("Не удалось найти поле для ввода промпта")

    el.click()
    # Clear just in case
    for _ in range(3):
        el.send_keys(Keys.CONTROL, 'a')
        el.send_keys(Keys.DELETE)
    el.send_keys(prompt)
    _log("Промпт вставлен в поле ввода")


def upload_image(driver: webdriver.Chrome, image_path: str, timeout: int = 30) -> None:
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"Изображение не найдено: {image_path}")

    wait = WebDriverWait(driver, timeout)

    # Strategy 1: try to find an <input type=file> and send_keys to it
    # Look for file inputs (even if hidden). We'll unhide via JS when needed.
    file_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
    for inp in file_inputs:
        try:
            driver.execute_script("arguments[0].removeAttribute('hidden'); arguments[0].style.display='block'; arguments[0].style.visibility='visible'; arguments[0].style.opacity='1'; arguments[0].style.height='1px';", inp)
            inp.send_keys(os.path.abspath(image_path))
            _log("Изображение загружено через input[type=file]")
            # Wait for thumb/attachment to appear in UI
            _wait_image_attached(driver, timeout=timeout)
            return
        except Exception:
            continue

    # Strategy 2: click the visible upload button to reveal the hidden input and retry
    upload_button_selectors = [
        # Russian aria-label from snippet
        "button[aria-label='Открыть меню загрузки файлов']",
        # English aria-label (different locales)
        "button[aria-label='Open file upload menu']",
        # Generic icon button near input
        ".upload-card-button.open",
        "button[data-test-id='hidden-local-image-upload-button']",
    ]

    for sel in upload_button_selectors:
        btns = driver.find_elements(By.CSS_SELECTOR, sel)
        if not btns:
            continue
        try:
            btns[0].click()
            time.sleep(1.0)
            # Re-scan for inputs and try again
            file_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
            for inp in file_inputs:
                try:
                    driver.execute_script("arguments[0].removeAttribute('hidden'); arguments[0].style.display='block'; arguments[0].style.visibility='visible'; arguments[0].style.opacity='1'; arguments[0].style.height='1px';", inp)
                    inp.send_keys(os.path.abspath(image_path))
                    _log("Изображение загружено через input[type=file] после клика по кнопке загрузки")
                    _wait_image_attached(driver, timeout=timeout)
                    return
                except Exception:
                    continue
        except Exception:
            continue

    raise RuntimeError("Не удалось загрузить изображение: не найден подходящий input[type=file] или кнопка загрузки")


def _wait_image_attached(driver: webdriver.Chrome, timeout: int = 30) -> None:
    wait = WebDriverWait(driver, timeout)
    # Heuristics: look for thumbnails/attachments chips right above the input area
    possible_attachment_selectors = [
        "img[alt*='attachment']",
        "img[alt*='изображение']",
        "div[class*='attachment'] img",
        "div[class*='uploader'] img",
        "div[class*='preview'] img",
    ]
    end_time = time.time() + timeout
    while time.time() < end_time:
        for sel in possible_attachment_selectors:
            if driver.find_elements(By.CSS_SELECTOR, sel):
                _log("Обнаружена миниатюра загруженного изображения")
                return
        time.sleep(0.75)
    _log("Не удалось подтвердить миниатюру изображения — продолжаем вслепую")


def submit_prompt(driver: webdriver.Chrome, timeout: int = 30) -> None:
    # Try clicking send button if enabled; else send Enter in the text area
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
            # Check disabled state heuristically
            aria_disabled = btn.get_attribute("aria-disabled")
            if aria_disabled and aria_disabled.lower() == "true":
                continue
            try:
                btn.click()
                _log("Нажата кнопка отправки")
                return
            except (ElementNotInteractableException, Exception):
                continue
        except TimeoutException:
            continue

    # Fallback: press Enter in editor
    el = find_editable_input(driver)
    if el is None:
        raise RuntimeError("Не найдено поле ввода для отправки промпта")
    el.send_keys(Keys.ENTER)
    _log("Отправка через клавишу Enter")


def wait_for_response(driver: webdriver.Chrome, timeout: int = 120) -> None:
    """Wait for some response content to appear after submission."""
    wait = WebDriverWait(driver, timeout)
    try:
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "conversation-turn, response-container, .mdc-card, div[class*='response']")) > 0)
        _log("Ответ появился (обнаружены элементы ответа)")
    except TimeoutException:
        _log("Таймаут ожидания ответа. Возможно, ответ есть, но селекторы изменились.")


def run(url: Optional[str], image: Optional[str], prompt: str, headless: bool, profile: Optional[str]) -> int:
    driver = build_driver(headless=headless, profile=profile)

    try:
        target_urls = [url] if url else []
        target_urls += [u for u in DEFAULT_URLS if u not in target_urls]

        for u in target_urls:
            _log(f"Открываю {u} ...")
            driver.get(u)
            time.sleep(2.5)

            # If not already logged in, ask the user to do so.
            _log("Пожалуйста, войдите в аккаунт Google (если требуется), затем нажмите Enter в терминале...")
            try:
                input()
            except EOFError:
                # If running non-interactive, just wait a bit
                time.sleep(10)

            try:
                wait_for_gemini_ready(driver, timeout=60)
                break
            except TimeoutException:
                _log("Не удалось найти поле ввода на этом URL; пробуем следующий...")
                continue
        else:
            _log("Не удалось загрузить интерфейс Gemini ни по одному из URL.")
            return 2

        # Upload image if provided
        if image:
            upload_image(driver, image)

        # Type prompt
        type_prompt(driver, prompt)

        # Submit
        submit_prompt(driver)

        # Wait for response
        wait_for_response(driver)
        _log("Готово. Вы можете просмотреть результат в открытом окне браузера.")
        return 0

    finally:
        if headless:
            driver.quit()
        else:
            _log("Браузер оставлен открытым. Закройте окно вручную, когда закончите.")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Automate Gemini UI to generate result with an image + prompt.")
    p.add_argument("--image", type=str, default=None, help="Путь к изображению для загрузки (опционально)")
    p.add_argument("--prompt", type=str, required=True, help="Текст запроса/промпта")
    p.add_argument("--url", type=str, default=None, help="URL интерфейса Gemini (по умолчанию пробует несколько)")
    p.add_argument("--headless", action="store_true", help="Запуск в headless-режиме (без видимого окна)")
    p.add_argument("--profile", type=str, default=None, help="Путь к профилю Chrome (для сохранения сессии)")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    try:
        code = run(url=args.url, image=args.image, prompt=args.prompt, headless=args.headless, profile=args.profile)
        sys.exit(code)
    except KeyboardInterrupt:
        _log("Остановлено пользователем")
        sys.exit(130)
    except Exception as e:
        _log(f"Ошибка: {e}")
        sys.exit(1)
