import os
import time
import re
from pathlib import Path
from typing import List, Optional, Tuple

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError, Error as PWError
import sys
import asyncio

# На Windows нужен ProactorEventLoop для subprocess в Playwright
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Используем вашу ротацию VPN из видео-скрипта
from generate_wan2_video import switch_vpn_to_full_host, load_hosts_file

# --- In-process state to keep VPN rotation across multiple calls (no files) ---
_IMAGE_HOSTS = None  # type: Optional[List[str]]
_IMAGE_HOST_POS = 0
_IMAGE_POS_INITIALIZED = False


def _ensure_image_hosts(path: str = "good_hosts_for_images.txt") -> List[str]:
    global _IMAGE_HOSTS
    if _IMAGE_HOSTS is None:
        _IMAGE_HOSTS = load_hosts_file(path)
    return _IMAGE_HOSTS
# ------------------------------------------------------------------------------

# URL страницы LoRA. Можно переопределять через аргументы функций.
DEFAULT_SPACE_URL = "https://prithivmlmods-flux-lora-dlc.hf.space"

WAIT_TIMEOUT_MS = 45000

# Если Space/Gradio иногда открывается "пустым" (вкладка с вечным прелоадером,
# но в DOM почти ничего нет), то не ждём общий долгий таймаут.
# Делаем быстрый чек первичной инициализации и один автоповтор (reload).
INITIAL_UI_BOOT_TIMEOUT_SEC = 20

class QuotaExceededError(Exception):
    pass

class TimeoutExceededError(Exception):
    pass

class CancelledError(Exception):
    pass

# вставь это рядом с объявлениями исключений (после TimeoutExceededError)
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

def _is_dns_or_host_error(err: Exception) -> bool:
    s = str(err).lower()
    return any(x in s for x in [
        "err_name_not_resolved",
        "name not resolved",
        "enotfound",
        "dns",
        "err_connection_timed_out",
        "err_internet_disconnected",
    ])


def _quick_boot_check(page, timeout_sec: int = INITIAL_UI_BOOT_TIMEOUT_SEC) -> bool:
    """Быстрый чек, что Space реально "ожил".

    Проблема: иногда вкладка открывается, но UI не монтируется (пустая страница/вечный прелоадер).
    В этом случае ждать общий WAIT_TIMEOUT_MS смысла нет.

    Возвращает True, если заметили признаки живого UI (textarea/#gen_btn или достаточный объём DOM).
    """
    deadline = time.time() + max(1, int(timeout_sec))
    last_err = None

    while time.time() < deadline:
        try:
            # Самый надёжный признак именно для этого Space — наличие textarea и кнопки.
            if page.locator("textarea[data-testid='textbox']").first.count() > 0:
                return True
            if page.locator("#gen_btn").first.count() > 0:
                return True

            # Если селекторов ещё нет, попробуем отличить "пустую оболочку" от реальной загрузки.
            # В твоём кейсе бывает: html/head/body есть, иногда даже gradio-app/main есть,
            # но UI не монтируется вообще. Поэтому критерии тут ДОЛЖНЫ быть жёсткими.
            metrics = page.evaluate(
                """() => {
                    const hasBody = !!document.body;
                    const body = document.body;
                    const txt = (body && (body.innerText || '')) || '';
                    const textLen = txt.replace(/\s+/g,' ').trim().length;
                    const elCount = document.getElementsByTagName('*').length;

                    const hasGradioApp = !!document.querySelector('gradio-app');
                    const hasGradioContainer = !!document.querySelector('.gradio-container');

                    // Попытка проверить, что webcomponent реально отрендерил что-то внутри.
                    // (в некоторых сборках gradio-app использует shadow DOM)
                    let gradioShadowElCount = 0;
                    let gradioShadowTextLen = 0;
                    try {
                      const ga = document.querySelector('gradio-app');
                      const root = ga && ga.shadowRoot;
                      if (root) {
                        gradioShadowElCount = root.querySelectorAll('*').length;
                        gradioShadowTextLen = (root.innerText || '').replace(/\s+/g,' ').trim().length;
                      }
                    } catch (e) {}

                    return {
                      hasBody,
                      textLen,
                      elCount,
                      hasGradioApp,
                      hasGradioContainer,
                      gradioShadowElCount,
                      gradioShadowTextLen,
                    };
                }"""
            )

            # Живой UI: либо появился конкретный control (textarea/#gen_btn), либо Gradio реально отрендерился.
            # ВАЖНО: наличие <main> или <gradio-app> само по себе НЕ считаем успехом.
            if metrics and (metrics.get('gradioShadowElCount', 0) >= 30 or metrics.get('gradioShadowTextLen', 0) >= 30):
                return True

            # Фоллбек: если DOM действительно "богатый" по элементам/тексту — считаем что UI не пустой.
            # Порог поднят, чтобы не засчитывать пустую заглушку.
            if metrics and (metrics.get('elCount', 0) >= 250 or metrics.get('textLen', 0) >= 200):
                return True
        except Exception as e:
            last_err = e

        time.sleep(0.25)

    # Диагностика, чтобы понять, что именно отрисовано в "зависшем" кейсе.
    try:
        diag = page.evaluate(
            """() => {
                return {
                  url: location.href,
                  title: document.title || '',
                  readyState: document.readyState,
                  hasBody: !!document.body,
                  bodyTextLen: ((document.body && document.body.innerText) || '').replace(/\s+/g,' ').trim().length,
                  elCount: document.getElementsByTagName('*').length,
                  hasTextbox: !!document.querySelector("textarea[data-testid='textbox']"),
                  hasGenBtn: !!document.querySelector('#gen_btn'),
                  hasGradioApp: !!document.querySelector('gradio-app'),
                  hasGradioContainer: !!document.querySelector('.gradio-container'),
                };
            }"""
        )
        print(f"[boot-check] not ready after {timeout_sec}s; diag={diag}")
    except Exception as e:
        print(f"[boot-check] diag evaluate failed: {e}")

    if last_err:
        # не фейлим чек из-за мелких transient ошибок, просто считаем что не загрузилось
        print(f"[boot-check] last error: {last_err}")
    return False


def _ensure_page_bootstrapped(page, space_url: str) -> None:
    """Проверяет, что после goto UI действительно смонтировался.

    Важно: тут НЕ делаем reload/goto.
    На практике при "зависшем" фронте reload может тоже подвисать.
    Надёжнее: быстро зафейлить (20с) и дать внешнему коду полностью пересоздать браузер.
    """
    if _quick_boot_check(page, timeout_sec=INITIAL_UI_BOOT_TIMEOUT_SEC):
        return

    raise TimeoutExceededError(
        "Space page opened but UI did not bootstrap (blank/loader). "
        f"Aborting early after ~{INITIAL_UI_BOOT_TIMEOUT_SEC}s."
    )

def _toast_state(page) -> Optional[tuple]:
    try:
        els = page.get_by_test_id("toast-body")
        if els.count() == 0:
            return None
        txt = els.nth(els.count() - 1).inner_text(timeout=1000) or ""
        low = txt.lower()
        # явное превышение квоты / отсутствие GPU
        if ("exceeded" in low and "quota" in low) or \
           ("no available gpu" in low) or \
           ("no gpu" in low) or \
           ("insufficient" in low and "gpu" in low):
            return ("quota", txt)
        # ожидание GPU (очередь) — ждём, не ротируем
        if "waiting for a gpu" in low or "zerogpu queue" in low or "waiting for gpu" in low:
            return ("queue", txt)
    except Exception:
        pass
    return None

# Пять нужных LoRA. Будем искать по подстроке (без регистра) в подписи.
TARGET_LORAS = [
    "Indo Realism",
    "Flux Face Realism",
    "Super Portraits",
    "Epic Realism",
    "Ultra Realism"
]

# Размеры (ближайшие к 960x1536 с шагом 64)
TARGET_WIDTH = 960
TARGET_HEIGHT = 1536


def _wait_app_ready(page, timeout_ms: int = WAIT_TIMEOUT_MS) -> bool:
    deadline = time.time() + (timeout_ms / 1000.0)
    last_err = None
    while time.time() < deadline:
        try:
            # Текстовое поле Prompt и кнопка Generate
            if page.locator("textarea[data-testid='textbox']").first.count() == 0:
                page.wait_for_selector("textarea[data-testid='textbox']", timeout=2000)
            if page.locator("#gen_btn").first.count() == 0:
                page.wait_for_selector("#gen_btn", timeout=2000)
            return True
        except Exception as e:
            last_err = e
            time.sleep(0.2)
    if last_err:
        raise last_err
    return False

def _generated_block_root(page):
    try:
        root = page.locator("xpath=//label[contains(normalize-space(.), 'Generated Image')]/ancestor::div[contains(@class,'block')][1]").first
        if root.count() > 0:
            return root
    except Exception:
        pass
    return None


def _current_output_src(page) -> Optional[str]:
    """
    Возвращает src итогового изображения, приоритетно из блока "Generated Image".
    Если не нашли там — используем прежние общие селекторы с фильтрацией галереи.
    """
    # 1) Жёстко из блока Generated Image
    root = _generated_block_root(page)
    if root is not None:
        try:
            imgs = root.locator("xpath=.//img").all()
            for img in reversed(imgs[-8:]):  # последние до 8 штук
                try:
                    vis = img.is_visible()
                    if not vis:
                        continue
                except Exception:
                    pass
                src = (img.get_attribute("src") or "").strip()
                if src:
                    return src
        except Exception:
            pass

    # 2) Фоллбек — старые селекторы с исключением галереи
    sel_candidates = [
        "img[alt='Generated Image']",
        "div[data-testid='image'] img",
        "div#component-21 img",
        "main img",
    ]
    for sel in sel_candidates:
        try:
            imgs = page.locator(sel)
            cnt = imgs.count()
            for i in range(min(cnt, 10)):
                img = imgs.nth(cnt - 1 - i)
                try:
                    in_gallery = img.evaluate("el => !!el.closest('.gallery-item, .grid-container, #gallery')")
                    if in_gallery:
                        continue
                except Exception:
                    pass
                try:
                    vis = img.is_visible()
                    if not vis:
                        continue
                except Exception:
                    pass
                src = (img.get_attribute("src") or "").strip()
                if src:
                    return src
        except Exception:
            continue
    return None

def _current_download_href(page) -> Optional[str]:
    """
    Возвращает href с кнопки скачивания (a.download-link), если он есть в блоке Generated Image.
    """
    try:
        anchors = page.locator("a.download-link[href]")
        cnt = anchors.count()
        for i in range(min(cnt, 5)):
            a = anchors.nth(cnt - 1 - i)
            try:
                if not a.is_visible():
                    continue
            except Exception:
                pass
            href = (a.get_attribute("href") or "").strip()
            if href:
                return href
    except Exception:
        pass
    return None

def _get_best_image_url(page) -> Optional[str]:
    """
    Сначала пытаемся взять явный download href из Gradio (a.download-link),
    если не нашли — берём src у видимого итогового img.
    """
    href = _current_download_href(page)
    if href:
        return href
    return _current_output_src(page)


def _download_via_anchor(page, out_path: str, timeout_ms: int = 30000) -> Optional[str]:
    """
    Если у блока Generated Image есть <a class="download-link"> —
    попробуем скачать кликом через Playwright (accept_downloads=True).
    Возвращает сохранённый путь или None, если способ не сработал.
    """
    try:
        root = _generated_block_root(page) or page
        a = root.locator("a.download-link[href]").last
        if a.count() == 0:
            return None
        # Вычислим расширение из href при возможности
        href = (a.get_attribute("href") or "").split("?")[0]
        ext = os.path.splitext(href)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            ext = os.path.splitext(out_path)[1].lower() or ".png"
        if not out_path.lower().endswith(ext):
            out_path = str(Path(out_path).with_suffix(ext))

        with page.expect_download(timeout=timeout_ms) as dl_info:
            # кликаем по внутренней кнопке, если есть, иначе по самому <a>
            inner_btn = a.locator("button").last
            if inner_btn.count() > 0:
                inner_btn.click()
            else:
                a.click()
        download = dl_info.value
        # Сохраняем напрямую в нужный путь
        os.makedirs(Path(out_path).parent, exist_ok=True)
        download.save_as(out_path)
        if Path(out_path).exists() and os.path.getsize(out_path) > 0:
            return out_path
    except Exception as e:
        print(f"[anchor-download] failed: {e}")
    return None
    """
    Возвращает src итогового изображения, игнорируя картинки, лежащие в галерее LoRA.
    Берём приоритетно img[alt='Generated Image'], затем другие кандидаты, выбираем последний видимый не-галерейный.
    """
    sel_candidates = [
        "img[alt='Generated Image']",
        "div[data-testid='image'] img",
        "div#component-21 img",
        "main img.svelte-1pijsyv",
    ]
    for sel in sel_candidates:
        try:
            imgs = page.locator(sel)
            cnt = imgs.count()
            # перебираем с конца (чаще всего нужный img добавляется позже)
            for i in range(min(cnt, 8)):
                img = imgs.nth(cnt - 1 - i)
                try:
                    # отсекаем изображения внутри галереи выбора LoRA
                    in_gallery = img.evaluate("el => !!el.closest('.gallery-item, .grid-container, #gallery')")
                    if in_gallery:
                        continue
                except Exception:
                    pass
                try:
                    vis = img.is_visible()
                    if not vis:
                        continue
                except Exception:
                    pass
                src = (img.get_attribute("src") or "").strip()
                if src:
                    return src
        except Exception:
            continue
    return None

def _collect_gallery_srcs(page) -> set[str]:
    """
    Возвращает множество src всех картинок в галерее LoRA, чтобы исключать их как ложные совпадения.
    """
    out = set()
    try:
        imgs = page.locator("div.gallery-item img, div#gallery img, .gallery-item img")
        total = imgs.count()
        for i in range(total):
            try:
                s = (imgs.nth(i).get_attribute("src") or "").strip()
                if s:
                    out.add(s)
            except Exception:
                continue
    except Exception:
        pass
    return out

def _is_generate_disabled(page) -> bool:
    try:
        btn = page.locator("#gen_btn").first
        if btn.count() == 0:
            return False
        # Playwright: is_disabled(), иначе по атрибуту
        try:
            return btn.is_disabled()
        except Exception:
            return btn.get_attribute("disabled") is not None
    except Exception:
        return False

def _wait_generation_cycle(page, start_src: Optional[str], max_wait_sec: int, reject_srcs: Optional[set[str]] = None):
    """
    Ждём пока кнопка станет disabled (старт), затем пока снова станет enabled (финиш),
    затем ждём новый src (отличный от start_src и не входящий в reject_srcs) и стабилизацию.
    """
    deadline = time.time() + max_wait_sec
    # 1) дождаться disable (если не успели — не критично)
    t0 = time.time()
    while time.time() < min(deadline, t0 + 10):
        st = _toast_state(page)
        if st:
            kind, txt = st
            if kind == "quota":
                raise QuotaExceededError(txt)
            if kind == "queue":
                time.sleep(1); continue
        if _is_generate_disabled(page):
            break
        time.sleep(0.2)

    # 2) дождаться enable (завершение генерации)
    while time.time() < deadline:
        st = _toast_state(page)
        if st:
            kind, txt = st
            if kind == "quota":
                raise QuotaExceededError(txt)
            if kind == "queue":
                time.sleep(2); continue
        if not _is_generate_disabled(page):
            break
        time.sleep(0.5)

    if time.time() >= deadline:
        raise TimeoutExceededError("Истек таймаут ожидания завершения генерации (кнопка не активировалась)")

    # 3) дождаться финального кадра (стабильность)
    remain = max(5, int(deadline - time.time()))
    return _wait_final_image_stable(page, start_src=start_src, reject_srcs=reject_srcs, min_stable_sec=5, timeout_sec=remain)

def _open_advanced(page, timeout_ms: int = 10000):
    # Кнопка "Advanced Settings" (в HTML — button.label-wrap)
    btn = page.locator("button.label-wrap:has-text('Advanced Settings')").first
    btn.wait_for(state="visible", timeout=timeout_ms)
    btn.scroll_into_view_if_needed()
    btn.click(force=True)


def _set_prompt(page, text: str) -> bool:
    # По метке "Prompt" → ближайший textarea
    try:
        label = page.get_by_text("Prompt", exact=False).first
        container = label.locator("xpath=ancestor::label[1]")
        ta = container.locator("textarea[data-testid='textbox']").first
        ta.click()
        ta.fill(text)
        return True
    except Exception:
        pass
    # Фоллбек: первый textarea на странице
    try:
        ta = page.locator("textarea[data-testid='textbox']").first
        ta.click()
        ta.fill(text)
        return True
    except Exception:
        return False

def _wait_final_image_stable(page, start_src: Optional[str], reject_srcs: Optional[set[str]] = None, min_stable_sec: float = 15, timeout_sec: int = 60) -> str:
    """
    Ждём, пока итоговое изображение стабильно (src/размеры не меняются) min_stable_sec,
    полностью загружено (complete + naturalWidth/Height > 0), и src не относится к галерее.
    """
    reject_srcs = reject_srcs or set()
    deadline = time.time() + timeout_sec
    last_src = None
    last_dims = None
    last_change = time.time()

    def _img_info():
        src = _current_output_src(page)
        dims = page.evaluate(
            """() => {
                // 1) Ищем блок 'Generated Image' и берем последний IMG внутри него
                const lab = Array.from(document.querySelectorAll('label.svelte-19djge9.float, label[data-testid="block-label"]'))
                  .find(el => /generated image/i.test(el.textContent || ''));
                let img = null;
                if (lab) {
                  const root = lab.closest('div.block');
                  if (root) {
                    const imgs = root.querySelectorAll('img');
                    if (imgs && imgs.length) img = imgs[imgs.length - 1];
                  }
                }
                // 2) Фоллбек — общий видимый IMG не из галереи
                if (!img) {
                  const candidates = Array.from(document.querySelectorAll('main img, img'));
                  for (let i = candidates.length - 1; i >= 0; i--) {
                    const el = candidates[i];
                    const inGallery = el.closest('.gallery-item, .grid-container, #gallery');
                    const style = el.ownerDocument.defaultView.getComputedStyle(el);
                    const visible = style && style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
                    if (!inGallery && visible) { img = el; break; }
                  }
                }
                if (!img) return null;
                return { nw: img.naturalWidth || 0, nh: img.naturalHeight || 0, complete: !!img.complete };
            }"""
        )
        return src, dims

    while time.time() < deadline:
        st = _toast_state(page)
        if st:
            kind, txt = st
            if kind == "quota":
                raise QuotaExceededError(txt)
            if kind == "queue":
                time.sleep(1.5)
                continue

        cur_src, dims = _img_info()

        # игнорируем пустые/галерейные/старые src
        if cur_src and cur_src == start_src:
            cur_src = None
        if cur_src and cur_src in reject_srcs:
            cur_src = None

        if cur_src != last_src or dims != last_dims:
            last_change = time.time()
            last_src = cur_src
            last_dims = dims

        stable_enough = (time.time() - last_change) >= min_stable_sec
        if cur_src and dims and dims.get("complete") and dims.get("nw", 0) > 64 and dims.get("nh", 0) > 64 and stable_enough:
            return cur_src

        time.sleep(0.3)

    raise TimeoutExceededError("Не дождались стабилизации финального изображения")

def _set_number_input(page, aria_name: str, value: int | float) -> bool:
    try:
        el = page.get_by_role("spinbutton", name=aria_name)
        if el.count() == 0:
            el = page.locator(f"input[data-testid='number-input'][aria-label='{aria_name}']")
        if el.count() == 0:
            return False
        inp = el.first
        inp.scroll_into_view_if_needed()
        inp.click()
        inp.press("Control+A")
        inp.type(str(value))
        inp.press("Enter")
        inp.evaluate(
            """(el, val) => {
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.blur();
            }""",
            str(value),
        )
        try:
            cur = inp.input_value(timeout=800)
            return str(cur).strip() == str(value)
        except Exception:
            return True
    except Exception:
        return False


def _set_size(page, width: int, height: int) -> None:
    _open_advanced(page, 8000)
    _set_number_input(page, "number input for Width", width)
    _set_number_input(page, "number input for Height", height)


def _toggle_randomize_seed(page, checked: bool = True):
    try:
        cb = page.locator("input[data-testid='checkbox']").first
        if cb.count() > 0:
            is_checked = cb.is_checked()
            if is_checked != checked:
                cb.click()
    except Exception:
        pass

def _click_generate_strict(page) -> bool:
    try:
        btn = page.locator("#gen_btn").first
        if btn.count() > 0:
            btn.scroll_into_view_if_needed()
            btn.click()
            return True
        # fallbacks
        for name in ["Generate", "Run", "Submit", "Start"]:
            b = page.get_by_role("button", name=name, exact=False)
            if b.count() > 0:
                b.first.scroll_into_view_if_needed()
                b.first.click()
                return True
        b2 = page.locator("button:has-text('Generate')").first
        if b2.count() > 0:
            b2.scroll_into_view_if_needed()
            b2.click()
            return True
    except Exception:
        pass
    return False

def _scroll_gallery_to_label(page, name_substr: str) -> Optional[Tuple[object, str]]:
    """
    Ищем LoRA по надписи внутри .caption-label, без регистра.
    Возвращает (button, matched_text) или None.
    """
    low = name_substr.lower().strip()
    labels = page.locator("div.caption-label")
    count = labels.count()
    # Прокрутим сетку, если есть скролл
    try:
        grid = page.locator(".grid-container").first
    except Exception:
        grid = None

    seen = set()
    for _ in range(50):
        # обход ВСЕХ элементов (без искусственного ограничения 200)
        for i in range(count):
            try:
                lab = labels.nth(i)
                txt = (lab.inner_text() or "").strip()
                if txt and txt not in seen:
                    seen.add(txt)
                    if low in txt.lower():
                        btn = lab.locator("xpath=ancestor::button[1]").first
                        return (btn, txt)
            except Exception:
                continue
        # Сдвигаем прокрутку вниз, если можно
        try:
            if grid:
                grid.evaluate("node => node.scrollBy(0, node.clientHeight)")
            page.mouse.wheel(0, 1200)
            time.sleep(0.2)
        except Exception:
            break
    return None


def _scroll_gallery_to_lora_by_alt(page, name_substr: str) -> Optional[Tuple[object, str]]:
    """
    Фоллбек-поиск LoRA по alt у <img>, без регистра. Возвращает (button, matched_alt) или None.
    """
    low = name_substr.lower().strip()
    imgs = page.locator("div.gallery-item img, div#gallery img, .gallery-item img")
    total = imgs.count()
    for _ in range(50):
        for i in range(total):
            try:
                img = imgs.nth(i)
                alt = (img.get_attribute("alt") or "").strip()
                if alt and low in alt.lower():
                    btn = img.locator("xpath=ancestor::button[1]").first
                    return (btn, alt)
            except Exception:
                continue
        try:
            page.mouse.wheel(0, 1200)
            time.sleep(0.2)
        except Exception:
            break
    return None


def _select_lora(page, name_substr: str) -> bool:
    # 1) По подписи
    pair = _scroll_gallery_to_label(page, name_substr)
    # 2) Фоллбек по alt, если не нашли по подписи
    if not pair:
        pair = _scroll_gallery_to_lora_by_alt(page, name_substr)
    if not pair:
        return False
    btn, matched = pair
    btn.scroll_into_view_if_needed()
    btn.click()
    # Подтверждаем, что элемент стал выбранным (класс selected)
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            cls = btn.get_attribute("class") or ""
            if "selected" in cls:
                break
            time.sleep(0.15)
    except Exception:
        pass
    return True


def _click_generate(page) -> bool:
    btn = page.locator("#gen_btn").first
    if btn.count() == 0:
        return False
    btn.scroll_into_view_if_needed()
    btn.click()
    return True


def _wait_image_and_get_src(page, timeout_ms: int = 300000) -> Optional[str]:
    """
    Ждём появления/обновления итогового изображения и возвращаем его src.
    """
    # Пытаемся найти блок "Generated Image" → img
    sel_candidates = [
        "div#component-21 img",
        "div[data-testid='image'] img",
        "img[alt='Generated Image']",
        "main img.svelte-1pijsyv",  # fallback
    ]
    deadline = time.time() + (timeout_ms / 1000.0)
    last_src = None
    while time.time() < deadline:
        for sel in sel_candidates:
            try:
                img = page.locator(sel).first
                if img.count() > 0:
                    src = img.get_attribute("src") or ""
                    if src and src != last_src:
                        # Иногда бывает кратковременный blob, подождём стабильный src
                        time.sleep(0.6)
                        src2 = img.get_attribute("src") or ""
                        if src2:
                            return src2
                        return src
            except Exception:
                pass
        time.sleep(0.8)
    return None


def _save_image_from_src(src: str, out_path: str, page=None) -> str:
    """
    Сохраняем картинку надёжно. Порядок попыток:
    1) Playwright APIRequestContext (page.context.request.get) — наследует куки/заголовки контекста.
    2) Встроенный fetch внутри страницы (page.evaluate) с credentials: 'include'.
    3) requests как последний фоллбек.
    Работает и для blob:, и для обычных URL (для blob: пропускаем 3).
    """
    os.makedirs(Path(out_path).parent, exist_ok=True)

    def _write_bytes(data: bytes):
        with open(out_path, "wb") as f:
            f.write(data)
        return out_path

    def _write_b64_to_file(data_b64: str):
        import base64 as _b64
        return _write_bytes(_b64.b64decode(data_b64))

    # 1) Попробуем через Playwright APIRequestContext
    if page is not None:
        try:
            resp = page.context.request.get(src, timeout=60_000)
            if resp.ok:
                return _write_bytes(resp.body())
            else:
                print(f"[save] context.request.get failed: {resp.status} {resp.url}")
        except Exception as e:
            print(f"[save] context.request.get error: {e}")

        # 2) Попробуем через fetch в браузере (тащит куки)
        try:
            data_b64 = page.evaluate(
                """async (src) => {
                    const resp = await fetch(src, { credentials: 'include' });
                    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                    const blob = await resp.blob();
                    const buf = await blob.arrayBuffer();
                    let binary = '';
                    const bytes = new Uint8Array(buf);
                    const chunk = 0x8000;
                    for (let i = 0; i < bytes.length; i += chunk) {
                      const sub = bytes.subarray(i, i + chunk);
                      binary += String.fromCharCode.apply(null, sub);
                    }
                    return btoa(binary);
                }""",
                src
            )
            return _write_b64_to_file(data_b64)
        except Exception as e:
            print(f"[save] page.fetch error: {e}")

    # 3) Фоллбек через requests (для обычных URL, не blob:)
    if not src.startswith("blob:"):
        try:
            r = requests.get(src, timeout=240, headers={"Referer": DEFAULT_SPACE_URL, "User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return _write_bytes(r.content)
        except Exception as e:
            raise TimeoutExceededError(f"Не удалось скачать изображение (requests): {e}")

    raise TimeoutExceededError("Не удалось скачать изображение ни одним из способов (blob or protected URL)")


def _sanitize(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return name or "image"


def generate_one_image(
    space_url: str,
    prompt_text: str,
    lora_name: str,
    out_path: str,
    headless: bool = True,
    timeout_sec: int = 120,
    steps: Optional[int] = None,
    cancel_check: Optional[callable] = None,
) -> str:
    """
    Открывает страницу, ставит prompt, выбирает LoRA, задаёт размер, жмёт Generate, сохраняет PNG/JPG.
    Возвращает путь к сохранённому файлу.
    """
    from base64 import b64decode as _b64decode  # lazy import to avoid lints
    global base64
    import base64 as _base64
    base64 = _base64

    with sync_playwright() as p:
        if cancel_check and cancel_check():
            raise CancelledError("Отмена перед запуском браузера")

        # Иногда HF Space отдаёт HTML+gradio_config, но фронт не монтируется (вечный лоадер).
        # В таком кейсе reload внутри той же вкладки может не помочь.
        # Поэтому делаем 1 быстрый retry с ПОЛНЫМ пересозданием browser/context/page.
        browser = None
        context = None
        page = None
        last_err: Optional[Exception] = None

        for attempt in range(2):
            # на каждой попытке стартуем с чистого браузера
            try:
                browser = p.chromium.launch(headless=headless)
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()

                # Важно: иногда страница "зависает" ещё до появления document.body,
                # и тогда wait_until="domcontentloaded" может не наступить очень долго.
                # Поэтому делаем двухфазно:
                # 1) commit — дождаться, что навигация началась и документ создан.
                # 2) дальше вручную ждём появления body/UI коротким таймаутом.
                page.goto(space_url, wait_until="commit", timeout=120000)
                _ensure_page_bootstrapped(page, space_url)

                if cancel_check and cancel_check():
                    raise CancelledError("Отменено")

                _wait_app_ready(page, WAIT_TIMEOUT_MS)
                last_err = None
                break  # успех

            except CancelledError:
                raise

            except (PWTimeoutError, PWError, TimeoutExceededError) as e:
                last_err = e

                # Сетевые/хостовые ошибки → наверх, чтобы включилась ротация VPN
                if _is_dns_or_host_error(e) or _nav_is_host_or_net_error(e):
                    raise TimeoutExceededError(f"DNS/host error while opening {space_url}: {e}")

                # Пустая загрузка: даём 1 retry (attempt==0)
                if isinstance(e, TimeoutExceededError) and attempt == 0:
                    print("[boot-check] blank/loader detected; restarting browser (attempt 2/2)")
                    # закрываем и повторяем
                    try:
                        if context is not None:
                            context.close()
                    except Exception:
                        pass
                    try:
                        if browser is not None:
                            browser.close()
                    except Exception:
                        pass
                    context = None
                    browser = None
                    page = None
                    continue

                # прочее — как раньше
                raise

        if last_err is not None:
            raise TimeoutExceededError(f"UI not ready: {last_err}")

        # дальше код функции не меняем (browser/context/page остаются открытыми)

        if not _set_prompt(page, prompt_text):
            raise RuntimeError("Не удалось установить Prompt.")

        _set_size(page, TARGET_WIDTH, TARGET_HEIGHT)
        _toggle_randomize_seed(page, True)

        # Установить Steps, если передан (диапазон по UI: 1..50)
        if steps is not None:
            s = max(1, min(50, int(steps)))
            _set_number_input(page, "number input for Steps", s)

        if cancel_check and cancel_check():
            raise CancelledError("Отменено")
        if not _select_lora(page, lora_name):
            raise RuntimeError(f"Не удалось найти/выбрать LoRA: {lora_name}")

        # предыдущее изображение до нажатия (чтобы отследить обновление)
        start_src = _current_output_src(page)
        gallery_srcs = _collect_gallery_srcs(page)

        # нажимаем Generate
        if cancel_check and cancel_check():
            context.close(); browser.close();
            raise CancelledError("Отменено")
        if not _click_generate_strict(page):
            context.close()
            browser.close()
            raise RuntimeError("Кнопка Generate не найдена.")

        # ждём цикл генерации: disabled -> enabled + новый src, игнорируя src из галереи
        if cancel_check and cancel_check():
            context.close(); browser.close();
            raise CancelledError("Отменено")
        src = _wait_generation_cycle(page, start_src=start_src, max_wait_sec=timeout_sec, reject_srcs=gallery_srcs)
        if not src:
            context.close()
            browser.close()
            raise TimeoutExceededError("Истек таймаут ожидания результата (slow IP)")

        # Сначала пробуем официальной кнопкой загрузки (через Playwright downloads)
        saved = _download_via_anchor(page, out_path)
        if not saved:
            # Предпочитаем явный download href от Gradio, т.к. он стабильнее (не blob:)
            best_url = _get_best_image_url(page) or src

            # Если у out_path нет ожидаемого расширения, возьмём из URL, иначе по умолчанию .png
            ext = os.path.splitext(best_url.split("?")[0])[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                ext = os.path.splitext(out_path)[1].lower() or ".png"
            if not out_path.lower().endswith(ext):
                out_path = str(Path(out_path).with_suffix(ext))

            saved = _save_image_from_src(best_url, out_path, page=page)

        context.close()
        browser.close()
        return saved


def generate_all_lora_variants(
    space_url: str,
    prompt_text: str,
    out_dir: str,
    loras: Optional[List[str]] = None,
    vpn_start: int = 1,
    vpn_end: int = 250,
    headless: bool = True,
    steps: int = 41,
    hosts_start_pos: Optional[int] = None,  # <- делаем опциональным
    cancel_check: Optional[callable] = None,
    max_wait_sec: int = 180,
    enable_vpn_rotation: bool = True,
) -> List[str]:
    loras = loras or TARGET_LORAS
    out_dir = str(Path(out_dir).resolve()) 
    os.makedirs(out_dir, exist_ok=True)

    saved_paths: List[str] = []
    enable_vpn_rotation = bool(enable_vpn_rotation)
    hosts = _ensure_image_hosts("good_hosts_for_images.txt") if enable_vpn_rotation else []
    if enable_vpn_rotation and not hosts:
        raise SystemExit("Файл good_hosts_for_images.txt пуст или отсутствует.")

    global _IMAGE_HOST_POS, _IMAGE_POS_INITIALIZED
    if enable_vpn_rotation and not _IMAGE_POS_INITIALIZED:
        if hosts_start_pos is not None:
            _IMAGE_HOST_POS = int(hosts_start_pos) % len(hosts)
        _IMAGE_POS_INITIALIZED = True

    for i, lora in enumerate(loras, 1):
        if cancel_check and cancel_check():
            raise CancelledError("Отменено")
        fname = f"{i:02d}-{_sanitize(lora)}.png"
        out_path = str(Path(out_dir) / fname)

        tries = 0
        while True:
            if cancel_check and cancel_check():
                raise CancelledError("Отменено")
            try:
                saved = generate_one_image(
                    space_url=space_url,
                    prompt_text=prompt_text,
                    lora_name=lora,
                    out_path=out_path,
                    headless=headless,
                    timeout_sec=int(max_wait_sec),
                    steps=steps,
                    cancel_check=cancel_check,
                )
                saved_paths.append(saved)
                break
            except CancelledError:
                raise
            except Exception as e:
                if not enable_vpn_rotation:
                    raise
                print(f"[LoRA '{lora}'] Will rotate due to: {e}")
                rotated = False
                tried = 0
                while tried < len(hosts):
                    if cancel_check and cancel_check():
                        raise CancelledError("Отменено")
                    host = hosts[_IMAGE_HOST_POS % len(hosts)]
                    print(f"Rotating VPN to host: {host}")
                    if switch_vpn_to_full_host(host, retries=2, backoff=1.5, pause_after_connect=1.0):
                        rotated = True
                        _IMAGE_HOST_POS = (_IMAGE_HOST_POS + 1) % len(hosts)
                        break
                    _IMAGE_HOST_POS = (_IMAGE_HOST_POS + 1) % len(hosts)
                    tried += 1
                if not rotated:
                    # Важно: если VPN реально не переключился, повторять генерацию бессмысленно —
                    # откроется браузер на том же IP и мы снова упрёмся в ту же квоту/ошибку.
                    raise TimeoutExceededError(
                        f"[LoRA '{lora}'] VPN rotation failed (external IP did not change on all hosts)."
                    )
                continue

        # плановая ротация после успешной генерации (между картинками)
        if not enable_vpn_rotation:
            continue

        rotated = False
        tried = 0
        while tried < len(hosts):
            if cancel_check and cancel_check():
                raise CancelledError("Отменено")
            host = hosts[_IMAGE_HOST_POS % len(hosts)]
            print(f"Planned rotate to host: {host}")
            if switch_vpn_to_full_host(host, retries=2, backoff=1.5, pause_after_connect=1.0):
                rotated = True
                _IMAGE_HOST_POS = (_IMAGE_HOST_POS + 1) % len(hosts)
                break
            _IMAGE_HOST_POS = (_IMAGE_HOST_POS + 1) % len(hosts)
            tried += 1
        if not rotated:
            print("Предупреждение: не удалось переключить VPN ни на один доступный хост.")

    return saved_paths
