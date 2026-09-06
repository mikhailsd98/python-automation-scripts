import argparse
import os
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError, Error as PWError

# импортируем из вашего VPN-скрипта
from proxy_v_generator import (
    disconnect as vpn_disconnect,
    set_vpn_server_in_pbk as vpn_set_server,
    connect as vpn_connect,
    external_ip as vpn_external_ip,
    _looks_dns_or_host_error as vpn_is_host_err,
    VPN_NAME, USERNAME, PASSWORD,
    ensure_disconnected as vpn_ensure_disconnected,  # <-- добавить
)

# Суффикс домена для узлов VPN
HOST_SUFFIX = ".hideservers.net"

# Пулы префиксов: основной и резервный для видео
POOL_PREFIXES_WAN2 = ["103-219-21-", "23-90-179-", "154-47-19-", "193-118-55-", "193-161-246-"]

SPACE_URL = "https://zerogpu-aoti-wan2-2-fp8da-aoti-faster.hf.space"

WAIT_TIMEOUT_MS = 45000  # общий таймаут ожиданий UI (мс)

# Тексты для каждой генерации
PROMPT_TEXT = (
    "A 5-second professional, super slow establishing shot of a clean, empty place."
    "The camera slowly pans in the best direction for this image. The camera makes a single, super slow, "
    "professional pan without sudden movements, revealing the layout of the place partly."
    "The camera speed should be consistently slow throughout the video."
    "Nothing appears or moves except the camera."
)

NEGATIVE_PROMPT_TEXT = (
    "Bright tones, overexposed, static, blurred details, subtitles, style, works, paintings, images, static, "
    "overall gray, worst quality, low quality, JPEG compression residue, ugly, incomplete, extra fingers, poorly "
    "drawn hands, poorly drawn faces, deformed, disfigured, misshapen limbs, fused fingers, still picture, messy "
    "background, three legs, many people in the background, walking backwards, watermark, text, signature, humans, "
    "animals, sudden movements, unexpected objects"
)

class QuotaExceededError(Exception):
    pass

class TimeoutExceededError(Exception):
    pass

class CancelledError(Exception):
    pass

def _toast_state(page) -> Optional[tuple]:
    try:
        els = page.get_by_test_id("toast-body")
        if els.count() == 0:
            return None

        el = els.nth(els.count() - 1)
        cls = (el.get_attribute("class") or "").lower()
        txt = (el.inner_text(timeout=1000) or "").strip()
        low = txt.lower()

        # Пытаемся достать title/body (если есть)
        title = ""
        body = ""
        try:
            title = (el.locator(".toast-title").first.inner_text(timeout=500) or "").strip()
        except Exception:
            pass
        try:
            body = (el.locator(".toast-text").first.inner_text(timeout=500) or "").strip()
        except Exception:
            pass
        low_title = title.lower()
        low_body = body.lower()

        # Очередь — ждём с ограничением
        if "waiting for a gpu" in low or "zerogpu queue" in low or "waiting for gpu" in low or "queue" in low_title:
            return ("queue", title or txt)

        # Квота/недоступность GPU — сразу выходим на ротацию
        if (
            "quota" in low or "exceeded your gpu quota" in low
            or "priority queue" in low or "could not allocate a gpu" in low
            or "no available gpu" in low or "no gpu" in low
            or "quota" in low_title or "quota" in low_body
        ):
            return ("quota", title or txt)

        # Любой тост с классом error — тоже как ошибка
        if "error" in cls:
            return ("error", title or txt)
    except Exception:
        pass
    return None

def _download_output_video_with_quota(page, out_path: str, max_wait_sec: int = 900, queue_max_wait_sec: int = 300, cancel_check: Optional[callable] = None) -> Optional[str]:
    deadline = time.time() + max_wait_sec
    queue_since = None
    last_err = None
    while time.time() < deadline:
        # 1) Тосты: квота vs очередь
        st = _toast_state(page)
        if st:
            kind, txt = st
            if kind == "quota":
                raise QuotaExceededError(txt)
            if kind == "error":
                # Любая ошибка — немедленно ротация/повтор
                raise TimeoutExceededError(txt)
            if kind == "queue":
                if queue_since is None:
                    queue_since = time.time()
                else:
                    if time.time() - queue_since >= queue_max_wait_sec:
                        raise TimeoutExceededError(f"Превышено ожидание очереди ({queue_max_wait_sec} сек)")
                time.sleep(2)
                continue
            queue_since = None

        # 2) Download-кнопка
        # Cancellation check inside polling loop
        if cancel_check and cancel_check():
            raise CancelledError("Отменено")
        try:
            download_btn = page.get_by_role("link", name="Download", exact=False)
            if download_btn.count() == 0:
                download_btn = page.get_by_role("button", name="Download", exact=False)
            if download_btn.count() > 0:
                with page.expect_download(timeout=300000) as dl_info:
                    download_btn.first.click()
                download = dl_info.value
                download.save_as(out_path)
                return out_path
        except Exception as e:
            last_err = e

        # 3) <video src=...> (не blob:)
        # Cancellation check before alternate download path
        if cancel_check and cancel_check():
            raise CancelledError("Отменено")
        try:
            video = page.locator("video").first
            if video.count() > 0:
                src = video.get_attribute("src")
                if src and not src.startswith("blob:"):
                    p2 = None
                    try:
                        with page.context.expect_page() as newp:
                            page.evaluate("url => window.open(url, '_blank')", src)
                        p2 = newp.value
                        try:
                            with p2.expect_download(timeout=120000) as dl2:
                                pass
                        except Exception:
                            pass
                    finally:
                        if p2:
                            try:
                                p2.close()
                            except Exception:
                                pass
        except Exception as e:
            last_err = e

        time.sleep(2)

    # Общий дедлайн
    if last_err:
        raise TimeoutExceededError(f"Истек общий таймаут ожидания видео: {last_err}")
    raise TimeoutExceededError("Истек общий таймаут ожидания видео")

def _set_duration_to_five(page, timeout_ms: int = 60000):
    target = "5"

    # 1) Пробуем точный testid number-input
    try:
        num = page.locator("input[data-testid='number-input']").first
        if num.count() > 0:
            num.scroll_into_view_if_needed()
            num.click()
            # надёжно: выделить и ввести значение
            num.press("Control+A")
            num.type(target)
            num.press("Enter")
            # плюс явные события и blur
            num.evaluate(
                """(el, val) => {
                    el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.blur();
                }""",
                target
            )
            try:
                cur = num.input_value(timeout=1000)
                if cur.strip() == target:
                    return True
            except Exception:
                pass
    except Exception:
        pass

    # 2) Резерв: по роли spinbutton
    candidates = [
        ("spinbutton", "number input for Duration (seconds)"),
        ("spinbutton", "duration_seconds"),
    ]
    for role, name in candidates:
        try:
            sb = page.get_by_role(role, name=name)
            if sb.count() > 0:
                el = sb.first
                el.scroll_into_view_if_needed()
                el.click()
                el.press("Control+A")
                el.type(target)
                el.press("Enter")
                el.evaluate(
                    """(el, val) => {
                        el.value = val;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.blur();
                    }""",
                    target
                )
                try:
                    cur = el.input_value(timeout=1000)
                    if cur.strip() == target:
                        return True
                except Exception:
                    pass
        except Exception:
            continue

    # 3) Последний резерв: слайдер
    try:
        slider = page.locator("input[type='range'][aria-label*='Duration (seconds)']").first
        if slider.count() > 0:
            slider.scroll_into_view_if_needed()
            slider.evaluate(
                """(el, val) => {
                    el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                target
            )
            return True
    except Exception:
        pass

    return False

def _wait_app_ready(page, timeout_ms: int = WAIT_TIMEOUT_MS) -> bool:
    deadline = time.time() + (timeout_ms / 1000.0)
    last_err = None
    while time.time() < deadline:
        try:
            # 1) загрузчик файлов готов?
            if page.get_by_test_id("file-upload").first.count() == 0:
                page.wait_for_selector("input[data-testid='file-upload']", timeout=2000)

            # 2) есть кнопка Advanced Settings?
            if (
                page.locator("button:has-text('Advanced Settings')").first.count() == 0 and
                page.locator("button.label-wrap").first.count() == 0
            ):
                page.wait_for_selector("button:has-text('Advanced Settings'), button.label-wrap", timeout=2000)

            # 3) есть контрол длительности (число или слайдер)?
            if (
                page.locator("input[data-testid='number-input']").first.count() == 0 and
                page.locator("input[type='range'][aria-label*='Duration (seconds)']").first.count() == 0
            ):
                page.wait_for_selector(
                    "input[data-testid='number-input'], input[type='range'][aria-label*='Duration (seconds)']",
                    timeout=2000,
                )

            return True
        except Exception as e:
            last_err = e
        time.sleep(0.3)
    if last_err:
        raise last_err
    return False

def _open_advanced_strict(page, timeout_ms: int = 10000):
    # ждём кнопку с текстом "Advanced Settings" или .label-wrap
    btn = page.locator("button:has-text('Advanced Settings')").first
    try:
        btn.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        btn = page.locator("button.label-wrap").first
        btn.wait_for(state="visible", timeout=timeout_ms)

    # клик с force и ожидание появления "Negative Prompt"
    for _ in range(2):
        btn.scroll_into_view_if_needed()
        btn.click(force=True)
        try:
            page.get_by_text("Negative Prompt", exact=True).first.wait_for(timeout=timeout_ms // 2)
            return
        except Exception:
            time.sleep(0.2)
    # последний шанс — клик по координатам
    try:
        box = btn.bounding_box()
        if box:
            page.mouse.click(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
            page.get_by_text("Negative Prompt", exact=True).first.wait_for(timeout=timeout_ms // 2)
    except Exception:
        pass

def _nav_is_host_or_net_error(err: Exception) -> bool:
    s = str(err).lower()
    return any(x in s for x in [
        "err_name_not_resolved",
        "name not resolved",
        "enotfound",
        "dns",
        "err_connection_timed_out",
        "err_internet_disconnected",
        "err_connection_closed",
        "net::err_connection_closed",
    ])

def _set_prompt(page, text: str) -> bool:
    # По метке "Prompt" → textarea
    try:
        label = page.get_by_text("Prompt", exact=True)
        container = label.locator("xpath=ancestor::label[1]")
        ta = container.locator("textarea[data-testid='textbox']").first
        ta.fill(text)
        return True
    except Exception:
        pass
    # Фоллбек: первый textarea
    try:
        page.locator("textarea[data-testid='textbox']").first.fill(text)
        return True
    except Exception:
        return False

def _ensure_advanced_open(page) -> None:
    # Открыть "Advanced Settings", если секция скрыта
    try:
        if page.get_by_text("Negative Prompt", exact=True).count() == 0:
            btn = page.get_by_role("button", name="Advanced Settings", exact=False)
            if btn.count() == 0:
                # запасной вариант: кнопка с классом label-wrap
                btn = page.locator("button.label-wrap")
            if btn.count() > 0:
                btn.first.click()
    except Exception:
        pass

def _set_negative_prompt(page, text: str) -> bool:
    try:
        _ensure_advanced_open(page)
        label = page.get_by_text("Negative Prompt", exact=True)
        container = label.locator("xpath=ancestor::label[1]")
        ta = container.locator("textarea[data-testid='textbox']").first
        ta.fill(text)
        return True
    except Exception:
        # Фоллбек: второй textarea на странице (обычно negative)
        try:
            tas = page.locator("textarea[data-testid='textbox']")
            if tas.count() >= 2:
                tas.nth(1).fill(text)
                return True
        except Exception:
            pass
    return False

def _click_generate(page):
    # Пробуем точный текст кнопки из UI
    btn = page.get_by_role("button", name="Generate Video", exact=False)
    if btn.count() > 0:
        btn.first.scroll_into_view_if_needed()
        btn.first.click()
        return True

    # Пробуем распространённые названия кнопки
    candidates = ["Generate", "Run", "Submit", "Генерировать", "Start"]
    for text in candidates:
        btn = page.get_by_role("button", name=text, exact=False)
        if btn.count() > 0:
            btn.first.scroll_into_view_if_needed()
            btn.first.click()
            return True

    # Фоллбек: любая prominent-кнопка
    try:
        page.locator("button").first.scroll_into_view_if_needed()
        page.locator("button").first.click()
        return True
    except Exception:
        return False

def _download_output_video(page, out_path: str, max_wait_sec: int = 900) -> Optional[str]:
    # Ждём появления блока с видео или кнопки Download
    deadline = time.time() + max_wait_sec
    last_err = None
    while time.time() < deadline:
        try:
            # Если есть кнопка Download — используем событие скачивания
            download_btn = page.get_by_role("link", name="Download", exact=False)
            if download_btn.count() == 0:
                download_btn = page.get_by_role("button", name="Download", exact=False)

            if download_btn.count() > 0:
                with page.expect_download(timeout=300000) as dl_info:
                    download_btn.first.click()
                download = dl_info.value
                download.save_as(out_path)
                return out_path

            # Фоллбек: пробуем найти <video> и взять src (если не blob:)
            video = page.locator("video").first
            if video.count() > 0:
                src = video.get_attribute("src")
                if src and not src.startswith("blob:"):
                    # Открываем src в новой вкладке и скачиваем через ссылку download (если она есть)
                    with page.context.expect_page() as newp:
                        page.evaluate("url => window.open(url, '_blank')", src)
                    p2 = newp.value
                    try:
                        with p2.expect_download(timeout=120000) as dl2:
                            # иногда там сразу инициируется загрузка; если нет — не критично
                            pass
                    except PWTimeoutError:
                        pass
                    # Если загрузки не было, пробуем напрямую
                    try:
                        # через route браузер не всегда отдаёт как download; используем page.content()
                        # Проще: скопировать через fetch в браузере и вернуть blob как base64 — это усложнит код.
                        # Поэтому лучше дождаться кнопки Download.
                        pass
                    finally:
                        p2.close()
            time.sleep(2)
        except Exception as e:
            last_err = e
            time.sleep(2)
    if last_err:
        raise last_err
    return None

def generate(space_url: str, image_path: str, out_path: str, proxy: Optional[str] = None, headless: bool = True,
            max_wait_sec: int = 900, queue_max_wait_sec: int = 300, cancel_check: Optional[callable] = None):
    image_path = str(Path(image_path).resolve())
    out_path = str(Path(out_path).resolve())
    os.makedirs(Path(out_path).parent, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            proxy={"server": proxy} if proxy else None
        )
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        try:
            if cancel_check and cancel_check():
                raise CancelledError("Отменено")
            page.goto(space_url, wait_until="domcontentloaded", timeout=120000)

            _wait_app_ready(page, WAIT_TIMEOUT_MS)
            if cancel_check and cancel_check():
                raise CancelledError("Отменено")
            _open_advanced_strict(page, WAIT_TIMEOUT_MS)
            st = _toast_state(page)
            if st and st[0] in ("quota", "error"):
                raise TimeoutExceededError(st[1])
            time.sleep(0.2)

            file_input = page.get_by_test_id("file-upload").first
            file_input.set_input_files(image_path)
            if cancel_check and cancel_check():
                raise CancelledError("Отменено")
            st = _toast_state(page)
            if st and st[0] in ("quota", "error"):
                raise TimeoutExceededError(st[1])

            try:
                page.locator("div[data-testid='image'] img").first.wait_for(timeout=120000)
            except Exception:
                pass

            _set_prompt(page, PROMPT_TEXT)
            _set_negative_prompt(page, NEGATIVE_PROMPT_TEXT)
            _set_duration_to_five(page)

            if cancel_check and cancel_check():
                raise CancelledError("Отменено")
            if not _click_generate(page):
                raise RuntimeError("Не удалось найти кнопку запуска (Generate Video/Generate/Run/Submit).")
            st = _toast_state(page)
            if st and st[0] in ("quota", "error"):
                raise TimeoutExceededError(st[1])

            try:
                saved = _download_output_video_with_quota(page, out_path, max_wait_sec=max_wait_sec, queue_max_wait_sec=queue_max_wait_sec, cancel_check=cancel_check)
            except QuotaExceededError:
                raise
            except TimeoutExceededError:
                raise

            if not saved:
                raise RuntimeError("Не удалось получить итоговое видео.")

            return saved
        except (PWTimeoutError, PWError) as e:
            # Навигационный таймаут или сетевой сбой → сигнал наверх для ротации
            if isinstance(e, PWTimeoutError) or _nav_is_host_or_net_error(e):
                raise TimeoutExceededError(f"Навигация или ошибка сети: {e}")
            else:
                # прочие ошибки Playwright считаем логическими — пусть всплывают
                raise
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

def switch_vpn_to_index(
    index: int,
    retries: int = 2,
    backoff: float = 1.5,
    pause_after_connect: float = 1.0,
    pools: Optional[list[str]] = None,
    require_ip_change: bool = False,
) -> bool:
    pools_to_try = pools or POOL_PREFIXES_WAN2
    print(f"\n=== Switching VPN '{VPN_NAME}' to index {index} across pools ===")

    ip_before = None
    if require_ip_change:
        try:
            ip_before = vpn_external_ip(timeout=15)
            print("External IP (before):", ip_before)
        except Exception as e:
            print("Failed to get external IP (before):", e)

    ok = False
    last_out = ""
    for prefix in pools_to_try:
        host = f"{prefix}{index}{HOST_SUFFIX}"
        print(f"Trying host: {host}")
        for attempt in range(1, retries + 1):
            vpn_ensure_disconnected(VPN_NAME)
            vpn_set_server(VPN_NAME, host)
            ok, last_out = vpn_connect(VPN_NAME, USERNAME, PASSWORD)
            if not ok:
                if vpn_is_host_err(last_out):
                    print("Skip: host/DNS error, moving to next pool or server.")
                    break
                sleep_s = max(1.0, backoff * attempt)
                print(f"Connect failed (attempt {attempt}/{retries}). Retrying after {sleep_s:.1f}s ...")
                time.sleep(sleep_s)
                continue

            time.sleep(pause_after_connect)

            if require_ip_change:
                try:
                    ip_after = vpn_external_ip(timeout=15)
                    print("External IP (after):", ip_after)
                    if ip_before and ip_after and ip_after == ip_before:
                        print("[VPN] Warning: rasdial succeeded but external IP did NOT change; treating as failure.")
                        try:
                            import subprocess
                            rp = subprocess.run(
                                'route print -4',
                                shell=True,
                                capture_output=True,
                                text=True,
                                encoding='cp866',
                                errors='replace'
                            ).stdout
                            lines = [ln for ln in (rp or '').splitlines() if '0.0.0.0' in ln]
                            if lines:
                                print('[VPN] Default routes (route print -4):')
                                for ln in lines[:20]:
                                    print(ln)
                        except Exception:
                            pass
                        ok = False
                        time.sleep(1.0)
                        continue
                    return True
                except Exception as e:
                    print("Failed to get external IP (after):", e)
                    ok = False
                    time.sleep(1.0)
                    continue

            # Non-strict mode: consider rasdial success as success.
            try:
                ip = vpn_external_ip(timeout=15)
                print("External IP:", ip)
            except Exception as e:
                print("Failed to get external IP:", e)
            return True

    print("Failed to connect to any pool for index", index)
    return False

# NEW: использовать готовые хосты из файла
def load_hosts_file(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"{path} not found")
    hosts: list[str] = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # допускаем форматы "host", "host<tab>ip", "host ip"
        host = s.split()[0]
        hosts.append(host)
    # убрать дубликаты, сохранив порядок
    seen = set()
    out = []
    for h in hosts:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out

def switch_vpn_to_full_host(
    host: str,
    retries: int = 2,
    backoff: float = 1.5,
    pause_after_connect: float = 1.0,
    require_ip_change: bool = False,
) -> bool:
    print(f"\n=== Switching VPN '{VPN_NAME}' to host {host} ===")

    ip_before = None
    if require_ip_change:
        # Diagnostics for strict mode
        try:
            ip_before = vpn_external_ip(timeout=15)
            print("External IP (before):", ip_before)
        except Exception as e:
            print("Failed to get external IP (before):", e)

    ok = False
    last_out = ""
    for attempt in range(1, retries + 1):
        vpn_ensure_disconnected(VPN_NAME)
        vpn_set_server(VPN_NAME, host)
        ok, last_out = vpn_connect(VPN_NAME, USERNAME, PASSWORD)
        if not ok:
            if vpn_is_host_err(last_out):
                print("Skip: host/DNS error.")
                break
            sleep_s = max(1.0, backoff * attempt)
            print(f"Connect failed (attempt {attempt}/{retries}). Retrying after {sleep_s:.1f}s ...")
            time.sleep(sleep_s)
            continue

        # rasdial сказал OK
        time.sleep(pause_after_connect)

        # Strict mode: require external IP change
        if require_ip_change:
            try:
                ip_after = vpn_external_ip(timeout=15)
                print("External IP (after):", ip_after)
                if ip_before and ip_after and ip_after == ip_before:
                    print("[VPN] Warning: rasdial succeeded but external IP did NOT change; treating as failure.")
                    # Extra diagnostics: default routes
                    try:
                        import subprocess
                        rp = subprocess.run(
                            'route print -4',
                            shell=True,
                            capture_output=True,
                            text=True,
                            encoding='cp866',
                            errors='replace'
                        ).stdout
                        lines = [ln for ln in (rp or '').splitlines() if '0.0.0.0' in ln]
                        if lines:
                            print('[VPN] Default routes (route print -4):')
                            for ln in lines[:20]:
                                print(ln)
                    except Exception:
                        pass
                    ok = False
                    time.sleep(1.0)
                    continue
                return True
            except Exception as e:
                print("Failed to get external IP (after):", e)
                ok = False
                time.sleep(1.0)
                continue

        # Non-strict mode (working behavior): consider rasdial success as success.
        try:
            ip = vpn_external_ip(timeout=15)
            print("External IP:", ip)
        except Exception as e:
            print("Failed to get external IP:", e)
        return True

    print("Failed to connect to", host)
    return False

def main():
    ap = argparse.ArgumentParser("HF Space UI automation (Playwright)")
    ap.add_argument("--image", "-i", required=True, nargs="+", help="Путь(и) к изображению(ям)")
    ap.add_argument("--out", "-o", default=None, help="Путь для сохранения видео (mp4) — если один вход")
    ap.add_argument("--out-dir", default="outputs", help="Папка для сохранения, если изображений больше одного")
    ap.add_argument("--space", default=SPACE_URL, help="URL Space")
    ap.add_argument("--proxy", default=None, help="proxy, например http://user:pass@host:port")
    ap.add_argument("--headless", action="store_true", help="Запуск без окна браузера")

    # Ротация VPN
    ap.add_argument("--vpn-rotate", action="store_true", help="Включить ротацию VPN между генерациями")
    ap.add_argument("--vpn-start", type=int, default=1, help="Начальный индекс VPN (по умолчанию 1)")
    ap.add_argument("--vpn-end", type=int, default=250, help="Конечный индекс VPN (по умолчанию 250)")
    ap.add_argument("--vpn-retries", type=int, default=2, help="Повторы на один узел при ошибках")
    ap.add_argument("--vpn-backoff", type=float, default=1.5, help="Бэкофф повтора (сек)")
    ap.add_argument("--vpn-pause", type=float, default=1.0, help="Пауза после подключения (сек)")

    # NEW: список рабочих хостов для видео
    ap.add_argument("--vpn-hosts-file", type=str, default="good_hosts_for_videos.txt", help="TXT с готовыми VPN-хостами (по одному в строке)")

    args = ap.parse_args()

    images = [str(Path(p).resolve()) for p in args.image]

    # NEW: загрузка готовых хостов (если включена ротация)
    video_hosts = load_hosts_file(args.vpn_hosts_file) if args.vpn_rotate else []
    vh_pos = 0  # позиция в списке для многофайлового режима

    if len(images) == 1:
        if not args.out:
            raise SystemExit("--out обязателен при одном входном изображении")
        out_path = str(Path(args.out).resolve())
        os.makedirs(Path(out_path).parent, exist_ok=True)

        if not args.vpn_rotate:
            saved = generate(args.space, images[0], out_path, proxy=args.proxy, headless=args.headless)
            print(f"Saved video to: {saved}")
            return

        # c ротацией при квоте: используем список готовых хостов
        hosts = video_hosts
        if not hosts:
            raise SystemExit(f"В файле {args.vpn_hosts_file} нет хостов")
        pos = 0
        attempts = 0
        max_attempts = len(hosts)

        while attempts < max_attempts:
            try:
                saved = generate(args.space, images[0], out_path, proxy=args.proxy, headless=args.headless)
                print(f"Saved video to: {saved}")
                return
            except (QuotaExceededError, TimeoutExceededError) as e:
                print(f"Rotate reason: {e}")
                rotated = False
                tried = 0
                while tried < len(hosts):
                    host = hosts[pos % len(hosts)]
                    print(f"Rotating VPN to host: {host}")
                    if switch_vpn_to_full_host(
                        host,
                        retries=args.vpn_retries,
                        backoff=args.vpn_backoff,
                        pause_after_connect=args.vpn_pause
                    ):
                        rotated = True
                        pos = (pos + 1) % len(hosts)
                        break
                    pos = (pos + 1) % len(hosts)
                    tried += 1
                if not rotated:
                    raise SystemExit("Не удалось переключить VPN на доступный хост из списка.")
                attempts += 1
        raise SystemExit("Исчерпаны попытки после ротации VPN по списку хостов.")
    # Несколько входов → пишем в папку
    out_dir = str(Path(args.out_dir).resolve())
    os.makedirs(out_dir, exist_ok=True)

    for k, img in enumerate(images):
        out_path = str(Path(out_dir) / (Path(img).stem + ".mp4"))

        while True:
            try:
                saved = generate(args.space, img, out_path, proxy=args.proxy, headless=args.headless)
                print(f"[{k+1}/{len(images)}] Saved video to: {saved}")
                break
            except (QuotaExceededError, TimeoutExceededError) as e:
                print(f"[{k+1}/{len(images)}] Rotate reason: {e}")
                # Ротация VPN по списку хостов и повтор этого же изображения
                hosts = video_hosts
                if not hosts:
                    raise SystemExit(f"В файле {args.vpn_hosts_file} нет хостов")
                rotated = False
                tried = 0
                while tried < len(hosts):
                    host = hosts[vh_pos % len(hosts)]
                    print(f"Rotating VPN to host: {host}")
                    if switch_vpn_to_full_host(
                        host,
                        retries=args.vpn_retries,
                        backoff=args.vpn_backoff,
                        pause_after_connect=args.vpn_pause
                    ):
                        rotated = True
                        vh_pos = (vh_pos + 1) % len(hosts)
                        break
                    vh_pos = (vh_pos + 1) % len(hosts)
                    tried += 1
                if not rotated:
                    raise SystemExit("Не удалось переключить VPN ни на один хост из списка.")
                continue

        # Плановая ротация между заданиями (после успешной генерации)
        if args.vpn_rotate and (k + 1) < len(images):
            hosts = video_hosts
            if hosts:
                tried = 0
                rotated = False
                while tried < len(hosts):
                    host = hosts[vh_pos % len(hosts)]
                    print(f"Planned rotate to host: {host}")
                    if switch_vpn_to_full_host(
                        host,
                        retries=args.vpn_retries,
                        backoff=args.vpn_backoff,
                        pause_after_connect=args.vpn_pause
                    ):
                        rotated = True
                        vh_pos = (vh_pos + 1) % len(hosts)
                        break
                    vh_pos = (vh_pos + 1) % len(hosts)
                    tried += 1
                if not rotated:
                    print("Предупреждение: не удалось переключить VPN ни на один хост из списка.")
            else:
                print("Предупреждение: список хостов пуст — пропускаю плановую ротацию.")

if __name__ == "__main__":
    main()