import os
import sys
import time
import signal
import random
import re
import json
from collections import deque
from dataclasses import dataclass
from typing import Optional, Iterable, Any

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    # Allow importing this module in environments where 'requests' isn't installed
    # (e.g., limited test sandboxes). Runtime calls to Gemini will error clearly.
    requests = None  # type: ignore

# --- Graceful shutdown ---
shutdown_requested = False


def handle_shutdown_signal(signum, frame):
    global shutdown_requested
    if not shutdown_requested:
        print("\nРЎРёРіРЅР°Р» РѕСЃС‚Р°РЅРѕРІРєРё РїРѕР»СѓС‡РµРЅ. Р—Р°РІРµСЂС€Р°СЋ С‚РµРєСѓС‰СѓСЋ Р·Р°РґР°С‡Сѓ...")
        shutdown_requested = True
    else:
        print("\nРџРѕРІС‚РѕСЂРЅС‹Р№ СЃРёРіРЅР°Р». РџСЂРёРЅСѓРґРёС‚РµР»СЊРЅС‹Р№ РІС‹С…РѕРґ.")
        sys.exit(1)


def setup_signals():
    try:
        signal.signal(signal.SIGINT, handle_shutdown_signal)
        signal.signal(signal.SIGTERM, handle_shutdown_signal)
    except ValueError:
        # Not in main thread (e.g. Streamlit)
        pass


# --- Gemini config ---

def _load_api_keys() -> list[str]:
    """Load API keys from env if provided, else fallback to a hardcoded list.

    Env options:
      - GEMINI_API_KEYS: comma/semicolon/whitespace-separated
      - GEMINI_API_KEY: single key
    """
    env_many = os.getenv("GEMINI_API_KEYS", "").strip()
    env_one = os.getenv("GEMINI_API_KEY", "").strip()
    if env_many:
        parts = re.split(r"[\s,;]+", env_many)
        keys = [p.strip() for p in parts if p.strip()]
        if keys:
            return keys
    if env_one:
        return [env_one]

    # Fallback (existing behaviour in this repo)
    return [
        "AIzaSyA-pBPECS91lPpG_TfS82i5jlRj2LPbcLU",
        "AIzaSyC3ZZlvgw67VS9bYjBuoNWeTRilzH9EpVc",
        "AIzaSyBCyPgGyGyOD2iBGwnhWY4GtGr__fJZ_sk",
        "AIzaSyCn41iq0IcG-sPV27hHZQVtNTNYDleDnFs",
        "AIzaSyAf9xq74b40OMAFx2tmjzSxIFnlDBfXmlI",
    ]


GEMINI_API_KEYS = _load_api_keys()
current_gemini_key_index = 0

MODELS = [
    # "models/gemini-3.1-flash-lite-preview",
    # "models/gemini-2.5-flash-lite",
    # "models/gemini-3-pro",
    # "models/gemini-3-flash-preview",
    "models/gemini-2.5-flash",
    # "models/gemini-2.5-pro",
    # "models/gemini-2.5-flash-lite",
]
current_model_index = 0

RATE_LIMITS = {
    # "models/gemini-3.1-flash-lite-preview": 30,
    # "models/gemini-3-pro": 30,
    "models/gemini-2.5-flash": 30,
    # "models/gemini-3-flash-preview": 30,
    # "models/gemini-2.5-pro": 30,
    # "models/gemini-2.5-flash-lite": 30,
}
rate_windows = {idx: deque() for idx in range(len(MODELS))}
last_429_at = {idx: 0.0 for idx in range(len(MODELS))}

# anti-bot jitter delay between requests
MIN_REQUEST_DELAY_SEC = float(os.getenv("GEMINI_MIN_DELAY_SEC", "3.8"))
MAX_REQUEST_DELAY_SEC = float(os.getenv("GEMINI_MAX_DELAY_SEC", "7.8"))


def _sleep_jitter_before_request():
    if MAX_REQUEST_DELAY_SEC <= 0:
        return
    lo = max(0.0, MIN_REQUEST_DELAY_SEC)
    hi = max(lo, float(MAX_REQUEST_DELAY_SEC))
    delay = random.uniform(lo, hi)
    if delay > 0:
        print(f"  - Jitter-РїР°СѓР·Р° РїРµСЂРµРґ Р·Р°РїСЂРѕСЃРѕРј: {delay:.2f}СЃ")
        time.sleep(delay)


def get_current_url() -> str:
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
        print(f"  - Р”РѕСЃС‚РёРіРЅСѓС‚ РјРёРЅСѓС‚РЅС‹Р№ Р»РёРјРёС‚ ({rpm} RPM) РґР»СЏ {name}. РџР°СѓР·Р° {sleep_for}СЃ...")
        time.sleep(sleep_for)


def _redact_key(url: str) -> str:
    try:
        return re.sub(r"(key=)[^&]+", r"\1***", url)
    except Exception:
        return url


def call_gemini_text(prompt_text: str, timeout_sec: int = 90) -> str | None:
    """Call Gemini with text-only prompt.

    Returns response text or None if blocked.
    """
    global current_model_index, current_gemini_key_index, last_429_at

    if requests is None:  # type: ignore[truthy-bool]
        raise RuntimeError("Python package 'requests' is required. Install it via: pip install requests")

    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt_text},
                ]
            }
        ]
    }

    while True:
        if shutdown_requested:
            print("  - РћРїРµСЂР°С†РёСЏ РїСЂРµСЂРІР°РЅР° РїРѕР»СЊР·РѕРІР°С‚РµР»РµРј.")
            return ""

        if current_model_index >= len(MODELS):
            print("\nР›РёРјРёС‚С‹ РІСЃРµС… РјРѕРґРµР»РµР№ РґР»СЏ С‚РµРєСѓС‰РµРіРѕ РєР»СЋС‡Р° РёСЃС‡РµСЂРїР°РЅС‹.")
            current_model_index = 0
            current_gemini_key_index += 1

            if current_gemini_key_index >= len(GEMINI_API_KEYS):
                print("Р’СЃРµ API РєР»СЋС‡Рё РёСЃС‡РµСЂРїР°Р»Рё Р»РёРјРёС‚С‹. РџР°СѓР·Р° 5 РјРёРЅСѓС‚...")
                current_gemini_key_index = 0
                time.sleep(300)

            last_429_at = {idx: 0.0 for idx in range(len(MODELS))}
            print(f"РџРµСЂРµРєР»СЋС‡РёР»РёСЃСЊ РЅР° API РєР»СЋС‡ #{current_gemini_key_index + 1}.")
            continue

        model_name = MODELS[current_model_index]
        url = get_current_url()
        redacted_url = _redact_key(url)
        print(f"  - Р—Р°РїСЂРѕСЃ С‡РµСЂРµР· {model_name}вЂ¦")
        wait_for_rate_slot(current_model_index)

        try:
            print(f"  - РћС‚РїСЂР°РІРєР°: prompt_len={len(prompt_text)} chars, url={redacted_url}")
            _sleep_jitter_before_request()
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout_sec)
            resp.raise_for_status()
            data = resp.json() or {}

            pf = data.get("promptFeedback") or {}
            if "blockReason" in pf:
                reason = pf.get("blockReason")
                print(f"  - РљРѕРЅС‚РµРЅС‚ Р·Р°Р±Р»РѕРєРёСЂРѕРІР°РЅ (РїСЂРёС‡РёРЅР°: {reason}).")
                return None

            cand = (data.get("candidates") or [])
            if cand:
                content = cand[0].get("content") or {}
                parts = content.get("parts") or []
                if parts and "text" in parts[0]:
                    return parts[0]["text"]

            print("  - РќРµ СѓРґР°Р»РѕСЃСЊ РёР·РІР»РµС‡СЊ С‚РµРєСЃС‚ (РЅРµРёР·РІРµСЃС‚РЅР°СЏ СЃС‚СЂСѓРєС‚СѓСЂР° РѕС‚РІРµС‚Р°). РџРµСЂРµРєР»СЋС‡РµРЅРёРµ РјРѕРґРµР»Рё Рё РїР°СѓР·Р° 15СЃвЂ¦")
            current_model_index += 1
            time.sleep(15)
            continue

        except requests.exceptions.RequestException as e:
            resp = getattr(e, "response", None)
            status_code = resp.status_code if resp is not None else None
            reason = getattr(resp, "reason", None)
            body_text = None
            err_status = None
            err_message = None

            if resp is not None:
                try:
                    body_text = resp.text
                    j = resp.json()
                    if isinstance(j, dict) and "error" in j:
                        err = j.get("error") or {}
                        err_message = err.get("message")
                        err_status = err.get("status")
                except Exception:
                    pass

            print("  - HTTP РѕС€РёР±РєР° РїСЂРё РѕР±СЂР°С‰РµРЅРёРё Рє Gemini:")
            print(f"    вЂў status={status_code} {reason or ''}")
            print(f"    вЂў model={model_name}, key_index={current_gemini_key_index+1}/{len(GEMINI_API_KEYS)}")
            print(f"    вЂў url={redacted_url}")
            if err_status or err_message:
                print(f"    вЂў google.error.status={err_status}")
                print(f"    вЂў google.error.message={err_message}")
            if body_text:
                bt = body_text.strip()
                if len(bt) > 1200:
                    bt = bt[:1200] + "вЂ¦ [truncated]"
                print("    вЂў response_body=\n" + bt)

            if status_code == 429:
                now = time.time()
                if (now - last_429_at[current_model_index]) > 120:
                    last_429_at[current_model_index] = now
                    print("  - РћС€РёР±РєР° 429 (РјРёРЅСѓС‚РЅС‹Р№ Р»РёРјРёС‚). РџР°СѓР·Р° 61СЃ Рё РїРѕРІС‚РѕСЂвЂ¦")
                    time.sleep(61)
                else:
                    print("  - РџРѕРІС‚РѕСЂРЅР°СЏ 429 (РІРѕР·РјРѕР¶РЅРѕ РґРЅРµРІРЅРѕР№ Р»РёРјРёС‚). РџРµСЂРµРєР»СЋС‡РµРЅРёРµ РјРѕРґРµР»РёвЂ¦")
                    current_model_index += 1
                continue
            elif status_code in (400, 404):
                print(f"  - РћС€РёР±РєР° {status_code}. РџРµСЂРµРєР»СЋС‡РµРЅРёРµ РјРѕРґРµР»РёвЂ¦")
                current_model_index += 1
                continue
            elif status_code in (401, 403):
                print("  - Р”РѕСЃС‚СѓРї Р·Р°РїСЂРµС‰С‘РЅ (401/403). РџРµСЂРµРєР»СЋС‡Р°СЋ API-РєР»СЋС‡ Рё СЃР±СЂР°СЃС‹РІР°СЋ РјРѕРґРµР»СЊ РЅР° РїРµСЂРІСѓСЋвЂ¦")
                current_gemini_key_index += 1
                if current_gemini_key_index >= len(GEMINI_API_KEYS):
                    current_gemini_key_index = 0
                    print("  - Р’СЃРµ РєР»СЋС‡Рё РїРµСЂРµРїСЂРѕР±РѕРІР°РЅС‹. РџР°СѓР·Р° 60СЃвЂ¦")
                    time.sleep(60)
                current_model_index = 0
                time.sleep(3)
                continue
            elif status_code and status_code >= 500:
                print(f"  - Р’СЂРµРјРµРЅРЅР°СЏ РѕС€РёР±РєР° СЃРµСЂРІРµСЂР° ({status_code}). РџР°СѓР·Р° 15СЃвЂ¦")
                time.sleep(15)
                continue
            else:
                print(f"  - РћС€РёР±РєР° СЃРµС‚Рё: {e}. РџР°СѓР·Р° 15СЃвЂ¦")
                time.sleep(15)
                continue

        except Exception as e:
            print(f"  - РќРµРёР·РІРµСЃС‚РЅР°СЏ РѕС€РёР±РєР°: {e}. РџР°СѓР·Р° 15СЃвЂ¦")
            time.sleep(15)
            continue


# --- Post text -> Pinterest data ---

MAX_POST_CHARS = int(os.getenv("PIN_POST_MAX_CHARS", "15000"))


def normalize_post_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def strip_html(html: str) -> str:
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;", " ", html)
    html = re.sub(r"&amp;", "&", html)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def _parse_provided_keywords(provided: object) -> list[str]:
    """Parse user-provided Pinterest keywords.

    Accepts either:
    - multiline string (one keyword per line)
    - list/tuple/set of strings

    Returns de-duplicated keywords (case-insensitive), preserving first-seen order.
    """
    if provided is None:
        return []

    items: list[str] = []
    if isinstance(provided, str):
        # Allow users to paste with commas or newlines; normalize to lines.
        txt = provided.replace(",", "\n")
        items = [ln.strip() for ln in txt.splitlines()]
    elif isinstance(provided, (list, tuple, set)):
        items = [str(x).strip() for x in provided if str(x).strip()]
    else:
        items = [str(provided).strip()]

    out: list[str] = []
    seen: set[str] = set()
    for kw in items:
        kw = re.sub(r"\s+", " ", (kw or "").strip())
        if not kw:
            continue
        key = kw.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(kw.lower())
    return out


PROMPT_THEME_BY_DOMAIN = {
    "nestingmuse.com": "decor",
    "spaceofmuse.com": "decor",
    "glowuproutine.com": "fashion",
    "sweethomecookery.com": "food",
}

PROMPT_THEME_ALIASES = {
    "decor": "decor",
    "home decor": "decor",
    "interior": "decor",
    "interiors": "decor",
    "design": "decor",
    "food": "food",
    "recipe": "food",
    "recipes": "food",
    "cooking": "food",
    "fashion": "fashion",
    "style": "fashion",
    "outfits": "fashion",
    "moda": "fashion",
}

PROMPT_THEME_PROFILES = {
    "decor": {
        "vertical": "home decor, interior design, and outdoor living content",
        "power_words": "'Ideas', 'Guide', 'Secrets', 'Hacks', 'Tips', 'Trends', 'Decor', 'Design', 'Style', or 'Makeover'",
        "theme_examples": "'Green Bedroom Design' or 'Christmas Decor Ideas'",
        "detail_examples": "'sustainable linen layers, edible centerpieces, and luxurious velvet accents'",
        "topic_guardrail": "Keep the language rooted in rooms, decor, styling, layouts, furniture, outdoor spaces, or home aesthetics.",
        "avoid_line": "Avoid food, recipe, outfit, wardrobe, or beauty wording unless the source text clearly supports it.",
    },
    "food": {
        "vertical": "food, recipes, meal planning, and entertaining content",
        "power_words": "'Recipes', 'Ideas', 'Guide', 'Recipe', 'Tips', 'Meals', 'Desserts', 'Snacks', 'Menu', 'Dinner', 'Brunch', or 'Appetizers'",
        "theme_examples": "'High Protein Breakfast Guide' or 'Memorial Day Food Ideas'",
        "detail_examples": "'whipped ricotta crostini, charred corn salsa, and smoky chipotle drizzle'",
        "topic_guardrail": "Keep the language rooted in recipes, meals, dishes, ingredients, cooking methods, menu ideas, or serving occasions.",
        "avoid_line": "Avoid interior decor, furniture, room makeover, or wardrobe/style wording unless the source text clearly supports it.",
    },
    "fashion": {
        "vertical": "fashion, outfit, and wardrobe styling content",
        "power_words": "'Outfits', 'Looks', 'Style Guide', 'Trends', 'Ideas', 'Capsule', 'Essentials', 'Finds', 'Inspo', or 'Styling Tips'",
        "theme_examples": "'Summer Outfit Ideas' or 'Old Money Style Guide'",
        "detail_examples": "'mesh ballet flats, pearl hair clips, and oversized linen vests'",
        "topic_guardrail": "Keep the language rooted in outfits, clothing, accessories, wardrobe staples, seasonal style, or styling advice.",
        "avoid_line": "Avoid home decor, furniture, room design, recipe, ingredient, or cooking wording unless the source text clearly supports it.",
    },
}


def _normalize_prompt_theme(topic_theme: str | None = None, site_domain: str | None = None) -> str:
    domain = str(site_domain or "").strip().strip("\"'").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    if domain in PROMPT_THEME_BY_DOMAIN:
        return PROMPT_THEME_BY_DOMAIN[domain]

    theme = str(topic_theme or "").strip().lower()
    if theme in PROMPT_THEME_ALIASES:
        return PROMPT_THEME_ALIASES[theme]
    return "decor"


def _build_pinterest_prompt(
    post_text: str,
    provided_keywords: list[str] | None = None,
    topic_theme: str | None = None,
    site_domain: str | None = None,
) -> str:
    # IMPORTANT: We do NOT force JSON output here.
    provided_keywords = provided_keywords or []
    provided_block = "\n".join(f"- {kw}" for kw in provided_keywords) if provided_keywords else "- (none)"
    theme_key = _normalize_prompt_theme(topic_theme=topic_theme, site_domain=site_domain)
    profile = PROMPT_THEME_PROFILES.get(theme_key) or PROMPT_THEME_PROFILES["decor"]

    return (
        f"Act as a Pinterest SEO & Growth Expert for {profile['vertical']}. "
        "Your task is to write one high-ranking Title and one Description that look human-written but are heavily optimized for Pinterest search.\n\n"
        "INPUT DATA:\n"
        "- BLOG POST CONTENT: " + post_text + "\n"
        "- TARGET KEYWORDS: " + provided_block + "\n"
        f"- TOPIC THEME: {theme_key}\n\n"

        "STEP 1: SELECT A POWER HOOK\n"
        f"Start the title with a strong Pinterest-friendly long-tail keyword hook containing a word such as {profile['power_words']}, "
        "or any similar high-intent term that perfectly fits the topic.\n\n"

        "STEP 2: GENERATE ONE SEO TITLE\n"
        "- Start with the Power Hook from Step 1.\n"
        "- Naturally integrate the most important long-tail keywords.\n"
        f"- TOPIC FIT: {profile['topic_guardrail']}\n"
        "- CHARACTER LIMIT: Aim for as close to 100 characters as possible. Do not leave empty space; use the room to add more relevant long-tail phrases.\n\n"

        "STEP 3: WRITE A DENSE SEO DESCRIPTION\n"
        "- Write one cohesive, natural paragraph.\n"
        "- KEYWORD LOGIC: If 'TARGET KEYWORDS' are provided, use them exactly as phrases. If 'TARGET KEYWORDS' is empty or insufficient, brainstorm high-volume, long-tail Pinterest search terms based on the blog post content. Focus more on long-tail keywords rather than two-word phrases.\n"
        "- STRICT NO-REPEAT RULE: Do NOT reuse the same long-tail phrases or exact keyword combinations used in the Title. Use synonyms and alternative high-volume search terms to cover a broader search intent.\n"
        f"- CORE THEME FOCUS: Prioritize primary topic long-tail keywords (e.g., {profile['theme_examples']}) over niche details or sub-topics like {profile['detail_examples']}. IMPORTANT: High-level topic phrases MUST outweigh specific details in the description.\n"
        f"- TOPIC SAFETY: {profile['avoid_line']}\n"
        "- DENSITY & LENGTH: Aim for as close to 500 characters as possible (the maximum limit). Use every character to include functional, descriptive long-tail keywords that people actually search for. No fluff adjectives, only value-driven phrases.\n"
        "- TONE: Expert blogger style. Helpful, engaging, but extremely keyword-dense.\n\n"

        "OUTPUT FORMAT (STRICT):\n"
        "TITLE:\n"
        "[One optimized title up to 100 chars]\n\n"
        "DESCRIPTION:\n"
        "[One dense paragraph up to 500 chars]\n\n"
        "All output must be in English."
    )


def _safe_len(s: object) -> int:
    return len(s) if isinstance(s, str) else 0


def _strip_double_asterisks(text: str) -> str:
    """Remove Gemini's occasional Markdown bold markers (**...**).

    Gemini sometimes wraps inserted keywords with double asterisks.
    For Pinterest output we want plain text.
    """
    if not isinstance(text, str) or not text:
        return "" if text is None else str(text)
    return text.replace("**", "")


def _extract_section(raw: str, header: str, next_headers: list[str]) -> str:
    """Extract text between HEADER: and next header."""
    # Normalize newlines
    txt = raw.replace("\r\n", "\n").replace("\r", "\n")
    # Find header line
    m = re.search(rf"^\s*{re.escape(header)}\s*:\s*$", txt, flags=re.I | re.M)
    if not m:
        return ""
    start = m.end()
    # Find next header after start
    end = len(txt)
    for h in next_headers:
        m2 = re.search(rf"^\s*{re.escape(h)}\s*:\s*$", txt[start:], flags=re.I | re.M)
        if m2:
            end = min(end, start + m2.start())
    return txt[start:end].strip()


def _extract_section_flexible(raw: str, header: str, next_headers: list[str]) -> str:
    """Extract section for both styles:
    - HEADER:\n<multiline value>
    - HEADER: <inline value>
    """
    txt = raw.replace("\r\n", "\n").replace("\r", "\n")
    m = re.search(rf"^\s*{re.escape(header)}\s*:\s*(.*)$", txt, flags=re.I | re.M)
    if not m:
        return ""

    inline = (m.group(1) or "").strip()
    start = m.end()
    end = len(txt)
    for h in next_headers:
        m2 = re.search(rf"^\s*{re.escape(h)}\s*:\s*(?:.*)$", txt[start:], flags=re.I | re.M)
        if m2:
            end = min(end, start + m2.start())

    block = txt[start:end].strip()
    if inline and block:
        return (inline + "\n" + block).strip()
    return (inline or block).strip()


def _parse_single_title(section: str) -> str:
    lines = [ln.strip() for ln in (section or "").splitlines() if ln.strip()]
    if not lines:
        return ""

    title = lines[0]
    title = re.sub(r"^\s*title\s*:\s*", "", title, flags=re.I)
    title = re.sub(r"^\s*\d+[\)\.\:\-]\s*", "", title)
    title = re.sub(r"^\s*[-вЂў*]\s*", "", title)
    title = re.sub(r"\s+", " ", title).strip().strip("\"'")

    # Ignore placeholder templates accidentally echoed by the model.
    if re.fullmatch(r"\[[^\]]+\]", title):
        return ""
    return title


def _parse_single_description(section: str) -> str:
    desc = " ".join((section or "").split())
    desc = re.sub(r"^\s*description\s*:\s*", "", desc, flags=re.I).strip().strip("\"'")
    if re.fullmatch(r"\[[^\]]+\]", desc):
        return ""
    return desc


def _parse_titles(section: str) -> list[str]:
    lines = [ln.strip() for ln in section.split("\n") if ln.strip()]
    out: list[str] = []
    for ln in lines:
        # 1) Title...
        ln = re.sub(r"^\s*\d+\)\s*", "", ln).strip()
        # normalize multi-spaces
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

        # normalize spacing around |
        ln = re.sub(r"\s*\|\s*", " | ", ln).strip()
        out.append(ln)

    # keep first 5 if model returned more
    return out[:5]


def _parse_hashtags(section: str) -> str:
    # keep exactly as one line for easy copy-paste
    s = " ".join(section.replace("\n", " ").split())
    # Ensure hashtags start with # if the model forgot
    parts = [p for p in s.split(" ") if p.strip()]
    fixed = []
    for p in parts:
        if not p.startswith("#"):
            fixed.append("#" + p.lstrip("#"))
        else:
            fixed.append(p)
    # attempt to keep 10 tags if more were returned
    fixed10 = fixed[:10]
    return " ".join(fixed10).strip()


def _parse_three_word_keywords(section: str) -> list[str]:
    lines = [ln.strip() for ln in section.split("\n") if ln.strip()]
    out: list[str] = []
    for ln in lines:
        ln = re.sub(r"^\s*[-вЂў]\s*", "", ln).strip()
        # remove numbering if any
        ln = re.sub(r"^\s*\d+\)\s*", "", ln).strip()
        # collapse whitespace
        ln = " ".join(ln.split())
        if not ln:
            continue
        out.append(ln)
    return out[:10]


# --- Post-processing helpers (formatting requirements for Pinterest output) ---

MAX_DESCRIPTION_CHARS = int(os.getenv("PIN_DESC_MAX_CHARS", "500"))


def _truncate_at_word_boundary(text: str, limit: int) -> str:
    """Truncate text to <= limit chars, trying not to cut words."""
    text = (text or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    cut = text[:limit].rstrip()
    # try to cut to last space if it's not too far back
    last_space = cut.rfind(" ")
    if last_space >= max(0, limit - 30):
        cut = cut[:last_space].rstrip()

    # add ellipsis if we had to truncate
    if cut and not cut.endswith("..."):
        if len(cut) + 3 <= limit:
            cut = cut + "..."
        else:
            cut = cut[:-3].rstrip() + "..."
    return cut


def _insert_article_marker(title: str) -> str:
    """Insert '(Article рџ‘‰рџ”—)' after the first segment (before the first separator).

    Important: we intentionally do NOT treat '-' as a separator because it frequently
    appears inside words/phrases and can break titles.
    """
    t = (title or "").strip()
    if not t or "(article рџ‘‰рџ”—)" in t.lower():
        return t

    # Prefer the explicit separator the prompt suggests.
    m = re.search(r"\s*(\||/|\\|вЂ”)\s*", t)
    if not m:
        # If the model didn't use a separator, just append.
        return t + " (Article рџ‘‰рџ”—)"

    sep = m.group(1)
    left = t[: m.start()].rstrip()
    right = t[m.end() :].lstrip()

    # Keep original separator token; normalize spaces around it.
    return f"{left} (Article рџ‘‰рџ”—) {sep} {right}".strip()


def _make_topic_label_from_article_title(article_title: str) -> str:
    """Create a short 'topic' label used as Segment A in Pinterest titles.

    Goal: something like 'Grandmillennial Style Decor Ideas'.

    Heuristic:
      - take left side of ':' or ' - ' or ' вЂ” '
      - drop trailing 'How to ...' / 'Guide' style suffixes
      - ensure it ends with 'Decor Ideas' if it contains the word 'Decor'
    """
    t = (article_title or "").strip()
    if not t:
        return ""

    # Prefer part before colon/dash (common in blog H1s)
    for sep in (" : ", ":", " вЂ” ", " - ", "вЂ“", "вЂ”"):
        if sep in t:
            t = t.split(sep, 1)[0].strip()
            break

    # Remove common suffix starters
    t = re.split(r"\bHow to\b|\bA Guide\b|\bGuide\b|\bTips\b", t, maxsplit=1, flags=re.I)[0].strip()

    # Normalize spaces
    t = " ".join(t.split())

    # If it already ends with Ideas, keep.
    if re.search(r"\bideas\b\s*$", t, flags=re.I):
        return t

    # If it looks like a decor topic, make it '... Decor Ideas'
    if re.search(r"\bdecor\b", t, flags=re.I):
        # avoid doubling 'Decor'
        base = re.sub(r"\bdecor\b\s*$", "Decor", t, flags=re.I).strip()
        return f"{base} Ideas"

    # Otherwise just add 'Decor Ideas'
    return f"{t} Decor Ideas"


def _extract_blog_title_from_post_text(post_text: str) -> str:
    """Try to extract the exact H1/article title from a CTRL+A pasted blog page.

    Your pages often look like:
      ... Blog Search?

      <H1 title>

      <subtitle/lede>

    We use a heuristic: find the first non-empty line after 'Search?' (or 'Search?\n')
    that looks like a title (not nav words).
    """
    if not post_text:
        return ""

    # Work on a line-based representation (do NOT normalize spaces here).
    txt = post_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in txt.split("\n")]

    # Find the last occurrence of 'Search?' (some pages might contain it multiple times).
    search_idx = None
    for i, ln in enumerate(lines):
        if ln.lower() == "search?":
            search_idx = i

    start = (search_idx + 1) if search_idx is not None else 0

    # Skip known nav words that may appear in the header.
    nav_words = {
        "outdoor",
        "about",
        "contact",
        "blog",
        "search?",
        "search",
        "menu",
        "home",
    }

    def looks_like_title(s: str) -> bool:
        if not s:
            return False
        low = s.lower()
        if low in nav_words:
            return False
        # avoid lines that are mostly punctuation or too short
        if len(s) < 6:
            return False
        # title is usually not a hashtag line
        if s.lstrip().startswith("#"):
            return False
        # allow colons, dashes, quotes, etc.
        return True

    def shrink_title(s: str) -> str:
        s = " ".join((s or "").split()).strip()
        if not s:
            return ""
        # If title+body are in one line, cut at the first sentence-ending dot.
        dot = s.find(".")
        if 10 <= dot <= 140:
            return s[:dot].strip()
        # Otherwise, keep it reasonably short.
        if len(s) > 120:
            return s[:120].rstrip() + "вЂ¦"
        return s

    for ln in lines[start:]:
        ln = " ".join(ln.split())
        if looks_like_title(ln):
            return shrink_title(ln)

    # Fallback for CSV/compact texts where the whole post is in one cell:
    # take the first sentence up to the first '.' (or first line) as a title.
    compact = " ".join(txt.split())
    if compact:
        return shrink_title(compact)

    return ""


def _infer_article_title_from_titles(titles: list[str]) -> str:
    """Fallback inference if we couldn't extract the exact blog title from the post text."""
    if not titles:
        return ""
    first = (titles[0] or "").strip()
    if not first:
        return ""

    # Remove our marker if present.
    first = re.sub(r"\s*\(Article\ рџ‘‰рџ”—)\s*", " ", first, flags=re.I).strip()

    # use first segment before the first separator
    m = re.search(r"\s*(\||/|\\|вЂ”)\s*", first)
    if not m:
        return first
    return first[: m.start()].strip()


def _prefix_description_with_click_link(desc: str, article_title: str) -> str:
    """Return description as:

    вќ‡пёЏ Click the link to read the full article "..." and make your dream home a reality.

    <main description>

    The whole DESCRIPTION must stay within MAX_DESCRIPTION_CHARS.
    """
    desc = (desc or "").strip()
    article_title = (article_title or "").strip()

    if not article_title:
        click_line = "вќ‡пёЏ Click the link to read the full article and make your dream home a reality."
    else:
        click_line = f"вќ‡пёЏ Click the link to read the full article \"{article_title}\"."

    # If there is no main description, just return the click line (trimmed).
    if not desc:
        return _truncate_at_word_boundary(click_line, MAX_DESCRIPTION_CHARS)

    joiner = "\n\n"
    reserved = len(joiner) + len(click_line)
    if reserved >= MAX_DESCRIPTION_CHARS:
        # Degenerate case: keep only the click line.
        return _truncate_at_word_boundary(click_line, MAX_DESCRIPTION_CHARS)

    main_limit = MAX_DESCRIPTION_CHARS - reserved
    main = _truncate_at_word_boundary(desc, main_limit)
    return (click_line + joiner + main).strip()


def _generate_pinterest_assets_from_post_text_impl(
    post_text: str,
    provided_keywords: object = None,
    topic_theme: str | None = None,
    site_domain: str | None = None,
) -> dict:
    """Main implementation: generate Pinterest data from a blog post text."""
    normalized = normalize_post_text(post_text)
    truncated = normalized
    was_truncated = False
    if len(truncated) > MAX_POST_CHARS:
        truncated = truncated[:MAX_POST_CHARS]
        was_truncated = True

    provided_keywords_list = _parse_provided_keywords(provided_keywords)
    prompt = _build_pinterest_prompt(
        truncated,
        provided_keywords=provided_keywords_list,
        topic_theme=topic_theme,
        site_domain=site_domain,
    )
    text = call_gemini_text(prompt)
    if text is None:
        return {
            "description": "",
            "hashtags": "",
            "three_word_keywords": [],
            "title_options": [],
            "_raw": "",
            "_meta": {"blocked": True},
        }

    raw = (text or "").strip()
    raw = _strip_double_asterisks(raw)

    # Parse NEW format first:
    # TITLE:
    # DESCRIPTION:
    title_sec = _extract_section_flexible(raw, "TITLE", ["DESCRIPTION", "HASHTAGS", "THREE-WORD KEYWORDS", "TITLE OPTIONS"])
    desc_sec = _extract_section_flexible(raw, "DESCRIPTION", ["HASHTAGS", "THREE-WORD KEYWORDS", "TITLE OPTIONS"])
    title_one = _parse_single_title(title_sec)
    desc_main = _parse_single_description(desc_sec)

    # Backward-compatible fallback for older responses.
    old_titles_sec = _extract_section(raw, "TITLE OPTIONS", ["DESCRIPTION", "HASHTAGS", "THREE-WORD KEYWORDS"])
    old_desc_sec = _extract_section(raw, "DESCRIPTION", ["HASHTAGS", "THREE-WORD KEYWORDS", "TITLE OPTIONS"])
    hashtags_sec = _extract_section(raw, "HASHTAGS", ["THREE-WORD KEYWORDS", "DESCRIPTION", "TITLE OPTIONS"])
    three_sec = _extract_section(raw, "THREE-WORD KEYWORDS", ["HASHTAGS", "DESCRIPTION", "TITLE OPTIONS"])

    old_titles = _parse_titles(old_titles_sec) if old_titles_sec else []
    if not title_one and old_titles:
        title_one = (old_titles[0] or "").strip()

    if not desc_main and old_desc_sec:
        desc_main = _parse_single_description(old_desc_sec)

    # Keep output shape stable for Streamlit/CSV pipeline.
    titles = [title_one] if title_one else []
    desc = _truncate_at_word_boundary(desc_main, MAX_DESCRIPTION_CHARS)
    hashtags = _parse_hashtags(hashtags_sec) if hashtags_sec else ""
    three = _parse_three_word_keywords(three_sec) if three_sec else []

    return {
        "description": desc,
        "hashtags": hashtags,
        "three_word_keywords": three,
        "title_options": titles,
        "_raw": raw,
        "_meta": {
            "post_chars": len(normalized),
            "post_chars_used": len(truncated),
            "post_truncated": was_truncated,
            "description_chars": _safe_len(desc),
            "hashtags_chars": _safe_len(hashtags),
        },
    }


# Public API: this is the name used by the Streamlit UI

def generate_pinterest_assets_from_post_text(
    post_text: str,
    provided_keywords: object = None,
    topic_theme: str | None = None,
    site_domain: str | None = None,
) -> dict:
    """Generate Pinterest assets from blog post text.

    `provided_keywords` can be a multiline string (one keyword per line) or a list of keywords.
    These keywords will be naturally integrated into title options and the description.
    """
    return _generate_pinterest_assets_from_post_text_impl(
        post_text,
        provided_keywords=provided_keywords,
        topic_theme=topic_theme,
        site_domain=site_domain,
    )


# -----------------------------------------------------------------------------
# Board suggestion (Gemini) вЂ” used by Streamlit UIs to auto-fill Pinterest board
# -----------------------------------------------------------------------------


def load_board_names_for_matching(path: str) -> list[str]:
    """Load canonical board names (one per line) for strict matching.

    Returns list WITHOUT the leading empty option ("") that UIs sometimes include.
    """

    try:
        if not path or not os.path.exists(path):
            return []

        raw_lines = open(path, "r", encoding="utf-8", errors="ignore").read().splitlines()

        def norm(s: str) -> str:
            s = (s or "").replace("\u00A0", " ")
            s = " ".join(s.strip().split())
            return s

        names = [norm(ln) for ln in raw_lines if norm(ln)]
        # De-dupe while preserving order (exact string match)
        seen: set[str] = set()
        out: list[str] = []
        for n in names:
            if n not in seen:
                out.append(n)
                seen.add(n)
        return out
    except Exception:
        return []


def _strip_code_fences(s: str) -> str:
    txt = (s or "").strip()
    if not txt:
        return ""
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt, flags=re.I)
        txt = re.sub(r"\s*```\s*$", "", txt)
    return txt.strip()


def _extract_first_json_blob(text: str) -> str:
    """Extract the first JSON object/array substring from text (best-effort)."""
    t = _strip_code_fences(text)
    if not t:
        return ""

    # Prefer an object that contains "boards".
    m = re.search(r"\{[\s\S]*?\}\s*$", t)
    if m and "boards" in m.group(0):
        return m.group(0).strip()

    # Any object
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        return m.group(0).strip()

    # Any array
    m = re.search(r"\[[\s\S]*\]", t)
    if m:
        return m.group(0).strip()

    return t


def _parse_boards_json(response_text: str, expected_n: int) -> list[str] | None:
    blob = _extract_first_json_blob(response_text)
    if not blob:
        return None

    try:
        obj = json.loads(blob)
    except Exception:
        return None

    boards: Any = None
    if isinstance(obj, dict):
        boards = obj.get("boards")
    elif isinstance(obj, list):
        boards = obj

    if not isinstance(boards, list):
        return None

    out: list[str] = [str(x or "") for x in boards]
    if expected_n >= 0 and len(out) != expected_n:
        # Wrong length => treat as invalid
        return None
    return out


def _build_board_suggestion_prompt(
    *,
    pins: list[dict[str, str]],
    board_names: list[str],
    max_desc_chars: int = 260,
) -> str:
    """Build a single strict prompt that asks Gemini to choose EXACT board names."""

    # Only give canonical non-empty names to the model.
    boards_block = "\n".join(f"- {b}" for b in board_names)

    def _clean(s: str) -> str:
        s = (s or "").replace("\u00A0", " ")
        s = " ".join(s.strip().split())
        return s

    def _short_desc(s: str) -> str:
        s = _clean(s)
        if max_desc_chars and len(s) > int(max_desc_chars):
            return s[: int(max_desc_chars)].rstrip() + "вЂ¦"
        return s

    pins_lines: list[str] = []
    for i, p in enumerate(pins, 1):
        title = _clean(p.get("title") or "")
        desc = _short_desc(p.get("description") or "")
        pins_lines.append(f"{i}. TITLE: {title}\n   DESCRIPTION: {desc}")

    pins_block = "\n".join(pins_lines)

    return (
        "You are helping to assign Pinterest pins to existing Pinterest boards.\n"
        "You MUST choose the single most suitable board name for each pin from the allowed list below.\n\n"
        "CRITICAL RULES:\n"
        "- You are NOT allowed to invent new board names.\n"
        "- Every returned board name MUST match one of the allowed board names EXACTLY (character-for-character).\n"
        "- If nothing fits, return an empty string \"\" for that pin.\n"
        "- Preserve the original order of pins.\n"
        "- Output ONLY valid JSON. No markdown, no code fences, no explanations.\n\n"
        "ALLOWED BOARD NAMES (copy-paste EXACTLY from here):\n"
        f"{boards_block}\n\n"
        "PINS (order matters):\n"
        f"{pins_block}\n\n"
        "OUTPUT JSON SCHEMA (STRICT):\n"
        "{\n  \"boards\": [\n    \"<board for pin #1>\",\n    \"<board for pin #2>\",\n    ...\n  ]\n}\n"
    )


def suggest_boards_for_pins(
    pins: list[dict[str, str]],
    *,
    board_names_path: str,
    max_desc_chars: int = 260,
    max_prompt_chars: int = 26000,
) -> list[str]:
    """Suggest Pinterest board name for each pin via Gemini (strict matching).

    pins: list of {"title": str, "description": str}

    Returns a list of board names (or "") of the same length and in the same order.

    Safety:
    - We validate that every returned board is exactly one of the names from board_names_path.
    - Any unknown value becomes "".
    - If parsing fails, returns [""] * len(pins).

    If prompt is too large, we auto-split into chunks while preserving order.
    """

    pins = pins or []
    n = len(pins)
    if n == 0:
        return []

    board_names = load_board_names_for_matching(board_names_path)
    if not board_names:
        return [""] * n

    allowed = set(board_names)

    def _run_chunk(chunk: list[dict[str, str]]) -> list[str]:
        prompt = _build_board_suggestion_prompt(
            pins=chunk,
            board_names=board_names,
            max_desc_chars=max_desc_chars,
        )

        resp = call_gemini_text(prompt, timeout_sec=120)
        if resp is None:
            return [""] * len(chunk)

        parsed = _parse_boards_json(resp, expected_n=len(chunk))
        if parsed is None:
            return [""] * len(chunk)

        # Strict validation
        out: list[str] = []
        for b in parsed:
            b = (b or "").replace("\u00A0", " ")
            b = " ".join(b.strip().split())
            out.append(b if b in allowed else "")
        return out

    # Chunking: grow chunk size until prompt is within max_prompt_chars
    # (rough heuristic: prompt length grows roughly linearly with chunk size)
    # Start with all pins in one go (as requested), fallback to splitting if too big.
    prompt_all = _build_board_suggestion_prompt(pins=pins, board_names=board_names, max_desc_chars=max_desc_chars)
    if len(prompt_all) <= int(max_prompt_chars):
        return _run_chunk(pins)

    # Split into chunks of ~25 and adjust if still too big
    out_all: list[str] = []
    start = 0
    chunk_size = 25
    while start < n:
        chunk = pins[start : start + chunk_size]
        # Ensure chunk prompt fits; if not, reduce chunk size
        while chunk_size > 1:
            ptxt = _build_board_suggestion_prompt(pins=chunk, board_names=board_names, max_desc_chars=max_desc_chars)
            if len(ptxt) <= int(max_prompt_chars):
                break
            chunk_size = max(1, chunk_size // 2)
            chunk = pins[start : start + chunk_size]

        out_all.extend(_run_chunk(chunk))
        start += len(chunk)

    # Guarantee length
    if len(out_all) != n:
        return [""] * n
    return out_all



@dataclass
class PostInput:
    source: str
    text: str


def read_text_file(path: str, encoding: str = "utf-8") -> str:
    with open(path, "r", encoding=encoding, errors="ignore") as f:
        return f.read()


def iter_posts_from_dir(dir_path: str) -> Iterable[PostInput]:
    exts = {".txt", ".html", ".htm"}
    for name in sorted(os.listdir(dir_path)):
        p = os.path.join(dir_path, name)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in exts:
            continue
        raw = read_text_file(p)
        text = strip_html(raw) if ext in {".html", ".htm"} else raw
        yield PostInput(source=os.path.abspath(p), text=text)


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        ax = os.path.abspath(x)
        if ax not in seen and os.path.exists(ax):
            seen.add(ax)
            out.append(ax)
    return out


# --- CLI ---

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Generate Pinterest title/description from blog post TEXT (Gemini).\n"
            "Supports single post text, text files, or a directory of posts (.txt/.html)."
        )
    )

    parser.add_argument("--text", help="Post text (single).")
    parser.add_argument("--text-file", action="append", default=[], help="Path to a .txt/.html file. Can be used multiple times.")
    parser.add_argument("--dir", help="Directory with .txt/.html posts.")

    parser.add_argument(
        "--out",
        "-o",
        help=(
            "Output path. If ends with .txt (or .json), writes a combined TEXT file. "
            "Otherwise treats it as an output directory and writes one .txt file per post."
        ),
    )

    args = parser.parse_args()

    inputs: list[PostInput] = []

    if args.text:
        inputs.append(PostInput(source="inline", text=args.text))

    if args.text_file:
        for p in _dedupe_keep_order(args.text_file):
            ext = os.path.splitext(p)[1].lower()
            raw = read_text_file(p)
            text = strip_html(raw) if ext in {".html", ".htm"} else raw
            inputs.append(PostInput(source=p, text=text))

    if args.dir:
        dir_path = os.path.abspath(args.dir)
        if not os.path.isdir(dir_path):
            print(f"Directory not found: {dir_path}")
            sys.exit(1)
        inputs.extend(list(iter_posts_from_dir(dir_path)))

    if not inputs:
        print("No inputs provided. Use --text, --text-file (repeatable) or --dir.")
        sys.exit(1)

    combined_out_file: Optional[str] = None
    out_dir: Optional[str] = None

    if args.out:
        out_path = os.path.abspath(args.out)
        low = out_path.lower()
        if low.endswith(".txt") or low.endswith(".json"):
            # Write combined TEXT file (even if user passed .json by habit)
            combined_out_file = out_path
        else:
            out_dir = out_path
            os.makedirs(out_dir, exist_ok=True)
    else:
        if len(inputs) > 1:
            out_dir = os.path.abspath("results_post_texts")
            os.makedirs(out_dir, exist_ok=True)

    if len(inputs) == 1 and not out_dir and not combined_out_file:
        post = inputs[0]
        pin = generate_pinterest_assets_from_post_text(post.text)
        # Print in a human-readable format (same as model output sections)
        if isinstance(pin, dict) and pin.get("_raw"):
            print(pin["_raw"])
        else:
            print(str(pin))
        return

    results: list[dict] = []
    for idx, post in enumerate(inputs, 1):
        print(f"\n===== [{idx}/{len(inputs)}] {post.source} =====")
        try:
            pin = generate_pinterest_assets_from_post_text(post.text)
            item = {"source": post.source, "pinterest": pin}
            results.append(item)

            if out_dir:
                base = os.path.splitext(os.path.basename(post.source))[0] if post.source != "inline" else f"post_{idx:02d}"
                out_file = os.path.join(out_dir, f"{base}.txt")
                raw = pin.get("_raw") if isinstance(pin, dict) else None
                with open(out_file, "w", encoding="utf-8") as f:
                    f.write(raw or "")
                print(f"  - Saved: {out_file}")
        except Exception as e:
            err = {"source": post.source, "error": str(e)}
            results.append(err)
            print(f"  - Error: {e}")

    if combined_out_file:
        with open(combined_out_file, "w", encoding="utf-8") as f:
            for item in results:
                src = item.get("source")
                pin = item.get("pinterest") or {}
                raw = pin.get("_raw") if isinstance(pin, dict) else ""
                f.write(f"===== {src} =====\n")
                f.write((raw or "") + "\n\n")
        print(f"\nCombined TEXT saved: {combined_out_file}")
    elif out_dir:
        print(f"\nDone. Per-post TXT files are in: {out_dir}")


if __name__ == "__main__":
    setup_signals()
    main()

