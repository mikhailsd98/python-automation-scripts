import os
import json
import tempfile
import streamlit as st
from typing import Optional

# Импортируем ваши функции (используют ротацию моделей/ключей и inlineData)
import generate_pinterest_texts as gptx

st.set_page_config(page_title="Pinterest Generator (Gemini)", layout="wide")

st.title("Pinterest Content Generator (Gemini)")
st.caption("Перетащите изображение ниже. Сервис локальный — ключи и файлы никуда не отправляются, кроме Gemini API.")

uploaded = st.file_uploader(
    "Перетащите сюда изображения (JPEG/PNG/WebP) — можно несколько",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True
)

if uploaded:
    files = uploaded  # список UploadedFile
    st.info(f"Выбрано файлов: {len(files)}")

    if st.button(f"Сгенерировать контент для {len(files)} изображений", type="primary"):
        with st.spinner("Генерация... (каждая картинка 10–60 сек)"):
            results = []
            for i, up in enumerate(files, 1):
                st.write(f"Обработка [{i}/{len(files)}]: {up.name}")
                suffix = os.path.splitext(up.name or "")[1] or ".jpg"
                import tempfile
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(up.getbuffer())
                    tmp_path = tmp.name
                try:
                    kw = gptx.generate_keywords_and_image_prompt(tmp_path)
                    pin = gptx.generate_pinterest_assets(tmp_path)
                    results.append((up, kw, pin))
                finally:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        st.markdown("---")
        for up, kw, pin in results:
            with st.expander(up.name, expanded=False):
                st.image(up, caption="Предпросмотр", width=320)
                left, right = st.columns(2, gap="large")

                with left:
                    st.subheader("30 long-tail ключевых фраз")
                    if kw.get("keywords_30") is None:
                        st.warning("Контент заблокирован API при попытке получить ключевые фразы.")
                    else:
                        st.text_area("Копируйте (Ctrl+C)", kw.get("keywords_30", ""), height=160)

                    st.subheader("Итоговый prompt для генерации изображения")
                    st.text_area("Копируйте (Ctrl+C)", kw.get("image_prompt", "") or "", height=140)

                with right:
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

                        data_blob = json.dumps(
                            {"image": up.name, "keywords": kw, "pinterest": pin},
                            ensure_ascii=False, indent=2
                        ).encode("utf-8")
                        st.download_button("Скачать JSON", data=data_blob, file_name=f"{os.path.splitext(up.name)[0]}.json", mime="application/json")
                    else:
                        st.warning("Pinterest-данные пришли не в JSON. Ниже — сырой вывод.")
                        raw = (pin or {}).get("_raw") if isinstance(pin, dict) else ""
                        st.text_area("Копируйте (Ctrl+C)", raw or "", height=260)
else:
    st.info("Загрузите изображения, чтобы начать.")