import os
import json
import tempfile
import streamlit as st
from typing import Optional
import traceback
import sys
import asyncio
import time
from datetime import datetime
import secrets
import streamlit.components.v1 as components
import generate_wan2_video as gw2
from proxy_v_generator import set_vpn_server_in_pbk as vpn_set_server, connect as vpn_connect, ensure_disconnected as vpn_ensure_disconnected, VPN_NAME, USERNAME, PASSWORD

# На Windows нужен ProactorEventLoop для subprocess в Playwright
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Standalone app keeps the old safe default (VPN required), but the unified
# pipeline can explicitly set GEMINI_REQUIRE_VPN=0 before importing/rendering us.
os.environ.setdefault("GEMINI_REQUIRE_VPN", "1")

# Импортируем ваши функции (используют ротацию моделей/ключей и inlineData)
import generate_pinterest_texts as gptx
import pinterest_csv_helpers as pch
# Новый модуль Playwright-автоматизации для LoRA-страницы
import generate_flux_lora_images as gfl
# Модуль генерации видео (WAN2)
import generate_wan2_video as gw2

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


# ------------------------ Entrypoint ------------------------
def main() -> None:
    st.set_page_config(page_title="Pinterest Generator (Gemini)", layout="wide")

    st.title("Pinterest Content Generator (Gemini)")
    st.caption("Перетащите изображение ниже. Сервис локальный — ключи и файлы никуда не отправляются, кроме Gemini API.")

    # URL страницы LoRA Space для генерации изображений
    space_url = st.text_input(
        "FLUX LoRA Space URL",
        value=os.environ.get("FLUX_LORA_SPACE_URL", "https://prithivmlmods-flux-lora-dlc.hf.space"),
        help="Укажите URL страницы FLUX LoRA (Gradio), на которой будет запускаться генерация изображений.",
    )

    # URL страницы WAN2 для генерации видео
    wan2_space_url = st.text_input(
        "WAN2 Video Space URL",
        value=os.environ.get("WAN2_SPACE_URL", getattr(gw2, "SPACE_URL", "")),
        help="URL Hugging Face Space для генерации видео (WAN2).",
    )

    # Выбор LoRA моделей для генерации изображений
    st.markdown("---")
    st.subheader("Выбор LoRA моделей")
    _available_loras = getattr(gfl, "TARGET_LORAS", [
        "Indo Realism",
        "Flux Face Realism",
        "Super Portraits",
        "Epic Realism",
        "Ultra Realism",
    ])
    # Инициализация состояния выбора (по умолчанию выбран только Flux Face Realism)
    if "selected_loras" not in st.session_state:
        try:
            if "Flux Face Realism" in _available_loras:
                st.session_state["selected_loras"] = ["Flux Face Realism"]
            else:
                st.session_state["selected_loras"] = [str(_available_loras[0])] if _available_loras else []
        except Exception:
            st.session_state["selected_loras"] = []

    # Рисуем чекбоксы по моделям
    cols = st.columns(5, gap="small")
    _current_selected = set(st.session_state.get("selected_loras", []))
    _new_selected = []
    for i, lname in enumerate(_available_loras):
        with cols[i % 5]:
            key = f"lora_sel_{i}"
            checked = st.checkbox(lname, value=(lname in _current_selected), key=key)
            if checked:
                _new_selected.append(lname)

    # Кнопки выбрать все/очистить
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Выбрать все LoRA"):
            _new_selected = list(_available_loras)
    with c2:
        if st.button("Снять все выделения"):
            _new_selected = []

    # Обновляем состояние
    st.session_state["selected_loras"] = _new_selected
    st.caption(f"Выбрано LoRA: {len(_new_selected)} из {len(_available_loras)}")
    st.markdown("---")

    pipeline_vpn_enabled = os.getenv("GEMINI_REQUIRE_VPN", "1").strip() == "1"

    headless = st.checkbox("Запуск без окна браузера (headless)", value=True)
    if not pipeline_vpn_enabled:
        st.session_state["stage4_vpn_rotate_video"] = False
    vpn_rotate_video_ui = st.checkbox(
        "Ротация VPN при квоте (для видео)",
        value=bool(pipeline_vpn_enabled),
        disabled=not bool(pipeline_vpn_enabled),
        key="stage4_vpn_rotate_video",
    )
    vpn_rotate_video = bool(vpn_rotate_video_ui and pipeline_vpn_enabled)
    vpn_one_try = st.checkbox(
        "VPN: одна попытка подключения (retries=1)",
        value=bool(st.session_state.get("vpn_one_try", False)),
        key="vpn_one_try",
        help="Если включить — при переключении VPN на конкретный хост будет только 1 попытка вместо 2.",
    )
    vpn_retries = 1 if vpn_one_try else 2
    # Apply to Gemini-side VPN rotation too (generate_pinterest_texts.py)
    os.environ["GEMINI_VPN_RETRIES"] = "1" if vpn_one_try else "2"
    os.environ.setdefault("GEMINI_VPN_BACKOFF", "1.5")
    # Таймауты ожидания
    col_t1, col_t2, col_t3 = st.columns(3)
    with col_t1:
        max_wait_min = st.number_input("Максимальное ожидание видео (мин)", min_value=1, max_value=60, value=5, step=1)
    with col_t2:
        queue_wait_min = st.number_input("Макс. ожидание очереди (мин)", min_value=1, max_value=30, value=5, step=1)
    with col_t3:
        max_wait_images_min = st.number_input(
            "Максимальное ожидание изображения (мин)",
            min_value=1,
            max_value=60,
            value=5,
            step=1,
            help="Таймаут на одну LoRA-картинку (ожидание генерации/скачивания в HF Space).",
        )

    st.session_state["max_wait_images_min"] = max_wait_images_min

    vpn_hosts_file = st.text_input(
        "TXT со списком рабочих VPN-хостов (для видео)",
        value="good_hosts_for_videos.txt",
        help="Один хост на строку, например 74-80-180-218.hideservers.net",
        disabled=not bool(pipeline_vpn_enabled),
    )
    vpn_hosts_start_pos = st.number_input(
        "С какого хоста в списке начинать (позиция, 0 = первый)",
        min_value=0, value=0, step=1, disabled=not bool(pipeline_vpn_enabled)
    )

    # Global scheduling setting (applies to Pinterest CSV schedule)
    st.session_state.setdefault("pins_per_day", 10)

    pins_per_day = st.number_input(
        "Максимум пинов в день (auto schedule)",
        min_value=1,
        max_value=100,
        value=int(st.session_state.get("pins_per_day") or 10),
        step=1,
        help="Сколько пинов максимум ставить на один день. Остальные переносятся на следующие дни.",
    )
    st.session_state["pins_per_day"] = int(pins_per_day)

    # Unified pipeline uses this flag to disable its autorefresh while Stage4 runs.
    # Safe in standalone mode too.
    st.session_state.setdefault("stage4_running", False)

    uploaded = st.file_uploader(
        "Перетащите сюда изображения (JPEG/PNG/WebP) — можно несколько",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
        key="stage4_uploaded_files",
    )

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

    # Persistent cache file
    PERSIST_FILE = os.path.abspath("result.json")

    def _load_persisted_state():
        if os.path.exists(PERSIST_FILE):
            try:
                with open(PERSIST_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data
            except Exception:
                return {}
        return {}

    def _persist_state():
        data = {
            "gen_results": [
                {
                    "upload_name": up.name,
                    "kw": kw,
                    "saved_paths": saved_paths,
                    "out_dir": out_dir,
                }
                for (up, kw, saved_paths, out_dir) in st.session_state.get("gen_results", [])
            ],
            "video_selection": st.session_state.get("video_selection", {}),
            "pinterest_selection": st.session_state.get("pinterest_selection", {}),
            "generated_videos": st.session_state.get("generated_videos", []),
            "pinterest_results": st.session_state.get("pinterest_results", {}),
            "selected_loras": st.session_state.get("selected_loras", []),
        }
        try:
            with open(PERSIST_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # Load persisted on startup
    if "_state_loaded" not in st.session_state:
        persisted = _load_persisted_state()
        if persisted:
            # uploaded files cannot be restored, but we can reconstruct items with paths
            st.session_state["gen_results"] = []
            for item in persisted.get("gen_results", []):
                up_stub = type("UploadStub", (), {"name": item.get("upload_name", "")})()
                st.session_state["gen_results"].append((up_stub, item.get("kw", {}), item.get("saved_paths", []), item.get("out_dir")))
            st.session_state["video_selection"] = persisted.get("video_selection", {})
            st.session_state["pinterest_selection"] = persisted.get("pinterest_selection", {})
            st.session_state["generated_videos"] = persisted.get("generated_videos", [])
            st.session_state["pinterest_results"] = persisted.get("pinterest_results", {})
            if persisted.get("selected_loras") is not None:
                st.session_state["selected_loras"] = persisted.get("selected_loras", [])
        st.session_state["_state_loaded"] = True

    # Stop flags and Ctrl+C handling
    if "stop_images" not in st.session_state:
        st.session_state["stop_images"] = False
    if "stop_videos" not in st.session_state:
        st.session_state["stop_videos"] = False

    # Install Ctrl+C (SIGINT) handler to cancel long-running tasks but keep app alive
    try:
        import signal
        import threading
        if "_sigint_installed" not in st.session_state:
            st.session_state["_sigint_installed"] = True
            _rovodev_stop_event = threading.Event()

            def _rovodev_sig_handler(signum, frame):
                # set flags so current generation cooperatively stops
                st.session_state["stop_images"] = True
                st.session_state["stop_videos"] = True
                try:
                    _rovodev_stop_event.set()
                except Exception:
                    pass
                try:
                    _persist_state()
                except Exception:
                    pass
                print("\n  Stopping...\n  Stopping...\n")

            signal.signal(signal.SIGINT, _rovodev_sig_handler)
            # Also try to handle SIGTERM where available
            if hasattr(signal, "SIGTERM"):
                try:
                    signal.signal(signal.SIGTERM, _rovodev_sig_handler)
                except Exception:
                    pass

        def _is_cancelled():
            try:
                return st.session_state.get("stop_images") or st.session_state.get("stop_videos") or (_rovodev_stop_event.is_set() if '_rovodev_stop_event' in globals() else False)
            except Exception:
                return st.session_state.get("stop_images") or st.session_state.get("stop_videos")
    except Exception:
        def _is_cancelled():
            return st.session_state.get("stop_images") or st.session_state.get("stop_videos")

    if uploaded:
        files = uploaded  # список UploadedFile
        st.info(f"Выбрано файлов: {len(files)}")

        col_gen, col_stop = st.columns(2)
        with col_gen:
            start_images = st.button(f"Сгенерировать контент для {len(files)} изображений", type="primary")

        # Unified pipeline can trigger this stage without clicking the button
        if bool(st.session_state.get("stage4_autorun")):
            start_images = True
            # clear flag so it doesn't rerun forever
            st.session_state["stage4_autorun"] = False
        with col_stop:
            stop_images_btn = st.button("Остановить генерацию изображений")
            if stop_images_btn:
                st.session_state["stop_images"] = True
                st.warning("Запрошена остановка генерации изображений. Дождитесь завершения текущего шага...")
                _persist_state()
        if start_images:
            st.session_state["stop_images"] = False
            st.session_state["stage4_running"] = True

            # Unified pipeline: mark Stage4 as started for the current Stage3 run_id
            # so the orchestrator doesn't keep re-queuing/trying to autorun repeatedly.
            try:
                rid = str(st.session_state.get("unified_current_stage3_run_id") or "").strip()
                if rid:
                    st.session_state["unified_stage4_started_for_stage3_run_id"] = rid
            except Exception:
                pass

            _persist_state()
            try:
                with st.spinner("Генерация... (каждая картинка 10–60 сек на текст + 1–3 мин на выбранные LoRA изображения)"):
                    gen_root = create_generation_root("generated_images")
                    results = []
                    for i, up in enumerate(files, 1):
                        st.write(f"Обработка [{i}/{len(files)}]: {up.name}")
                        suffix = os.path.splitext(up.name or "")[1] or ".jpg"
                        import tempfile
                        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                            tmp.write(up.getbuffer())
                            tmp_path = tmp.name
                        try:
                            if st.session_state.get("stop_images"):
                                raise KeyboardInterrupt("Остановлено пользователем")
                            kw = gptx.generate_keywords_and_image_prompt(tmp_path)

                            # После успешного обращения к Gemini — переключаем VPN на следующий хост для генерации картинок
                            if pipeline_vpn_enabled:
                                if "img_vpn_hosts" not in st.session_state:
                                    try:
                                        st.session_state["img_vpn_hosts"] = gw2.load_hosts_file("good_hosts_for_images.txt")
                                    except Exception:
                                        st.session_state["img_vpn_hosts"] = []
                                if "img_vpn_pos" not in st.session_state:
                                    st.session_state["img_vpn_pos"] = 0

                                img_hosts = st.session_state.get("img_vpn_hosts") or []
                                if img_hosts:
                                    host = img_hosts[st.session_state["img_vpn_pos"] % len(img_hosts)]
                                    st.write(f"Переключаю VPN (для изображений) на хост: {host}")
                                    if gw2.switch_vpn_to_full_host(host, retries=int(vpn_retries), backoff=1.5, pause_after_connect=1.0):
                                        st.session_state["img_vpn_pos"] = (st.session_state["img_vpn_pos"] + 1) % len(img_hosts)

                            # Генерация изображений по выбранным LoRA после получения prompt
                            prompt_text = kw.get("image_prompt") or ""
                            image_base = os.path.splitext(up.name)[0]
                            out_dir = os.path.abspath(os.path.join(gen_root, image_base))
                            os.makedirs(out_dir, exist_ok=True)

                            saved_paths = []
                            if prompt_text and space_url.strip():
                                selected_loras = st.session_state.get("selected_loras", [])
                                if not selected_loras:
                                    st.warning("Не выбраны LoRA модели. Откройте секцию 'Выбор LoRA моделей' и отметьте хотя бы одну.")
                                else:
                                    st.write(f"Запуск генерации LoRA изображений ({len(selected_loras)} шт)...")
                                    try:
                                        saved_paths = gfl.generate_all_lora_variants(
                                            space_url=space_url.strip(),
                                            prompt_text=prompt_text,
                                            out_dir=out_dir,
                                            loras=selected_loras,
                                            headless=headless,
                                            hosts_start_pos=int(vpn_hosts_start_pos),
                                            cancel_check=_is_cancelled,
                                            max_wait_sec=int(st.session_state.get("max_wait_images_min", 6) * 60),
                                            enable_vpn_rotation=bool(pipeline_vpn_enabled),
                                        )

                                        # Rename generated files to random alphanumeric names (do not leak LoRA name).
                                        renamed: list[str] = []
                                        for p in saved_paths:
                                            try:
                                                ext = os.path.splitext(p)[1] or ".png"
                                                # Try a few times to avoid collisions.
                                                new_path = None
                                                for _ in range(10):
                                                    token = secrets.token_hex(8)
                                                    cand = os.path.join(os.path.dirname(p), f"{token}{ext}")
                                                    if not os.path.exists(cand):
                                                        new_path = cand
                                                        break
                                                if new_path is None:
                                                    renamed.append(p)
                                                    continue
                                                os.replace(p, new_path)
                                                renamed.append(new_path)
                                            except Exception:
                                                renamed.append(p)
                                        saved_paths = renamed

                                        st.success(f"Сохранено {len(saved_paths)} изображений в папку: {out_dir}")
                                    except Exception as e:
                                        st.error("Не удалось сгенерировать LoRA-изображения. Смотрите детализацию ниже.")
                                        st.exception(e)
                                        st.code(traceback.format_exc())
                            else:
                                st.warning("Промпт пуст или не задан URL Space — пропущена генерация изображений.")

                            results.append((up, kw, saved_paths, out_dir))
                        except KeyboardInterrupt as e:
                            st.warning(str(e) or "Остановлено пользователем")
                            break
                        except Exception as e:
                            # Важно: если VPN не подключился, gptx теперь кидает RuntimeError чтобы не идти в Gemini без VPN.
                            msg = str(e) or repr(e)
                            if "VPN" in msg or "SSTP" in msg or "Gemini request is blocked" in msg:
                                st.error(
                                    "VPN не подключился — запрос к Gemini заблокирован (чтобы не использовать обычный IP).\n"
                                    f"Детали: {msg}"
                                )
                            else:
                                st.error(f"Ошибка при обработке изображения: {msg}")

                            # сохраняем запись, чтобы UI не ломался и было видно, на каком файле упало
                            kw = {"keywords_30": None, "image_prompt": "", "_error": msg}
                            results.append((up, kw, [], ""))
                        finally:
                            try:
                                os.remove(tmp_path)
                            except Exception:
                                pass

                    # сохраняем результаты в сессию, чтобы чекбоксы не сбрасывались при каждом клике
                    st.session_state["gen_results"] = results
            finally:
                st.session_state["stage4_running"] = False

    # --- дальше код уже вне if st.button(...), но внутри if uploaded: ---
        st.markdown("---")
        # читаем результаты из сессии (если есть)
        data = st.session_state.get("gen_results", [])
        for up, kw, saved_paths, out_dir in data:
            with st.expander(up.name, expanded=False):
                st.image(up, caption="Предпросмотр", width=320)
                left, right = st.columns(2, gap="large")

                with left:
                    st.subheader("30 long-tail ключевых фраз")
                    if kw.get("keywords_30") is None:
                        if kw.get("_error"):
                            st.warning(f"Не удалось получить ключевые фразы: {kw.get('_error')}")
                        else:
                            st.warning("Контент заблокирован API при попытке получить ключевые фразы.")
                    else:
                        st.text_area("Копируйте (Ctrl+C)", kw.get("keywords_30", ""), height=160)

                    st.subheader("Итоговый prompt для генерации изображения")
                    st.text_area("Копируйте (Ctrl+C)", kw.get("image_prompt", "") or "", height=140)

                with right:
                    st.subheader("Данные для Pinterest")
                    st.info("Пока не сгенерировано. Отметьте ниже нужные изображения и нажмите кнопку 'Сгенерировать Pinterest данные' внизу.")

                # Превью сгенерированных LoRA изображений с чекбоксами
                st.markdown(f"**LoRA версии ({len(saved_paths)} шт)**")
                if saved_paths:
                    cols = st.columns(5, gap="small")
                    for idx, p in enumerate(saved_paths):
                        with cols[idx % 5]:
                            safe_key = p.replace("\\", "_").replace("/", "_").replace(":", "_").replace(".", "_")

                            st.image(p, caption=os.path.basename(p), use_column_width=True)
                            copy_path_button(p, key=f"img_{safe_key}")

                            state_key = f"video_sel_{safe_key}"
                            if "video_selection" not in st.session_state:
                                st.session_state["video_selection"] = {}
                            if state_key not in st.session_state:
                                st.session_state[state_key] = bool(st.session_state["video_selection"].get(p, False))
                            checked = st.checkbox("Выбрать для видео", key=state_key)
                            st.session_state["video_selection"][p] = checked

                            pin_state_key = f"pin_sel_{safe_key}"
                            if "pinterest_selection" not in st.session_state:
                                st.session_state["pinterest_selection"] = {}
                            if pin_state_key not in st.session_state:
                                st.session_state[pin_state_key] = bool(st.session_state["pinterest_selection"].get(p, False))
                            checked_pin = st.checkbox("Выбрать для Pinterest", key=pin_state_key)
                            st.session_state["pinterest_selection"][p] = checked_pin
                else:
                    st.info("Изображения не были сгенерированы.")
    else:
        st.info("Загрузите изображения, чтобы начать.")

    # Внизу страницы — секция запуска видео-генерации по отмеченным картинкам
    # Persistent store for generated videos
    if "generated_videos" not in st.session_state:
        st.session_state["generated_videos"] = []

    selected = [p for p, v in st.session_state.get("video_selection", {}).items() if v]
    if selected:
        st.info(f"Выбрано изображений для видео: {len(selected)}")
        colv1, colv2 = st.columns(2)
        with colv1:
            start_videos_btn = st.button(f"Сгенерировать видео ({len(selected)})", type="primary")
        with colv2:
            stop_videos_btn = st.button("Остановить генерацию видео")
            if stop_videos_btn:
                st.session_state["stop_videos"] = True
                st.warning("Запрошена остановка генерации видео. Дождитесь завершения текущего шага...")
                _persist_state()
        if start_videos_btn:
            st.session_state["stop_videos"] = False
            _persist_state()
            with st.spinner("Генерация видео..."):
                saved_videos = []
                # подготовка списка хостов и позиции для ротации
                hosts = gw2.load_hosts_file(vpn_hosts_file) if vpn_rotate_video else []
                vh_pos = int(vpn_hosts_start_pos)
                if vpn_rotate_video and hosts:
                    host = hosts[vh_pos % len(hosts)]
                    st.write(f"Стартую VPN на хост: {host}")
                    if gw2.switch_vpn_to_full_host(host, retries=int(vpn_retries), backoff=1.5, pause_after_connect=1.0):
                        vh_pos = (vh_pos + 1) % len(hosts)
                for i, img_path in enumerate(selected, 1):
                    if st.session_state.get("stop_videos"):
                        st.info("Остановка по запросу пользователя.")
                        break
                    out_path = os.path.splitext(img_path)[0] + ".mp4"

                    attempts = 0 
                    while True:
                        try:
                            saved = gw2.generate(
                                wan2_space_url.strip() or gw2.SPACE_URL,
                                img_path,
                                out_path,
                                headless=headless,
                                max_wait_sec=int(max_wait_min * 60),
                                queue_max_wait_sec=int(queue_wait_min * 60),
                                cancel_check=_is_cancelled,
                            )
                            saved_videos.append(saved)
                            # persist in session
                            if "generated_videos" not in st.session_state:
                                st.session_state["generated_videos"] = []
                            if saved not in st.session_state["generated_videos"]:
                                st.session_state["generated_videos"].append(saved)
                            st.success(f"[{i}/{len(selected)}] Готово: {os.path.basename(saved)}")
                            break
                        except gw2.CancelledError:
                            st.warning(f"[{i}/{len(selected)}] Отменено пользователем")
                            # Попробуем гарантированно разорвать VPN чтобы не висело
                            try:
                                vpn_ensure_disconnected(VPN_NAME, wait_sec=6.0)
                            except Exception:
                                pass
                            break
                        except (gw2.QuotaExceededError, gw2.TimeoutExceededError) as e:
                            if not vpn_rotate_video:
                                st.error(f"[{i}/{len(selected)}] {type(e).__name__}: пропуск: {img_path}")
                                break
                            if not hosts:
                                st.error(f"[{i}/{len(selected)}] Список хостов пуст или не загружен: {vpn_hosts_file}")
                                break
                            rotated = False
                            tried = 0
                            while tried < len(hosts):
                                host = hosts[vh_pos % len(hosts)]
                                st.write(f"Переключаю VPN на хост: {host}")
                                if gw2.switch_vpn_to_full_host(host, retries=int(vpn_retries), backoff=1.5, pause_after_connect=1.0):
                                    rotated = True
                                    vh_pos = (vh_pos + 1) % len(hosts)
                                    break
                                vh_pos = (vh_pos + 1) % len(hosts)
                                tried += 1
                            if not rotated:
                                st.error(f"[{i}/{len(selected)}] Не удалось переключить VPN ни на один хост из списка: пропуск.")
                                break
                            attempts += 1
                            if attempts >= len(hosts):
                                st.error(f"[{i}/{len(selected)}] Все хосты не дали результат в отведённое время: пропуск.")
                                break
                            continue
                        except Exception as e:
                            st.error(f"[{i}/{len(selected)}] Ошибка: {e}")
                            break

                # if saved_videos:
                #     st.markdown("**Сгенерированные видео**")
                #     cols = st.columns(4, gap="small")
                #     for i, vp in enumerate(saved_videos):
                #         with cols[i % 4]:
                #             st.video(vp)
                #             safe_vid_key = vp.replace("\\", "_").replace("/", "_").replace(":", "_").replace(".", "_")
                #             copy_path_button(vp, key=f"vid_{safe_vid_key}")

    # --- Постоянный блок отображения всех сгенерированных видео (из сессии) ---
    if st.session_state.get("generated_videos"):
        st.markdown("**Сгенерированные видео**")
        cols = st.columns(4, gap="small")
        for i, vp in enumerate(st.session_state["generated_videos"]):
            with cols[i % 4]:
                st.video(vp)
                safe_vid_key = vp.replace("\\", "_").replace("/", "_").replace(":", "_").replace(".", "_")
                copy_path_button(vp, key=f"vid_{safe_vid_key}")

    # --- Внизу страницы — секция запуска Pinterest-генерации по отмеченным картинкам ---

    # Pinterest CSV export state
    st.session_state.setdefault("pinterest_image_urls", {})  # {local_image_path -> hosted_url}
    st.session_state.setdefault("pinterest_export_rows", [])
    st.session_state.setdefault("pinterest_export_sig", None)
    st.session_state.setdefault("pinterest_export_version", 0)

    pin_selected = [p for p, v in st.session_state.get("pinterest_selection", {}).items() if v]

    if pin_selected:
        st.info(f"Выбрано изображений для Pinterest: {len(pin_selected)}")
        if st.button(f"Сгенерировать Pinterest данные ({len(pin_selected)})", type="primary"):
            # Для этой кнопки VPN не нужен.
            # В generate_pinterest_texts VPN включается, если env GEMINI_REQUIRE_VPN=1.
            # Поэтому делаем локальный override на время генерации Pinterest-данных.
            _prev_require_vpn = os.environ.get("GEMINI_REQUIRE_VPN")
            os.environ["GEMINI_REQUIRE_VPN"] = "0"
            try:
                with st.spinner("Генерация Pinterest данных..."):
                    pin_results = {}
                    for i, img_path in enumerate(pin_selected, 1):
                        try:
                            pin_raw = gptx.generate_pinterest_assets(img_path)
                            pin_results[img_path] = pch.normalize_pinterest_pin(pin_raw)
                            st.success(f"[{i}/{len(pin_selected)}] Готово: {os.path.basename(img_path)}")
                        except Exception as e:
                            msg = str(e) or repr(e)
                            st.error(f"[{i}/{len(pin_selected)}] Ошибка: {msg}")
                    st.session_state["pinterest_results"] = pin_results
            finally:
                # Restore app-level setting
                if _prev_require_vpn is None:
                    os.environ.pop("GEMINI_REQUIRE_VPN", None)
                else:
                    os.environ["GEMINI_REQUIRE_VPN"] = _prev_require_vpn

    # Показ результатов Pinterest (если уже сгенерированы)
    if st.session_state.get("pinterest_results"):
        st.markdown("**Сгенерированные Pinterest данные**")
        for img_path, pin in st.session_state["pinterest_results"].items():
            with st.expander(os.path.basename(img_path), expanded=False):
                st.image(img_path, caption=os.path.basename(img_path), width=320)
                safe_key = img_path.replace("\\", "_").replace("/", "_").replace(":", "_").replace(".", "_")
                copy_path_button(img_path, key=f"pin_img_{safe_key}")

                if isinstance(pin, dict) and pin.get("description") is not None:
                    desc = pin.get("description") or ""
                    hashtags = pin.get("hashtags") or []
                    three = pin.get("three_word_keywords") or []
                    titles = pin.get("title_options") or []

                    st.markdown("**Описание (<=600 символов)**")
                    st.text_area("Копируйте (Ctrl+C)", desc, height=160)
                    st.caption(f"Длина: {len(desc)}")

                    st.markdown("**Хештеги (10 шт, суммарно <=200 символов)**")
                    ht_joined = " ".join(f"#{h.lstrip('#')}" for h in hashtags)
                    st.text_area("Копируйте (Ctrl+C)", ht_joined, height=100)
                    st.caption(f"Длина: {len(ht_joined)}")

                    st.markdown("**3‑словные ключевые (10 шт)**")
                    st.code("\n".join(three) or "", language=None)

                    st.markdown("**Варианты Title (5 шт)**")
                    st.code("\n".join(titles) or "", language=None)

                    raw = (pin or {}).get("_raw") if isinstance(pin, dict) else ""
                    if isinstance(raw, str) and raw.strip():
                        st.markdown("**Raw ответ Gemini**")
                        st.text_area("Raw (Ctrl+C)", raw, height=260, key=f"raw_pin_{safe_key}")

                    data_blob = json.dumps(
                        {"image_path": img_path, "pinterest": pin},
                        ensure_ascii=False, indent=2
                    ).encode("utf-8")
                    base_name = os.path.splitext(os.path.basename(img_path))[0]
                    st.download_button(
                        "Скачать JSON",
                        data=data_blob,
                        file_name=f"{base_name}.pinterest.json",
                        mime="application/json"
                    )
                else:
                    st.warning("Pinterest-данные пришли не в JSON. Ниже — сырой вывод.")
                    raw = (pin or {}).get("_raw") if isinstance(pin, dict) else ""
                    st.text_area("Копируйте (Ctrl+C)", raw or "", height=260)

    st.markdown("---")
    st.subheader("Pinterest bulk CSV")

    with st.expander("Pinterest CSV export settings", expanded=False):
        include_hashtags = st.checkbox("Добавлять hashtags в Description", value=True)
        st.caption(f"Auto schedule: максимум пинов в день = {int(st.session_state.get('pins_per_day') or 10)}")
        imgbb_default = pch.get_default_imgbb_api_key()
        imgbb_api_key = st.text_input(
            "IMGBB API key (для загрузки картинок и заполнения Media URL)",
            value=imgbb_default,
            type="password",
        )
        st.caption("Если не хотите грузить в imgbb, оставьте ключ пустым и заполните Media URL вручную в таблице.")

    pin_map = dict(st.session_state.get("pinterest_results") or {})
    if not pin_map:
        st.info("Сначала сгенерируйте Pinterest данные (шаг выше), затем появится экспорт CSV.")
    else:
        def _pin_content_sig(pin: object) -> tuple:
            if not isinstance(pin, dict):
                return tuple()
            titles = pin.get("title_options")
            if isinstance(titles, list):
                titles_t = tuple(str(x or "") for x in titles)
            else:
                titles_t = (str(titles or ""),) if titles else tuple()

            three = pin.get("three_word_keywords")
            if isinstance(three, list):
                three_t = tuple(str(x or "") for x in three)
            else:
                three_t = (str(three or ""),) if three else tuple()

            return (
                str(pin.get("description") or ""),
                str(pin.get("hashtags") or ""),
                titles_t,
                three_t,
            )

        pins_sig = tuple((p, _pin_content_sig(pin_map.get(p))) for p in sorted(pin_map.keys()))
        export_sig = (
            tuple(sorted(pin_map.keys())),
            bool(include_hashtags),
            int(st.session_state.get('pins_per_day') or 10),
            pins_sig,
        )

        if st.session_state.get("pinterest_export_sig") != export_sig:
            schedule = pch.build_publish_schedule_iso(
                len(pin_map),
                max_per_day=int(st.session_state.get('pins_per_day') or 10),
            )
            st.session_state.pinterest_export_rows = pch.build_default_export_rows(
                pin_map,
                include_hashtags=bool(include_hashtags),
                publish_schedule=schedule,
            )
            st.session_state.pinterest_export_sig = export_sig

        board_options = pch.load_board_names()
        editor_version = int(st.session_state.get("pinterest_export_version") or 0)
        editor_key = f"pinterest_export_editor_{abs(hash(export_sig))}_v{editor_version}"

        edited_export = st.data_editor(
            st.session_state.pinterest_export_rows,
            key=editor_key,
            use_container_width=True,
            hide_index=True,
            column_config={
                "local_image_path": st.column_config.TextColumn("local_image_path", disabled=True, width="large"),
                "Title": st.column_config.TextColumn("Title", width="large"),
                "Media URL": st.column_config.TextColumn("Media URL", width="large"),
                "Pinterest board": st.column_config.SelectboxColumn(
                    "Pinterest board",
                    options=board_options,
                    help="Выберите board из списка (можно начать печатать).",
                    width="medium",
                ),
                "Thumbnail": st.column_config.TextColumn("Thumbnail", width="medium"),
                "Description": st.column_config.TextColumn("Description", width="large"),
                "Link": st.column_config.TextColumn("Link", width="large"),
                "Publish date": st.column_config.TextColumn("Publish date", width="medium"),
                "Keywords": st.column_config.TextColumn("Keywords", width="large"),
            },
        )

        def _to_records(val):
            if val is None:
                return []
            if hasattr(val, "to_dict"):
                try:
                    return list(val.to_dict("records"))
                except Exception:
                    pass
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
            return []

        st.session_state.pinterest_export_rows_live = _to_records(edited_export)
        export_rows = st.session_state.get("pinterest_export_rows_live") or st.session_state.get("pinterest_export_rows") or []

        export_disabled = not bool(export_rows)
        if st.button("Сгенерировать CSV (и при необходимости загрузить картинки в imgbb)", disabled=export_disabled):
            urls_cache: dict[str, str] = dict(st.session_state.get("pinterest_image_urls") or {})

            need_paths = pch.list_needed_local_paths(export_rows)
            missing = pch.validate_local_paths_exist(need_paths)
            if missing:
                st.error("Не найдены локальные файлы картинок (проверьте пути):\n- " + "\n- ".join(missing[:10]))
                st.stop()

            to_upload = [p for p in sorted(set(need_paths)) if p not in urls_cache]
            if to_upload:
                if not (imgbb_api_key or "").strip():
                    st.error("Нужен IMGBB API key для автозаполнения Media URL (или заполните Media URL вручную в таблице).")
                    st.stop()

                prog = st.progress(0.0)
                with st.spinner(f"Загрузка в imgbb: {len(to_upload)} шт"):
                    for idx2, p in enumerate(to_upload, 1):
                        try:
                            img_bytes = open(p, "rb").read()
                            url_hosted = pch.imgbb_upload_bytes(img_bytes, api_key=imgbb_api_key, filename=os.path.basename(p))
                        except Exception as e:
                            st.error(f"Не удалось загрузить {os.path.basename(p)} в imgbb: {e}")
                            st.stop()
                        urls_cache[p] = url_hosted
                        prog.progress(idx2 / max(1, len(to_upload)))

            st.session_state.pinterest_image_urls = urls_cache
            pch.fill_media_urls_from_cache(export_rows, urls_cache=urls_cache)
            st.session_state.pinterest_export_rows = export_rows

            csv_bytes = pch.build_pinterest_bulk_csv_bytes(export_rows)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            st.success(f"Pinterest CSV сформирован: {len(export_rows)} строк")
            st.download_button(
                "Скачать Pinterest CSV",
                data=csv_bytes,
                file_name=f"pinterest_bulk_{ts}.csv",
                mime="text/csv; charset=utf-8",
            )


if __name__ == "__main__":
    main()
