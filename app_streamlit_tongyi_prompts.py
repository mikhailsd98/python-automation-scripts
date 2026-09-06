import os
import json
import streamlit as st
import traceback
import sys
import asyncio
import time
from datetime import datetime
from pathlib import Path
import streamlit.components.v1 as components

import generate_tongyi_images as gti

# На Windows нужен ProactorEventLoop для subprocess в Playwright
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def copy_path_button(path: str, key: str):
    import json as _json

    html = """
    <button id="{id}" style="margin-top:6px;padding:2px 8px;font-size:12px">Скопировать путь</button>
    <script>
    const btn = document.getElementById("{id}");
    btn.addEventListener("click", async () => {{
        try {{
            await navigator.clipboard.writeText({text});
            const old = btn.innerText;
            btn.innerText = "Скопировано!";
            setTimeout(() => btn.innerText = old, 1200);
        }} catch (e) {{
            console.error(e);
            alert("Не удалось скопировать");
        }}
    }});
    </script>
    """.format(id=f"cpy_{key}", text=_json.dumps(path))
    components.html(html, height=40)


st.set_page_config(page_title="Tongyi Image Generator (Prompts)", layout="wide")

st.title("Tongyi Image Generator (Prompts)")
st.caption("Вводите готовые промпты (по одному на строку) — и скрипт сгенерирует изображения по очереди, с ротацией VPN как раньше.")

space_url = st.text_input(
    "Tongyi Space URL",
    value=os.environ.get("TONGYI_SPACE_URL", gti.DEFAULT_SPACE_URL),
)

size_text = st.text_input(
    "Размер (select option text)",
    value=os.environ.get("TONGYI_SIZE_TEXT", gti.TARGET_SIZE_TEXT),
    help="Текст опции в select. По задаче: 832x1248 ( 2:3 )",
)

headless = st.checkbox("Запуск без окна браузера (headless)", value=True)

col_t1, col_t2 = st.columns(2)
with col_t1:
    max_wait_images_min = st.number_input(
        "Максимальное ожидание 1 изображения (мин)",
        min_value=1,
        max_value=60,
        value=6,
        step=1,
    )
with col_t2:
    vpn_hosts_start_pos = st.number_input(
        "С какого хоста в good_hosts_for_images.txt начинать (позиция)",
        min_value=0,
        value=0,
        step=1,
    )

st.markdown("---")

st.subheader("Промпты")

uploaded_txt = st.file_uploader("Загрузить .txt со списком промптов (опционально)", type=["txt"], accept_multiple_files=False)

default_prompts = """A cinematic portrait photo of a woman in a red dress, soft lighting, shallow depth of field
A realistic product photo of a modern smartwatch on a wooden table, studio lighting"""

prompts_text = st.text_area(
    "Промпты (1 строка = 1 картинка)",
    value="" if uploaded_txt else default_prompts,
    height=240,
)

if uploaded_txt is not None:
    try:
        raw = uploaded_txt.getvalue().decode("utf-8", errors="ignore")
        prompts_text = raw
        st.info(f"Загружено из файла: {uploaded_txt.name}")
    except Exception as e:
        st.error(f"Не удалось прочитать TXT: {e}")


def _parse_prompts(txt: str) -> list[str]:
    out: list[str] = []
    for line in (txt or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            continue
        out.append(s)
    return out


prompts = _parse_prompts(prompts_text)
st.caption(f"Всего промптов: {len(prompts)}")


def create_generation_root(parent: str = "generated_images") -> str:
    date_str = time.strftime("%Y-%m-%d")
    base = os.path.abspath(parent)
    root = os.path.join(base, date_str)
    if not os.path.exists(root):
        os.makedirs(root, exist_ok=True)
        return root
    i = 2
    while True:
        cand = os.path.join(base, f"{date_str}_{i}")
        if not os.path.exists(cand):
            os.makedirs(cand, exist_ok=True)
            return cand
        i += 1


PERSIST_FILE = os.path.abspath("result.json")


def _load_persisted_state():
    if os.path.exists(PERSIST_FILE):
        try:
            with open(PERSIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _persist_state():
    data = {
        "tongyi_results": st.session_state.get("tongyi_results", []),
    }
    try:
        with open(PERSIST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


if "_state_loaded" not in st.session_state:
    persisted = _load_persisted_state()
    if persisted:
        st.session_state["tongyi_results"] = persisted.get("tongyi_results", [])
    st.session_state["_state_loaded"] = True


if "stop_gen" not in st.session_state:
    st.session_state["stop_gen"] = False


# Ctrl+C handling
try:
    import signal
    import threading

    if "_sigint_installed" not in st.session_state:
        st.session_state["_sigint_installed"] = True
        _rovodev_stop_event = threading.Event()

        def _rovodev_sig_handler(signum, frame):
            st.session_state["stop_gen"] = True
            try:
                _rovodev_stop_event.set()
            except Exception:
                pass
            try:
                _persist_state()
            except Exception:
                pass
            print("\nStopping...\n")

        signal.signal(signal.SIGINT, _rovodev_sig_handler)
        if hasattr(signal, "SIGTERM"):
            try:
                signal.signal(signal.SIGTERM, _rovodev_sig_handler)
            except Exception:
                pass

    def _is_cancelled() -> bool:
        try:
            return bool(st.session_state.get("stop_gen")) or (_rovodev_stop_event.is_set() if "_rovodev_stop_event" in globals() else False)
        except Exception:
            return bool(st.session_state.get("stop_gen"))

except Exception:

    def _is_cancelled() -> bool:
        return bool(st.session_state.get("stop_gen"))


st.markdown("---")

col1, col2 = st.columns(2)
with col1:
    start_btn = st.button("Запустить генерацию", type="primary", disabled=(len(prompts) == 0))
with col2:
    stop_btn = st.button("Остановить")
    if stop_btn:
        st.session_state["stop_gen"] = True
        st.warning("Запрошена остановка. Дождитесь завершения текущего шага...")
        _persist_state()


if start_btn:
    st.session_state["stop_gen"] = False
    _persist_state()

    gen_root = create_generation_root("generated_images")
    run_dir = os.path.abspath(os.path.join(gen_root, f"tongyi_{datetime.now().strftime('%H%M%S')}"))
    os.makedirs(run_dir, exist_ok=True)

    prog = st.progress(0.0)
    log_box = st.empty()
    _log_lines: list[str] = []

    def _cb(i: int, total: int, prompt: str, item: dict):
        # Update progress + short log
        prog.progress(i / max(1, total))
        status = "OK" if item.get("path") else "ERR"
        short = (prompt or "").replace("\n", " ")[:120]
        _log_lines.append(f"[{i}/{total}] {status}: {short}")
        _log_lines[:] = _log_lines[-20:]
        log_box.code("\n".join(_log_lines), language=None)

    with st.spinner("Генерация изображений..."):
        try:
            results = gti.generate_batch_detailed(
                space_url=space_url.strip() or gti.DEFAULT_SPACE_URL,
                prompts=prompts,
                out_dir=run_dir,
                headless=headless,
                hosts_start_pos=int(vpn_hosts_start_pos),
                cancel_check=_is_cancelled,
                max_wait_sec=int(max_wait_images_min * 60),
                size_text=size_text.strip() or gti.TARGET_SIZE_TEXT,
                progress_callback=_cb,
            )
        except gti.CancelledError:
            st.warning("Отменено")
            results = []
        except Exception as e:
            msg = str(e) or repr(e)
            st.error(f"Ошибка: {msg}")
            st.code(traceback.format_exc())
            results = []

    st.session_state["tongyi_results"] = results
    _persist_state()


# Render results
st.markdown("---")
st.subheader("Результаты")

rows = st.session_state.get("tongyi_results") or []
if not rows:
    st.info("Пока нет результатов")
else:
    for i, r in enumerate(rows, 1):
        prompt = r.get("prompt") or ""
        path = r.get("path") or ""
        err = r.get("error")
        title = f"{i:03d}: {prompt[:80]}" + ("..." if len(prompt) > 80 else "")
        with st.expander(title, expanded=False):
            st.code(prompt)
            if err:
                st.error(err)
            if path and os.path.exists(path):
                st.image(path, caption=os.path.basename(path), use_column_width=True)
                copy_path_button(path, key=f"tongyi_{i}")
            else:
                st.warning("Файл не найден")
