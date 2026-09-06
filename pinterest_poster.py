import os
import time
from typing import List, Dict, Optional

from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


def _safe_click(driver, by, selector, timeout=10) -> bool:
    try:
        el = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, selector)))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        time.sleep(0.8)
        el.click()
        return True
    except Exception:
        return False


def _try_click_publish(driver) -> bool:
    attempts = [
        (By.CSS_SELECTOR, "[data-test-id='storyboard-publish-button']"),
        (By.XPATH, "//button[.//div[contains(., 'Publish')]]"),
        (By.XPATH, "//button[contains(., 'Publish') or contains(., 'Save') or contains(., 'Опубликовать') or contains(., 'Сохранить')]"),
    ]
    for how, sel in attempts:
        if _safe_click(driver, how, sel, timeout=10):
            return True
    return False


def post_videos_to_pinterest(
    video_paths: List[str],
    meta_by_video: Dict[str, Dict[str, Optional[str]]],
    headless: bool = False,
    login_wait_sec: int = 20,
    action_delay_sec: float = 3.0,  # было 2.0
    publish: bool = False,
    debugger_address: Optional[str] = None,
    chrome_user_data_dir: Optional[str] = None,
    chrome_profile_dir: Optional[str] = None,
    keep_open: bool = False,
) -> List[Dict]:
    options = ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    if debugger_address:
        options.add_experimental_option("debuggerAddress", debugger_address)
    elif chrome_user_data_dir:
        options.add_argument(f"--user-data-dir={chrome_user_data_dir}")
        if chrome_profile_dir:
            options.add_argument(f"--profile-directory={chrome_profile_dir}")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")

    driver = Chrome(options=options)
    wait = WebDriverWait(driver, 60)

    results: List[Dict] = []
    url = "https://www.pinterest.com/pin-creation-tool/"
    try:
        driver.get(url)
        time.sleep(max(0, login_wait_sec))

        for vp in video_paths:
            res = {"video": vp, "ok": False, "error": None}
            try:
                # На каждый пин — чистый заход на страницу создания
                driver.get(url)
                time.sleep(action_delay_sec)

                # 1) Поле загрузки файла
                wait.until(EC.presence_of_element_located((By.ID, "storyboard-upload-input")))
                upload = driver.find_element(By.ID, "storyboard-upload-input")
                upload.send_keys(os.path.abspath(vp))
                time.sleep(action_delay_sec + 2.0)  # новая строка

                # 2) Ждём появления превью/кнопки редактирования (признак завершения аплоада)
                try:
                    WebDriverWait(driver, 180).until(EC.any_of(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "[data-test-id='storyboard-thumbnail'] video")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, "[data-test-id='video-cover-container-edit-cover-button']")),
                    ))
                except Exception:
                    # иногда аплоад готов, но элементы не отображены — идём дальше с задержкой
                    pass
                time.sleep(action_delay_sec + 2.0)

                meta = meta_by_video.get(vp) or {}
                title = (meta.get("title") or "").strip()
                desc = (meta.get("description") or "").strip()

                # 3) Title (опционально)
                if title:
                    try:
                        ti = driver.find_element(By.ID, "storyboard-selector-title")
                        ti.clear()
                        ti.send_keys(title[:100])
                        time.sleep(action_delay_sec)
                    except Exception:
                        pass

                # 4) Description
                if desc:
                    editor = None
                    try:
                        # стабильнее искать через контейнер с data-test-id
                        container = driver.find_element(By.CSS_SELECTOR, "[data-test-id='storyboard-description-field-container']")
                        container.click()
                        time.sleep(0.5)
                        editor = driver.find_element(By.CSS_SELECTOR, "[data-test-id='storyboard-description-field-container'] [contenteditable='true']")
                    except Exception:
                        # запасной вариант — по aria-label EN
                        try:
                            editor = driver.find_element(By.CSS_SELECTOR, "div[contenteditable='true'][aria-label='Add a detailed description']")
                        except Exception:
                            editor = None

                    if editor:
                        editor.click()
                        editor.send_keys(desc[:800])  # суммарные лимиты ~800, из них ~600 описание
                        time.sleep(action_delay_sec)

                # 6) Завершение: либо опубликовать, либо оставить как черновик
                if publish:
                    _try_click_publish(driver)
                    time.sleep(5 + action_delay_sec)
                else:
                    # даём странице применить изменения (автосейв черновика)
                    try:
                        driver.find_element(By.TAG_NAME, "body").click()
                    except Exception:
                        pass
                    time.sleep(5 + action_delay_sec)

                res["ok"] = True
            except Exception as e:
                res["error"] = str(e)
            results.append(res)

        return results
    finally:
        try:
            if not keep_open:
                driver.quit()
        except Exception:
            pass

if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Тестовый постинг в Pinterest из терминала")
    parser.add_argument("-f", "--files", nargs="+", help="Пути к медиа (mp4/jpg/png/webp)")
    parser.add_argument("-j", "--json", help="JSON с метаданными. Формат: список объектов {path, description, title} или dict {path: {description, title}}")
    parser.add_argument("-d", "--desc", help="Описание по умолчанию (если нет в JSON)")
    parser.add_argument("-t", "--title", help="Title по умолчанию (если нет в JSON)")
    parser.add_argument("--headless", action="store_true", help="Запуск без окна браузера")
    parser.add_argument("--login-wait", type=int, default=20, help="Пауза для входа в Pinterest (сек)")
    parser.add_argument("--delay", type=float, default=2.0, help="Задержка между действиями (сек)")
    parser.add_argument("--publish", action="store_true", help="Кликнуть Publish (по умолчанию — черновик)")
    parser.add_argument("--chrome-user-data-dir", help="Путь к Chrome user data dir")
    parser.add_argument("--chrome-profile-dir", default="Default", help="Имя профиля (напр. 'Default')")
    parser.add_argument("--debugger-address", help="host:port уже запущенного Chrome")
    parser.add_argument("--keep-open", action="store_true", help="Не закрывать браузер по завершении")
    args = parser.parse_args()

    media_paths = []
    if args.files:
        for p in args.files:
            ap = os.path.abspath(p)
            if os.path.exists(ap):
                media_paths.append(ap)
            else:
                print(f"Внимание: файл не найден: {p}")

    meta_by_video: Dict[str, Dict[str, Optional[str]]] = {}

    if args.json and os.path.exists(args.json):
        with open(args.json, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for item in data:
                path = item.get("path") or item.get("file") or item.get("video")
                if not path:
                    continue
                ap = os.path.abspath(path)
                desc = (item.get("description") or "").strip()
                title = (item.get("title") or None)
                meta_by_video[ap] = {"description": desc, "title": title}
                if os.path.exists(ap) and ap not in media_paths:
                    media_paths.append(ap)
        elif isinstance(data, dict):
            for path, item in data.items():
                ap = os.path.abspath(path)
                desc = (item.get("description") or "").strip()
                title = (item.get("title") or None)
                meta_by_video[ap] = {"description": desc, "title": title}
                if os.path.exists(ap) and ap not in media_paths:
                    media_paths.append(ap)

    # Фолбэки из аргументов для тех медиа, у кого нет меты
    for ap in media_paths:
        if ap not in meta_by_video:
            meta_by_video[ap] = {
                "description": (args.desc or "").strip(),
                "title": (args.title or None),
            }

    if not media_paths:
        print("Не переданы медиа-файлы. Укажите --files и/или --json.")
        raise SystemExit(2)

    print(f"Медиа к публикации: {len(media_paths)} шт. Открываю Pinterest...")
    results = post_videos_to_pinterest(
        media_paths,
        meta_by_video,
        headless=bool(args.headless),
        login_wait_sec=int(args.login_wait),
        action_delay_sec=float(args.delay),
        publish=bool(args.publish),
        debugger_address=args.debugger_address,
        chrome_user_data_dir=args.chrome_user_data_dir,
        chrome_profile_dir=args.chrome_profile_dir,
        keep_open=bool(args.keep_open),
    )
    ok = sum(1 for r in results if r.get("ok"))
    fail = [r for r in results if not r.get("ok")]
    if args.publish:
        print(f"Готово: {ok}/{len(results)} опубликовано")
    else:
        print(f"Готово: {ok}/{len(results)} черновиков заполнено")
    if fail:
        print("Ошибки:")
        for r in fail:
            print(f"- {r['video']}: {r.get('error')}")