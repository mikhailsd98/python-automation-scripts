# Streamlit app: Gemini image generation/editing with dynamic prompts
# Usage: streamlit run gemini_image_generator_streamlit.py

import os
import io
import base64
import time
import mimetypes
import requests
import streamlit as st

# Reuse API keys from existing script
try:
    from generate_pinterest_texts import GEMINI_API_KEYS
except Exception:
    GEMINI_API_KEYS = [os.environ.get("GEMINI_API_KEY", "")]  # fallback

st.set_page_config(page_title="Gemini Image Generator", layout="wide")
st.title("Gemini Image Generator / Editor")
st.caption("Введите несколько промптов. Нажмите + чтобы добавить поле. К каждому промпту автоматически добавится преамбула и постамбула. Будет использовано изображение 10x16.jpg из корня проекта как исходник.")

# --- Models that can be selected ---
# Note: не все модели поддерживают генерацию изображений через response_mimeType=image/png.
# Наиболее надежный вариант: models/imagegeneration (новый Images API). Также попробуем универсальные модели через generateContent.
MODEL_OPTIONS = [
    "models/imagegeneration",            # Images API (requires Images API enabled)
    "models/imagegeneration@006",        # Images API (newer)
    "models/gemini-2.5-flash-image",     # Text models that can return image via generateContent
    "models/gemini-2.0-flash",
    "models/gemini-2.0-flash-lite",
]

def _read_image_inline(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Файл не найден: {path}")
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        ext = (os.path.splitext(path)[1] or '').lower()
        if ext in (".jpg", ".jpeg"):
            mime = "image/jpeg"
        elif ext == ".png":
            mime = "image/png"
        elif ext == ".webp":
            mime = "image/webp"
        else:
            mime = "application/octet-stream"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return {"mimeType": mime, "data": b64}

BASE_IMAGE_PATH = "10x16.jpg"

# Session state for dynamic prompts
if "prompts" not in st.session_state:
    st.session_state.prompts = [""]

col1, col2 = st.columns([3, 1])
with col1:
    model = st.selectbox("Модель", options=MODEL_OPTIONS, index=0)
with col2:
    max_outputs = st.number_input("Картинок на промпт", min_value=1, max_value=4, value=1, step=1, help="Сколько изображений генерировать для каждого промпта (если поддерживается моделью)")

st.markdown("**Промпты**")
new_prompts = []
for i, val in enumerate(st.session_state.prompts):
    new_val = st.text_input(f"Промпт #{i+1}", value=val, key=f"prompt_{i}")
    new_prompts.append(new_val)

col_a, col_b = st.columns([1, 3])
with col_a:
    if st.button("+ Добавить поле"):
        st.session_state.prompts.append("")
        st.rerun()
with col_b:
    if len(st.session_state.prompts) > 1:
        if st.button("− Убрать последнее поле"):
            st.session_state.prompts = st.session_state.prompts[:-1]
            st.rerun()

# Update session prompts from inputs
st.session_state.prompts = new_prompts

st.markdown("---")

# Helper: try Images API first, then fallback to generateContent with responseMimeType

def _post_images_api(api_key: str, model_name: str, prompt_text: str, base_inline: dict, n_images: int = 1, timeout_sec: int = 120):
    """Use the Images API endpoints.
    If base_inline is provided -> images:edit, else -> images:generate.
    Returns (images, meta) where images is list[(mime, bytes)] and meta contains diagnostics.
    """
    if not model_name.startswith("models/imagegeneration"):
        return [], {"error": "invalid_model", "message": "Images API only for imagegeneration models"}

    # Prefer the unified Images API endpoints:
    # - POST v1beta/images:edit for editing with a base image
    # - POST v1beta/images:generate for pure text-to-image
    if base_inline:
        url = f"https://generativelanguage.googleapis.com/v1beta/images:edit?key={api_key}"
        body = {
            "model": model_name,
            "image": {"inlineData": base_inline},
            "prompt": {"text": prompt_text},
            "parameters": {
                "numberOfImages": int(n_images)
            }
        }
    else:
        url = f"https://generativelanguage.googleapis.com/v1beta/images:generate?key={api_key}"
        body = {
            "model": model_name,
            "prompt": {"text": prompt_text},
            "parameters": {
                "numberOfImages": int(n_images)
            }
        }

    try:
        resp = requests.post(url, json=body, timeout=timeout_sec)
        status = resp.status_code
        text = resp.text
        meta = {"url": url, "status": status, "response_text": text}
        if status >= 400:
            return [], meta
        data = resp.json() or {}
        out = []
        images = data.get("images") or []
        for item in images:
            if isinstance(item, dict):
                mime = item.get("mimeType") or "image/png"
                b64 = item.get("data")
                if b64:
                    try:
                        out.append((mime, base64.b64decode(b64)))
                    except Exception:
                        pass
        meta["parsed_count"] = len(out)
        return out, meta
    except Exception as e:
        return [], {"url": url, "error": str(e)}


def _post_generate_content_for_image(api_key: str, model_name: str, prompt_text: str, base_inline: dict, timeout_sec: int = 180):
    """Use generateContent with response_mimeType=image/png. Returns (images, meta)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:generateContent?key={api_key}"
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt_text},
                    {"inlineData": base_inline},
                ],
            }
        ],
        "generationConfig": {
            "response_mime_type": "image/png"
        }
    }
    try:
        resp = requests.post(url, json=body, timeout=timeout_sec)
        status = resp.status_code
        text = resp.text
        if status >= 400:
            return [], {"url": url, "status": status, "response_text": text}
        data = resp.json() or {}
        out = []
        cands = data.get("candidates") or []
        for c in cands:
            content = c.get("content") or {}
            parts = content.get("parts") or []
            for p in parts:
                if "inlineData" in p:
                    idata = p.get("inlineData") or {}
                    mime = idata.get("mimeType") or "image/png"
                    b64 = idata.get("data")
                    if b64:
                        try:
                            out.append((mime, base64.b64decode(b64)))
                        except Exception:
                            pass
        return out, {"url": url, "status": status, "parsed_count": len(out)}
    except Exception as e:
        return [], {"url": url, "error": str(e)}


def generate_images_with_prompts(prompts: list[str], model_name: str, n_images_per_prompt: int = 1):
    # Build final prompts with pre/post text
    pre = "Change the white image using this Prompt: "
    post = " DO NOT LEAVE BLANK WHITE SPACE, THIS IS IMPORTANT "
    final_prompts = [f"{pre}{p.strip()}{post}" for p in prompts if (p or "").strip()]

    if not final_prompts:
        st.warning("Добавьте хотя бы один непустой промпт.")
        return []

    # Read base image
    base_inline = _read_image_inline(BASE_IMAGE_PATH)

    results = []  # list of tuples (prompt, [(mime, bytes), ...])

    # Try each API key in order
    keys = [k for k in GEMINI_API_KEYS if k]
    if not keys:
        st.error("Нет доступных API ключей для Gemini. Добавьте в generate_pinterest_texts.GEMINI_API_KEYS или переменную окружения GEMINI_API_KEY.")
        return []

    for fp in final_prompts:
        images_for_prompt = []
        success = False
        last_err = None
        last_meta = None
        for key in keys:
            try:
                if model_name.startswith("models/imagegeneration"):
                    imgs, meta = _post_images_api(key, model_name, fp, base_inline, n_images=int(n_images_per_prompt))
                    if imgs:
                        images_for_prompt = imgs
                        success = True
                        break
                    else:
                        last_meta = meta
                        # try next key if available
                        time.sleep(0.5)
                        continue
                else:
                    imgs, meta = _post_generate_content_for_image(key, model_name, fp, base_inline)
                    if imgs:
                        images_for_prompt = imgs
                        success = True
                        break
                    else:
                        last_meta = meta
                        time.sleep(0.5)
                        continue
            except requests.HTTPError as e:
                last_err = e
                status = e.response.status_code if e.response is not None else None
                # Collect error body for diagnostics
                err_body = None
                try:
                    err_body = e.response.text
                except Exception:
                    pass
                # Simple backoff and continue to next key on auth/quota
                if status in (401, 403, 429):
                    time.sleep(2)
                    continue
                else:
                    # For 400/404/others, do not fallback to text models; try next key
                    time.sleep(1)
                    continue
            except Exception as e:
                last_err = e
                time.sleep(1)
                continue
        # Capture readable error text if failed
        err_text = None
        if not success and last_meta is not None:
            # Prefer meta diagnostics (URL/status/body)
            err_text = f"URL: {last_meta.get('url')}\nStatus: {last_meta.get('status')}\nBody: {str(last_meta.get('response_text'))[:1500]}\nError: {last_meta.get('error')}"
        elif not success and isinstance(last_err, requests.HTTPError) and getattr(last_err, 'response', None) is not None:
            try:
                err_text = last_err.response.text
            except Exception:
                err_text = str(last_err)
        elif not success and last_err is not None:
            err_text = str(last_err)
        results.append((fp, images_for_prompt, err_text if not success else None))
    return results


if st.button("Сгенерировать изображения", type="primary"):
    try:
        res = generate_images_with_prompts(st.session_state.prompts, model, n_images_per_prompt=int(max_outputs))
    except FileNotFoundError as e:
        st.error(str(e))
        res = []

    st.markdown("---")
    for idx, (prompt_text, images, err) in enumerate(res, 1):
        with st.expander(f"Промпт #{idx}", expanded=True):
            st.code(prompt_text)
            if images:
                cols = st.columns(3)
                for j, (mime, img_bytes) in enumerate(images):
                    with cols[j % 3]:
                        st.image(io.BytesIO(img_bytes), caption=f"Image {j+1}")
                        fname = f"gen_{idx}_{j+1}." + ("png" if mime == "image/png" else ("jpg" if mime == "image/jpeg" else "bin"))
                        st.download_button(
                            label="Скачать",
                            data=img_bytes,
                            file_name=fname,
                            mime=mime or "application/octet-stream",
                        )
            else:
                st.warning("Не удалось получить изображение для этого промпта.")
                if err is not None:
                    st.caption(f"Ошибка: {err}")
else:
    # Show preview of base image
    with st.sidebar:
        st.markdown("### Базовое изображение")
        if os.path.exists(BASE_IMAGE_PATH):
            st.image(BASE_IMAGE_PATH, caption=BASE_IMAGE_PATH, use_container_width=True)
        else:
            st.error(f"Базовый файл не найден: {BASE_IMAGE_PATH}")
