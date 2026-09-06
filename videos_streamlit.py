# app_streamlit_video.py
import os
import sys
import asyncio
import traceback
import streamlit as st
from pathlib import Path
import time
import streamlit.components.v1 as components

import generate_wan2_video as gw2

from pinterest_poster import post_videos_to_pinterest
import generate_pinterest_texts as gptx

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

def create_generation_root(parent: str) -> str:
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

# На Windows нужен ProactorEventLoop для subprocess в Playwright
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

st.set_page_config(page_title="Image → Video Converter (WAN2)", layout="wide")

st.title("Image → Video Converter (WAN2)")
st.caption("Перетащите изображения; для каждого будет сгенерировано видео через WAN2 Space.")

# Конфигурация WAN2
wan2_space_url = st.text_input(
    "WAN2 Video Space URL",
    value=os.environ.get("WAN2_SPACE_URL", getattr(gw2, "SPACE_URL", "")),
    help="URL Hugging Face Space для генерации видео (WAN2).",
)

# Общие опции
headless = st.checkbox("Запуск без окна браузера (headless)", value=True)
vpn_rotate_video = st.checkbox("Ротация VPN при квоте (для видео)", value=True)
vpn_one_try = st.checkbox(
    "VPN: одна попытка подключения (retries=1)",
    value=bool(st.session_state.get("vpn_one_try", False)),
    key="vpn_one_try",
    help="Если включить — при переключении VPN на конкретный хост будет только 1 попытка вместо 2.",
)
vpn_retries = 1 if vpn_one_try else 2
# Таймауты ожидания
col_t1, col_t2 = st.columns(2)
with col_t1:
    max_wait_min = st.number_input("Максимальное ожидание видео (мин)", min_value=1, max_value=60, value=10, step=1)
with col_t2:
    queue_wait_min = st.number_input("Макс. ожидание очереди (мин)", min_value=1, max_value=30, value=5, step=1)

# Куда сохранять
out_dir = st.text_input("Папка для сохранения видео", value="generated_videos")

# Файл хостов VPN и стартовая позиция
vpn_hosts_file = st.text_input(
    "TXT со списком рабочих VPN-хостов (для видео)",
    value="good_hosts_for_videos.txt",
    help="Один хост на строку (например 74-80-180-218.hideservers.net)",
)
vpn_hosts_start_pos = st.number_input(
    "С какого хоста в списке начинать (позиция, 0 = первый)",
    min_value=0, value=0, step=1
)

# Загрузка изображений
uploaded = st.file_uploader(
    "Перетащите сюда изображения (JPEG/PNG/WebP) — можно несколько",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True
)

if uploaded:
    st.info(f"Выбрано файлов: {len(uploaded)}")
    # Предпросмотр
    cols = st.columns(4)
    for i, up in enumerate(uploaded):
        with cols[i % 4]:
            st.image(up, caption=up.name, use_container_width=True)

    if st.button(f"Сгенерировать видео ({len(uploaded)})", type="primary"):
        gen_root = create_generation_root(out_dir)
        saved_videos = []
        hosts = gw2.load_hosts_file(vpn_hosts_file) if vpn_rotate_video else []
        vh_pos = int(vpn_hosts_start_pos)

        name_counts = {}

        with st.spinner("Генерация видео..."):
            for i, up in enumerate(uploaded, 1):
                base = os.path.splitext(up.name or f"image_{i}")[0]
                cnt = name_counts.get(base, 0)
                name_counts[base] = cnt + 1
                k = 1 if cnt == 0 else (cnt + 1)
                folder_name = base if k == 1 else f"{base} ({k})"
                sub_dir = Path(gen_root) / folder_name
                while sub_dir.exists():
                    k += 1
                    folder_name = f"{base} ({k})"
                    sub_dir = Path(gen_root) / folder_name
                sub_dir.mkdir(parents=True, exist_ok=True)
                out_path = str(sub_dir / f"{base}.mp4")

                # Сохраняем входной файл во временный путь (Playwright ждёт путь на диске)
                suffix = os.path.splitext(up.name or "")[1] or ".jpg"
                tmp_path = None
                try:
                    import tempfile
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                        tmp.write(up.getbuffer())
                        tmp_path = tmp.name

                    # Сохраняем постоянную копию исходного изображения рядом с видео
                    src_img_path = str(sub_dir / f"{base}{suffix}")
                    try:
                        with open(src_img_path, "wb") as fsrc:
                            fsrc.write(up.getbuffer())
                    except Exception:
                        src_img_path = None

                    attempts = 0
                    while True:
                        try:
                            saved = gw2.generate(
                                wan2_space_url.strip() or gw2.SPACE_URL,
                                tmp_path,
                                out_path,
                                headless=headless,
                                max_wait_sec=int(max_wait_min * 60),
                                queue_max_wait_sec=int(queue_wait_min * 60),
                            )
                            # persist
                            if "generated_videos" not in st.session_state:
                                st.session_state["generated_videos"] = []
                            if saved not in st.session_state["generated_videos"]:
                                st.session_state["generated_videos"].append(saved)
                            if "video_sources" not in st.session_state:
                                st.session_state["video_sources"] = {}
                            if src_img_path:
                                st.session_state["video_sources"][saved] = src_img_path

                            saved_videos.append(saved)
                            st.success(f"[{i}/{len(uploaded)}] Готово: {os.path.basename(saved)}")
                            break
                        except (gw2.QuotaExceededError, gw2.TimeoutExceededError) as e:
                            if not vpn_rotate_video:
                                st.error(f"[{i}/{len(uploaded)}] {type(e).__name__}: пропуск {up.name}")
                                break
                            hosts = gw2.load_hosts_file(vpn_hosts_file) if vpn_rotate_video else []
                            if not hosts:
                                st.error(f"[{i}/{len(uploaded)}] Список хостов пуст или не загружен: {vpn_hosts_file}")
                                break
                            # Ротация по списку хостов и повтор этого же изображения
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
                                st.error(f"[{i}/{len(uploaded)}] Не удалось переключить VPN ни на один хост из списка: пропуск.")
                                break
                            attempts += 1
                            if attempts >= len(hosts):
                                st.error(f"[{i}/{len(uploaded)}] Все хосты не дали результат в отведённое время: пропуск.")
                                break
                            continue
                        except Exception as e:
                            st.error(f"[{i}/{len(uploaded)}] Ошибка: {e}")
                            break
                finally:
                    if tmp_path:
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass

        if saved_videos:
            st.markdown("**Сгенерированные видео**")
            if "pinterest_selection_videos" not in st.session_state:
                st.session_state["pinterest_selection_videos"] = {}
            cols = st.columns(4, gap="small")
            for i, vp in enumerate(saved_videos):
                with cols[i % 4]:
                    st.video(vp)
                    safe_vid_key = vp.replace("\\", "_").replace("/", "_").replace(":", "_").replace(".", "_")
                    copy_path_button(vp, key=f"vid_{safe_vid_key}")
                    state_key = f"pin_vid_sel_{safe_vid_key}"
                    if state_key not in st.session_state:
                        st.session_state[state_key] = bool(st.session_state["pinterest_selection_videos"].get(vp, False))
                    checked_pin = st.checkbox("Выбрать для Pinterest", key=state_key)
                    st.session_state["pinterest_selection_videos"][vp] = checked_pin
else:
    st.info("Загрузите изображения, чтобы начать.")

# --- Постоянный блок отображения всех сгенерированных видео (из сессии) ---
if "generated_videos" not in st.session_state:
    st.session_state["generated_videos"] = []
if "pinterest_selection_videos" not in st.session_state:
    st.session_state["pinterest_selection_videos"] = {}
if "video_sources" not in st.session_state:
    st.session_state["video_sources"] = {}
if "pinterest_results_videos" not in st.session_state:
    st.session_state["pinterest_results_videos"] = {}

if st.session_state.get("generated_videos"):
    st.markdown("**Сгенерированные видео (сеанс)**")
    cols = st.columns(4, gap="small")
    for i, vp in enumerate(st.session_state["generated_videos"]):
        with cols[i % 4]:
            st.video(vp)
            safe_vid_key = vp.replace("\\", "_").replace("/", "_").replace(":", "_").replace(".", "_")
            copy_path_button(vp, key=f"vid_persist_{safe_vid_key}")
            state_key = f"pin_vid_sel_persist_{safe_vid_key}"
            if state_key not in st.session_state:
                st.session_state[state_key] = bool(st.session_state["pinterest_selection_videos"].get(vp, False))
            checked_pin = st.checkbox("Выбрать для Pinterest", key=state_key)
            st.session_state["pinterest_selection_videos"][vp] = checked_pin

st.markdown("---")
pin_selected_videos = [p for p, v in st.session_state.get("pinterest_selection_videos", {}).items() if v]
if pin_selected_videos:
    st.info(f"Выбрано видео для Pinterest: {len(pin_selected_videos)}")
    if st.button(f"Сгенерировать Pinterest данные ({len(pin_selected_videos)})", type="primary"):
        with st.spinner("Генерация Pinterest данных..."):
            for i, vp in enumerate(pin_selected_videos, 1):
                img_path = st.session_state["video_sources"].get(vp)
                if not img_path or not os.path.exists(img_path):
                    st.error(f"[{i}/{len(pin_selected_videos)}] Не найден исходный снимок для видео: {os.path.basename(vp)}")
                    continue
                try:
                    pin = gptx.generate_pinterest_assets(img_path)
                    # Храним по ключу img_path, как в app_streamlit.py
                    st.session_state["pinterest_results_videos"][img_path] = pin
                    st.success(f"[{i}/{len(pin_selected_videos)}] Готово: {os.path.basename(vp)}")
                except Exception as e:
                    st.error(f"[{i}/{len(pin_selected_videos)}] Ошибка: {e}")

# Показ результатов Pinterest (если уже сгенерированы)
if st.session_state.get("pinterest_results_videos"):
    st.markdown("**Сгенерированные Pinterest данные**")
    for img_path, pin in st.session_state["pinterest_results_videos"].items():
        with st.expander(os.path.basename(img_path), expanded=False):
            if os.path.exists(img_path):
                st.image(img_path, caption=os.path.basename(img_path), width=320)

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

# --- Публикация в Pinterest ---
st.markdown("---")
st.subheader("Постинг/публикация в Pinterest")

generated_videos = list(st.session_state.get("generated_videos", []))
pin_results_map = st.session_state.get("pinterest_results_videos", {})

# Настройки Chrome/публикации
colc1, colc2 = st.columns(2)
with colc1:
    chrome_user_data_dir = st.text_input(
        "Chrome user-data-dir",
        value="C:\\Temp\\ChromeSProfile",
        help="Рекомендуется отдельная папка, не основной профиль Chrome."
    )
    chrome_profile_dir = st.text_input("Chrome profile directory", value="Default")
with colc2:
    debugger_address = st.text_input(
        "Debugger address (опционально)",
        value="",
        help="Например 127.0.0.1:9222, если вы заранее запустили Chrome с --remote-debugging-port=9222"
    )

colw1, colw2, colw3 = st.columns(3)
with colw1:
    login_wait = st.number_input("Пауза для входа (сек)", min_value=0, max_value=120, value=20, step=5)
with colw2:
    action_delay = st.number_input("Задержка между действиями (сек)", min_value=0.0, max_value=10.0, value=3.0, step=0.5)
with colw3:
    keep_open = st.checkbox("Не закрывать браузер", value=True)

publish = st.checkbox("Публиковать (иначе черновики)", value=False)

def _build_meta_for_videos(videos, pin_map):
    # Индекс по базе пути без расширения у исходного изображения
    idx = {}
    for img_path, pin in (pin_map or {}).items():
        base = os.path.splitext(img_path)[0]
        idx[base] = pin

    meta = {}
    for vp in videos:
        base = os.path.splitext(vp)[0]
        pin = idx.get(base)
        # Если нашли Pinterest‑данные — собираем описание и (опц.) title
        if isinstance(pin, dict) and pin.get("description"):
            desc = pin.get("description") or ""
            hashtags = pin.get("hashtags") or []
            ht = " ".join(h if str(h).startswith("#") else f"#{h}" for h in hashtags)
            full_desc = (desc + ("\n\n" + ht if ht else "")).strip()
            titles = pin.get("title_options") or []
            title = titles[0] if titles else None
            meta[vp] = {"description": full_desc, "title": title}
        else:
            # Если описания нет — заполним пустым, чтобы всё равно открыть и загрузить медиа
            meta[vp] = {"description": "", "title": None}
    return meta

disabled = not bool(generated_videos)
if st.button("Опубликовать в Pinterest", type="primary", disabled=disabled):
    if not generated_videos:
        st.warning("Нет сгенерированных видео.")
    else:
        meta_by_video = _build_meta_for_videos(generated_videos, pin_results_map)
        with st.spinner("Открываю Pinterest и заполняю..."):
            results = post_videos_to_pinterest(
                generated_videos,
                meta_by_video,
                headless=False,
                login_wait_sec=int(login_wait),
                action_delay_sec=float(action_delay),
                publish=bool(publish),
                debugger_address=(debugger_address.strip() or None),
                chrome_user_data_dir=(chrome_user_data_dir.strip() or None),
                chrome_profile_dir=(chrome_profile_dir.strip() or None),
                keep_open=bool(keep_open),
            )
        ok = sum(1 for r in results if r.get("ok"))
        fail = [r for r in results if not r.get("ok")]
        if publish:
            st.success(f"Готово: {ok}/{len(results)} опубликовано")
        else:
            st.success(f"Готово: {ok}/{len(results)} черновиков заполнено")
        if fail:
            st.error(f"Не удалось: {len(fail)}")
            for r in fail:
                st.write(f"- {os.path.basename(r['video'])}: {r.get('error')}")