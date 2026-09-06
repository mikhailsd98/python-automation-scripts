# pinterest_post_texts_streamlit.py
# Streamlit UI to generate Pinterest metadata from BLOG POST TEXT (not images).
# Extended: bulk mode from input CSV (post_text,pin_filename) + image upload -> imgbb -> export CSV.

import base64
import csv
import io
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import requests
import streamlit as st

import generate_pinterest_texts_for_post as gpt_post


def _load_board_names(path: str = "board_names.txt") -> list[str]:
    """Load Pinterest board names from a text file (one board per line).

    We normalize whitespace (strip, collapse spaces, remove NBSP) and keep only
    canonical values from the file (NO duplicates like lowercase variants).

    We also include an empty option "" first, so the user can leave it blank.
    """
    try:
        if not os.path.exists(path):
            return [""]

        raw_lines = open(path, "r", encoding="utf-8", errors="ignore").read().splitlines()

        def norm(s: str) -> str:
            s = (s or "").replace("\u00A0", " ")  # NBSP -> space
            s = " ".join(s.strip().split())  # collapse whitespace
            return s

        names = [norm(ln) for ln in raw_lines if norm(ln)]

        seen: set[str] = set()
        out: list[str] = [""]
        for n in names:
            if n not in seen:
                out.append(n)
                seen.add(n)
        return out
    except Exception:
        return [""]


THEME_LABELS: dict[str, str] = {
    "decor": "Decor",
    "food": "Food",
    "fashion": "Fashion",
}

DOMAIN_ALIASES: dict[str, str] = {
    "nestigmuse.com": "nestingmuse.com",
}

DOMAIN_THEME_MAP: dict[str, str] = {
    "nestingmuse.com": "decor",
    "spaceofmuse.com": "decor",
    "sweethomecookery.com": "food",
    "glowuproutine.com": "fashion",
}

DOMAIN_BOARD_FILE_MAP: dict[str, str] = {
    "nestingmuse.com": "board_names.txt",
    "spaceofmuse.com": "board_names2.txt",
    "sweethomecookery.com": "board_names3.txt",
    "glowuproutine.com": "board_names4.txt",
}


def _normalize_domain(value: str) -> str:
    s = str(value or "").strip().strip("\"'").lower()
    if not s:
        return ""

    if "://" in s:
        try:
            s = urlparse(s).netloc.strip().lower() or s
        except Exception:
            pass

    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip()
    if s.startswith("www."):
        s = s[4:]
    return DOMAIN_ALIASES.get(s, s)


def _extract_domain_from_url(url: str) -> str:
    return _normalize_domain(url)


def _detect_row_domain_from_values(row: dict[str, Any], get_value) -> str:
    direct_candidates = [
        get_value(row, "domain"),
        get_value(row, "site_domain"),
        get_value(row, "host"),
        get_value(row, "site"),
        get_value(row, "base_url"),
    ]
    for cand in direct_candidates:
        domain = _normalize_domain(cand)
        if domain:
            return domain

    return _extract_domain_from_url(get_value(row, "post_link"))


def _detect_single_row_theme(rows: list[dict[str, Any]]) -> str:
    themes = {str(r.get("theme") or "").strip().lower() for r in rows if str(r.get("theme") or "").strip()}
    return next(iter(themes)) if len(themes) == 1 else ""


def _detect_single_board_file(rows: list[dict[str, Any]]) -> str:
    board_files = {
        str(r.get("board_names_path") or "").strip()
        for r in rows
        if str(r.get("board_names_path") or "").strip()
    }
    return next(iter(board_files)) if len(board_files) == 1 else ""


st.set_page_config(page_title="Pinterest Data from Post Text (Gemini)", layout="wide")

st.title("Pinterest Data Generator — From Post Text (Gemini)")
st.caption(
    "Генерация Pinterest-данных по тексту поста: title options, description, hashtags. "
    "Также поддерживается bulk-режим: входной CSV + пачка изображений -> imgbb -> результирующий CSV."
)


# -------------------------
# Helpers
# -------------------------

def _init_state():
    if "manual_posts" not in st.session_state:
        st.session_state.manual_posts = [{"text": "", "keywords": ""}]

    # bulk mode state
    st.session_state.setdefault("bulk_rows", [])
    st.session_state.setdefault("bulk_results", [])
    st.session_state.setdefault("bulk_image_urls", {})
    st.session_state.setdefault("bulk_uploaded_images", {})  # {basename -> bytes}
    st.session_state.setdefault("bulk_csv_sig", None)
    st.session_state.setdefault("bulk_keywords_edits", None)
    st.session_state.setdefault("bulk_export_rows", [])
    st.session_state.setdefault("bulk_export_sig", None)
    st.session_state.setdefault("pinterest_topic_theme", "decor")
    st.session_state.setdefault("bulk_auto_theme", "")
    st.session_state.setdefault("bulk_auto_board_file", "")


def add_post():
    st.session_state.manual_posts.append({"text": "", "keywords": ""})


def remove_post(idx: int):
    if len(st.session_state.manual_posts) <= 1:
        return
    st.session_state.manual_posts.pop(idx)


def _safe_basename(name: str) -> str:
    name = (name or "").strip()
    name = name.replace("\\", "/")
    return os.path.basename(name)


def _extract_title_from_post_text(post_text: str, max_len: int = 100) -> str:
    """Try to extract a human-friendly title from post_text.

    Heuristics:
    1) First non-empty line.
    2) Cut at the first '.' if it's reasonably close.
    3) Fallback to first max_len chars.
    """
    txt = (post_text or "").strip()
    if not txt:
        return "(no title)"

    # First non-empty line
    first_line = ""
    for line in txt.splitlines():
        if line.strip():
            first_line = line.strip()
            break
    if not first_line:
        first_line = txt

    # If there's a dot early, consider it end of title.
    dot = first_line.find(".")
    if 10 <= dot <= max_len:
        cand = first_line[:dot].strip()
        if cand:
            return cand

    # Otherwise trim to max_len
    if len(first_line) > max_len:
        return first_line[:max_len].rstrip() + "…"
    return first_line


def _parse_input_csv(uploaded_file) -> list[dict[str, str]]:
    """Parse uploaded CSV expecting columns: post_text, pin_filename.

    Optionally supports a third column: keywords (per-row).

    Поддерживает типичный Excel-экспорт:
    - UTF-8 (with or without BOM)
    - разделитель: автоопределение (`,`, `;`, tab)
    - header names: case-insensitive, ignores surrounding spaces

    Returns list of dicts: {row, post_text, pin_filename}.
    """
    raw = uploaded_file.getvalue() if uploaded_file is not None else b""
    if not raw:
        return []

    text = raw.decode("utf-8-sig", errors="replace")

    # Detect delimiter
    sample = text[:4096]
    delimiter = ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except Exception:
        # Fallback: if header line obviously uses ';' or tab
        first_line = (text.splitlines()[0] if text.splitlines() else "")
        if ";" in first_line and "," not in first_line:
            delimiter = ";"
        elif "\t" in first_line and "," not in first_line and ";" not in first_line:
            delimiter = "\t"

    f = io.StringIO(text)
    reader = csv.DictReader(f, delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")

    # Build a mapping: normalized_header -> actual_header
    def norm(h: str) -> str:
        # also remove common invisible chars
        h = (h or "").replace("\ufeff", "")
        return " ".join(h.strip().lower().split())

    header_map: dict[str, str] = {norm(h): h for h in reader.fieldnames if h is not None}

    def get_value(row: dict[str, Any], logical_name: str) -> str:
        key = header_map.get(norm(logical_name))
        if not key:
            return ""
        return str(row.get(key) or "")

    required = {"post_text", "pin_filename"}
    missing = [c for c in required if norm(c) not in header_map]
    if missing:
        raise ValueError(
            "CSV is missing required columns: "
            + ", ".join(sorted(missing))
            + f" (detected delimiter: {repr(delimiter)})"
        )

    rows: list[dict[str, str]] = []
    for i, r in enumerate(reader, 1):
        post_text = get_value(r, "post_text").strip()
        pin_filename = _safe_basename(get_value(r, "pin_filename"))
        keywords = get_value(r, "keywords").strip()
        post_link = get_value(r, "post_link").strip()
        domain = _detect_row_domain_from_values(r, get_value)
        theme = DOMAIN_THEME_MAP.get(domain, "")
        board_names_path = DOMAIN_BOARD_FILE_MAP.get(domain, "")
        if not post_text and not pin_filename and not keywords and not post_link:
            continue
        rows.append(
            {
                "row": str(i),
                "post_text": post_text,
                "pin_filename": pin_filename,
                "keywords": keywords,
                "post_link": post_link,
                "domain": domain,
                "theme": theme,
                "board_names_path": board_names_path,
            }
        )
    return rows


def _imgbb_upload_bytes(image_bytes: bytes, api_key: str, filename: str | None = None, timeout_sec: int = 90) -> str:
    """Upload image bytes to imgbb and return hosted image URL."""
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("IMGBB API key is empty")

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data = {"key": api_key, "image": b64}

    # Optional name: imgbb supports 'name' (without extension) for the image.
    if filename:
        base = os.path.splitext(_safe_basename(filename))[0]
        if base:
            data["name"] = base

    resp = requests.post("https://api.imgbb.com/1/upload", data=data, timeout=timeout_sec)
    resp.raise_for_status()
    j = resp.json() or {}
    if not isinstance(j, dict) or not j.get("success"):
        # imgbb returns {success:false, error:{message:...}}
        err = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else None
        raise RuntimeError(f"imgbb upload failed: {err or 'unknown error'}")

    d = j.get("data") or {}
    # Prefer direct URL
    url = d.get("url") or d.get("display_url")
    if not url:
        raise RuntimeError("imgbb upload failed: response has no url")
    return str(url)


def _build_description(pin: dict[str, Any], include_hashtags: bool) -> str:
    desc = (pin.get("description") or "").strip()
    hashtags = (pin.get("hashtags") or "").strip()
    if include_hashtags and hashtags:
        return (desc + "\n\n" + hashtags).strip()
    return desc


def _remove_article_marker_from_title(title: str) -> str:
    """Remove hardcoded '(Article ...)' marker from a generated title."""
    t = (title or "").strip()
    if not t:
        return ""
    t = re.sub(r"\s*\(Article[^)]*\)\s*", " ", t, flags=re.I)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


def _strip_leading_article_cta(description: str) -> str:
    """Remove leading 'Click/Tap the link to ... full article' line if present."""
    desc = (description or "").strip()
    if not desc:
        return ""

    lines = desc.splitlines()
    if not lines:
        return desc

    first = lines[0].strip()
    cta_re = re.compile(
        r"(Click|Tap)\s+the\s+link\s+to\s+(read|explore)\s+the\s+full\s+article\b",
        flags=re.I,
    )
    if not cta_re.search(first):
        return desc

    # Keep the neural text only (the rest of DESCRIPTION after CTA line).
    return "\n".join(lines[1:]).strip()


def _sanitize_generated_pin(pin: dict[str, Any]) -> dict[str, Any]:
    """Drop stream-specific wrappers so UI/export contain only model-generated text."""
    if not isinstance(pin, dict):
        return pin

    out = dict(pin)

    titles = pin.get("title_options")
    if isinstance(titles, list):
        cleaned_titles: list[str] = []
        for t in titles:
            cleaned = _remove_article_marker_from_title(str(t or ""))
            if cleaned:
                cleaned_titles.append(cleaned)
        out["title_options"] = cleaned_titles

    out["description"] = _strip_leading_article_cta(str(pin.get("description") or ""))
    return out


def _board_specific_description_tweak(description: str, board_names_path: str) -> str:
    """No-op: keep DESCRIPTION unchanged by board-specific rules."""
    _ = board_names_path
    return (description or "").strip()


_init_state()


# -------------------------
# Input mode
# -------------------------
mode = st.radio(
    "Режим",
    [
        "Вставить тексты (много постов, кнопка добавления)",
        "Загрузить файлы (.txt/.html)",
        "Bulk: CSV (post_text,pin_filename) + изображения -> imgbb -> CSV",
    ],
)

st.markdown("---")


# -------------------------
# Mode 1/2: existing behaviour
# -------------------------
if mode in {"Вставить тексты (много постов, кнопка добавления)", "Загрузить файлы (.txt/.html)"}:
    posts: list[dict] = []

    if mode == "Вставить тексты (много постов, кнопка добавления)":
        st.subheader("Посты")

        for i, item in enumerate(st.session_state.manual_posts):
            with st.container(border=True):
                header_cols = st.columns([5, 1])
                header_cols[0].markdown(f"**Пост #{i+1}**")
                header_cols[1].button(
                    "Удалить",
                    key=f"rm_{i}",
                    on_click=remove_post,
                    args=(i,),
                    disabled=(len(st.session_state.manual_posts) <= 1),
                )

                text = st.text_area("Текст поста", value=item.get("text", ""), height=140, key=f"text_{i}")
                keywords_text = st.text_area(
                    "Keywords (по одному в строке, без запятых)",
                    value=item.get("keywords", ""),
                    height=110,
                    key=f"keywords_{i}",
                    help="Вставьте список ключевых слов из pinclicks. По одному keyword на строку.",
                )

                st.session_state.manual_posts[i]["text"] = text
                st.session_state.manual_posts[i]["keywords"] = keywords_text

        st.button("+ Добавить пост", on_click=add_post, type="primary")
        st.caption("Кнопка добавления находится снизу, чтобы было удобно при скролле")

        for i, item in enumerate(st.session_state.manual_posts):
            if (item.get("text") or "").strip():
                posts.append(
                    {
                        "source": f"inline_{i+1:02d}",
                        "text": item.get("text") or "",
                        "keywords": item.get("keywords") or "",
                    }
                )

    else:
        uploaded_keywords_text = st.text_area(
            "Keywords для всех загруженных постов (по одному в строке, без запятых)",
            value="",
            height=120,
            help=(
                "Если вы загружаете файлы, вставьте сюда общий список keywords, "
                "который нужно естественно встроить в title и description."
            ),
        )

        uploaded = st.file_uploader(
            "Загрузите .txt/.html файлы постов — можно несколько",
            type=["txt", "html", "htm"],
            accept_multiple_files=True,
        )

        if uploaded:
            run_dir = os.path.join(
                os.getcwd(),
                "pinterest_uploads",
                "post_texts_" + datetime.now().strftime("%Y%m%d_%H%M%S"),
            )
            os.makedirs(run_dir, exist_ok=True)

            for i, up in enumerate(uploaded, 1):
                safe_name = os.path.basename(up.name or f"post_{i:02d}.txt")
                path = os.path.join(run_dir, f"{i:02d}_{safe_name}")
                with open(path, "wb") as f:
                    f.write(up.getbuffer())

                ext = os.path.splitext(path)[1].lower()
                raw = open(path, "r", encoding="utf-8", errors="ignore").read()
                text = gpt_post.strip_html(raw) if ext in {".html", ".htm"} else raw
                posts.append({"source": path, "text": text, "keywords": uploaded_keywords_text})

    st.markdown("---")

    st.write(f"Постов к обработке: {len(posts)}")

    if len(posts) == 0:
        st.info("Добавьте хотя бы один текст поста (или загрузите файлы).")
        st.stop()

    theme_options = list(THEME_LABELS.keys())
    current_theme = str(st.session_state.get("pinterest_topic_theme") or "decor")
    if current_theme not in theme_options:
        current_theme = "decor"
        st.session_state.pinterest_topic_theme = current_theme

    selected_theme = st.selectbox(
        "Theme for generation",
        options=theme_options,
        index=theme_options.index(current_theme),
        format_func=lambda x: THEME_LABELS.get(x, x),
        key="pinterest_topic_theme",
        help="Choose the Pinterest niche the prompt should match.",
    )

    include_hashtags = st.checkbox("Добавлять hashtags в description (description + 2 перевода строки + hashtags)", value=True)

    if st.button(f"Сгенерировать Pinterest данные ({len(posts)})", type="primary"):
        results = []
        with st.spinner("Генерация... (каждый пост 10–60 сек)"):
            for i, p in enumerate(posts, 1):
                st.write(f"Обработка [{i}/{len(posts)}]: {p['source']}")
                pin = gpt_post.generate_pinterest_assets_from_post_text(
                    p["text"],
                    provided_keywords=p.get("keywords") or "",
                    topic_theme=selected_theme,
                )
                if isinstance(pin, dict):
                    pin = _sanitize_generated_pin(pin)
                results.append({"source": p["source"], "pinterest": pin, "keywords": p.get("keywords") or ""})

        st.success("Готово")
        st.markdown("---")

        for r in results:
            src = r.get("source")
            pin = r.get("pinterest") or {}
            with st.expander(str(src), expanded=False):
                if not isinstance(pin, dict):
                    st.error("Unexpected response type")
                    st.stop()

                desc = (pin.get("description") or "").strip()
                hashtags = (pin.get("hashtags") or "").strip()
                three = pin.get("three_word_keywords") or []
                titles = pin.get("title_options") or []
                raw = (pin.get("_raw") or "").strip()

                if not desc and not hashtags and not titles and raw:
                    st.warning(
                        "Не удалось распарсить секции генератора (TITLE/DESCRIPTION). Ниже — сырой вывод."
                    )
                    st.text_area("Raw", raw, height=300)
                    continue

                st.markdown("**Title**")
                st.code("\n".join(titles) or "", language=None)

                combined = _build_description(pin, include_hashtags=include_hashtags)
                st.markdown("**Описание (готово для копипаста)**")
                st.text_area("Description", combined, height=180, key=f"desc_{src}")
                st.caption(
                    f"Длина description: {len(desc)} | длина hashtags: {len(hashtags)} | всего: {len(combined)}"
                )

                st.markdown("**3‑словные ключевые (10 шт)**")
                st.code("\n".join(three) or "", language=None)

                with st.expander("Raw output", expanded=False):
                    st.text_area("Raw", raw, height=260, key=f"raw_{src}")


# -------------------------
# Mode 3: bulk CSV + images + imgbb + export
# -------------------------
else:
    st.subheader("Bulk-режим: CSV + изображения")

    st.markdown(
        "**Вход:** CSV со столбцами `post_text` и `pin_filename` (одна строка = один пин).\n"
        "Опционально можно добавить колонку `keywords` (keywords для конкретной строки).\n"
        "Опционально можно добавить колонку `post_link` (ссылка на пост, попадёт в колонку Link).\n\n"
        "**Затем:** загружаете изображения (много сразу). `pin_filename` должен совпадать с именем файла изображения.\n\n"
        "**Выход:** CSV со столбцами `title`, `description`, `image_url`."
    )

    col1, col2 = st.columns([1, 1])
    with col1:
        input_csv = st.file_uploader("Загрузите input CSV", type=["csv"], accept_multiple_files=False)
    with col2:
        uploaded_images = st.file_uploader(
            "Загрузите изображения (JPEG/PNG/WebP) — можно несколько",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True,
        )

    provided_keywords = st.text_area(
        "Keywords (общие для всех строк, по одному в строке; fallback если в строке keywords пустые)",
        value="",
        height=120,
    )

    include_hashtags = st.checkbox("В description добавлять hashtags (как в интерфейсе) ", value=True)

    # IMGBB API key
    # Рекомендуется хранить ключ в переменной окружения IMGBB_API_KEY,
    # но для удобства оставляем дефолт как вы указали.
    # os.getenv("IMGBB_API_KEY") or
    imgbb_default = "b97eac8568d1eaa7b92c734683106ee0"
    imgbb_api_key = st.text_input( 
        "IMGBB API key (лучше через переменную окружения IMGBB_API_KEY)",
        value=imgbb_default,
        type="password",
    )

    if input_csv is not None:
        try:
            # Compute a lightweight signature to detect actual file changes.
            csv_bytes = input_csv.getvalue()
            csv_sig = (len(csv_bytes), hash(csv_bytes[:20000]))
            prev_sig = st.session_state.get("bulk_csv_sig")

            rows = _parse_input_csv(input_csv)
            st.session_state.bulk_rows = rows

            # Reset dependent state ONLY when a different CSV is uploaded.
            if prev_sig != csv_sig:
                st.session_state.bulk_csv_sig = csv_sig
                st.session_state.bulk_results = []
                st.session_state.bulk_image_urls = {}
                st.session_state.bulk_uploaded_images = {}
                auto_theme = _detect_single_row_theme(rows)
                auto_board_file = _detect_single_board_file(rows)
                st.session_state.bulk_auto_theme = auto_theme
                st.session_state.bulk_auto_board_file = auto_board_file
                if auto_theme:
                    st.session_state.pinterest_topic_theme = auto_theme
                if auto_board_file and os.path.exists(auto_board_file):
                    st.session_state.pinterest_board_names_path = auto_board_file

        except Exception as e:
            # Diagnostic: show header line for quick debugging
            try:
                raw_preview = input_csv.getvalue()[:5000].decode("utf-8-sig", errors="replace")
                first_line = raw_preview.splitlines()[0] if raw_preview.splitlines() else ""
                st.caption(f"CSV header preview: {first_line}")
            except Exception:
                pass
            st.error(f"Не удалось прочитать CSV: {e}")
            st.stop()

    rows = st.session_state.get("bulk_rows") or []

    if rows:
        st.write(f"Строк в CSV: {len(rows)}")

        st.markdown("### Keywords по строкам (отдельное поле для каждого поста)")
        st.caption("Если поле keywords пустое — будут использованы общие (fallback) keywords выше.")

        # Render one textarea per row (as requested)
        for r in rows:
            row_id = str(r.get("row"))
            fname = r.get("pin_filename") or ""
            key = f"bulk_kw_{row_id}"

            if key not in st.session_state:
                st.session_state[key] = r.get("keywords", "")

            with st.container(border=True):
                title_preview = _extract_title_from_post_text(r.get("post_text") or "")
                st.markdown(f"**{title_preview}**")
                meta_parts = [f"Row {row_id}", fname]
                if r.get("domain"):
                    meta_parts.append(f"domain: {r.get('domain')}")
                if r.get("theme"):
                    meta_parts.append(f"theme: {THEME_LABELS.get(str(r.get('theme')), str(r.get('theme')))}")
                st.caption(" | ".join([part for part in meta_parts if part]))

                st.text_area(
                    "Keywords (по одному в строке, без запятых)",
                    key=key,
                    height=100,
                )

                # Optional: show post text for context
                with st.expander("Показать post_text", expanded=False):
                    st.text_area("post_text", value=r.get("post_text") or "", height=160, disabled=True, key=f"pt_{row_id}")

            # sync back to rows
            r["keywords"] = (st.session_state.get(key) or "")

        st.session_state.bulk_rows = rows

        with st.expander("Показать полный input CSV (preview)", expanded=False):
            st.dataframe(rows, use_container_width=True, hide_index=True)

    # Cache uploaded images in session_state, because Streamlit can rerender and
    # lose the in-memory UploadedFile objects depending on interaction.
    if uploaded_images:
        for up in uploaded_images:
            st.session_state.bulk_uploaded_images[_safe_basename(up.name)] = up.getvalue()

    images_by_name: dict[str, bytes] = dict(st.session_state.get("bulk_uploaded_images") or {})
    if images_by_name:
        st.write(f"Изображений в кеше: {len(images_by_name)}")

    if rows:
        missing_imgs = sorted({r["pin_filename"] for r in rows if r.get("pin_filename")} - set(images_by_name.keys()))
        if missing_imgs and uploaded_images:
            st.warning(
                "Не найдены изображения для следующих pin_filename (проверьте совпадение имени файла):\n- "
                + "\n- ".join(missing_imgs[:50])
                + ("\n…" if len(missing_imgs) > 50 else "")
            )

    # --- Pinterest board settings (should be set BEFORE generation) ---
    # These settings affect:
    # - which board list is used for Gemini auto-assignment
    # - which options appear in the table SelectboxColumn
    st.session_state.setdefault("pinterest_board_names_path", "board_names.txt")
    st.session_state.setdefault("auto_assign_boards", True)

    with st.expander("Pinterest CSV settings", expanded=False):
        st.checkbox(
            "Автоматически подобрать Pinterest board через Gemini (строго из списка)",
            value=bool(st.session_state.get("auto_assign_boards", True)),
            key="auto_assign_boards",
            help=(
                "После генерации будет сделан отдельный запрос к Gemini: для каждого пина выберет наиболее подходящий board "
                "ИСКЛЮЧИТЕЛЬНО из выбранного файла board_names*.txt. Любые несовпадения будут заменены на пустое значение."
            ),
        )

        theme_options = list(THEME_LABELS.keys())
        current_theme = str(st.session_state.get("pinterest_topic_theme") or "decor")
        if current_theme not in theme_options:
            current_theme = "decor"
            st.session_state.pinterest_topic_theme = current_theme

        st.selectbox(
            "Theme for generation",
            options=theme_options,
            index=theme_options.index(current_theme),
            format_func=lambda x: THEME_LABELS.get(x, x),
            key="pinterest_topic_theme",
            help="CSV rows with a recognized domain keep their own auto theme; this selector is the fallback for the rest.",
        )

        auto_theme = str(st.session_state.get("bulk_auto_theme") or "").strip()
        if auto_theme:
            st.caption(f"Auto theme from CSV domain: {THEME_LABELS.get(auto_theme, auto_theme)}")

        board_file_options: list[str] = []
        for board_file in ("board_names.txt", "board_names2.txt", "board_names3.txt", "board_names4.txt"):
            if os.path.exists(board_file):
                board_file_options.append(board_file)

        auto_board_file = str(st.session_state.get("bulk_auto_board_file") or "").strip()
        if auto_board_file:
            st.caption(f"Auto board file from CSV domain: {auto_board_file}")

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

    gen_disabled = not bool(rows)
    if st.button("1) Сгенерировать title/description", type="primary", disabled=gen_disabled):
        results: list[dict[str, Any]] = []
        with st.spinner(f"Генерация... ({len(rows)} шт)"):
            for i, r in enumerate(rows, 1):
                st.write(f"[{i}/{len(rows)}] {r.get('pin_filename') or ''}")
                row_kw = (r.get("keywords") or "").strip()
                eff_kw = row_kw if row_kw else provided_keywords
                eff_theme = str(r.get("theme") or st.session_state.get("pinterest_topic_theme") or "decor").strip().lower()
                pin = gpt_post.generate_pinterest_assets_from_post_text(
                    r.get("post_text") or "",
                    provided_keywords=eff_kw,
                    topic_theme=eff_theme,
                    site_domain=r.get("domain") or "",
                )
                if isinstance(pin, dict):
                    pin = _sanitize_generated_pin(pin)
                titles = pin.get("title_options") if isinstance(pin, dict) else None
                title = (titles[0] if isinstance(titles, list) and titles else "")
                desc_out = _build_description(pin if isinstance(pin, dict) else {}, include_hashtags=include_hashtags)

                results.append(
                    {
                        "row": r.get("row"),
                        "pin_filename": r.get("pin_filename"),
                        "post_link": r.get("post_link") or "",
                        "domain": r.get("domain") or "",
                        "theme": eff_theme,
                        "keywords": eff_kw,
                        "title": title,
                        "description": desc_out,
                        "_pin": pin,
                    }
                )

        st.session_state.bulk_results = results
        st.success("Тексты сгенерированы")

    results = st.session_state.get("bulk_results") or []
    if results:
        st.markdown("---")
        st.subheader("Результаты генерации")

        # Build / keep Pinterest export table (editable)
        # Note: Pinterest "Keywords" column is usually meant for short tags (e.g. "healthy, hummus, summer").
        # We do NOT auto-fill it from the long SEO phrases used in the Gemini prompt.
        board_names_path_current = str(st.session_state.get("pinterest_board_names_path") or "board_names.txt")
        export_sig = (tuple((x.get("row"), x.get("pin_filename")) for x in results), board_names_path_current)

        def _build_even_publish_schedule_iso(n: int) -> list[str]:
            """Return a list of ISO datetimes evenly spread from now until next midnight.

            IMPORTANT: This function returns *local* wall-clock times (naive datetimes formatted
            as ISO). We apply the Pinterest timezone fix only at CSV export time, so the
            editable table continues to show local times.

            Example: now=16:00, n=3 => [16:00, 20:00, 24:00(next day 00:00)]
            """

            if n <= 0:
                return []

            now = datetime.now()
            # First post = "now" (but without seconds) so it never schedules in the past.
            start = now.replace(second=0, microsecond=0)
            # End = next midnight
            end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

            if n == 1:
                return [start.strftime("%Y-%m-%dT%H:%M:%S")]

            total_seconds = max(0.0, (end - start).total_seconds())
            step = total_seconds / float(max(1, n - 1))

            out: list[str] = []
            for i in range(n):
                dt = start + timedelta(seconds=step * i)
                out.append(dt.strftime("%Y-%m-%dT%H:%M:%S"))
            return out

        if st.session_state.get("bulk_export_sig") != export_sig:
            schedule = _build_even_publish_schedule_iso(len(results))

            export_rows = []
            for idx, x in enumerate(results):
                fname = (x.get("pin_filename") or "").strip()
                desc = _board_specific_description_tweak((x.get("description") or "").strip(), board_names_path_current)
                export_rows.append(
                    {
                        # keep pin_filename for mapping (hidden/disabled)
                        "pin_filename": fname,
                        "Title": (x.get("title") or "").strip(),
                        "Media URL": "",  # will be filled after imgbb upload
                        "Pinterest board": "",
                        "Thumbnail": "",
                        "Description": desc,
                        "Link": (x.get("post_link") or "").strip(),
                        "Publish date": schedule[idx] if idx < len(schedule) else "",
                        "Keywords": "",
                    }
                )

            # Auto-assign boards via Gemini (strictly from selected board_names file)
            st.session_state.setdefault("auto_assign_boards", True)
            st.session_state.setdefault("bulk_board_assign_sig", None)
            if bool(st.session_state.get("auto_assign_boards", True)) and os.path.exists(board_names_path_current):
                if st.session_state.get("bulk_board_assign_sig") != export_sig:
                    with st.spinner("Подбираю Pinterest board для каждого пина через Gemini..."):
                        pins_for_boards = [
                            {
                                "title": str(r.get("Title") or ""),
                                "description": str(r.get("Description") or ""),
                            }
                            for r in export_rows
                        ]
                        boards = gpt_post.suggest_boards_for_pins(
                            pins_for_boards,
                            board_names_path=board_names_path_current,
                            max_desc_chars=260,
                        )
                        if isinstance(boards, list) and len(boards) == len(export_rows):
                            for i, b in enumerate(boards):
                                export_rows[i]["Pinterest board"] = (b or "").strip()
                    st.session_state.bulk_board_assign_sig = export_sig

            st.session_state.bulk_export_rows = export_rows
            st.session_state.bulk_export_sig = export_sig

        st.markdown("### Pinterest CSV (редактируемая таблица)")
        st.caption(
            "Заполните Pinterest board / Link / Publish date / Keywords перед экспортом. "
            "Media URL заполнится автоматически после загрузки изображений в imgbb."
        )

        board_names_path = str(st.session_state.get("pinterest_board_names_path") or "board_names.txt")
        board_options = _load_board_names(path=board_names_path)
        # Build a search-friendly list while keeping canonical values only.
        # Streamlit selectbox search is usually case-insensitive, but hidden spaces break it.
        # We already normalized whitespace in _load_board_names().

        # IMPORTANT: keep board selection inside the table (SelectboxColumn), but avoid
        # overwriting the backing data on every rerender (that causes the "double click" revert).
        # Include board_names_path in key so changing source refreshes editor + column options.
        editor_key = f"bulk_export_editor_{abs(hash((export_sig, board_names_path)))}"

        edited_export = st.data_editor(
            st.session_state.bulk_export_rows,
            key=editor_key,
            use_container_width=True,
            hide_index=True,
            column_config={
                "pin_filename": st.column_config.TextColumn("pin_filename", disabled=True, width="medium"),
                "Title": st.column_config.TextColumn("Title", width="large"),
                "Media URL": st.column_config.TextColumn("Media URL", disabled=True, width="large"),
                "Pinterest board": st.column_config.SelectboxColumn(
                    "Pinterest board",
                    options=board_options,
                    help="Выберите board из списка (можно начать печатать, поиск не чувствителен к регистру в большинстве случаев).",
                    width="medium",
                ),
                "Thumbnail": st.column_config.TextColumn("Thumbnail", width="medium"),
                "Description": st.column_config.TextColumn("Description", width="large"),
                "Link": st.column_config.TextColumn("Link", width="large"),
                "Publish date": st.column_config.TextColumn("Publish date", width="medium"),
                "Keywords": st.column_config.TextColumn("Keywords", width="large"),
            },
        )

        def _to_records(val: Any) -> list[dict[str, Any]]:
            # data_editor can return list[dict] or a pandas DataFrame
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

        # Persist edits as list-of-dicts to keep later code simple.
        # Sometimes the editor can temporarily return a non-serializable object;
        # in that case, keep the previous value to avoid "double click" feeling.
        # Do NOT write back to bulk_export_rows on every rerender.
        # We'll use the edited table value for export.
        recs = _to_records(edited_export)
        st.session_state.bulk_export_rows_live = recs

        # --- Validation: ensure all selected boards exist in the chosen board_names file ---
        # Note: Gemini auto-assign already does strict validation (unknown -> ""),
        # but this check is useful after manual edits, copy/paste, or if options changed.
        allowed_boards = set(board_options)
        invalid_rows: list[tuple[int, str]] = []
        for i, row in enumerate(recs):
            b = str((row or {}).get("Pinterest board") or "").replace("\u00A0", " ")
            b = " ".join(b.strip().split())
            if not b:
                continue
            if b not in allowed_boards:
                invalid_rows.append((i + 1, b))  # 1-based for humans

        if not invalid_rows:
            st.success("✅ Проверка boards: все значения в колонке 'Pinterest board' присутствуют в выбранном txt файле.")
        else:
            st.error(
                "⚠️ Найдены значения в колонке 'Pinterest board', которых НЕТ в выбранном txt файле. "
                "Pinterest может создать новые boards при импорте CSV — исправьте вручную."
            )
            st.write("Проблемные строки (номер строки в таблице → значение):")
            st.code("\n".join([f"{row_num}: {val}" for row_num, val in invalid_rows[:50]]), language="text")
            if len(invalid_rows) > 50:
                st.caption(f"Показано 50 из {len(invalid_rows)} проблемных строк")

        with st.expander("Полные тексты (для копирования)", expanded=False):
            for x in results:
                fname = x.get("pin_filename") or ""
                row_id = x.get("row") or ""
                with st.expander(f"{row_id} • {fname}", expanded=False):
                    st.text_area("Title", value=x.get("title") or "", height=80, key=f"res_title_{row_id}")
                    st.text_area("Description", value=x.get("description") or "", height=220, key=f"res_desc_{row_id}")
                    if isinstance(x.get("_pin"), dict) and (x.get("_pin") or {}).get("_raw"):
                        with st.expander("Raw (Gemini output)", expanded=False):
                            st.text_area("Raw", value=(x.get("_pin") or {}).get("_raw") or "", height=260, key=f"res_raw_{row_id}")

    export_disabled = not (rows and results)
    if st.button("2) Сгенерировать CSV (загрузит картинки в imgbb)", disabled=export_disabled):
        # Re-read from session_state inside button handler (avoid any local stale state)
        results = st.session_state.get("bulk_results") or []
        if not results:
            st.error("Нет результатов генерации. Сначала нажмите: 1) Сгенерировать title/description")
            st.stop()

        images_by_name = dict(st.session_state.get("bulk_uploaded_images") or {})
        if not images_by_name:
            st.error("Сначала загрузите изображения (они должны появиться в 'Изображений в кеше').")
            st.stop()

        # Validate filename mapping
        needed = [x.get("pin_filename") for x in results if (x.get("pin_filename") or "").strip()]
        if len(needed) != len(results):
            st.error(
                "В некоторых строках пустой pin_filename. Проверьте input CSV. "
                f"Непустых: {len(needed)} из {len(results)}"
            )
            st.stop()

        missing = [n for n in needed if n not in images_by_name]
        if missing:
            st.error(
                "Не хватает изображений для некоторых строк. Проверьте совпадение имени файла. Например: "
                + ", ".join(missing[:10])
            )
            st.stop()

        st.info(f"Сопоставлено изображений по именам: {len(set(needed))} (строк: {len(results)})")

        # Upload images (dedupe)
        urls: dict[str, str] = dict(st.session_state.get("bulk_image_urls") or {})

        if not (imgbb_api_key or "").strip():
            st.error("Укажите IMGBB API key (или задайте IMGBB_API_KEY в окружении).")
            st.stop()

        to_upload = [n for n in sorted(set(needed)) if n not in urls]
        prog = st.progress(0.0)

        if to_upload:
            with st.spinner(f"Загрузка изображений в imgbb: {len(to_upload)} шт"):
                for idx, fname in enumerate(to_upload, 1):
                    img_bytes = images_by_name[fname]
                    try:
                        url = _imgbb_upload_bytes(img_bytes, api_key=imgbb_api_key, filename=fname)
                    except Exception as e:
                        st.error(f"Не удалось загрузить {fname} в imgbb: {e}")
                        st.stop()
                    urls[fname] = url
                    prog.progress(idx / max(1, len(to_upload)))
        else:
            st.info("Все необходимые изображения уже загружены (использую кеш из session_state).")

        st.session_state.bulk_image_urls = urls
        st.success("Изображения готовы")

        # Build Pinterest bulk CSV (Pinterest uploader-friendly)
        export_rows = (
            st.session_state.get("bulk_export_rows_live")
            or st.session_state.get("bulk_export_rows")
            or []
        )
        # Normalize in case something stored a DataFrame-like object
        if hasattr(export_rows, "to_dict"):
            try:
                export_rows = list(export_rows.to_dict("records"))
            except Exception:
                export_rows = []

        if not export_rows:
            st.error("Нет таблицы экспорта. Сначала выполните генерацию (1) и убедитесь, что таблица Pinterest CSV отображается.")
            st.stop()

        # Fill Media URL based on pin_filename -> uploaded url
        for row in export_rows:
            fname = (row.get("pin_filename") or "").strip()
            row["Media URL"] = urls.get(fname, row.get("Media URL") or "")

        st.session_state.bulk_export_rows = export_rows

        out = io.StringIO()
        writer = csv.writer(
            out,
            delimiter=",",
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\r\n",
        )

        header = [
            "Title",
            "Media URL",
            "Pinterest board",
            "Thumbnail",
            "Description",
            "Link",
            "Publish date",
            "Keywords",
        ]
        writer.writerow(header)

        def _normalize_publish_date_for_pinterest_csv(publish_date: str) -> str:
            """Normalize 'Publish date' so Pinterest schedules at the intended local time.

            Pinterest bulk CSV importer commonly interprets timezone-less ISO datetimes as UTC.
            When we type local time (e.g. 11:04) into the table, Pinterest may shift it by the
            local UTC offset (e.g. schedule at 14:04 for UTC+3).

            Strategy:
            - If the string already contains an explicit timezone ('Z', '+03:00', etc.) we keep it.
            - If it's a timezone-less ISO like 'YYYY-MM-DDTHH:MM:SS', we treat it as *local time*
              and convert to a timezone-less UTC string.
            """

            s = (publish_date or "").strip()
            if not s:
                return ""

            # If user already provided timezone information, don't touch it.
            # We only consider timezone markers after the seconds part.
            # Examples we want to keep as-is:
            # - 2026-01-31T11:04:00Z
            # - 2026-01-31T11:04:00+03:00
            # - 2026-01-31T11:04:00-05:00
            if s.endswith("Z"):
                return s
            if len(s) > 19:
                tail = s[19:]
                if "+" in tail or "-" in tail:
                    return s

            # Best-effort parse: accept 'YYYY-MM-DDTHH:MM:SS' or 'YYYY-MM-DD HH:MM:SS'.
            try:
                s_norm = s.replace(" ", "T")
                dt_local_naive = datetime.strptime(s_norm, "%Y-%m-%dT%H:%M:%S")
            except Exception:
                # If format is unknown, leave as-is.
                return s

            local_tz = datetime.now().astimezone().tzinfo
            if local_tz is None:
                return s_norm

            dt_local = dt_local_naive.replace(tzinfo=local_tz)
            dt_utc_naive = dt_local.astimezone(timezone.utc).replace(tzinfo=None)
            return dt_utc_naive.strftime("%Y-%m-%dT%H:%M:%S")

        wrote = 0
        for row in export_rows:
            writer.writerow([
                (row.get("Title") or "").strip(),
                (row.get("Media URL") or "").strip(),
                (row.get("Pinterest board") or "").strip(),
                (row.get("Thumbnail") or "").strip(),
                (row.get("Description") or "").strip(),
                (row.get("Link") or "").strip(),
                _normalize_publish_date_for_pinterest_csv((row.get("Publish date") or "").strip()),
                (row.get("Keywords") or "").strip(),
            ])
            wrote += 1

        st.success(f"Pinterest CSV сформирован: {wrote} строк")

        # Pinterest expects a standard CSV (comma-separated) and typically does NOT like BOM.
        out_bytes = out.getvalue().encode("utf-8")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        st.download_button(
            "Скачать Pinterest CSV",
            data=out_bytes,
            file_name=f"pinterest_bulk_{ts}.csv",
            mime="text/csv; charset=utf-8",
        )

        # Also show a quick preview
        st.markdown("---")
        st.subheader("Preview Pinterest CSV")
        st.dataframe(
            [
                {
                    "Title": r.get("Title"),
                    "Media URL": r.get("Media URL"),
                    "Pinterest board": r.get("Pinterest board"),
                    "Description": r.get("Description"),
                    "Publish date": r.get("Publish date"),
                    "Keywords": r.get("Keywords"),
                }
                for r in st.session_state.get("bulk_export_rows") or []
            ],
            use_container_width=True,
            hide_index=True,
        )
