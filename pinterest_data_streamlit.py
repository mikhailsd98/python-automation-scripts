# pinterest_data_streamlit.py
import os
import json
from datetime import datetime
import streamlit as st
import streamlit.components.v1 as components

import generate_pinterest_texts as gptx
import pinterest_csv_helpers as pch


def copy_path_button(path: str, key: str):
    import json as _json
    html = """
    <button id=\"{id}\" style=\"margin-top:6px;padding:2px 8px;font-size:12px\">Скопировать путь</button>
    <script>
    const btn = document.getElementById(\"{id}\");
    btn.addEventListener(\"click\", async () => {{
        try {{
            await navigator.clipboard.writeText({text});
            const old = btn.innerText;
            btn.innerText = \"Скопировано!\";
            setTimeout(() => btn.innerText = old, 1200);
        }} catch (e) {{
            console.error(e);
            alert(\"Не удалось скопировать\");
        }}
    }});
    </script>
    """.format(id=f"cpy_{key}", text=_json.dumps(path))
    components.html(html, height=40)

st.set_page_config(page_title="Pinterest Data Generator (Gemini)", layout="wide")

st.title("Pinterest Data Generator (Gemini)")
st.caption("Перетащите изображения — для каждой картинки будет сгенерировано только Pinterest-описание, хэштеги, 3‑словные ключи и варианты title. Без промпта для генерации изображения.")

# Global scheduling settings (applies to CSV schedule)
st.session_state.setdefault("pins_per_day", 10)
# Start day for auto schedule (defaults to today)
st.session_state.setdefault("publish_start_date", datetime.now().date())

col_a, col_b = st.columns([1, 1])
with col_a:
    pins_per_day = st.number_input(
        "Максимум пинов в день (auto schedule)",
        min_value=1,
        max_value=100,
        value=int(st.session_state.get("pins_per_day") or 10),
        step=1,
        help="Сколько пинов максимум ставить на один день. Остальные переносятся на следующие дни.",
    )
with col_b:
    publish_start_date = st.date_input(
        "Стартовый день для Publish date",
        value=st.session_state.get("publish_start_date") or datetime.now().date(),
        help="С какого дня начинать автозаполнение 'Publish date' (по умолчанию сегодня).",
    )

st.session_state["pins_per_day"] = int(pins_per_day)
st.session_state["publish_start_date"] = publish_start_date

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

    # --- Pinterest board settings (should be set BEFORE generation) ---
    # Affects:
    # - which board list is used for Gemini auto-assignment
    # - which options appear in the export table SelectboxColumn
    st.session_state.setdefault("pinterest_board_names_path", "board_names.txt")
    st.session_state.setdefault("auto_assign_boards", True)
    st.session_state.setdefault("pinterest_board_assign_sig", None)

    with st.expander("Pinterest board settings", expanded=False):
        st.checkbox(
            "Автоматически подобрать Pinterest board через Gemini (строго из списка)",
            value=bool(st.session_state.get("auto_assign_boards", True)),
            key="auto_assign_boards",
            help=(
                "Сделает отдельный запрос к Gemini и подберёт board для каждого пина. "
                "Выбор СТРОГО из выбранного файла board_names*.txt (любые несовпадения станут пустыми). "
                "Чтобы не перетирать ручные правки, автозаполнение будет заполнять только пустые ячейки."
            ),
        )

        board_file_options: list[str] = []
        if os.path.exists("board_names.txt"):
            board_file_options.append("board_names.txt")
        if os.path.exists("board_names2.txt"):
            board_file_options.append("board_names2.txt")

        # Fallback: keep the current value even if file is missing (user might mount later).
        current_board_file = str(st.session_state.get("pinterest_board_names_path") or "board_names.txt")
        if current_board_file not in board_file_options:
            board_file_options = [current_board_file] + board_file_options

        selected_board_file = st.selectbox(
            "Файл со списком boards (для выпадающего списка в таблице)",
            options=board_file_options or ["board_names.txt"],
            index=(
                board_file_options.index(current_board_file)
                if board_file_options and current_board_file in board_file_options
                else 0
            ),
            key="pinterest_board_names_path",
            help="Выберите файл, из которого будут подгружаться названия boards в селекте колонки 'Pinterest board'.",
        )

        if not os.path.exists(selected_board_file):
            st.warning(f"Файл boards не найден: {selected_board_file}. Выпадающий список будет пустым.")

    # Persist last generation in session_state so UI doesn't disappear on reruns
    st.session_state.setdefault("pinterest_gen_results", [])

    if st.button(f"Сгенерировать Pinterest данные ({len(uploaded)})", type="primary"):
        with st.spinner("Генерация... (каждая картинка 10–60 сек)"):
            results = []

            # Persist uploads to disk so we can show/copy a real filesystem path.
            run_dir = os.path.join(
                os.getcwd(),
                "pinterest_uploads",
                datetime.now().strftime("%Y%m%d_%H%M%S"),
            )
            os.makedirs(run_dir, exist_ok=True)

            for i, up in enumerate(uploaded, 1):
                st.write(f"Обработка [{i}/{len(uploaded)}]: {up.name}")
                suffix = os.path.splitext(up.name or "")[1] or ".jpg"

                # Save original upload to a stable path
                safe_name = os.path.basename(up.name or f"image_{i}{suffix}")
                out_path = os.path.join(run_dir, f"{i:02d}_{safe_name}")
                with open(out_path, "wb") as f:
                    f.write(up.getbuffer())

                pin_raw = gptx.generate_pinterest_assets(out_path)
                pin = pch.normalize_pinterest_pin(pin_raw)
                results.append({"name": up.name, "path": out_path, "pin": pin})

            st.session_state.pinterest_gen_results = results

    # Render results (and CSV export) outside the button so they persist on reruns.
    results = st.session_state.get("pinterest_gen_results") or []

    if results:
        # --- Pinterest bulk CSV (export) ---
        st.markdown("---")
        st.subheader("Pinterest bulk CSV")

        # Board names source selection affects the SelectboxColumn options below.
        st.session_state.setdefault("pinterest_board_names_path", "board_names.txt")
        # Auto-assign boards via Gemini (same mechanism as pinterest_post_texts_streamlit.py)
        st.session_state.setdefault("auto_assign_boards", True)
        st.session_state.setdefault("pinterest_board_assign_sig", None)

        with st.expander("Pinterest CSV export settings", expanded=False):
            include_hashtags = st.checkbox("Добавлять hashtags в Description", value=True)
            st.caption(
                f"Auto schedule: максимум пинов в день = {int(st.session_state.get('pins_per_day') or 10)} | "
                f"старт = {str(st.session_state.get('publish_start_date') or datetime.now().date())}"
            )

            imgbb_default = pch.get_default_imgbb_api_key()
            imgbb_api_key = st.text_input(
                "IMGBB API key (для загрузки картинок и заполнения Media URL)",
                value=imgbb_default,
                type="password",
            )
            st.caption("Если не хотите грузить в imgbb, оставьте ключ пустым и заполните Media URL вручную в таблице.")

        pin_map = {r.get("path"): r.get("pin") for r in results if r.get("path")}

        def _pin_content_sig(pin: object) -> tuple:
            """Create a small, stable signature from pin content.

            Needed because Streamlit session_state can keep old export rows even when
            new generation results arrive (same image paths).
            """

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

        # Rebuild default rows not only when image set changes, but also when export settings or
        # generated content changes.
        export_sig = (
            tuple(sorted(pin_map.keys())),
            bool(include_hashtags),
            int(st.session_state.get('pins_per_day') or 10),
            str(st.session_state.get('publish_start_date') or datetime.now().date()),
            str(st.session_state.get("pinterest_board_names_path") or "board_names.txt"),
            pins_sig,
        )
        if "pinterest_export_sig" not in st.session_state:
            st.session_state.pinterest_export_sig = None
        if "pinterest_export_rows" not in st.session_state:
            st.session_state.pinterest_export_rows = []
        st.session_state.setdefault("pinterest_export_version", 0)
        st.session_state.setdefault("pinterest_image_urls", {})

        if st.session_state.get("pinterest_export_sig") != export_sig:
            schedule = pch.build_publish_schedule_iso(
                len(pin_map),
                max_per_day=int(st.session_state.get('pins_per_day') or 10),
                start_date=st.session_state.get('publish_start_date'),
            )
            st.session_state.pinterest_export_rows = pch.build_default_export_rows(
                pin_map,
                include_hashtags=bool(include_hashtags),
                publish_schedule=schedule,
            )
            st.session_state.pinterest_export_sig = export_sig

        # Auto-assign boards via Gemini (strictly from selected board_names file)
        board_names_path_current = str(st.session_state.get("pinterest_board_names_path") or "board_names.txt")
        if bool(st.session_state.get("auto_assign_boards", True)) and os.path.exists(board_names_path_current):
            # Run auto-assign at most once per export_sig (but allow rerun if signature changed)
            if st.session_state.get("pinterest_board_assign_sig") != export_sig:
                export_rows_cur = st.session_state.get("pinterest_export_rows") or []

                def _is_blank_board(v: object) -> bool:
                    return not str(v or "").replace("\u00A0", " ").strip()

                pins_for_boards = [
                    {
                        "title": str(r.get("Title") or ""),
                        "description": str(r.get("Description") or ""),
                    }
                    for r in export_rows_cur
                    if isinstance(r, dict)
                ]

                # Only attempt if we have rows and at least one empty cell.
                if export_rows_cur and any(_is_blank_board(r.get("Pinterest board")) for r in export_rows_cur if isinstance(r, dict)):
                    with st.spinner("Подбираю Pinterest board для каждого пина через Gemini..."):
                        try:
                            import generate_pinterest_texts_for_post as gpt_post

                            boards = gpt_post.suggest_boards_for_pins(
                                pins_for_boards,
                                board_names_path=board_names_path_current,
                                max_desc_chars=260,
                            )
                        except Exception as e:
                            boards = None
                            st.warning(f"Не удалось авто-подобрать boards: {e}")

                    if isinstance(boards, list) and len(boards) == len(export_rows_cur):
                        changed = False
                        for i, b in enumerate(boards):
                            if i >= len(export_rows_cur):
                                break
                            if not isinstance(export_rows_cur[i], dict):
                                continue
                            if _is_blank_board(export_rows_cur[i].get("Pinterest board")):
                                export_rows_cur[i]["Pinterest board"] = (b or "").strip()
                                changed = True
                        if changed:
                            st.session_state.pinterest_export_rows = export_rows_cur
                            # Force editor refresh so new values appear immediately.
                            st.session_state.pinterest_export_version = int(st.session_state.get("pinterest_export_version") or 0) + 1

                st.session_state.pinterest_board_assign_sig = export_sig

        board_options = pch.load_board_names(path=board_names_path_current)
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

        st.markdown("---")
        # --- Per-image result display ---
        for r in results:
            pin = r.get("pin")
            up_name = r.get("name") or os.path.basename(r.get("path") or "")

            with st.expander(up_name, expanded=False):
                st.image(r["path"], caption=os.path.basename(r["path"]), width=320)
                safe_key = r["path"].replace("\\", "_").replace("/", "_").replace(":", "_").replace(".", "_")
                copy_path_button(r["path"], key=f"pin_data_img_{safe_key}")

                st.subheader("Данные для Pinterest")
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
                        st.text_area("Raw (Ctrl+C)", raw, height=260, key=f"raw_pin_data_{safe_key}")

                    data_blob = json.dumps(
                        {"image": up_name, "image_path": r["path"], "pinterest": pin},
                        ensure_ascii=False, indent=2
                    ).encode("utf-8")
                    base = os.path.splitext(up_name)[0]
                    st.download_button("Скачать JSON", data=data_blob, file_name=f"{base}_pinterest.json", mime="application/json")
                else:
                    st.warning("Pinterest-данные пришли не в JSON. Ниже — сырой вывод.")
                    raw = (pin or {}).get("_raw") if isinstance(pin, dict) else ""
                    st.text_area("Копируйте (Ctrl+C)", raw or "", height=260)
else:
    st.info("Загрузите изображения, чтобы начать.")