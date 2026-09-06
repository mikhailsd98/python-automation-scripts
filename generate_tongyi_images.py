import os
import re
import time
from pathlib import Path
from typing import List, Optional

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError, Error as PWError

import sys
import asyncio

# На Windows нужен ProactorEventLoop для subprocess в Playwright
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from generate_wan2_video import switch_vpn_to_full_host, load_hosts_file

DEFAULT_SPACE_URL = "https://tongyi-mai-z-image-turbo.hf.space/"
WAIT_TIMEOUT_MS = 45_000
INITIAL_UI_BOOT_TIMEOUT_SEC = 20

TARGET_SIZE_TEXT = "832x1248 ( 2:3 )"

# --- In-process state to keep VPN rotation across multiple calls (no files) ---
_IMAGE_HOSTS: Optional[List[str]] = None
_IMAGE_HOST_POS = 0
_IMAGE_POS_INITIALIZED = False


class QuotaExceededError(Exception):
    pass


class TimeoutExceededError(Exception):
    pass


class NoGPUAvailableError(Exception):
    """HF ZeroGPU queue did not allocate GPU (toast like 'No GPU was available after 60s...')."""


class CancelledError(Exception):
    pass


from typing import Optional, Callable

_LOG_HOOK: Optional[Callable[[str], None]] = None


def _safe_print(msg: str) -> None:
    """Print that won't crash on Windows consoles with non-UTF8 codepages."""
    try:
        print(msg)
    except UnicodeEncodeError:
        # Convert to ASCII with backslash escapes for non-ascii chars.
        try:
            safe = msg.encode("ascii", errors="backslashreplace").decode("ascii", errors="ignore")
            print(safe)
        except Exception:
            # last resort: swallow
            pass


def _log(msg: str) -> None:
    """Log to console + optional UI hook."""
    _safe_print(msg)
    global _LOG_HOOK
    if _LOG_HOOK is not None:
        try:
            _LOG_HOOK(msg)
        except Exception:
            pass


def _ensure_image_hosts(path: str = "good_hosts_for_images.txt") -> List[str]:
    global _IMAGE_HOSTS
    if _IMAGE_HOSTS is None:
        _IMAGE_HOSTS = load_hosts_file(path)
    return _IMAGE_HOSTS


def _is_dns_or_host_error(err: Exception) -> bool:
    s = str(err).lower()
    return any(
        x in s
        for x in [
            "err_name_not_resolved",
            "name not resolved",
            "enotfound",
            "dns",
            "err_connection_timed_out",
            "err_internet_disconnected",
            "err_connection_closed",
            "net::err_connection_closed",
        ]
    )


def _quick_boot_check(page, timeout_sec: int = INITIAL_UI_BOOT_TIMEOUT_SEC) -> bool:
    deadline = time.time() + max(1, int(timeout_sec))
    while time.time() < deadline:
        try:
            # Typical Gradio textbox
            if page.locator("textarea[data-testid='textbox']").first.count() > 0:
                return True
            # Some Spaces use input/textarea without testid
            if page.locator("textarea").first.count() > 0:
                return True
            # Typical Gradio generate button
            if page.locator("#gen_btn").first.count() > 0:
                return True
            if page.get_by_role("button", name=re.compile(r"^(generate|run|submit|start)$", re.I)).count() > 0:
                return True
        except Exception:
            pass
        time.sleep(0.25)

    try:
        diag = page.evaluate(
            """() => ({
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
            })"""
        )
        print(f"[tongyi boot-check] not ready after {timeout_sec}s; diag={diag}")
    except Exception:
        pass

    return False


def _ensure_page_bootstrapped(page, space_url: str) -> None:
    if _quick_boot_check(page, timeout_sec=INITIAL_UI_BOOT_TIMEOUT_SEC):
        return
    raise TimeoutExceededError(
        "Space page opened but UI did not bootstrap (blank/loader). "
        f"Aborting early after ~{INITIAL_UI_BOOT_TIMEOUT_SEC}s. url={space_url}"
    )


def _toast_state(page) -> Optional[tuple]:
    """Detect quota/queue/gpu-allocation toasts if present (Gradio).

    Returns (kind, text) where kind in: "quota" | "queue" | "nogpu".
    """

    try:
        els = page.get_by_test_id("toast-body")
        if els.count() == 0:
            return None
        txt = els.nth(els.count() - 1).inner_text(timeout=1000) or ""
        low = txt.lower()

        # hard quota / plan limitation
        if ("exceeded" in low and "quota" in low) or ("no available gpu" in low) or ("insufficient" in low and "gpu" in low):
            return ("quota", txt)

        # waiting in queue
        if "waiting for a gpu" in low or "zerogpu queue" in low or "waiting for gpu" in low:
            return ("queue", txt)

        # the exact problematic case user reports (different variants happen)
        if (
            "no gpu was available" in low
            or ("no gpu" in low and "available" in low)
            or ("after 60s" in low and "gpu" in low)
            or ("higher priority" in low and "zerogpu" in low)
            or ("priority" in low and "queue" in low)
        ):
            return ("nogpu", txt)

    except Exception:
        pass

    return None


def _wait_app_ready(page, timeout_ms: int = WAIT_TIMEOUT_MS) -> None:
    deadline = time.time() + (timeout_ms / 1000.0)
    last_err: Optional[Exception] = None
    while time.time() < deadline:
        try:
            if page.locator("textarea[data-testid='textbox']").first.count() == 0:
                # fallback to any textarea
                if page.locator("textarea").first.count() == 0:
                    page.wait_for_selector("textarea", timeout=2000)
            return
        except Exception as e:
            last_err = e
            time.sleep(0.2)
    if last_err:
        raise last_err


def _set_prompt(page, text: str) -> bool:
    # Strict selector from provided HTML
    try:
        ta = page.locator(
            "textarea[data-testid='textbox'][placeholder*='Enter your prompt']"
        ).first
        if ta.count() > 0:
            ta.click()
            ta.fill(text)
            return True
    except Exception:
        pass

    # Prefer labelled block containing "Prompt"
    try:
        label = page.get_by_text("Prompt", exact=False).first
        container = label.locator("xpath=ancestor::label[1]")
        ta = container.locator("textarea[data-testid='textbox']").first
        if ta.count() == 0:
            ta = container.locator("textarea").first
        ta.click()
        ta.fill(text)
        return True
    except Exception:
        pass

    # Fallback: first textbox
    try:
        ta = page.locator("textarea[data-testid='textbox']").first
        if ta.count() == 0:
            ta = page.locator("textarea").first
        ta.click()
        ta.fill(text)
        return True
    except Exception:
        return False


def _is_generate_disabled(page) -> bool:
    # common ids
    for sel in ["#gen_btn", "button:has-text('Generate')", "button:has-text('Run')", "button:has-text('Submit')"]:
        try:
            btn = page.locator(sel).first
            if btn.count() == 0:
                continue
            try:
                return btn.is_disabled()
            except Exception:
                return btn.get_attribute("disabled") is not None
        except Exception:
            continue
    return False


def _click_generate(page) -> bool:
    # Strict selector from provided HTML
    try:
        btn = page.locator("button.lg.primary:has-text('Generate')").first
        if btn.count() > 0:
            btn.scroll_into_view_if_needed()
            btn.click(force=True)
            return True
    except Exception:
        pass

    # fallbacks
    for name in ["Generate", "Run", "Submit", "Start"]:
        try:
            b = page.get_by_role("button", name=name, exact=False)
            if b.count() > 0:
                b.first.scroll_into_view_if_needed()
                b.first.click(force=True)
                return True
        except Exception:
            continue

    try:
        b2 = page.locator("button:has-text('Generate')").first
        if b2.count() > 0:
            b2.scroll_into_view_if_needed()
            b2.click(force=True)
            return True
    except Exception:
        pass

    return False


def _generated_block_root(page):
    # Strict selector from provided HTML: label[data-testid='block-label'] contains "Generated Images"
    try:
        root = page.locator(
            "xpath=//label[@data-testid='block-label' and contains(normalize-space(.), 'Generated Images')]/ancestor::div[contains(@class,'block')][1]"
        ).first
        if root.count() > 0:
            return root
    except Exception:
        pass

    # Fallback: any label containing 'Generated'
    try:
        root = page.locator(
            "xpath=//label[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'generated')]/ancestor::div[contains(@class,'block')][1]"
        ).first
        if root.count() > 0:
            return root
    except Exception:
        pass

    return None


def _current_output_src(page) -> Optional[str]:
    # Prefer generated block
    root = _generated_block_root(page)
    if root is not None:
        try:
            imgs = root.locator("xpath=.//img").all()
            for img in reversed(imgs[-8:]):
                try:
                    if not img.is_visible():
                        continue
                except Exception:
                    pass
                src = (img.get_attribute("src") or "").strip()
                if src:
                    return src
        except Exception:
            pass

    # Fallback: last visible non-gallery img
    try:
        imgs = page.locator("img")
        cnt = imgs.count()
        for i in range(min(cnt, 20)):
            img = imgs.nth(cnt - 1 - i)
            try:
                if not img.is_visible():
                    continue
            except Exception:
                pass
            src = (img.get_attribute("src") or "").strip()
            if src:
                return src
    except Exception:
        pass
    return None


def _current_download_href(page) -> Optional[str]:
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
    return _current_download_href(page) or _current_output_src(page)


def _wait_download_ready(page, timeout_sec: float = 25.0) -> None:
    """Wait until either a download link or an output image src appears."""
    deadline = time.time() + max(1.0, float(timeout_sec))
    last_src = None
    while time.time() < deadline:
        try:
            if page.is_closed():
                return
        except Exception:
            pass

        try:
            if _current_download_href(page):
                return
        except Exception:
            pass

        try:
            cur_src = _current_output_src(page)
            if cur_src and cur_src != last_src:
                last_src = cur_src
        except Exception:
            pass

        time.sleep(0.3)


def _download_via_anchor(page, out_path: str, timeout_ms: int = 30_000) -> Optional[str]:
    try:
        root = _generated_block_root(page) or page
        a = root.locator("a.download-link[href]").last
        if a.count() == 0:
            return None
        href = (a.get_attribute("href") or "").split("?")[0]
        ext = os.path.splitext(href)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            ext = os.path.splitext(out_path)[1].lower() or ".png"
        if not out_path.lower().endswith(ext):
            out_path = str(Path(out_path).with_suffix(ext))

        with page.expect_download(timeout=timeout_ms) as dl_info:
            inner_btn = a.locator("button").last
            if inner_btn.count() > 0:
                inner_btn.click()
            else:
                a.click()
        download = dl_info.value
        os.makedirs(Path(out_path).parent, exist_ok=True)
        download.save_as(out_path)

        MIN_IMAGE_BYTES = 8 * 1024

        def _is_valid_image_header(path: Path) -> bool:
            try:
                with open(path, "rb") as f:
                    head = f.read(16)
                if head.startswith(b"\x89PNG\r\n\x1a\n"):
                    return True
                if head.startswith(b"\xff\xd8\xff"):
                    return True
                if head.startswith(b"RIFF") and b"WEBP" in head:
                    return True
            except Exception:
                return False
            return False

        # Wait for filesystem flush + stable size
        p = Path(out_path)
        last = -1
        stable_for = 0.0
        t0 = time.time()
        while time.time() - t0 < 6.0:
            try:
                if not p.exists():
                    time.sleep(0.2)
                    continue
                sz = p.stat().st_size
                if sz == last and sz > 0:
                    stable_for += 0.2
                else:
                    stable_for = 0.0
                last = sz
                if stable_for >= 1.0:
                    break
            except Exception:
                pass
            time.sleep(0.2)

        if p.exists():
            sz = p.stat().st_size
            if sz >= MIN_IMAGE_BYTES and _is_valid_image_header(p):
                return str(p)

            # Too small / not image => treat as failure
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as e:
        print(f"[tongyi anchor-download] failed: {e}")
    return None


def _save_image_from_src(src: str, out_path: str, page=None, referer: str = DEFAULT_SPACE_URL) -> str:
    os.makedirs(Path(out_path).parent, exist_ok=True)

    MIN_IMAGE_BYTES = 8 * 1024

    def _is_valid_image_header(path: Path) -> bool:
        try:
            with open(path, "rb") as f:
                head = f.read(16)
            if head.startswith(b"\x89PNG\r\n\x1a\n"):
                return True
            if head.startswith(b"\xff\xd8\xff"):
                return True
            if head.startswith(b"RIFF") and b"WEBP" in head:
                return True
        except Exception:
            return False
        return False

    def _atomic_write_bytes(final_path: str, data: bytes) -> str:
        p = Path(final_path)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

        sz = tmp.stat().st_size
        if sz < MIN_IMAGE_BYTES:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise TimeoutExceededError(f"Downloaded image too small ({sz} bytes)")

        if not _is_valid_image_header(tmp):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise TimeoutExceededError("Downloaded file does not look like an image (bad header)")

        os.replace(str(tmp), str(p))
        return str(p)

    def _write_bytes(data: bytes) -> str:
        return _atomic_write_bytes(out_path, data)

    def _write_b64_to_file(data_b64: str) -> str:
        import base64 as _b64
        return _write_bytes(_b64.b64decode(data_b64))

    if page is not None:
        # 1) APIRequestContext
        try:
            resp = page.context.request.get(src, timeout=60_000)
            if resp.ok:
                return _write_bytes(resp.body())
        except Exception as e:
            print(f"[tongyi save] context.request.get error: {e}")

        # 2) fetch inside page (handles protected URLs)
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
                src,
            )
            return _write_b64_to_file(data_b64)
        except Exception as e:
            print(f"[tongyi save] page.fetch error: {e}")

    # 3) requests fallback (non-blob)
    if not src.startswith("blob:"):
        try:
            r = requests.get(src, timeout=240, headers={"Referer": referer, "User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return _write_bytes(r.content)
        except Exception as e:
            raise TimeoutExceededError(f"Не удалось скачать изображение (requests): {e}")

    raise TimeoutExceededError("Не удалось скачать изображение (blob/protected URL)")


def _select_dropdown_option_by_text(page, option_text: str) -> bool:
    """Best-effort: open any dropdown and pick option by visible text.

    Used as a fallback.
    """

    def _try_pick() -> bool:
        try:
            # Prefer options rendered inside dropdown-options container (Gradio)
            opts_root = page.locator("#dropdown-options")
            if opts_root.count() > 0:
                opt = opts_root.get_by_text(option_text, exact=False)
                if opt.count() > 0:
                    opt.first.click()
                    return True
        except Exception:
            pass

        try:
            opt = page.get_by_text(option_text, exact=False)
            if opt.count() > 0:
                opt.first.click()
                return True
        except Exception:
            pass
        return False

    # Click any listbox inputs (per provided HTML)
    try:
        lbs = page.locator("input[role='listbox'][aria-controls='dropdown-options']")
        for j in range(min(lbs.count(), 6)):
            el = lbs.nth(j)
            try:
                el.scroll_into_view_if_needed()
                el.click(force=True)
                time.sleep(0.2)
                if _try_pick():
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return _try_pick()


def _select_width_height_ratio(page, option_text: str) -> bool:
    """Strict selector from provided HTML: Width x Height (Ratio) dropdown."""
    try:
        inp = page.locator("input[role='listbox'][aria-label='Width x Height (Ratio)']").first
        if inp.count() == 0:
            return False
        inp.scroll_into_view_if_needed()
        inp.click(force=True)
        time.sleep(0.15)

        # Options usually appear under #dropdown-options
        try:
            root = page.locator("#dropdown-options")
            if root.count() > 0:
                opt = root.get_by_text(option_text, exact=False)
                if opt.count() > 0:
                    opt.first.click(force=True)
                    return True
        except Exception:
            pass

        opt2 = page.get_by_text(option_text, exact=False)
        if opt2.count() > 0:
            opt2.first.click(force=True)
            return True
    except Exception:
        pass
    return False


def _wait_final_image_stable(page, start_src: Optional[str], min_stable_sec: float = 5.0, timeout_sec: int = 120) -> str:
    deadline = time.time() + timeout_sec
    last_src = None
    last_change = time.time()

    while time.time() < deadline:
        # If user manually closed the window, do not hang.
        # Important: some Playwright calls can transiently fail while the page is busy;
        # do NOT treat that as "page closed".
        try:
            if page.is_closed():
                raise TimeoutExceededError("Playwright page was closed while waiting for image")
        except Exception:
            # ignore transient Playwright errors here
            pass

        st = _toast_state(page)
        if st:
            kind, txt = st
            if kind == "quota":
                raise QuotaExceededError(txt)
            if kind == "nogpu":
                # This toast means HF ZeroGPU queue didn't allocate GPU within time.
                # Let caller decide whether to re-click Generate / reload / rotate VPN.
                raise NoGPUAvailableError(txt)
            if kind == "queue":
                time.sleep(2)
                continue

        cur_src = _current_output_src(page)
        if cur_src and cur_src == start_src:
            cur_src = None

        if cur_src != last_src:
            last_src = cur_src
            last_change = time.time()

        stable = (time.time() - last_change) >= min_stable_sec
        if cur_src and stable:
            return cur_src

        time.sleep(0.4)

    raise TimeoutExceededError("Не дождались стабилизации финального изображения")


def generate_one_image(
    space_url: str,
    prompt_text: str,
    out_path: str,
    headless: bool = True,
    timeout_sec: int = 240,
    cancel_check: Optional[callable] = None,
    size_text: str = TARGET_SIZE_TEXT,
) -> str:
    with sync_playwright() as p:
        if cancel_check and cancel_check():
            raise CancelledError("Отмена перед запуском браузера")

        browser = None
        context = None
        page = None
        last_err: Optional[Exception] = None

        def _close_safely() -> None:
            nonlocal context, browser
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

        try:
            for attempt in range(2):
                try:
                    browser = p.chromium.launch(headless=headless)
                    context = browser.new_context(accept_downloads=True)
                    page = context.new_page()

                    page.goto(space_url, wait_until="commit", timeout=120_000)
                    _ensure_page_bootstrapped(page, space_url)
                    _wait_app_ready(page, WAIT_TIMEOUT_MS)
                    last_err = None
                    break
                except CancelledError:
                    raise
                except (PWTimeoutError, PWError, TimeoutExceededError) as e:
                    last_err = e
                    if _is_dns_or_host_error(e):
                        raise TimeoutExceededError(f"DNS/host error while opening {space_url}: {e}")
                    if isinstance(e, TimeoutExceededError) and attempt == 0:
                        print("[tongyi boot-check] blank/loader detected; restarting browser (attempt 2/2)")
                        _close_safely()
                        page = None
                        continue
                    raise

            if last_err is not None:
                raise TimeoutExceededError(f"UI not ready: {last_err}")

            if cancel_check and cancel_check():
                raise CancelledError("Отменено")

            if not _set_prompt(page, prompt_text):
                raise RuntimeError("Не удалось установить Prompt")

            # Select size option. Prefer strict selector for "Width x Height (Ratio)".
            if size_text:
                ok_size = _select_width_height_ratio(page, size_text)
                if not ok_size:
                    ok_size = _select_dropdown_option_by_text(page, size_text)
                if not ok_size:
                    print(f"[tongyi] Warning: could not select size option: {size_text}")

            # We may need to retry on HF ZeroGPU GPU-allocation failure.
            nogpu_recover_left = 2

            while True:
                if cancel_check and cancel_check():
                    raise CancelledError("Отменено")

                start_src = _current_output_src(page)

                if not _click_generate(page):
                    raise RuntimeError("Кнопка Generate не найдена")

                try:
                    # Wait for enable/disable cycle in a generic way: simply wait for new stable image
                    src = _wait_final_image_stable(page, start_src=start_src, min_stable_sec=5.0, timeout_sec=int(timeout_sec))
                    break
                except NoGPUAvailableError as e:
                    if nogpu_recover_left <= 0:
                        raise
                    nogpu_recover_left -= 1

                    # 1) try a simple re-click (sometimes enough)
                    try:
                        time.sleep(2.0)
                        if _click_generate(page):
                            continue
                    except Exception:
                        pass

                    # 2) fallback: reload the app (UI can get stuck after the toast)
                    try:
                        page.reload(wait_until="commit", timeout=120_000)
                        _ensure_page_bootstrapped(page, space_url)
                        _wait_app_ready(page, WAIT_TIMEOUT_MS)
                        _set_prompt(page, prompt_text)
                        if size_text:
                            _select_width_height_ratio(page, size_text) or _select_dropdown_option_by_text(page, size_text)
                        continue
                    except Exception:
                        # give the outer layer a chance to rotate VPN / restart everything
                        raise

            # After src becomes stable, UI may still be rendering the download link.
            _wait_download_ready(page, timeout_sec=25.0)

            last_dl_err: Optional[Exception] = None
            saved: Optional[str] = None

            # Try a few times before giving up (HF sometimes lags download link / blob resolution)
            for _dl_attempt in range(3):
                if cancel_check and cancel_check():
                    raise CancelledError("Отменено")

                try:
                    saved = _download_via_anchor(page, out_path)
                    if saved:
                        return saved
                except Exception as e:
                    last_dl_err = e

                try:
                    best_url = _get_best_image_url(page) or src
                    ext = os.path.splitext(best_url.split("?")[0])[1].lower()
                    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                        ext = os.path.splitext(out_path)[1].lower() or ".png"
                    if not out_path.lower().endswith(ext):
                        out_path = str(Path(out_path).with_suffix(ext))
                    saved = _save_image_from_src(best_url, out_path, page=page, referer=space_url)
                    return saved
                except Exception as e:
                    last_dl_err = e

                time.sleep(1.0)

            if last_dl_err:
                raise last_dl_err
            raise TimeoutExceededError("Не удалось скачать изображение")
        finally:
            _close_safely()


def _sanitize_filename(name: str, max_len: int = 80) -> str:
    name = (name or "").strip().lower()
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"[^a-z0-9\-\._ ]+", "", name)
    name = name.replace(" ", "-")
    name = re.sub(r"-+", "-", name).strip("-._")
    if not name:
        name = "prompt"
    return name[:max_len]


def generate_batch(
    space_url: str,
    prompts: List[str],
    out_dir: str,
    headless: bool = True,
    hosts_start_pos: Optional[int] = None,
    cancel_check: Optional[callable] = None,
    max_wait_sec: int = 240,
    size_text: str = TARGET_SIZE_TEXT,
) -> List[str]:
    """Legacy helper: returns only successfully saved paths.

    For Streamlit usage where you need per-item errors/progress, prefer
    `generate_batch_detailed`.
    """
    res = generate_batch_detailed(
        space_url=space_url,
        prompts=prompts,
        out_dir=out_dir,
        headless=headless,
        hosts_start_pos=hosts_start_pos,
        cancel_check=cancel_check,
        max_wait_sec=max_wait_sec,
        size_text=size_text,
        progress_callback=None,
    )
    return [r["path"] for r in res if r.get("path")]


def generate_batch_detailed(
    space_url: str,
    prompts: List[str],
    out_dir: str,
    headless: bool = True,
    hosts_start_pos: Optional[int] = None,
    cancel_check: Optional[callable] = None,
    max_wait_sec: int = 240,
    size_text: str = TARGET_SIZE_TEXT,
    progress_callback: Optional[callable] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    on_image_saved: Optional[Callable[[int, int, str], str]] = None,
) -> list[dict]:
    """Generate images sequentially.

    Returns a list of dicts: {"prompt": str, "path": str|"", "error": str|None}.

    `progress_callback(i, total, prompt, result_dict)` is called after each item.
    """
    out_dir = str(Path(out_dir).resolve())
    os.makedirs(out_dir, exist_ok=True)

    # attach UI log hook for this run
    global _LOG_HOOK
    _prev_hook = _LOG_HOOK
    _LOG_HOOK = log_callback

    hosts = _ensure_image_hosts("good_hosts_for_images.txt")
    if not hosts:
        raise SystemExit("Файл good_hosts_for_images.txt пуст или отсутствует.")

    global _IMAGE_HOST_POS, _IMAGE_POS_INITIALIZED
    if not _IMAGE_POS_INITIALIZED:
        if hosts_start_pos is not None:
            _IMAGE_HOST_POS = int(hosts_start_pos) % len(hosts)
        _IMAGE_POS_INITIALIZED = True

    results: list[dict] = []

    total = len(prompts)
    for i, prompt in enumerate(prompts, 1):
        if cancel_check and cancel_check():
            raise CancelledError("Отменено")

        safe_stub = _sanitize_filename(prompt)[:40]
        fname = f"{i:03d}-{safe_stub or 'image'}.png"
        out_path = str(Path(out_dir) / fname)

        item = {"prompt": prompt, "path": "", "error": None}

        # If HF ZeroGPU queue didn't allocate GPU, prefer a few local retries before rotating VPN.
        soft_retry_left = 3

        while True:
            if cancel_check and cancel_check():
                raise CancelledError("Отменено")
            try:
                saved = generate_one_image(
                    space_url=space_url,
                    prompt_text=prompt,
                    out_path=out_path,
                    headless=headless,
                    timeout_sec=int(max_wait_sec),
                    cancel_check=cancel_check,
                    size_text=size_text,
                )

                # Allow caller to rename/move file immediately (e.g. to 1_pro_...)
                if on_image_saved is not None:
                    try:
                        saved = str(on_image_saved(i, total, saved) or saved)
                    except Exception as _cb_e:
                        _log(f"[tongyi] on_image_saved failed: {_cb_e}")

                item["path"] = saved
                item["error"] = None
                break
            except CancelledError:
                raise
            except Exception as e:
                msg = str(e) or repr(e)
                item["error"] = msg

                # HF ZeroGPU queue sometimes can't allocate GPU even though quota is ok.
                # In that case it's often enough to just retry Generate without VPN rotate.
                if (
                    soft_retry_left > 0
                    and (
                        isinstance(e, NoGPUAvailableError)
                        or "No GPU was available" in msg
                        or "ZeroGPU" in msg
                        or "higher priority" in msg
                        or "queues" in msg
                    )
                ):
                    wait_s = 5 * (4 - soft_retry_left)  # 5, 10, 15...
                    _log(f"[Prompt {i}] No GPU allocated yet. Retrying without VPN in {wait_s}s (left={soft_retry_left})")
                    soft_retry_left -= 1
                    time.sleep(wait_s)
                    continue

                _log(f"[Prompt {i}] Will rotate due to: {e}")
                rotated = False
                tried = 0
                while tried < len(hosts):
                    if cancel_check and cancel_check():
                        raise CancelledError("Отменено")
                    host = hosts[_IMAGE_HOST_POS % len(hosts)]
                    _log(f"Rotating VPN to host: {host}")
                    if switch_vpn_to_full_host(host, retries=2, backoff=1.5, pause_after_connect=1.0):
                        time.sleep(1.5)
                        rotated = True
                        _IMAGE_HOST_POS = (_IMAGE_HOST_POS + 1) % len(hosts)
                        break
                    _IMAGE_HOST_POS = (_IMAGE_HOST_POS + 1) % len(hosts)
                    tried += 1
                if not rotated:
                    raise TimeoutExceededError("VPN rotation failed on all hosts")
                continue

        results.append(item)

        # planned rotate between prompts
        if i < total:
            rotated = False
            tried = 0
            while tried < len(hosts):
                if cancel_check and cancel_check():
                    raise CancelledError("Отменено")
                host = hosts[_IMAGE_HOST_POS % len(hosts)]
                _log(f"Planned rotate to host: {host}")
                if switch_vpn_to_full_host(host, retries=2, backoff=1.5, pause_after_connect=1.0):
                    time.sleep(1.5)
                    rotated = True
                    _IMAGE_HOST_POS = (_IMAGE_HOST_POS + 1) % len(hosts)
                    break
                _IMAGE_HOST_POS = (_IMAGE_HOST_POS + 1) % len(hosts)
                tried += 1
            if not rotated:
                _log("[tongyi] Warning: could not rotate VPN to any host")

        if progress_callback is not None:
            try:
                progress_callback(i, total, prompt, item)
            except Exception:
                pass

    _LOG_HOOK = _prev_hook
    return results
