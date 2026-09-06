import os
import sys
import json
import time
import base64
import signal
import mimetypes
import requests
import random
from collections import deque
from typing import Optional
from datetime import datetime
from proxy_v_generator import (
    ensure_disconnected,
    VPN_NAME,
    set_vpn_server_in_pbk as vpn_set_server,
    connect as vpn_connect,
    is_connected as vpn_is_connected,
    external_ip as vpn_external_ip,
    USERNAME,
    PASSWORD,
)  # <-- добавлено и расширено для управления VPN

# --- ГРЕЙСФУЛ ШАТДАУН ---
shutdown_requested = False

# Last safety block reason from Gemini (if any)
LAST_BLOCK_REASON: str | None = None

def handle_shutdown_signal(signum, frame):
    global shutdown_requested
    if not shutdown_requested:
        print("\nСигнал остановки получен. Завершаю текущую задачу...")
        shutdown_requested = True
    else:
        print("\nПовторный сигнал. Принудительный выход.")
        sys.exit(1)

def setup_signals():
    try:
        signal.signal(signal.SIGINT, handle_shutdown_signal)
        signal.signal(signal.SIGTERM, handle_shutdown_signal)
    except ValueError:
        # Не в главном потоке (Streamlit) — пропускаем
        pass

# --- КОНФИГ ДЛЯ GEMINI (адаптировано из fetch_tmdb_movies.py) ---
GEMINI_API_KEYS = [
    # "AIzaSyDRim4WhYM2iXlf7sYTaIjWJ_1kx9Q62dQ",
    "AIzaSyBCyPgGyGyOD2iBGwnhWY4GtGr__fJZ_sk",
    "AIzaSyCn41iq0IcG-sPV27hHZQVtNTNYDleDnFs",
    "AIzaSyA-pBPECS91lPpG_TfS82i5jlRj2LPbcLU",
    "AIzaSyC3ZZlvgw67VS9bYjBuoNWeTRilzH9EpVc",
    "AIzaSyAf9xq74b40OMAFx2tmjzSxIFnlDBfXmlI",
    # "AIzaSyAmD3Nv6WcdBK3aoLAlARcQsvqv-RqTSCo",
    # "AIzaSyDSvSIUZooqz746y6CVA7IoGjFrDWyj5L4",
    # "AIzaSyCI0qt3OOliBaM_QOztawFqmBMo5AGw_kY",
    # "AIzaSyCrgDaMYgIZG-SKxJTJ1ShoE1YaG3mwMSw",
    # "AIzaSyDJKKtMCmM-_YOsWZ-p2MMfwRwtwOyMXvI"
]
current_gemini_key_index = 0

MODELS = [
    # "models/gemini-3-flash",
    "models/gemini-3.1-flash-lite-preview",
    # "models/gemini-3-flash-preview",
    "models/gemini-2.5-flash-lite",
    # "models/gemini-2.5-pro",
    # "models/gemini-3-pro",
    # "models/gemini-2.0-flash",
    # "models/gemini-2.0-flash-lite",
]
current_model_index = 0

RATE_LIMITS = {
    # "models/gemini-3-flash": 30,
    "models/gemini-3.1-flash-lite-preview": 30,
    # "models/gemini-3-flash-preview": 30,
    # "models/gemini-2.5-pro": 30,
    # "models/gemini-3-pro": 30,
    # "models/gemini-2.0-flash": 30,
    "models/gemini-2.5-flash-lite": 30,
}
rate_windows = {idx: deque() for idx in range(len(MODELS))}
last_429_at = {idx: 0.0 for idx in range(len(MODELS))}

# --- ДОП. ЗАДЕРЖКА МЕЖДУ ЗАПРОСАМИ (anti-bot jitter) ---
# Чтобы снизить шанс блокировок из-за слишком ровного/частого трафика.
# Можно переопределить через env: GEMINI_MIN_DELAY_SEC / GEMINI_MAX_DELAY_SEC
MIN_REQUEST_DELAY_SEC = float(os.getenv("GEMINI_MIN_DELAY_SEC", "3.8"))
MAX_REQUEST_DELAY_SEC = float(os.getenv("GEMINI_MAX_DELAY_SEC", "7.8"))


def _sleep_jitter_before_request():
    """Небольшая рандомная пауза перед каждым реальным запросом."""
    if MAX_REQUEST_DELAY_SEC <= 0:
        return
    lo = max(0.0, MIN_REQUEST_DELAY_SEC)
    hi = max(lo, float(MAX_REQUEST_DELAY_SEC))
    delay = random.uniform(lo, hi)
    if delay > 0:
        print(f"  - Jitter-пауза перед запросом: {delay:.2f}с")
        time.sleep(delay)

def get_current_url():
    model = MODELS[current_model_index]
    current_key = GEMINI_API_KEYS[current_gemini_key_index]
    return f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent?key={current_key}"

def wait_for_rate_slot(model_idx: int):
    name = MODELS[model_idx]
    rpm = RATE_LIMITS.get(name, 15)
    dq = rate_windows[model_idx]
    while True:
        now = time.time()
        while dq and (now - dq[0]) >= 60:
            dq.popleft()
        if len(dq) < rpm:
            dq.append(now)
            return
        sleep_for = max(1, int(60 - (now - dq[0]) + 1))
        print(f"  - Достигнут минутный лимит ({rpm} RPM) для {name}. Пауза {sleep_for}с...")
        time.sleep(sleep_for)

# --- УТИЛИТЫ ДЛЯ ИЗОБРАЖЕНИЙ ---
def read_image_inline_data(image_path: str) -> dict:
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Файл не найден: {image_path}")
    mime, _ = mimetypes.guess_type(image_path)
    if not mime:
        # по умолчанию
        ext = (os.path.splitext(image_path)[1] or '').lower()
        if ext in ('.jpg', '.jpeg'):
            mime = 'image/jpeg'
        elif ext == '.png':
            mime = 'image/png'
        elif ext == '.webp':
            mime = 'image/webp'
        else:
            mime = 'application/octet-stream'
    with open(image_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('utf-8')
    return {"mimeType": mime, "data": b64}

# --- БАЗОВЫЙ ВЫЗОВ GEMINI С КАРТИНКОЙ И РОТАЦИЕЙ ---

# VPN rotation state (for Gemini reachability)
_VPN_HOSTS_CACHE: list[str] | None = None
_VPN_HOST_POS: int = 0


def _load_hosts_file(path: str) -> list[str]:
    hosts: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                h = (ln or "").strip()
                if h and not h.startswith("#"):
                    hosts.append(h)
    except Exception:
        return []
    return hosts


def _vpn_hosts_for_gemini() -> list[str]:
    """Load VPN hosts list once and keep order stable."""
    global _VPN_HOSTS_CACHE

    hosts_file = os.getenv("GEMINI_VPN_HOSTS_FILE", "good_hosts_for_images.txt").strip()
    hosts = _load_hosts_file(hosts_file) if hosts_file else []

    # Optional single-host override (first priority)
    single = os.getenv("GEMINI_VPN_HOST", "").strip()
    if single:
        hosts = [single] + [h for h in hosts if h != single]

    if not hosts:
        raise RuntimeError(
            f"VPN is not connected and hosts list is empty (file={hosts_file}). "
            "Gemini request is blocked to avoid using non-VPN IP."
        )

    # Cache only when it matches current env selection; simplest approach: always refresh if env points elsewhere
    _VPN_HOSTS_CACHE = hosts
    return hosts


def _force_rotate_vpn_for_gemini(reason: str = "") -> None:
    """Disconnect and switch VPN to the next host from the list.

    This is used when current VPN is connected but Gemini calls keep failing in a way
    that looks like VPN-level blocking.
    """
    global _VPN_HOST_POS

    hosts = _vpn_hosts_for_gemini()
    if not hosts:
        raise RuntimeError("VPN hosts list is empty; cannot rotate.")

    msg = f"[VPN] rotate requested: {reason}" if reason else "[VPN] rotate requested"
    print(msg)

    # Always disconnect first (we want a new path)
    try:
        ensure_disconnected(VPN_NAME, wait_sec=6.0)
    except Exception as e:
        print(f"[VPN] disconnect error (ignored): {e}")

    last_err: Exception | None = None
    for step in range(len(hosts)):
        host = hosts[_VPN_HOST_POS % len(hosts)]
        print(f"[VPN] rotating to host [{_VPN_HOST_POS % len(hosts) + 1}/{len(hosts)}]: {host}")
        _VPN_HOST_POS = (_VPN_HOST_POS + 1) % len(hosts)
        try:
            vpn_set_server(VPN_NAME, host)
            ok, out = vpn_connect(VPN_NAME, USERNAME, PASSWORD)
            if not ok:
                last_err = RuntimeError((out or "").strip() or "rasdial failed")
                time.sleep(1.0)
                continue

            # Validate route to Gemini APIs
            t0 = time.time()
            while time.time() - t0 < 20.0:
                try:
                    r = requests.get("https://generativelanguage.googleapis.com/", timeout=8)
                    if r is not None:
                        print("[VPN] rotate result: SUCCESS (Gemini reachable)")
                        time.sleep(1.0)
                        return
                except Exception:
                    pass
                time.sleep(2.0)

            last_err = RuntimeError(f"Connected to {host} but Gemini still not reachable")
            time.sleep(1.0)
        except Exception as e:
            last_err = e
            time.sleep(1.0)
            continue

    raise RuntimeError("Failed to rotate VPN to a Gemini-reachable host") from last_err


def _ensure_vpn_for_gemini() -> None:
    # Для нашей задачи важно не "сменился ли IP", а "можем ли мы достучаться до Gemini".
    # На Windows часто включён split-tunneling, и тогда внешний IP может НЕ меняться даже при активном VPN.
    def _can_reach_gemini() -> bool:
        try:
            import requests

            # Любой HTTP-ответ означает, что сеть/маршрутизация до Google APIs есть.
            # (Ключ может быть неверным — это не важно для проверки канала.)
            r = requests.get(
                "https://generativelanguage.googleapis.com/",
                timeout=8,
            )
            return r is not None
        except Exception:
            return False
    """Ensure SSTP VPN is connected before Gemini calls.

    Strategy (matches prior app behavior):
    - If already connected -> do nothing.
    - Else, iterate hosts from good_hosts_for_images.txt (or env override) until connected.
    - Never allow falling back to non-VPN IP.
    """

    # Если уже подключены — проверим, что Gemini реально доступен.
    if vpn_is_connected(VPN_NAME):
        print("[VPN] already connected; checking Gemini reachability...")
        if _can_reach_gemini():
            print("[VPN] Gemini reachable -> OK")
            return
        print("[VPN] Gemini still not reachable; will rotate hosts...")

    hosts = _vpn_hosts_for_gemini()

    last_err: Exception | None = None

    global _VPN_HOST_POS

    retries = int(os.getenv("GEMINI_VPN_RETRIES", "2") or 2)
    backoff = float(os.getenv("GEMINI_VPN_BACKOFF", "1.5") or 1.5)

    # Start from last remembered position so we don't stick to a bad host.
    for step in range(len(hosts)):
        idx = (_VPN_HOST_POS + step) % len(hosts)
        host = hosts[idx]
        print(f"\n=== Switching VPN {VPN_NAME} to host {host} ===")

        try:
            # We retry the *same* host several times before moving on.
            for attempt in range(1, max(1, retries) + 1):
                # Reset only if there is an active connection; otherwise don't spam rasdial/disconnect.
                try:
                    if vpn_is_connected(VPN_NAME):
                        ensure_disconnected(VPN_NAME, wait_sec=6.0)
                except Exception as e:
                    print(f"[VPN] disconnect check error: {e}")

                try:
                    vpn_set_server(VPN_NAME, host)
                except Exception as e:
                    last_err = e
                    print(f"[VPN] failed to set server {host}: {e}")
                    break

                ok, out = vpn_connect(VPN_NAME, USERNAME, PASSWORD)
                out_txt = (out or "").strip()
                if out_txt:
                    tail = "\n".join(out_txt.splitlines()[-8:])
                    print(tail)

                if not ok:
                    last_err = RuntimeError(out_txt or "rasdial returned non-zero")
                    sleep_s = max(1.0, backoff * attempt)
                    print(f"Connect failed (attempt {attempt}/{retries}). Retrying after {sleep_s:.1f}s ...")
                    time.sleep(sleep_s)
                    continue

                # rasdial returned success; validate that Gemini is reachable through this connection.
                t0 = time.time()
                ok_reach = False
                while time.time() - t0 < 20.0:
                    if _can_reach_gemini():
                        ok_reach = True
                        break
                    time.sleep(2.0)

                if ok_reach:
                    # Remember next position for future rotations
                    _VPN_HOST_POS = (idx + 1) % len(hosts)
                    time.sleep(1.0)
                    return

                last_err = RuntimeError(f"Connected to {host} but Gemini is still not reachable")
                sleep_s = max(1.0, backoff * attempt)
                print(f"Connect failed (attempt {attempt}/{retries}). Retrying after {sleep_s:.1f}s ...")
                time.sleep(sleep_s)

        except Exception as e:
            last_err = e
            print(f"[VPN] unexpected error for host {host}: {e}")
            time.sleep(1.0)
            continue

    raise RuntimeError(
        "SSTP VPN connection failed for all hosts; Gemini request is blocked to avoid using non-VPN IP."
    ) from last_err


def _redact_key(url: str) -> str:
    try:
        import re
        # replace key param value with ****
        return re.sub(r"(key=)[^&]+", r"\1***", url)
    except Exception:
        return url
def _looks_like_vpn_block_for_gemini(status_code: int | None, err_status: str | None, err_message: str | None, exc: Exception | None = None) -> bool:
    """Heuristic: error likely depends on current VPN egress.

    We only use this when GEMINI_REQUIRE_VPN=1.
    """
    msg = (err_message or "")
    low = msg.lower()
    est = (err_status or "")
    est_low = est.lower()

    # Typical geo / access blocks
    # 1) Location-based restriction can come as 400 FAILED_PRECONDITION
    if status_code == 400 and ("failed_precondition" in est_low or "precondition" in est_low):
        if any(x in low for x in [
            "location is not supported",
            "user location is not supported",
            "not supported for the api use",
            "not supported",
            "country", "region",
        ]):
            return True

    # 2) Permission-based blocks can come as 401/403
    if status_code in (401, 403):
        if any(x in low for x in [
            "location", "not supported", "unavailable", "access", "blocked",
            "country", "region",
        ]):
            return True
        if any(x in est_low for x in ["permission_denied", "unauthenticated"]):
            # could be key, but sometimes geo-based; treat as possibly VPN-dependent
            return True

    # Network-level errors can be due to VPN host quality
    if exc is not None:
        s = str(exc).lower()
        if any(x in s for x in [
            "timed out", "timeout",
            "connection aborted", "connection reset",
            "connection refused",
            "remote end closed connection",
            "name or service not known", "temporary failure in name resolution",
            "ssleoferror", "ssl", "tls",
        ]):
            return True

    return False


def call_gemini_with_image(
    prompt_text: str,
    image_path: str,
    timeout_sec: int = 90,
    *,
    response_mime_type: str | None = None,
) -> str | None:
    """
    Отправляет промпт + изображение в Gemini. В случае ошибок: переключает модели/ключи, уважает лимиты.
    Возвращает текст ответа или None если контент заблокирован.
    """
    global current_model_index, current_gemini_key_index, last_429_at, LAST_BLOCK_REASON
    LAST_BLOCK_REASON = None

    require_vpn = os.getenv("GEMINI_REQUIRE_VPN", "0").strip() == "1"

    # ВАЖНО: перед обращением к Gemini — при необходимости гарантировать SSTP VPN.
    # По умолчанию (GEMINI_REQUIRE_VPN=0) поведение старое: VPN не трогаем.
    if require_vpn:
        _ensure_vpn_for_gemini()

    # If some VPN hosts connect OK but Gemini API calls still fail, we rotate hosts.
    vpn_fail_streak = 0
    vpn_rotations_done = 0
    try:
        vpn_rotations_limit = len(_vpn_hosts_for_gemini()) if require_vpn else 0
    except Exception:
        vpn_rotations_limit = 0

    inline = read_image_inline_data(image_path)
    headers = {'Content-Type': 'application/json'}
    # parts: текст + inlineData с картинкой
    base_payload: dict = {
        "contents": [
            {
                "parts": [
                    {"text": prompt_text},
                    {"inlineData": inline},
                ]
            }
        ]
    }

    # Ask Gemini to emit valid JSON when possible.
    # If the model ignores this, we still have a robust fallback parser.
    if response_mime_type:
        base_payload["generationConfig"] = {"responseMimeType": response_mime_type}
    while True:
        if shutdown_requested:
            print("  - Операция прервана пользователем.")
            return ""

        if current_model_index >= len(MODELS):
            print("\nЛимиты всех моделей для текущего ключа исчерпаны.")
            current_model_index = 0
            current_gemini_key_index += 1

            if current_gemini_key_index >= len(GEMINI_API_KEYS):
                print("Все API ключи исчерпали лимиты. Пауза 5 минут...")
                current_gemini_key_index = 0
                time.sleep(300)

            last_429_at = {idx: 0.0 for idx in range(len(MODELS))}
            print(f"Переключились на API ключ #{current_gemini_key_index + 1}.")
            continue

        model_name = MODELS[current_model_index]
        url = get_current_url()
        redacted_url = _redact_key(url)
        print(f"  - Запрос через {model_name}…")
        wait_for_rate_slot(current_model_index)

        try:
            # Диагностика запроса
            try:
                file_sz = os.path.getsize(image_path)
            except Exception:
                file_sz = 0
            print(
                f"  - Отправка: prompt_len={len(prompt_text)} chars, image_mime={inline.get('mimeType')}, "
                f"image_size={round(file_sz/1024,1)} KB, url={redacted_url}"
            )

            _sleep_jitter_before_request()
            resp = requests.post(url, json=base_payload, headers=headers, timeout=timeout_sec)
            resp.raise_for_status()
            data = resp.json() or {}

            # Блокировка контента
            pf = data.get('promptFeedback') or {}
            if 'blockReason' in pf:
                reason = pf.get('blockReason')
                LAST_BLOCK_REASON = str(reason) if reason is not None else "UNKNOWN"
                print(f"  - Контент заблокирован (причина: {LAST_BLOCK_REASON}).")
                return None

            cand = (data.get('candidates') or [])
            if cand:
                content = cand[0].get('content') or {}
                parts = content.get('parts') or []
                if parts and 'text' in parts[0]:
                    # Successful response -> reset VPN failure streak
                    vpn_fail_streak = 0
                    return parts[0]['text']

            print("  - Не удалось извлечь текст (неизвестная структура ответа). Переключение модели и пауза 15с…")
            current_model_index += 1
            time.sleep(15)
            continue

        except requests.exceptions.RequestException as e:
            resp = getattr(e, 'response', None)
            status_code = resp.status_code if resp is not None else None
            reason = getattr(resp, 'reason', None)
            body_text = None
            err_status = None
            err_message = None
            if resp is not None:
                try:
                    body_text = resp.text
                    j = resp.json()
                    # Google Generative Language API формат ошибки
                    if isinstance(j, dict) and 'error' in j:
                        err = j.get('error') or {}
                        err_message = err.get('message')
                        err_status = err.get('status')
                except Exception:
                    pass

            # If this looks like current VPN blocks/breaks Gemini, rotate to next host and retry.
            if require_vpn and _looks_like_vpn_block_for_gemini(status_code, err_status, err_message, e):
                vpn_fail_streak += 1
                threshold = int(os.getenv("GEMINI_VPN_ROTATE_AFTER", "2") or 2)
                if threshold < 1:
                    threshold = 1
                if vpn_fail_streak >= threshold and vpn_rotations_done < max(1, vpn_rotations_limit):
                    try:
                        _force_rotate_vpn_for_gemini(
                            reason=f"Gemini error via current VPN (status={status_code}, google_status={err_status})"
                        )
                        vpn_rotations_done += 1
                        vpn_fail_streak = 0
                        # Retry same request after rotation
                        continue
                    except Exception as re:
                        print(f"[VPN] rotation attempt failed: {re}")
                        # fall through to normal error handling

            print("  - HTTP ошибка при обращении к Gemini:")
            print(f"    • status={status_code} {reason or ''}")
            print(f"    • model={model_name}, key_index={current_gemini_key_index+1}/{len(GEMINI_API_KEYS)}")
            print(f"    • url={redacted_url}")
            if err_status or err_message:
                print(f"    • google.error.status={err_status}")
                print(f"    • google.error.message={err_message}")
            if body_text:
                bt = body_text.strip()
                if len(bt) > 1200:
                    bt = bt[:1200] + "… [truncated]"
                print("    • response_body=\n" + bt)

            if status_code == 429:
                now = time.time()
                if (now - last_429_at[current_model_index]) > 120:
                    last_429_at[current_model_index] = now
                    print("  - Ошибка 429 (минутный лимит). Пауза 61с и повтор…")
                    time.sleep(61)
                else:
                    print("  - Повторная 429 (возможно дневной лимит). Переключение модели…")
                    current_model_index += 1
                continue
            elif status_code in (400, 404):
                print(f"  - Ошибка {status_code}. Переключение модели…")
                current_model_index += 1
                continue
            elif status_code in (401, 403):
                # Чаще всего: ключ запрещён для этой модели или проект не имеет доступа
                print("  - Доступ запрещён (401/403). Переключаю API-ключ и сбрасываю модель на первую…")
                current_gemini_key_index += 1
                if current_gemini_key_index >= len(GEMINI_API_KEYS):
                    current_gemini_key_index = 0
                    print("  - Все ключи перепробованы. Пауза 60с…")
                    time.sleep(60)
                current_model_index = 0
                time.sleep(3)
                continue
            elif status_code and status_code >= 500:
                print(f"  - Временная ошибка сервера ({status_code}). Пауза 15с…")
                time.sleep(15)
                continue
            else:
                print(f"  - Ошибка сети: {e}. Пауза 15с…")
                time.sleep(15)
                continue
        except Exception as e:
            print(f"  - Неизвестная ошибка: {e}. Пауза 15с…")
            time.sleep(15)
            continue

# --- ЛОГИКА ГЕНЕРАЦИИ ---

def generate_keywords_and_image_prompt(image_path: str) -> dict:
    """
    1) Просит у нейросети описать картинку 30 long-tail ключевыми фразами на английском,
       строго без прелюдий/заголовков, только фразы через запятую.
    2) Оборачивает результат в финальный промпт для генерации изображения.
    """
    prompt = (
        "опиши картинку на английском в формате 30 long tail ключевых фраз которые лучше всего отражают что изображено на этой картинке. "
        "Должно быть как про дизайн так и про стиль, тон, эстетику, какую либо конкретину и другое. "
        "Очень важно чтобы ты не давал никаких предисловий по типу 'Here are the keypharases:' или заглавий типа 'Long-Tail Keywords for Image' "
        "и послесловий к фразам которые были тобой сгенерированы, должны быть только ключевые фразы через запятую согласно тому как я сказал"
    )
    text = call_gemini_with_image(prompt, image_path)
    if text is None:
        return {"keywords_30": None, "image_prompt": None}
    keywords_30 = (text or "").strip()
    image_prompt = f'CREATE AN ULTRA-REALISTIC, ULTRA-DETAILED, AND AESTHETIC IMAGE BY THESE KEYWORDS: "{keywords_30}"'
    return {"keywords_30": keywords_30, "image_prompt": image_prompt}

def _strip_double_asterisks(text: str) -> str:
    """Remove Gemini's occasional Markdown bold markers (**...**)."""

    if not isinstance(text, str) or not text:
        return "" if text is None else str(text)
    return text.replace("**", "")


def _extract_section(raw: str, header: str, next_headers: list[str]) -> str:
    """Extract text between HEADER: and next header (best-effort)."""

    import re

    txt = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    m = re.search(rf"^\s*{re.escape(header)}\s*:\s*$", txt, flags=re.I | re.M)
    if not m:
        return ""
    start = m.end()
    end = len(txt)
    for h in next_headers:
        m2 = re.search(rf"^\s*{re.escape(h)}\s*:\s*$", txt[start:], flags=re.I | re.M)
        if m2:
            end = min(end, start + m2.start())
    return txt[start:end].strip()


def _parse_titles(section: str) -> list[str]:
    import re

    lines = [ln.strip() for ln in (section or "").split("\n") if ln.strip()]
    out: list[str] = []
    for ln in lines:
        ln = re.sub(r"^\s*\d+\)\s*", "", ln).strip()
        ln = re.sub(r"^\s*[-\u2022]\s*", "", ln).strip()
        ln = re.sub(r"\s+", " ", ln)

        # If the model forgot the separator, try a very conservative fix:
        # split on '  ' (double space) or ' - ' only if it looks like two segments.
        if "|" not in ln:
            if "  " in ln:
                parts = [p.strip() for p in ln.split("  ") if p.strip()]
                if len(parts) >= 2:
                    ln = f"{parts[0]} | {' '.join(parts[1:])}"
            elif " - " in ln:
                parts = [p.strip() for p in ln.split(" - ") if p.strip()]
                if len(parts) == 2 and all(parts):
                    ln = f"{parts[0]} | {parts[1]}"

        ln = re.sub(r"\s*\|\s*", " | ", ln).strip()
        if ln:
            out.append(ln)
    return out[:5]


def _parse_hashtags(section: str) -> str:
    import re

    s = " ".join((section or "").replace("\n", " ").split())
    if not s:
        return ""
    tags = re.findall(r"#[_A-Za-z0-9]+", s)
    if tags:
        return " ".join(tags[:10]).strip()

    parts = [p for p in s.split(" ") if p.strip()]
    fixed: list[str] = []
    for p in parts:
        fixed.append(p if p.startswith("#") else "#" + p.lstrip("#"))
    return " ".join(fixed[:10]).strip()


def _parse_three_word_keywords(section: str) -> list[str]:
    import re

    lines = [ln.strip() for ln in (section or "").split("\n") if ln.strip()]
    out: list[str] = []
    for ln in lines:
        ln = re.sub(r"^\s*[-\u2022]\s*", "", ln).strip()
        ln = re.sub(r"^\s*\d+\)\s*", "", ln).strip()
        ln = " ".join(ln.split())
        if ln:
            out.append(ln)
    return out[:10]


def generate_pinterest_assets(image_path: str) -> dict:
    """
    Формирует структурированный ответ для Pinterest: описание, хэштеги, 3-словные ключи, варианты title.
    Отвечай строго в JSON, чтобы выход был легко парсить и соответствовать ограничениям по символам.
    """
    # Use the proven prompt style from generate_pinterest_texts_for_post.py.
    # IMPORTANT: We do NOT force JSON output here; we parse by section headers.
    prompt = (
        "Act as a Pinterest SEO Expert. Your goal is to generate high-ranking, keyword-dense content with minimum of fluff.\n\n"
        + "CRITICAL KEYWORD RULES:\n"
        + "- You must use the keywords NATURALLY: integrate them into readable English IN BOTH TITLE AND DESCRIPTION.\n\n"
        + "STEP 1: Create 15 high-volume Pinterest search phrases (exactly 3 words each).\n"
        + "- Use ONLY what you see in the image (style, room, objects, colors, vibe, materials).\n"
        + "- Prioritize high-intent home decor / interior design phrases that people actually search on Pinterest.\n\n"
        + "STEP 2: Generate 5 Title Options. Structure: 'Segment A | Segment B'.\n"
        + "- Segment A: MUST be a shortened, catchy main idea of the pin based on the image. Make Segment A DIFFERENT for all 5 titles.\n"
        + "- Segment B: Combine TWO different 3-word phrases from Step 1 using '&'.\n"
        + "- LENGTH LIMIT: Title #1 (Main Title) MUST be 100 characters or less (including spaces). If needed, shorten Segment A/B, drop extra words/emojis, and prioritize staying under 100 chars.\n"
        + "- Add 1-2 relevant emojis at the end of each title (but keep Title #1 within the 100-char limit).\n\n"
        + "STEP 3: Write an SEO-Only Description (approx. 500-600 characters).\n"
        + "- EXCLUSION: Do NOT use keywords from Title #1 (Main Title) in the description.\n"
        + "- RULE (STRUCTURE): Use all segments (A and B) from Titles #2 #3, #4, and #5 as the primary building blocks of the description.\n"
        + "- STYLE: Focus on using functional sentences while maintaining a natural flow throughout the text so it doesn't feel like you're just stuffing keywords. Minimize the use of vague adjectives like 'charming,' 'stunning,' 'beautiful,' or 'elegant.' Keep them to a minimum to fit more keywords. Ensure the keywords are relevant to avoid creating awkward sentences.\n"
        + "- DENSITY: Fill remaining space with unused 3-word phrases from Step 1.\n\n"
        + "STEP 4: Provide 10 SEO hashtags.\n\n"
        + "OUTPUT FORMAT (STRICT):\n"
        + "THREE-WORD KEYWORDS:\n"
        + "- word word word (15 lines)\n\n"
        + "TITLE OPTIONS:\n"
        + "1) ... (Main Title)\n"
        + "2) ...\n"
        + "3) ...\n"
        + "4) ...\n"
        + "5) ...\n\n"
        + "DESCRIPTION:\n"
        + "[A dense, technical block of SEO text. No fluff, no filler adjectives, just direct keyword integration.]\n\n"
        + "HASHTAGS:\n"
        + "#tag1 #tag2 ...\n\n"
        + "All output must be in English.\n\n"
        + "IMAGE: Use the attached image as the only source of truth."
    )
    text = call_gemini_with_image(prompt, image_path)
    if text is None:
        # If blocked, expose reason for UI/debug.
        return {
            "description": None,
            "hashtags": None,
            "three_word_keywords": None,
            "title_options": None,
            "_blocked_reason": LAST_BLOCK_REASON,
        }

    raw = _strip_double_asterisks((text or "").strip())

    # 1) Try strict JSON
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {
                "description": data.get("description"),
                "hashtags": data.get("hashtags"),
                "three_word_keywords": data.get("three_word_keywords"),
                "title_options": data.get("title_options"),
                "_raw": raw,
            }
    except Exception:
        pass

    # 2) Fallback: parse by section headers (same idea as generate_pinterest_texts_for_post.py)
    titles_sec = _extract_section(raw, "TITLE OPTIONS", ["DESCRIPTION", "HASHTAGS", "THREE-WORD KEYWORDS"])
    desc_sec = _extract_section(raw, "DESCRIPTION", ["HASHTAGS", "THREE-WORD KEYWORDS", "TITLE OPTIONS"])
    hashtags_sec = _extract_section(raw, "HASHTAGS", ["THREE-WORD KEYWORDS", "DESCRIPTION", "TITLE OPTIONS"])
    three_sec = _extract_section(raw, "THREE-WORD KEYWORDS", ["HASHTAGS", "DESCRIPTION", "TITLE OPTIONS"])

    titles = _parse_titles(titles_sec) if titles_sec else []
    desc = " ".join(desc_sec.split()) if desc_sec else ""
    hashtags = _parse_hashtags(hashtags_sec) if hashtags_sec else ""
    three = _parse_three_word_keywords(three_sec) if three_sec else []

    return {
        "description": desc or None,
        "hashtags": hashtags or None,
        "three_word_keywords": three or None,
        "title_options": titles or None,
        "_raw": raw,
    }

# --- CLI ---
def main():
    import argparse, glob

    parser = argparse.ArgumentParser(
        description="Генерация промпта для изображения и Pinterest-контента на основе картинки (Gemini)."
    )
    parser.add_argument("--image", "-i", help="Путь к изображению (jpg/png/webp).")
    parser.add_argument("--images", "-I", nargs="+", help="Список путей к изображениям.")
    parser.add_argument("--dir", "-d", help="Каталог, в котором искать изображения (jpg/jpeg/png/webp).")
    parser.add_argument("--out", "-o", help="Если один файл: путь к JSON. Если несколько: либо путь к папке для отдельных JSON, либо путь к одному общему .json для списка.")
    args = parser.parse_args()

    # Собираем входные пути
    paths = []
    if args.image:
        paths.append(args.image)
    if args.images:
        paths.extend(args.images)
    if args.dir:
        dir_path = args.dir
        exts = ("*.jpg", "*.jpeg", "*.png", "*.webp")
        for ptn in exts:
            paths.extend(glob.glob(os.path.join(dir_path, ptn)))

    # Дедупликация с сохранением порядка
    seen = set()
    ordered_paths = []
    for p in paths:
        ap = os.path.abspath(p)
        if ap not in seen and os.path.exists(ap):
            seen.add(ap)
            ordered_paths.append(ap)

    if not ordered_paths:
        print("Не передано ни одного изображения. Укажите --image, --images или --dir.")
        sys.exit(1)

    # Обработка одного файла (поведение как раньше)
    if len(ordered_paths) == 1:
        image_path = ordered_paths[0]
        print("\n=== Генерация 30 long-tail ключевых фраз и финального промпта для изображения ===")
        kw = generate_keywords_and_image_prompt(image_path)
        if kw["keywords_30"] is None:
            print("  - Не удалось сгенерировать ключевые фразы (контент заблокирован).")
        else:
            print("\n-- 30 long-tail (как есть) --")
            print(kw["keywords_30"])
            print("\n-- Итоговый промпт для генерации изображения --")
            print(kw["image_prompt"])

        print("\n=== Генерация Pinterest описания/хэштегов/тайтлов ===")
        pin = generate_pinterest_assets(image_path)
        print("\n-- Pinterest (структурированный вывод) --")
        print(json.dumps(pin, ensure_ascii=False, indent=2))

        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump({"image": image_path, "keywords": kw, "pinterest": pin}, f, ensure_ascii=False, indent=2)
            print(f"\nСохранено в: {args.out}")
        return

    # Пакетная обработка
    print(f"\nНайдено изображений: {len(ordered_paths)}")
    results = []

    combined_out_file = None
    out_dir = None
    if args.out:
        if args.out.lower().endswith(".json"):
            combined_out_file = os.path.abspath(args.out)
        else:
            out_dir = os.path.abspath(args.out)
            os.makedirs(out_dir, exist_ok=True)
    else:
        out_dir = os.path.abspath("results")
        os.makedirs(out_dir, exist_ok=True)

    for idx, image_path in enumerate(ordered_paths, 1):
        print(f"\n===== [{idx}/{len(ordered_paths)}] {image_path} =====")
        try:
            kw = generate_keywords_and_image_prompt(image_path)
            pin = generate_pinterest_assets(image_path)
            item = {"image": image_path, "keywords": kw, "pinterest": pin}
            results.append(item)

            if out_dir:
                base = os.path.splitext(os.path.basename(image_path))[0]
                out_file = os.path.join(out_dir, f"{base}.json")
                with open(out_file, "w", encoding="utf-8") as f:
                    json.dump(item, f, ensure_ascii=False, indent=2)
                print(f"  - Сохранено: {out_file}")
        except Exception as e:
            err = {"image": image_path, "error": str(e)}
            results.append(err)
            print(f"  - Ошибка при обработке: {e}")

    if combined_out_file:
        with open(combined_out_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\nИтоговый список сохранён в: {combined_out_file}")
    elif out_dir:
        print(f"\nГотово. Индивидуальные JSON лежат в папке: {out_dir}")

if __name__ == "__main__":
    setup_signals()
    main()