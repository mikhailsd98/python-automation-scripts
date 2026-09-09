# Unified Streamlit interface: Gemini (Playwright) → Photoshop watermark removal → WebP conversion
# Usage: streamlit run app_unified_streamlit.py

import os
import sys
import subprocess
import shutil
import queue
import random
from datetime import datetime
from pathlib import Path
from typing import List

import streamlit as st
import streamlit.components.v1 as components
import requests
from requests.auth import HTTPBasicAuth


def _get_secret(key: str, default: str = "") -> str:
    """Safely read Streamlit secrets.

    If no secrets.toml exists, Streamlit raises StreamlitSecretNotFoundError on access.
    We treat that case as "no secret" and return default.
    """
    try:
        return str(st.secrets.get(key, default))
    except Exception:
        return default


# --- WordPress REST helpers (read-only; used for contextual internal linking) ---
# These profiles mirror wp_bulk_upload_streamlit.py so you can reuse the same credentials.
WP_SITE_PROFILES: dict[str, dict[str, str]] = {
    "nestingmuse.com": {
        "base_url": "https://nestingmuse.com",
        "username": "mikeSD",
        "app_password": "iWVS f00D TLip bOVo pUcS pEPK",
    },
    "spaceofmuse.com": {
        "base_url": "https://spaceofmuse.com",
        "username": "Michaela_Shaffer",
        "app_password": "z5FL 8gCm GfM4 WY54 7S6v dQDQ",
    },
    "glowuproutine.com": {
        "base_url": "https://glowuproutine.com",
        "username": "Isabella_Vane",
        "app_password": "X9tW zfXa T7jW nNDQ ynCs gsnf",
    },
    "sweethomecookery.com": {
        "base_url": "https://sweethomecookery.com",
        "username": "Kia_Lawson",
        "app_password": "IdR5 kaiC pv4K gVyx SDsL cNEe",
    },
    "Custom": {"base_url": "", "username": "", "app_password": ""},
}

KNOWN_WP_DOMAINS = [
    "nestingmuse.com",
    "spaceofmuse.com",
    "glowuproutine.com",
    "sweethomecookery.com",
]

SITE_THEME_BY_DOMAIN = {
    "nestingmuse.com": "decor",
    "spaceofmuse.com": "decor",
    "glowuproutine.com": "fashion",
    "sweethomecookery.com": "recipes",
}

ARTICLE_THEME_LABELS = {
    "decor": "Decor / decor",
    "fashion": "Fashion / moda",
    "recipes": "Recipes / kulinariya",
}

SITE_CATEGORY_RULES = {
    "nestingmuse.com": {
        "allowed": "interiors, exteriors",
        "rule": (
            "Set category to EXACTLY one of these two slugs: 'interiors' or 'exteriors'. "
            "Choose based on the topic: indoor/home decor -> 'interiors'; patios/garden/backyard/exterior -> 'exteriors'."
        ),
    },
    "spaceofmuse.com": {
        "allowed": "home-interiors, outdoor-spaces",
        "rule": (
            "Set category to EXACTLY one of these two slugs: 'home-interiors' or 'outdoor-spaces'. "
            "Choose based on the topic: indoor/home decor -> 'home-interiors'; patios/garden/backyard/exterior -> 'outdoor-spaces'."
        ),
    },
    "glowuproutine.com": {
        "allowed": "outfit-ideas, seasonal-trends, style-guides, wardrobe-essentials",
        "rule": (
            "Set category to EXACTLY one of these slugs: 'outfit-ideas', 'seasonal-trends', 'style-guides', or 'wardrobe-essentials'. "
            "Choose the closest fit: outfit inspiration/look ideas -> 'outfit-ideas'; trend-driven or season-based topics -> 'seasonal-trends'; styling advice/how-to content -> 'style-guides'; closet basics/capsule/key staples -> 'wardrobe-essentials'."
        ),
    },
    "sweethomecookery.com": {
        "allowed": "beverages, breakfast-brunch, desserts, healthy-diet, main-courses, quick-easy, seasonal-eats, slow-cooker-air-fryer, snacks-apps, soups-salads, world-kitchen",
        "rule": (
            "Set category to EXACTLY one of these slugs: 'beverages', 'breakfast-brunch', 'desserts', 'healthy-diet', 'main-courses', 'quick-easy', 'seasonal-eats', 'slow-cooker-air-fryer', 'snacks-apps', 'soups-salads', or 'world-kitchen'. "
            "Choose the single closest fit based on the recipe/topic."
        ),
    },
}


def _normalize_wp_base_url(base_url: str) -> str:
    s = str(base_url or "").strip().rstrip("/")
    if not s:
        return ""
    if not (s.startswith("http://") or s.startswith("https://")):
        s = "https://" + s.lstrip("/")
    return s.rstrip("/")


def _strip_html_tags(s: str) -> str:
    import re as _re
    t = str(s or "")
    # remove tags conservatively
    t = _re.sub(r"<[^>]+>", "", t)
    return t


def _known_internal_link_hosts() -> set[str]:
    hosts = {str(x or "").strip().lower() for x in KNOWN_WP_DOMAINS if str(x or "").strip()}

    try:
        import urllib.parse as _urlparse

        for key in ("unif_il_base_url", "unif_tab0_uploads_base_url"):
            base_url = str(st.session_state.get(key) or "").strip()
            if not base_url:
                continue
            parsed = _urlparse.urlparse(base_url if "://" in base_url else "https://" + base_url)
            host = str(parsed.netloc or "").strip().lower()
            if host.startswith("www."):
                host = host[4:]
            if host:
                hosts.add(host)
    except Exception:
        pass

    return hosts


def _normalize_internal_link_href(href: str) -> str:
    """Normalize anchor href for WP internal links.

    - strips stray quotes / escaped quotes that sometimes leak from model output
    - converts known-site absolute URLs to root-relative paths like `/post-slug/`
    - keeps external URLs untouched
    """
    import html as _html
    import re as _re
    from urllib.parse import urlsplit as _urlsplit

    raw = str(href or "")
    if not raw.strip():
        return ""

    s = _html.unescape(raw).strip()
    s = (
        s.replace("\\&quot;", '"')
        .replace("\\&#34;", '"')
        .replace("\\&apos;", "'")
        .replace('\\"', '"')
        .replace("\\'", "'")
    )

    prev = None
    while s and s != prev:
        prev = s
        s = s.strip()
        s = s.lstrip("\\").rstrip("\\").strip()
        if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
            s = s[1:-1].strip()
        s = s.strip().strip('"').strip("'").strip()

    if not s:
        return ""

    if s.startswith("//"):
        s = "https:" + s

    lowered = s.lower()
    if lowered.startswith(("mailto:", "tel:", "javascript:", "#")):
        return s

    hosts = _known_internal_link_hosts()
    for host in sorted(hosts, key=len, reverse=True):
        for prefix in (host + "/", "www." + host + "/"):
            if lowered.startswith(prefix):
                tail = s[len(prefix):].lstrip("/")
                return "/" + tail if tail else "/"

    parts = _urlsplit(s)
    if parts.scheme in {"http", "https"}:
        host = str(parts.netloc or "").strip().lower()
        if host.startswith("www."):
            host = host[4:]
        if host in hosts:
            path = parts.path or "/"
            if not path.startswith("/"):
                path = "/" + path
            if parts.query:
                path += "?" + parts.query
            if parts.fragment:
                path += "#" + parts.fragment
            return path
        return s

    if not parts.scheme and not parts.netloc:
        if s.startswith(("/", "?", "#")):
            return s
        if _re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", s):
            return s
        while s.startswith("./"):
            s = s[2:]
        return "/" + s.lstrip("/")

    return s


def _wp_fetch_recent_posts(
    *,
    base_url: str,
    username: str | None = None,
    app_password: str | None = None,
    verify_ssl: bool = True,
    limit: int = 60,
    timeout_s: int = 30,
) -> list[dict]:
    """Fetch last published posts from WordPress via WP REST API.

    Returns list of {"title": str, "url": str} ordered newest->oldest.

    Notes:
    - Published posts are often publicly accessible without auth, but some sites/plugins restrict it.
    - If username/app_password provided, request uses HTTP Basic Auth (Application Password).
    """
    import html as _html
    import json as _json

    base = _normalize_wp_base_url(base_url)
    if not base:
        return []

    api_base = f"{base}/wp-json/wp/v2"
    per_page = max(1, min(100, int(limit or 60)))

    params = {
        "status": "publish",
        "per_page": per_page,
        "orderby": "date",
        "order": "desc",
        "_fields": "link,title",
    }

    auth = None
    if (username or "").strip() and (app_password or "").strip():
        auth = HTTPBasicAuth(str(username).strip(), str(app_password).strip())

    url = f"{api_base}/posts"
    resp = requests.get(url, params=params, auth=auth, verify=bool(verify_ssl), timeout=int(timeout_s))
    if not resp.ok:
        raise RuntimeError(f"WP REST error {resp.status_code} for GET {url}: {resp.text[:1200]}")

    try:
        data = resp.json()
    except ValueError as e:
        # Some WP stacks return JSON with UTF-8 BOM; requests/json may fail on that.
        try:
            data = _json.loads(resp.content.decode("utf-8-sig"))
        except Exception as e2:
            preview = ""
            try:
                preview = resp.content[:400].decode("utf-8", errors="replace")
            except Exception:
                preview = str(resp.text[:400])
            raise RuntimeError(
                f"WP REST invalid JSON for GET {url}: {e2}. Body preview: {preview}"
            ) from e
    if not isinstance(data, list):
        return []

    out: list[dict] = []
    for it in data[:per_page]:
        if not isinstance(it, dict):
            continue
        link = str(it.get("link") or "").strip()
        title_obj = it.get("title")
        title_rendered = ""
        if isinstance(title_obj, dict):
            title_rendered = str(title_obj.get("rendered") or "")
        elif isinstance(title_obj, str):
            title_rendered = title_obj

        title_clean = _html.unescape(_strip_html_tags(title_rendered)).strip()
        if not link:
            continue
        out.append({"title": title_clean, "url": link})

    # Ensure length <= limit
    return out[: max(0, int(limit or 60))]


_INTERNAL_LINKING_PROMPT_BLOCK = (
    "\n\nINTERNAL LINKING (SIMPLE RECOMMENDATIONS):\n"
    "At the very end of 1 or 2 sections, add a clear recommendation for a related post from the list below.\n\n"
    "RULES:\n"
    "- PLACEMENT: Only as the final sentence of a section. Never in the introduction.\n"
    "- RELEVANCE: The recommended post must strictly match the topic of the section, appear completely natural at the end of the section, and be completely consistent with the context of the section.\n"
    "- FORMAT: Use a natural recommendation phrase and embed the link in the text like this: <a href=\"URL\">anchor text</a>. IMPORTANT: Please remember to add the closing </a> tag - you sometimes forget to do this.\n"
    "- URL FORMAT: Use the candidate URL exactly as provided. Keep it relative (example: /post-slug/) and do not prepend the domain.\n"
    "- QUANTITY: Maximum 2 recommendations per article. Use 0 or 1 if there are no perfect matches.\n"
    "\nCANDIDATE INTERNAL LINKS:\n{candidates}\n"
)


def _build_internal_links_block(
    recent_posts: list[dict] | None,
    *,
    include_titles: bool = False,
) -> str:
    posts = recent_posts or []
    if not posts:
        return ""

    lines: list[str] = []
    for p in posts[:60]:
        try:
            url = _normalize_internal_link_href(str((p or {}).get("url") or "").strip())
            if not url:
                continue

            if include_titles:
                ttl = str((p or {}).get("title") or "").strip()
                if not ttl:
                    ttl = url
                ttl = " ".join(ttl.split())
                lines.append(f"- {ttl} — {url}")
            else:
                # Only URL to keep prompt small
                lines.append(f"- {url}")
        except Exception:
            continue

    if not lines:
        return ""

    return _INTERNAL_LINKING_PROMPT_BLOCK.format(candidates="\n".join(lines))


def _render_inline_allow_only_anchors(s: str) -> str:
    """Escape text for HTML output but preserve *only* <a href="...">..</a> tags.

    We need this to keep internal links clickable in Gutenberg blocks, while still
    escaping any other HTML the model might output.

    Behavior:
    - Converts **bold** to <strong>...</strong>
    - Preserves <a href="...">anchor</a> (single tag), but strips any nested tags
      inside the anchor text.
    - Escapes everything else.
    """
    import html as _html
    import re as _re

    raw = _repair_unclosed_anchor_tags_in_text(str(s or ""))

    a_re = _re.compile(
        r"<a\s+[^>]*href\s*=\s*(?P<href>(\"[^\"]*\"|'[^']*'|[^\s>]+))[^>]*>(?P<inner>.*?)</a>",
        flags=_re.IGNORECASE | _re.DOTALL,
    )

    anchors: list[str] = []

    def render_plain(txt: str) -> str:
        t = _html.escape(str(txt or ""), quote=False)
        t = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        return t

    def repl(m: _re.Match) -> str:
        href_raw = _normalize_internal_link_href((m.group("href") or "").strip().strip('"').strip("'"))
        inner_raw = m.group("inner") or ""
        inner_raw = _re.sub(r"<[^>]+>", "", inner_raw)

        href_safe = _html.escape(href_raw, quote=True)
        inner_safe = render_plain(inner_raw)

        token = f"__A_{len(anchors)}__"
        anchors.append(f"<a href=\"{href_safe}\">{inner_safe}</a>")
        return token

    no_a = a_re.sub(repl, raw)
    escaped = render_plain(no_a)

    for i, a_html in enumerate(anchors):
        escaped = escaped.replace(f"__A_{i}__", a_html)

    return escaped


def _repair_unclosed_anchor_tags_in_text(text: str) -> str:
    """Best-effort repair for LLM text where <a href="..."> misses </a>.

    Gemini sometimes starts an internal link correctly, then forgets to close it.
    We keep this deliberately narrow: only unmatched <a> tags are touched.
    """
    import re as _re

    s = str(text or "")
    if "<a" not in s.lower():
        return s

    tag_re = _re.compile(r"</?a\b[^>]*>", flags=_re.IGNORECASE | _re.DOTALL)

    def _find_unmatched_open_tags(txt: str) -> list[tuple[int, int]]:
        stack: list[tuple[int, int]] = []
        for m in tag_re.finditer(txt):
            tag = m.group(0) or ""
            if tag.lower().startswith("</a"):
                if stack:
                    stack.pop()
                continue
            stack.append((m.start(), m.end()))
        return stack

    def _pick_short_nounish_anchor_pos(scan: str) -> int | None:
        """Pick a compact fallback anchor ending near a likely noun."""
        m_boundary = _re.search(r"[.!?](?:[\"')\]]+)?(?=\s|$)|\n+", scan)
        phrase_scan = scan[: m_boundary.start()] if m_boundary else scan
        tokens = list(_re.finditer(r"\b[A-Za-z][A-Za-z'-]*\b", phrase_scan))
        if len(tokens) < 2:
            return None

        tokens = tokens[:8]
        function_words = {
            "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
            "in", "into", "is", "it", "its", "of", "on", "or", "so", "that",
            "the", "their", "these", "this", "those", "to", "was", "were",
            "when", "where", "which", "while", "with", "without", "your",
        }
        verbish_words = {
            "add", "adds", "adding", "bring", "brings", "bringing", "create",
            "creates", "creating", "feel", "feels", "feeling", "finish",
            "finishes", "finishing", "get", "gets", "getting", "give", "gives",
            "giving", "keep", "keeps", "keeping", "look", "looks", "looking",
            "make", "makes", "making", "round", "rounds", "rounding", "use",
            "uses", "using",
        }
        nounish_words = {
            "aesthetic", "aesthetics", "area", "areas", "article", "articles",
            "arrangement", "arrangements", "backdrop", "backdrops",
            "bathroom", "bathrooms", "bedroom", "bedrooms", "color", "colors",
            "combination", "combinations", "corner", "corners", "craft",
            "crafts", "decor", "design", "designs", "detail", "details",
            "display", "displays", "entryway", "entryways", "find", "finds",
            "finish", "finishes", "furniture", "guide", "guides", "home",
            "homes", "idea", "ideas", "inspiration", "interior", "interiors",
            "kitchen", "kitchens", "layer", "layers", "layout", "layouts",
            "lighting", "look", "looks", "material", "materials", "minimalism",
            "mood", "moods", "office", "offices", "palette", "palettes",
            "pattern", "patterns", "piece", "pieces", "pipe", "pipes", "post",
            "posts", "room", "rooms", "season", "seasons", "setup", "setups",
            "shape", "shapes", "shelf", "shelves", "space", "spaces", "style",
            "styles", "styling", "texture", "textures", "tip", "tips",
            "tone", "tones", "trend", "trends", "vibe", "vibes", "wall",
            "walls",
        }
        nounish_suffixes = (
            "age", "ance", "ence", "ery", "hood", "ion", "ism", "ist",
            "ity", "ment", "ness", "ship", "tion", "ure",
        )

        def _is_likely_noun(idx: int, word: str) -> bool:
            w = word.lower().strip("'")
            if w.endswith("'s"):
                w = w[:-2]
            if not w or w in function_words:
                return False
            if idx > 0 and tokens[idx - 1].group(0).lower() == "to":
                return False
            if w in nounish_words:
                return True
            if w in verbish_words or w.endswith("ly"):
                return False
            if w.endswith(nounish_suffixes):
                return True
            if w.endswith("s") and not w.endswith(("ss", "ous")):
                return True
            return False

        best: _re.Match[str] | None = None
        for idx, token in enumerate(tokens):
            if idx < 1:
                continue
            if _is_likely_noun(idx, token.group(0)):
                best = token

        if not best:
            return None

        pos = best.end()
        if pos < len(scan) and scan[pos : pos + 1] in ",;:":
            pos += 1
        return pos

    def _pick_before_infinitive_pos(scan: str) -> int | None:
        """Fallback: stop before 'to + verb' tails to avoid overlong links."""
        m_inf = _re.search(r"\bto\s+[A-Za-z][A-Za-z'-]*\b", scan, flags=_re.IGNORECASE)
        if not m_inf:
            return None
        before = scan[:m_inf.start()].rstrip()
        if not before:
            return None
        before_tokens = list(_re.finditer(r"\b[A-Za-z][A-Za-z'-]*\b", before))
        if len(before_tokens) < 2:
            return None
        return len(before)

    def _pick_close_pos(txt: str, open_end: int) -> int:
        # Never let one broken link swallow a later link.
        next_open = _re.search(r"<a\b", txt[open_end:], flags=_re.IGNORECASE)
        hard_limit = open_end + next_open.start() if next_open else len(txt)
        segment = txt[open_end:hard_limit]
        if not segment:
            return open_end

        scan = segment[: min(len(segment), 500)]

        # Prefer common SEO/internal-link anchor tail words.
        stopword_re = _re.compile(
            r"\b(?:guide|guides|idea|ideas|tip|tips|article|articles|post|posts|layout|layouts|design|designs|crafts|pieces|finds)\b",
            flags=_re.IGNORECASE,
        )
        stop_matches = list(stopword_re.finditer(scan))
        if stop_matches:
            m = stop_matches[0]
            pos = m.end()
            if pos < len(scan) and scan[pos : pos + 1] in ".,;:!?":
                pos += 1
            return open_end + pos

        nounish_pos = _pick_short_nounish_anchor_pos(scan)
        if nounish_pos is not None:
            return open_end + nounish_pos

        infinitive_pos = _pick_before_infinitive_pos(scan)
        if infinitive_pos is not None:
            return open_end + infinitive_pos

        # Fallback to the current sentence.
        m_sentence = _re.search(r"[.!?](?:[\"')\]]+)?(?=\s|$)", scan)
        if m_sentence:
            return open_end + m_sentence.end()

        # Last sane boundary: paragraph/line break, otherwise end of field.
        m_break = _re.search(r"\n+", scan)
        if m_break:
            return open_end + m_break.start()

        return hard_limit

    out = s
    # A few passes are enough for normal article text and avoid surprises on
    # pathological input.
    for _ in range(20):
        unmatched = _find_unmatched_open_tags(out)
        if not unmatched:
            break
        for _start, open_end in reversed(unmatched):
            close_pos = _pick_close_pos(out, open_end)
            out = out[:close_pos] + "</a>" + out[close_pos:]

    return out


def _remove_watermark_with_photoshop_single(
    input_path: str | Path,
    *,
    size_or_scale: int = 13,
    out_format: str = "PNG",
    mode: str = "scale",
    margin_left: int = 25,
    margin_bottom: int = 25,
    force: bool = False,
    unique: bool = False,
) -> tuple[Path | None, str | None]:
    """Remove Gemini watermark using Photoshop automation for a single image.

    This reuses the same automation as Tab 3 (photoshop_crop_bottom_right.py).

    Returns (filled_path, error).
    """

    try:
        src = Path(input_path).expanduser().resolve()
        if not src.exists():
            return None, f"Файл не найден: {src}"

        # Photoshop script writes next to source and appends `_filled`.
        # For PNG output, it becomes `<stem>_filled.png`.
        ext = ".png" if out_format.upper() == "PNG" else ".jpg"
        filled_path = src.with_name(f"{src.stem}_filled{ext}")

        # Default behavior: reuse cached result if it exists.
        # In Tab1 we want to regenerate after "пересоздать", so we'll pass force/unique.
        if filled_path.exists() and not force and not unique:
            return filled_path, None

        # If forcing regeneration, remove old cached filled file so Photoshop creates a fresh one.
        if filled_path.exists() and (force or unique):
            try:
                filled_path.unlink(missing_ok=True)  # py3.8+: supported
            except TypeError:
                # Python <3.8 compatibility
                try:
                    if filled_path.exists():
                        filled_path.unlink()
                except Exception:
                    pass

        cmd = [
            sys.executable,
            "photoshop_crop_bottom_right.py",
            str(src),
            str(int(size_or_scale)),
            out_format.upper(),
            mode,
            str(int(margin_left)),
            str(int(margin_bottom)),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return None, f"Photoshop error (code {proc.returncode}): {proc.stderr or proc.stdout}"

        created_path: Path | None = None

        if filled_path.exists():
            created_path = filled_path
        else:
            # Fallback: try to find any *_filled.* created next to src
            candidates = sorted(
                [p for p in src.parent.glob(f"{src.stem}_filled.*") if p.suffix.lower() in (".png", ".jpg", ".jpeg")],
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )
            if candidates:
                created_path = candidates[0]

        if not created_path or not created_path.exists():
            return None, "Не найден файл результата Photoshop (*_filled.*)"

        # If requested, move the filled file to a unique name to avoid reusing old cached outputs.
        if unique:
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                unique_path = created_path.with_name(f"{src.stem}_filled_{ts}{created_path.suffix}")
                # In case of extremely fast repeats, ensure uniqueness
                if unique_path.exists():
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    unique_path = created_path.with_name(f"{src.stem}_filled_{ts}{created_path.suffix}")
                created_path.replace(unique_path)
                created_path = unique_path
            except Exception:
                # If rename fails, still return the created file
                pass

        return created_path, None
    except Exception as e:
        return None, str(e)

# Helper: open folder in OS file explorer
def _clear_article_ui_cache(idx: int | str) -> None:
    """Clear Streamlit widget state for a single article result.

    В Streamlit значения виджетов живут в `st.session_state` по их `key`.
    Если результат статьи (текст/preview/download) обновился, а ключи остались
    прежними, Streamlit может показать старое значение из session_state.

    Мы храним ключи в формате `..._{idx}_{rev}` (rev = версия результата).
    Для надёжности при обновлениях удаляем *все* ключи, относящиеся к данному idx.
    """

    sidx = str(idx)

    # Удаляем все варианты ключей для этого idx (включая старые rev).
    prefixes = [
        f"unif_text_out_{sidx}",
        f"unif_wp_clean_preview_{sidx}",
        f"unif_wp_dl_{sidx}",
    ]

    for k in list(st.session_state.keys()):
        try:
            if not isinstance(k, str):
                continue
            for p in prefixes:
                # match exact key or key with version suffix
                if k == p or k.startswith(p + "_"):
                    del st.session_state[k]
                    break
        except Exception:
            pass


def _bump_article_rev(idx: int) -> int:
    """Increment and return the revision number for an article idx."""

    if "unif_text_rev_by_idx" not in st.session_state or not isinstance(st.session_state.unif_text_rev_by_idx, dict):
        st.session_state.unif_text_rev_by_idx = {}

    cur = int(st.session_state.unif_text_rev_by_idx.get(str(idx), 0) or 0)
    cur += 1
    st.session_state.unif_text_rev_by_idx[str(idx)] = cur
    return cur


def _get_article_rev(idx: int) -> int:
    if "unif_text_rev_by_idx" not in st.session_state or not isinstance(st.session_state.unif_text_rev_by_idx, dict):
        return 0
    return int(st.session_state.unif_text_rev_by_idx.get(str(idx), 0) or 0)


def _autosave_tab0_article_texts(results: list[dict]) -> None:
    """Autosave Tab0 article generation results to disk.

    This is used both after bulk generation and after single-item regeneration
    ("Пересоздать #N"), so that recovery JSON always includes the latest results.

    Never raises.
    """

    try:
        import json as _json_autosave
        from pathlib import Path as _Path_autosave

        ts_autosave = datetime.now().strftime("%Y%m%d_%H%M%S")
        date_autosave = datetime.now().strftime("%Y-%m-%d")

        autosave_dir = _Path_autosave("autosaves") / "app_unified_streamlit" / "tab0_article_texts" / date_autosave
        autosave_dir.mkdir(parents=True, exist_ok=True)

        autosave_payload = {
            "ts": ts_autosave,
            "date": date_autosave,
            "source": "tab0_article_texts",
            "prompts_per_section": int(st.session_state.get("unif_text_prompts_per_section") or 2),
            "titles_raw": str(st.session_state.get("unif_text_titles_raw") or ""),
            "template": str(st.session_state.get("unif_text_template") or ""),
            "results": results,
        }

        autosave_path = autosave_dir / f"unif_text_results_{ts_autosave}.json"
        autosave_path.write_text(
            _json_autosave.dumps(autosave_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        last_path = _Path_autosave("autosaves") / "app_unified_streamlit" / "_last_tab0_article_texts_autosave.json"
        last_path.parent.mkdir(parents=True, exist_ok=True)
        last_path.write_text(
            _json_autosave.dumps({"last_autosave": str(autosave_path.resolve()), "ts": ts_autosave}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        st.session_state["unif_text_last_autosave_path"] = str(autosave_path.resolve())
        st.session_state.pop("unif_text_last_autosave_error", None)
    except Exception as _e_autosave:
        # Never break the app because of autosave
        st.session_state["unif_text_last_autosave_error"] = str(_e_autosave)


# Helper: open folder in OS file explorer
def _open_folder(path_str: str | None):
    if not path_str:
        return
    try:
        p = str(Path(path_str).expanduser())
        if os.name == 'nt':
            os.startfile(p)  # type: ignore[attr-defined]
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', p])
        else:
            subprocess.Popen(['xdg-open', p])
    except Exception:
        pass


def _unique_path_if_exists(base: str | Path) -> Path:
    """Return a unique path if `base` already exists.

    This is used to avoid accidental overwrites when multiple Streamlit instances
    run simultaneously (e.g., different localhost ports) but save into the same
    output directory.

    We keep the original name when possible and only add a suffix on collision.
    """

    p = Path(base)
    try:
        if not p.exists():
            return p

        stem = p.stem
        suffix = p.suffix
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        pid = os.getpid()
        # Try a few times in case of extremely fast repeats
        for i in range(1, 1000):
            cand = p.with_name(f"{stem}__{ts}_pid{pid}_{i}{suffix}")
            if not cand.exists():
                return cand
        # Last resort
        return p.with_name(f"{stem}__{ts}_pid{pid}{suffix}")
    except Exception:
        return p


def _write_bytes_to_new_file(base: str | Path, data: bytes) -> Path:
    """Atomically write bytes without ever overwriting another worker's file.

    Checking ``Path.exists()`` and then opening the path for writing leaves a
    cross-process race: two Streamlit ports can both see the same filename as
    free and one silently overwrites the other.  ``xb`` performs the claim and
    the write as one filesystem operation.
    """
    p = Path(base)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    pid = os.getpid()
    for attempt in range(0, 1000):
        candidate = p if attempt == 0 else p.with_name(
            f"{p.stem}__{stamp}_pid{pid}_{attempt}{p.suffix}"
        )
        try:
            with open(candidate, "xb") as f:
                f.write(data)
            return candidate
        except FileExistsError:
            continue
    raise FileExistsError(f"Could not reserve a unique output filename for {p}")


def _predict_nbp_image_basename(task_idx: int, prompt_raw: str | None, *, j: int = 1, ext: str = "png") -> str:
    """Predict NBP-Pro filename (basename only) using the same slug rules as the saver.

    Saver logic (Tab2):
      fname = f"{task_idx}_pro_{slug}_{j:02d}.{ext}"
    where slug is derived from prompt_raw via:
      base_slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", prompt_raw)
      base_slug = re.sub(r"_+", "_", base_slug).strip("_")
      if not base_slug: base_slug = f"prompt_{task_idx}"
      MAX_BASENAME = 110; allowed_slug_len = max(1, MAX_BASENAME - len(prefix) - len(suffix))
      slug = base_slug[:allowed_slug_len]

    Note: ext is best-effort (default png). If an actual saved file exists, prefer that.
    """
    import re as _re

    idx = int(task_idx)
    prompt_raw = (prompt_raw or "").strip()

    base_slug = _re.sub(r"[^a-zA-Z0-9_-]+", "_", prompt_raw)
    base_slug = _re.sub(r"_+", "_", base_slug).strip("_")
    if not base_slug:
        base_slug = f"prompt_{idx}"

    prefix = f"{idx}_pro_"
    prefix_len = len(prefix)
    suffix = f"_{int(j):02d}.{ext}"
    suffix_len = len(suffix)

    MAX_BASENAME = 110
    allowed_slug_len = max(1, MAX_BASENAME - prefix_len - suffix_len)
    slug = base_slug[:allowed_slug_len]

    return f"{idx}_pro_{slug}_{int(j):02d}.{ext}"


def _get_latest_nbp_saved_image_path(task_idx: int) -> str | None:
    """Return latest Nano Banana Pro saved image path for a given task/prompt idx.

    Tab2 filenames use: <idx>_pro_<slug>_<nn>.<ext>
    """
    try:
        if not task_idx:
            return None
        saved = list(st.session_state.get("unif_pro_saved_paths") or [])
        if not saved:
            return None

        # Prefer in-session recorded paths
        cand: list[Path] = []
        prefix1 = f"{int(task_idx)}_pro_"
        prefix2 = f"{int(task_idx):02d}_pro_"
        for p in saved:
            try:
                if _extract_prompt_idx(p) != int(task_idx):
                    continue
                pp = Path(p)
                name = pp.name
                if not (name.startswith(prefix1) or name.startswith(prefix2)):
                    continue
                if pp.exists() and pp.is_file():
                    cand.append(pp)
            except Exception:
                continue

        if not cand:
            return None

        cand.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
        return str(cand[0])
    except Exception:
        return None


def _copy_image_to_windows_clipboard_via_powershell(image_path: str) -> bool:
    """Copy image file to Windows clipboard via PowerShell (STA).

    This avoids browser/iframe Clipboard API restrictions (which often block image copy).
    Returns True on success.
    """
    try:
        if os.name != "nt":
            return False
        p = str(Path(image_path).expanduser().resolve())
        if not p or not os.path.exists(p):
            return False

        # PowerShell must run in STA for Clipboard.
        # Escape single quotes for PowerShell single-quoted string literal.
        p_ps = p.replace("'", "''")
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            f"$img=[System.Drawing.Image]::FromFile('{p_ps}');"
            "[System.Windows.Forms.Clipboard]::SetImage($img);"
            "$img.Dispose();"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True,
            text=True,
        )
        return True
    except Exception:
        return False


def _open_chrome_window_with_profile(*, executable_path: str | None, user_data_dir: str | None, url: str | None = None) -> None:
    """Open a normal Chrome window using the given user-data-dir.

    This is meant for manual fallback (when automation is flaky).
    """
    try:
        exe = (executable_path or "").strip() or None
        udir = _normalize_user_data_dir(user_data_dir) if user_data_dir else None
        if not exe or not udir:
            return
        try:
            os.makedirs(udir, exist_ok=True)
        except Exception:
            pass

        cmd = [
            exe,
            f"--user-data-dir={udir}",
            "--lang=ru-RU",
            "--new-window",
        ]
        if url:
            cmd.append(str(url))

        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _fix_common_profile_path_typos(path_str: str) -> str:
    """Fix common copy/paste typos for Chrome profile paths.

    One frequent issue: missing path separator before `.chrome_automation_profile*`,
    resulting in paths like `...\\generate automation.chrome_automation_profile_4`.

    We only apply a conservative fix when `.chrome_automation_profile` occurs in the
    string and the preceding character is not a path separator.
    """
    try:
        s = (path_str or "").strip().strip('"').strip("'")

        # If the provided directory already exists, DO NOT "fix" it.
        # Users may intentionally have a folder name like `generate automation.chrome_automation_profile_4`.
        try:
            if s and Path(os.path.expanduser(s)).exists():
                return s
        except Exception:
            pass

        # Otherwise, try to fix a common typo: missing separator before (.)chrome_automation_profile
        keys = [".chrome_automation_profile", "chrome_automation_profile"]
        for key in keys:
            i = s.find(key)
            if i > 0:
                prev = s[i - 1]
                if prev not in {"/", "\\", os.sep}:
                    sep = "\\" if os.name == "nt" else os.sep
                    candidate = s[:i] + sep + s[i:]
                    # Only apply if the fixed path exists OR the original doesn't exist
                    try:
                        if Path(os.path.expanduser(candidate)).exists() or not Path(os.path.expanduser(s)).exists():
                            s = candidate
                            break
                    except Exception:
                        s = candidate
                        break
        return s
    except Exception:
        return path_str


def _normalize_user_data_dir(path_str: str | None) -> str | None:
    if not path_str:
        return None
    s = _fix_common_profile_path_typos(path_str)
    try:
        return str(Path(os.path.expanduser(s)).resolve())
    except Exception:
        try:
            return os.path.abspath(os.path.expanduser(s))
        except Exception:
            return s


def _get_profile_downloads_dir(user_data_dir: str | None) -> str | None:
    """Best-effort resolve Chrome profile Downloads directory.

    For manual mode: when you download an image by hand in the opened profile,
    we need to know where the file lands.

    Strategy:
    1) If profile Preferences contain download.default_directory -> use it.
    2) Fallback to ~/Downloads.
    """
    try:
        # 1) Try to read Chrome Preferences (Default profile)
        udir = _normalize_user_data_dir(user_data_dir) if user_data_dir else None
        if udir:
            pref = Path(udir) / "Default" / "Preferences"
            if pref.exists():
                try:
                    obj = _json.loads(pref.read_text(encoding="utf-8"))
                    d = (
                        (obj.get("download") or {}).get("default_directory")
                        or (obj.get("savefile") or {}).get("default_directory")
                    )
                    if d:
                        return str(Path(d).expanduser())
                except Exception:
                    pass

        # 2) Fallback
        return str(Path.home() / "Downloads")
    except Exception:
        return None


def _ensure_chrome_profile_download_dir(user_data_dir: str, download_dir: str) -> None:
    """Best-effort set Chrome download.default_directory for the given user-data-dir.

    This is mainly to make manual (non-automated) downloads deterministic per profile/card.
    """

    try:
        udir = _normalize_user_data_dir(user_data_dir) or user_data_dir
        pref_path = Path(udir) / "Default" / "Preferences"
        pref_path.parent.mkdir(parents=True, exist_ok=True)

        obj = {}
        if pref_path.exists():
            try:
                obj = _json.loads(pref_path.read_text(encoding="utf-8")) or {}
            except Exception:
                obj = {}

        dl_abs = str(Path(download_dir).expanduser().resolve())
        download_obj = dict((obj.get("download") or {}))
        download_obj["default_directory"] = dl_abs
        download_obj.setdefault("prompt_for_download", False)
        download_obj.setdefault("directory_upgrade", True)
        obj["download"] = download_obj

        pref_path.write_text(_json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # Never fail the app because of profile preference writes.
        return


def _featured_native_download_watch_dirs(profile_dir: str | None) -> list[str]:
    """Return Chrome's possible native-download folders for one featured worker."""

    candidates: list[str] = []
    try:
        root = Path(profile_dir).resolve() if profile_dir else None
        if root:
            for pref_path in (root / "Default" / "Preferences", root / "Preferences"):
                if not pref_path.is_file():
                    continue
                try:
                    import json as _json_local

                    prefs = _json_local.loads(pref_path.read_text(encoding="utf-8", errors="ignore"))
                    for section, key in (("download", "default_directory"), ("savefile", "default_directory")):
                        value = str((prefs.get(section) or {}).get(key) or "").strip()
                        if value:
                            candidates.append(value)
                except Exception:
                    pass
                break
    except Exception:
        pass

    try:
        fallback = Path(os.environ.get("USERPROFILE") or Path.home()) / "Downloads" if os.name == "nt" else Path.home() / "Downloads"
        candidates.append(str(fallback))
    except Exception:
        pass

    unique: list[str] = []
    for candidate in candidates:
        try:
            resolved = str(Path(candidate).expanduser().resolve())
            if Path(resolved).is_dir() and resolved not in unique:
                unique.append(resolved)
        except Exception:
            continue
    return unique


def _set_featured_profile_download_directory_temporarily(
    profile_dir: str | None, download_dir: str
) -> tuple[list[tuple[Path, bytes]], list[str]]:
    """Temporarily route one featured worker's profile to its private folder."""

    backups: list[tuple[Path, bytes]] = []
    notes: list[str] = []
    if not profile_dir:
        return backups, ["profile preference: skipped (no profile directory)"]

    try:
        import json as _json_local

        root = Path(profile_dir).resolve()
        target = str(Path(download_dir).resolve())
    except Exception as e:
        return backups, [f"profile preference: path resolution failed: {e}"]

    for pref_path in (root / "Default" / "Preferences", root / "Preferences"):
        if not pref_path.is_file():
            continue
        try:
            original = pref_path.read_bytes()
            prefs = _json_local.loads(original.decode("utf-8"))
            download = prefs.get("download")
            if not isinstance(download, dict):
                download = {}
                prefs["download"] = download
            download["default_directory"] = target
            download["prompt_for_download"] = False
            download["directory_upgrade"] = True

            tmp_path = pref_path.with_name(f"{pref_path.name}.featured-download-tmp")
            tmp_path.write_text(
                _json_local.dumps(prefs, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(str(tmp_path), str(pref_path))
            backups.append((pref_path, original))
            notes.append(f"profile preference pinned: {pref_path}")
        except Exception as e:
            notes.append(f"profile preference failed: {pref_path}: {e}")

    if not backups:
        notes.append("profile preference: no editable Preferences file found")
    return backups, notes


def _restore_featured_profile_download_directory(backups: list[tuple[Path, bytes]]) -> list[str]:
    """Restore Chrome preferences after a featured worker closes."""

    notes: list[str] = []
    for pref_path, original in backups or []:
        try:
            tmp_path = pref_path.with_name(f"{pref_path.name}.featured-restore-tmp")
            tmp_path.write_bytes(original)
            os.replace(str(tmp_path), str(pref_path))
            notes.append(f"profile preference restored: {pref_path}")
        except Exception as e:
            notes.append(f"profile preference restore failed: {pref_path}: {e}")
    return notes


def _pin_featured_chrome_download_directory(page, download_dir: str) -> str:
    """Set Chrome's live native-download directory through CDP."""

    try:
        target = str(Path(download_dir).resolve())
        session = page.context.new_cdp_session(page)
    except Exception as e:
        return f"CDP download path unavailable: {e}"

    try:
        for method in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
            try:
                session.send(
                    method,
                    {
                        "behavior": "allow",
                        "downloadPath": target,
                        "eventsEnabled": True,
                    },
                )
                return f"native Chrome path pinned via {method}: {target}"
            except Exception:
                continue
        return "CDP download path was rejected by this Chrome build"
    finally:
        try:
            session.detach()
        except Exception:
            pass



def _chrome_launch_args(extra: list[str] | None = None) -> list[str]:
    """Common Chrome args for Playwright persistent contexts.

    Adds a few stability flags for Windows GPU/Window-handle flakiness.
    """
    base = [
        "--lang=ru-RU",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-gpu-compositing",
    ]
    if extra:
        base.extend(extra)
    return base


def _cleanup_profile_locks(user_data_dir: str | None) -> None:
    """Best-effort cleanup of Chromium profile lock artefacts.

    When Chrome/Chromium is killed or crashes, it can leave lock files in the
    profile directory. The next startup may then immediately exit ("window
    flashes and closes") until the lock is cleared.

    This function removes only well-known *ephemeral* lock files and does not
    touch user data like Cookies/Preferences.
    """

    if not user_data_dir:
        return

    try:
        p = Path(str(user_data_dir))
    except Exception:
        return

    if not p.exists() or not p.is_dir():
        return

    candidates = [
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
        "LOCK",
        "lockfile",
        "DevToolsActivePort",
    ]

    for name in candidates:
        fp = p / name
        try:
            if fp.exists():
                fp.unlink()
        except Exception:
            pass


def _launch_persistent_ctx_with_retries(
    pw,
    *,
    user_data_dir: str | None,
    headless: bool,
    executable_path: str | None,
    downloads_path: str | None = None,
    extra_args: list[str] | None = None,
    attempts: int = 6,
):
    """Launch persistent context with retries for intermittent Windows flakiness.

    - Normalizes user-data-dir (fixes missing path separator before chrome profiles).
    - Creates the dir if needed.
    - Retries several times to mitigate `Browser.getWindowForTarget`.
    """

    norm_udir = _normalize_user_data_dir(user_data_dir) if user_data_dir else None
    norm_downloads = os.path.abspath(downloads_path) if downloads_path else None
    if norm_udir:
        try:
            os.makedirs(norm_udir, exist_ok=True)
        except Exception:
            pass

    last_err: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            # Important: clean stale locks before every attempt. On Windows this
            # can cause a "blink/close" startup until the lock disappears.
            _cleanup_profile_locks(norm_udir)

            # Serialize the launch phase (Windows flakiness when launching from threads)
            with _LAUNCH_LOCK:
                ctx = pw.chromium.launch_persistent_context(
                    user_data_dir=norm_udir,
                    headless=headless,
                    downloads_path=norm_downloads,
                    channel="chrome",
                    executable_path=executable_path or None,
                    # Reduce obvious automation fingerprints (can affect Gemini / AI Studio stability).
                    ignore_default_args=["--enable-automation"],
                    args=_chrome_launch_args((extra_args or []) + ["--disable-blink-features=AutomationControlled"]),
                )

            try:
                ctx.add_init_script(
                    """
                    // Hide Playwright/WebDriver automation flag.
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    """
                )
            except Exception:
                pass

            return ctx
        except Exception as e:
            last_err = e
            # exponential-ish backoff
            time.sleep(0.6 + (0.7 * attempt))
    raise last_err or RuntimeError("Failed to launch persistent context")


def _clone_profile_dir(src_dir: str, dst_dir: str) -> None:
    """Clone a Chrome user-data-dir to a temp directory.

    We skip caches and lock files. This is used to avoid profile locks when running
    multiple windows/contexts concurrently or when keep-open mode needs unique profiles.
    """

    def _ignore_profile(_dirpath: str, names: list[str]):
        skip_exact = {
            "Cache",
            "Code Cache",
            "GPUCache",
            "GrShaderCache",
            "ShaderCache",
            "DawnGraphiteCache",
            "DawnWebGPUCache",
            "GraphiteDawnCache",
            "Crashpad",
            "Crash Reports",
        }
        skip_prefix = ("Singleton",)
        ignored: list[str] = []
        for n in names:
            if n in skip_exact or any(n.startswith(p) for p in skip_prefix):
                ignored.append(n)
                continue
            # Common lock/port files
            if n.upper() == "LOCK" or n.lower() in {"lockfile", "devtoolsactiveport"}:
                ignored.append(n)
                continue
            # NOTE: do NOT exclude Service Worker.
            # Gemini may store auth/session/fetch handlers there; excluding it can lead to
            # "infinite generating" behavior in automated sessions while manual profiles work.
        return ignored

    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=False, ignore=_ignore_profile)


# Local modules
import convert_to_webp  # we will call convert_to_webp.run_streamlit_app() inside a tab
_PLAYWRIGHT_AVAILABLE = True
_PLAYWRIGHT_IMPORT_ERROR = ""
_PLAYWRIGHT_MISSING_MESSAGE = (
    "Playwright is not installed. Install it with `pip install playwright` "
    "and then run `playwright install chromium`."
)


def _raise_missing_playwright(*args, **kwargs):
    raise RuntimeError(_PLAYWRIGHT_MISSING_MESSAGE)


class _PlaywrightMissingHelpers:
    def _log(self, *args, **kwargs):
        return None

    def _pick_model(self, *args, **kwargs):
        raise RuntimeError(_PLAYWRIGHT_MISSING_MESSAGE)


try:
    import gemini_pw_helpers as gph

    # Use the original helpers from gemini_playwright_streamlit for full parity
    from gemini_playwright_streamlit import (
        BASE_IMAGE_PATH,
        _wait_input_ready,
        _start_new_chat,
        _attach_image,
        _wait_image_attached,
        _dismiss_overlays,
        _type_prompt,
        _click_send as _shared_click_send,
        _wait_and_download_generated_images,
        _debug_dom,
        _count_responses,
        _has_generated_images,
        _regenerate_prompt,
    )
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError as e:
    if getattr(e, "name", "") != "playwright":
        raise
    _PLAYWRIGHT_AVAILABLE = False
    _PLAYWRIGHT_IMPORT_ERROR = str(e)
    gph = _PlaywrightMissingHelpers()
    BASE_IMAGE_PATH = "10x16.jpg"
    _wait_input_ready = _raise_missing_playwright
    _start_new_chat = _raise_missing_playwright
    _attach_image = _raise_missing_playwright
    _wait_image_attached = _raise_missing_playwright
    _dismiss_overlays = _raise_missing_playwright
    _type_prompt = _raise_missing_playwright
    _shared_click_send = _raise_missing_playwright
    _click_send = _raise_missing_playwright
    _wait_and_download_generated_images = _raise_missing_playwright
    _debug_dom = _raise_missing_playwright
    _count_responses = _raise_missing_playwright
    _has_generated_images = _raise_missing_playwright
    _regenerate_prompt = _raise_missing_playwright
    sync_playwright = _raise_missing_playwright

from concurrent.futures import ThreadPoolExecutor
import threading

if not _PLAYWRIGHT_AVAILABLE:
    st.warning(
        _PLAYWRIGHT_MISSING_MESSAGE
        + (f" Import error: {_PLAYWRIGHT_IMPORT_ERROR}" if _PLAYWRIGHT_IMPORT_ERROR else "")
    )

# --- Concurrency safety ---
# IMPORTANT: Playwright persistent contexts share the underlying Chrome profile (user-data-dir).
# If two threads accidentally use the same profile simultaneously, they can end up typing/sending
# into the same Gemini window while it is already generating, which triggers Gemini UI errors.
# We therefore enforce a per-profile lock.
_PROFILE_LOCKS: dict[str, threading.Lock] = {}
_PROFILE_LOCKS_GUARD = threading.Lock()

# Launching Chrome (persistent context) from multiple threads can be flaky on Windows.
# We serialize ONLY the launch phase to avoid "blink and close" behaviour.
_LAUNCH_LOCK = threading.Lock()

# The Gemini "Copy response" button writes to the clipboard.
# Since the OS/browser clipboard is global, we serialize only that tiny section.
_CLIPBOARD_READ_LOCK = threading.Lock()


def _get_profile_lock(profile_dir: str | None) -> threading.Lock:
    key = str(profile_dir or "").strip()
    with _PROFILE_LOCKS_GUARD:
        lk = _PROFILE_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _PROFILE_LOCKS[key] = lk
        return lk


def _find_gemini_editor_for_send(page):
    selectors = [
        "textarea[formcontrolname='promptText']",
        "textarea[aria-label='Enter a prompt']",
        ".prompt-box-container textarea",
        "div.ql-editor.textarea.new-input-ui[contenteditable='true']",
        "div.ql-editor[contenteditable='true']",
        "[contenteditable='true'][role='textbox']",
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
        except Exception:
            el = None
        if el:
            return el
    return None


def _element_text_len(page, el) -> int:
    if not el:
        return -1
    try:
        return int(
            page.evaluate(
                """(el) => {
                  try {
                    const t = (el.value !== undefined ? el.value : (el.innerText || el.textContent || ''));
                    return (t || '').trim().length;
                  } catch(e) { return -1; }
                }""",
                el,
            )
        )
    except Exception:
        return -1


def _button_label_for_send_guard(page, el) -> str:
    if not el:
        return ""
    try:
        return str(
            page.evaluate(
                """(el) => {
                  try {
                    const btn = el.closest ? (el.closest('button') || el) : el;
                    const parts = [];
                    for (const a of ['aria-label', 'title', 'data-test-id', 'class']) {
                      try { parts.push(btn.getAttribute(a) || ''); } catch(e) {}
                    }
                    try { parts.push(btn.innerText || btn.textContent || ''); } catch(e) {}
                    try {
                      for (const ic of btn.querySelectorAll('mat-icon, .mat-icon, [fonticon]')) {
                        parts.push(ic.getAttribute('fonticon') || '');
                        parts.push(ic.innerText || ic.textContent || '');
                      }
                    } catch(e) {}
                    return parts.join(' ');
                  } catch(e) { return ''; }
                }""",
                el,
            )
            or ""
        ).lower()
    except Exception:
        return ""


def _is_stop_like_send_button(page, el) -> bool:
    label = _button_label_for_send_guard(page, el)
    if not label:
        return False
    stop_markers = [
        "stop",
        " stop",
        "stop ",
        "stop-generating",
        "stop generating",
        "stop response",
        "\u043e\u0441\u0442\u0430\u043d\u043e\u0432",
        "\u043f\u0440\u0435\u0440\u0432",
    ]
    return any(m in label for m in stop_markers)


def _gemini_stop_button_visible(page) -> bool:
    selectors = [
        "button.send-button.stop",
        "button[aria-label*='Stop' i]",
        "button[aria-label*='\u041e\u0441\u0442\u0430\u043d\u043e\u0432' i]",
        "button[data-test-id*='stop' i]",
        "[data-test-id*='stop-generating' i]",
        "button:has(.mat-icon[fonticon='stop'])",
        "button:has(mat-icon:has-text('stop'))",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc and loc.count() > 0 and loc.is_visible():
                return True
        except Exception:
            continue
    return False


def _button_near_editor(page, btn, editor) -> bool:
    if not btn or not editor:
        return True
    try:
        return bool(
            page.evaluate(
                """([btn, editor]) => {
                  try {
                    const br = btn.getBoundingClientRect();
                    const er = editor.getBoundingClientRect();
                    if (!br || !er || !br.width || !br.height || !er.width || !er.height) return true;
                    const bx = br.left + br.width / 2;
                    const by = br.top + br.height / 2;
                    const ex = er.left + er.width / 2;
                    const ey = er.top + er.height / 2;
                    return Math.abs(by - ey) <= 260 && Math.abs(bx - ex) <= 900;
                  } catch(e) { return true; }
                }""",
                [btn, editor],
            )
        )
    except Exception:
        return True


def _find_safe_send_button(page, editor=None):
    selectors = [
        "ms-run-button button[type='submit']",
        "button:has-text('Run')",
        "button[aria-label='Send message']",
        "button[aria-label='\u041e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435']",
        "button:has(.mat-icon[fonticon='send'])",
        "button .mat-icon[fonticon='send']",
        ".send-button:not(.stop)",
        "button[type='submit']:not(.stop)",
        "[data-test-id*='send' i]:not([data-test-id*='stop' i])",
    ]
    for sel in selectors:
        try:
            matches = page.query_selector_all(sel)
        except Exception:
            matches = []
        for el in matches:
            try:
                try:
                    tag = (el.evaluate("e => e.tagName") or "").lower()
                except Exception:
                    tag = ""
                if tag != "button":
                    try:
                        btn = el.evaluate_handle("e => e.closest('button')")
                        if btn:
                            el = btn
                    except Exception:
                        pass

                if _is_stop_like_send_button(page, el):
                    continue
                try:
                    if el.get_attribute("aria-disabled") == "true":
                        continue
                except Exception:
                    pass
                try:
                    if not el.is_visible():
                        continue
                except Exception:
                    pass
                if not _button_near_editor(page, el, editor):
                    continue
                return el
            except Exception:
                continue
    return None


def _click_send_no_stop(page, *, timeout_ms: int = 6500) -> bool:
    """Local app_unified send helper.

    The shared helper retries very quickly. In Gemini classic UI the Send button
    can become the Stop button before that retry observes a success signal; a
    second click then cancels the response. This local variant clicks only a
    send-like button, waits longer for the start signal, and never clicks a
    stop-like button. Kept local so the working pipeline helper is untouched.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return bool(_shared_click_send(page, timeout_ms=timeout_ms))

    deadline = time.time() + (max(1000, int(timeout_ms)) / 1000.0)
    editor = _find_gemini_editor_for_send(page)
    before_len = _element_text_len(page, editor)
    try:
        before_count = int(_count_responses(page))
    except Exception:
        before_count = 0

    def _started() -> bool:
        if _gemini_stop_button_visible(page):
            return True
        try:
            if int(_count_responses(page)) > before_count:
                return True
        except Exception:
            pass
        try:
            cur_editor = editor or _find_gemini_editor_for_send(page)
            if before_len > 0 and _element_text_len(page, cur_editor) == 0:
                return True
        except Exception:
            pass
        return False

    clicked_once = False
    enter_used = False

    while time.time() < deadline:
        try:
            _dismiss_overlays(page)
        except Exception:
            pass

        if _started():
            return True

        editor = editor or _find_gemini_editor_for_send(page)

        if clicked_once:
            # Give Gemini room to flip Send -> Stop / clear the editor before
            # returning. We deliberately do not click again after any successful
            # send click; that second click is what can cancel generation.
            try:
                page.wait_for_timeout(350)
            except Exception:
                time.sleep(0.35)
            if _started():
                return True
            continue

        btn = _find_safe_send_button(page, editor)
        if btn is not None:
            try:
                btn.click(timeout=900)
                clicked_once = True
            except Exception:
                try:
                    btn.click(force=True, timeout=900)
                    clicked_once = True
                except Exception:
                    try:
                        page.evaluate("el => el.click()", btn)
                        clicked_once = True
                    except Exception:
                        clicked_once = False

            if clicked_once:
                try:
                    page.wait_for_timeout(900)
                except Exception:
                    time.sleep(0.9)
                if _started():
                    return True
                continue

        # Fallback: Enter only when the editor is focused and no Stop is visible.
        if not enter_used and not _gemini_stop_button_visible(page) and editor is not None:
            try:
                page.evaluate("el => el.focus()", editor)
            except Exception:
                pass
            try:
                is_active = bool(page.evaluate("el => document.activeElement === el", editor))
            except Exception:
                is_active = True
            if is_active and _element_text_len(page, editor) > 0:
                try:
                    page.keyboard.press("Enter")
                    enter_used = True
                    clicked_once = True
                    page.wait_for_timeout(900)
                    if _started():
                        return True
                except Exception:
                    pass

        if before_len == 0:
            return False

        try:
            page.wait_for_timeout(180)
        except Exception:
            time.sleep(0.18)

    # If we did click and Gemini is slow to expose DOM signals, let the caller's
    # normal wait phase decide. The critical part is that we did not click Stop.
    return bool(clicked_once or enter_used)


_click_send = _shared_click_send


def _is_gemini_stopped_response_text(text: str | None) -> bool:
    low = str(text or "").lower()
    markers = [
        "\u0432\u044b \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u043b\u0438 \u0433\u0435\u043d\u0435\u0440\u0430\u0446\u0438\u044e \u043e\u0442\u0432\u0435\u0442\u0430",
        "you stopped this response",
        "you stopped generating",
        "response was stopped",
    ]
    return any(m in low for m in markers)


def _gemini_stopped_response_visible(page) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=1500)
    except Exception:
        body = ""
    return _is_gemini_stopped_response_text(body)

# Default URLs (same as original UI)
DEFAULT_URLS = ["https://gemini.google.com/app", "https://aistudio.google.com/app"]
import platform, time, urllib.request

# ---------------- Clipboard helpers (manual mode convenience) ----------------
# Streamlit doesn't provide a native clipboard API.
# We render a small HTML/JS button (inside a Streamlit component iframe) that copies
# either text or an image to the user's clipboard.
import json as _json

# Keep Nano Banana Pro prompt wrapper consistent across automation + manual copy
NBP_PROMPT_PRE = "Generate one image in a 10:16 vertical aspect ratio using this prompt: "
NBP_PROMPT_POST = " Fill the entire frame; do not leave blank white space."


def _clipboard_copy_text_button(*, label: str, text: str, key: str, help_text: str | None = None) -> None:
    """Render a compact button that copies `text` to clipboard.

    Implemented via a small HTML/JS component because Streamlit has no native clipboard API.
    To keep the UI compact (no wrapping / no extra labels), we:
    - force `white-space:nowrap`
    - avoid an extra <span> next to the button
    - show feedback by temporarily changing the button text to "Copied"
    """
    payload = _json.dumps(text or "")
    # Use a unique DOM id to avoid collisions across repeated components.
    dom_id = f"clip_txt_{key}".replace(" ", "_")
    safe_label = (label or "Copy").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""
    <div style=\"display:flex;align-items:center;flex-wrap:nowrap;overflow:visible\">
      <button id=\"{dom_id}\" style=\"display:inline-flex;align-items:center;justify-content:center;height:32px;padding:0 12px;margin:0;border:1px solid #999;border-radius:6px;background:#f8f8f8;cursor:pointer;white-space:nowrap;line-height:1;font-size:13px\">{safe_label}</button>
    </div>
    <script>
      const text = {payload};
      const btn = document.getElementById({ _json.dumps(dom_id) });
      const orig = btn ? btn.textContent : '';
      btn?.addEventListener('click', async () => {{
        try {{
          await navigator.clipboard.writeText(text);
          if (btn) btn.textContent = 'Copied';
          setTimeout(() => {{ if (btn) btn.textContent = orig; }}, 900);
        }} catch (e) {{
          if (btn) btn.textContent = 'Failed';
          setTimeout(() => {{ if (btn) btn.textContent = orig; }}, 900);
        }}
      }});
    </script>
    """
    # NOTE: older Streamlit versions don't support `key=` for components.html
    components.html(html, height=52)


def _clipboard_copy_image_button(*, label: str, image_bytes: bytes, mime: str, key: str) -> None:
    """Render a compact button that copies an image (bytes) to clipboard as a Blob."""
    import base64 as _b64

    b64 = _b64.b64encode(image_bytes or b"").decode("ascii")
    payload = _json.dumps(b64)
    mime_js = _json.dumps(mime or "application/octet-stream")
    dom_id = f"clip_img_{key}".replace(" ", "_")
    safe_label = (label or "Copy").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""
    <div style=\"display:flex;align-items:center;flex-wrap:nowrap;overflow:visible\"> 
      <button id=\"{dom_id}\" style=\"display:inline-flex;align-items:center;justify-content:center;height:32px;padding:0 12px;margin:0;border:1px solid #999;border-radius:6px;background:#f8f8f8;cursor:pointer;white-space:nowrap;line-height:1;font-size:13px\">{safe_label}</button>
    </div>
    <script>
      const b64 = {payload};
      const mime = {mime_js};
      const btn = document.getElementById({ _json.dumps(dom_id) });
      const orig = btn ? btn.textContent : '';
      function b64ToUint8(b64str) {{
        const bin = atob(b64str);
        const len = bin.length;
        const bytes = new Uint8Array(len);
        for (let i=0;i<len;i++) bytes[i] = bin.charCodeAt(i);
        return bytes;
      }}
      btn?.addEventListener('click', async () => {{
        try {{
          const bytes = b64ToUint8(b64);
          const blob = new Blob([bytes], {{type: mime}});
          await navigator.clipboard.write([new ClipboardItem({{[mime]: blob}})]);
          if (btn) btn.textContent = 'Copied';
          setTimeout(() => {{ if (btn) btn.textContent = orig; }}, 900);
        }} catch (e) {{
          if (btn) btn.textContent = 'Failed';
          setTimeout(() => {{ if (btn) btn.textContent = orig; }}, 900);
        }}
      }});
    </script>
    """
    # NOTE: older Streamlit versions don't support `key=` for components.html
    components.html(html, height=52)

# Custom Streamlit component: paste image via Ctrl+V
_paste_image = components.declare_component(
    "paste_image",
    path=str(Path("paste_image_component").resolve()),
)

# Windows asyncio policy tweak (as in original app)
import asyncio
if platform.system() == "Windows":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# Minimal local helpers to avoid importing the full Streamlit app module
from typing import Tuple

def _has_generated_images_simple(page) -> bool:
    try:
        last_loc = page.locator(
            ".presented-response-container, .response-container-content, structured-content-container, .response-container, div[class*='response']"
        ).last
        if not last_loc or last_loc.count() == 0:
            return False
        for sel in [
            '.attachment-container.generated-images',
            'single-image',
            'img.image.animate.loaded',
            'img.image',
            'canvas',
        ]:
            try:
                if last_loc.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return False


def _filter_imgs_min_bytes(imgs: List[Tuple[str, bytes]] | None, *, min_bytes: int = 25000) -> List[Tuple[str, bytes]]:
    """Drop obvious thumbnails/garbage (too small).

    We keep this in app_unified_streamlit.py to avoid changing core Playwright helpers.
    """
    out: List[Tuple[str, bytes]] = []
    for mime, blob in (imgs or []):
        try:
            if blob and len(blob) >= int(min_bytes):
                out.append((mime, blob))
        except Exception:
            continue
    return out


def _wait_and_collect_images_simple(page, timeout_s: int = 90, max_images: int = 6) -> List[Tuple[str, bytes]]:
    """Simple fallback collector: waits for Gemini image containers, then screenshots visible images.
    Returns list of (mime, bytes) tuples.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _has_generated_images_simple(page):
            break
        time.sleep(0.4)
    # Screenshot strategy
    try:
        containers = []
        for sel in [
            ".presented-response-container",
            ".response-container-content",
            "structured-content-container",
            ".attachment-container.generated-images",
            "single-image",
            ".response-container",
            "div[class*='response']",
        ]:
            containers.extend(page.query_selector_all(sel))
        if not containers:
            return []
        last = containers[-1]
        probes = [
            "single-image img.image.animate.loaded",
            "single-image img.image",
            ".attachment-container.generated-images img.image.loaded",
            ".attachment-container.generated-images img",
        ]
        results: List[Tuple[str, bytes]] = []
        seen = set()
        for psel in probes:
            if len(results) >= max_images:
                break
            try:
                ims = last.query_selector_all(psel)
            except Exception:
                ims = []
            for im in ims:
                if len(results) >= max_images:
                    break
                try:
                    src = im.get_attribute("src") or ""
                except Exception:
                    src = ""
                if src and src in seen:
                    continue
                if src:
                    seen.add(src)
                try:
                    if src.startswith("data:image/"):
                        header, b64 = src.split(",", 1)
                        mime = header.split(":", 1)[1].split(";")[0]
                        import base64 as _b64
                        data = _b64.b64decode(b64)
                        results.append((mime, data))
                        continue
                except Exception:
                    pass
                try:
                    data = im.screenshot(type="png")
                    if data:
                        results.append(("image/png", data))
                except Exception:
                    pass
        return results
    except Exception:
        return []


# ---------------- Text-generation helpers (Gemini UI) ----------------


def _try_parse_article_json(
    text: str,
    theme: str | None = None,
    target_domain: str | None = None,
) -> dict | None:
    """Parse article JSON from Gemini output.

    Supports raw JSON or fenced blocks like ```json ... ```.
    Returns dict or None.

    Note about newlines:
    - Proper JSON uses real newlines (or \n escape) and json.loads will decode them.
    - Sometimes the model double-escapes and returns literal backslash-n sequences ("\\n").
      We normalize those into real newlines for downstream Gutenberg/HTML rendering.
    """

    def _repair_unescaped_quotes_in_json(s: str) -> str:
        """Best-effort repair for common model JSON mistakes.

        Main target: unescaped double quotes inside JSON string values, e.g.:
          "title": "7 Ways to Master "Quiet Luxury" with ..."

        We keep this conservative: only when we are *inside* a JSON string and
        encounter a non-escaped `"` that does *not* look like the end of a JSON
        string token, we convert it to `\"`.

        This is heuristic, but works well for typical LLM outputs.
        """

        def _next_non_ws(text: str, start: int) -> str | None:
            for j in range(start, len(text)):
                c = text[j]
                if not c.isspace():
                    return c
            return None

        out: list[str] = []
        in_str = False
        esc = False

        for i, ch in enumerate(s or ""):
            if not in_str:
                out.append(ch)
                if ch == '"':
                    in_str = True
                    esc = False
                continue

            # in_str
            if esc:
                out.append(ch)
                esc = False
                continue

            if ch == "\\":
                out.append(ch)
                esc = True
                continue

            if ch == '"':
                nxt = _next_non_ws(s, i + 1)
                # If it looks like a normal string terminator (key or value), keep it.
                if nxt in {":", ",", "}", "]"}:
                    out.append(ch)
                    in_str = False
                    continue

                # Otherwise treat as stray quote inside string value.
                out.append('\\"')
                continue

            out.append(ch)

        return "".join(out)

    def _repair_invalid_escapes_in_json_strings(s: str) -> str:
        """Remove invalid JSON escapes inside string literals.

        LLMs sometimes output markdown-escaped characters inside JSON strings and even in JSON keys,
        e.g. "amazon\\_search\\_phrases", "backyard that 1950s flair\\!", or "\\<a ...\\>".
        In JSON, escapes like \\_ or \\! are invalid and break json.loads.

        Strategy: while inside a JSON string, if we see a backslash followed by a character
        that is NOT a valid JSON escape (" \\ / b f n r t u), we drop the backslash.
        """
        out: list[str] = []
        in_str = False
        esc = False

        for ch in (s or ""):
            if not in_str:
                out.append(ch)
                if ch == '"':
                    in_str = True
                    esc = False
                continue

            # in string
            if esc:
                if ch in {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}:
                    out.append("\\" + ch)
                else:
                    out.append(ch)  # drop the backslash
                esc = False
                continue

            if ch == "\\":
                esc = True
                continue

            out.append(ch)
            if ch == '"':
                # string ended
                in_str = False
                esc = False

        # Trailing backslash inside string: drop it
        return "".join(out)

    def _loads_with_repair(candidate: str):
        import json as _json

        try:
            return _json.loads(candidate)
        except _json.JSONDecodeError:
            # 1) fix invalid escapes like \_ or \! inside JSON strings
            fixed = _repair_invalid_escapes_in_json_strings(candidate)
            # 2) fix unescaped quotes inside JSON strings (e.g. href="...")
            fixed = _repair_unescaped_quotes_in_json(fixed)
            return _json.loads(fixed)

    def _normalize_llm_markdown_escapes(s: str) -> str:
        """Normalize common markdown escapes that frequently appear in LLM JSON output."""
        import re as _re

        txt = str(s or "")

        # Convert literal backslash escapes into real newlines (double-escaped newlines)
        txt = txt.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")

        # Unescape common markdown escapes that should be plain characters
        txt = (
            txt.replace("\\_", "_")
            .replace("\\!", "!")
            .replace("\\<", "<")
            .replace("\\>", ">")
            .replace("\\[", "[")
            .replace("\\]", "]")
            .replace("\\(", "(")
            .replace("\\)", ")")
        )

        # Normalize a common malformed link format produced by some models:
        # <a href="[URL](URL)">text</a>  ->  <a href="URL">text</a>
        txt = _re.sub(
            r"href=\"\[(https?://[^\]\s]+)\]\((https?://[^\)\s]+)\)\"",
            r"href=\"\2\"",
            txt,
            flags=_re.IGNORECASE,
        )
        txt = _re.sub(
            r"href='\[(https?://[^\]\s]+)\]\((https?://[^\)\s]+)\)'",
            r"href='\2'",
            txt,
            flags=_re.IGNORECASE,
        )

        return txt

    def _deep_normalize_newlines(obj):
        if isinstance(obj, str):
            return _repair_unclosed_anchor_tags_in_text(_normalize_llm_markdown_escapes(obj))
        if isinstance(obj, list):
            return [_deep_normalize_newlines(x) for x in obj]
        if isinstance(obj, dict):
            return {k: _deep_normalize_newlines(v) for k, v in obj.items()}
        return obj

    def _canonicalize_article_json_keys(obj):
        """Normalize common key-name deviations from Gemini into a canonical schema.

        Goal: downstream code should reliably find:
        - featured_image
        - excerpt
        - sections[].prompt1 / sections[].prompt2
        - sections[].h2 / sections[].text
        - conclusion_heading

        We keep original keys unless we can confidently map them.
        """
        import re as _re

        def norm(k: str) -> str:
            k = (k or "").strip().lower()
            k = k.replace("-", " ").replace("_", " ")
            k = _re.sub(r"\s+", " ", k).strip()
            return k

        if isinstance(obj, list):
            return [_canonicalize_article_json_keys(x) for x in obj]

        if not isinstance(obj, dict):
            return obj

        out: dict = {}
        for k, v in obj.items():
            nk = norm(k) if isinstance(k, str) else k
            v2 = _canonicalize_article_json_keys(v)

            # Top-level common mappings
            # Gemini sometimes outputs typos like "featurge".
            if nk in (
                "featured image",
                "featuredimage",
                "feature image",
                "featured img",
                "featured prompt",
                "featurge",
                "feauture",
                "feaimage",
                "feautured",
                "featured",
                "feature",
            ):
                out["featured_image"] = v2
                continue
            if nk in ("featured_image",):
                out["featured_image"] = v2
                continue
            if nk in ("excerpt", "summary", "meta description", "meta"):
                out["excerpt"] = v2
                continue
            if nk in ("conclusion heading", "conclusion_heading", "conclusion title"):
                out["conclusion_heading"] = v2
                continue

            # Section-level mappings (will be applied also on nested dicts)
            if nk in ("h2", "heading", "section heading", "section title"):
                out["h2"] = v2
                continue
            if nk in ("text", "body", "content", "section text"):
                out["text"] = v2
                continue

            # prompt1/prompt2 sometimes come as "prompt 1", "vertical image prompt 2", etc.
            if isinstance(nk, str) and "prompt" in nk:
                m = _re.search(r"(\d+)\s*$", nk)
                if m and m.group(1) in ("1", "2"):
                    out[f"prompt{m.group(1)}"] = v2
                    continue

            # Default: keep key as-is
            out[k] = v2

        # If the model used featuredImage camelCase, bring it over
        if "featured_image" not in out:
            fi = obj.get("featuredImage")
            if isinstance(fi, str) and fi.strip():
                out["featured_image"] = fi

        # Defensive normalization: keep featured_image aligned with the current theme/site rules.
        fi2 = out.get("featured_image")
        if isinstance(fi2, str) and fi2.strip():
            out["featured_image"] = _normalize_featured_image_prompt(
                fi2,
                theme=theme,
                target_domain=target_domain,
            )

        return out

    try:
        import json as _json
        import re as _re

        t = (text or "").strip()
        if not t:
            return None

        # Strip fenced code blocks
        m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, flags=_re.DOTALL | _re.IGNORECASE)
        if m:
            cand = m.group(1)
            data = _loads_with_repair(cand)
            if not isinstance(data, dict):
                return None
            data = _deep_normalize_newlines(data)
            data = _canonicalize_article_json_keys(data)
            return data

        # Fallback: find first {...} span
        i = t.find("{")
        j = t.rfind("}")
        if i != -1 and j != -1 and j > i:
            cand = t[i : j + 1]
            data = _loads_with_repair(cand)
            if not isinstance(data, dict):
                return None
            data = _deep_normalize_newlines(data)
            data = _canonicalize_article_json_keys(data)
            return data
        return None
    except Exception:
        return None


def _format_repaired_article_json_text(
    text: str,
    theme: str | None = None,
    target_domain: str | None = None,
) -> str:
    """Return pretty article JSON text with parser-level repairs applied, if possible."""
    try:
        import json as _json

        data = _try_parse_article_json(text, theme=theme, target_domain=target_domain)
        if isinstance(data, dict):
            return _json.dumps(data, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return str(text or "")


def _article_json_to_markdown(data: dict) -> str:
    """Render parsed article JSON to Markdown."""
    title = (data.get("title") or "").strip()
    intro = (data.get("introduction") or "").strip()
    concl_h = (data.get("conclusion_heading") or "Conclusion").strip() or "Conclusion"
    concl_t = (data.get("conclusion") or "").strip()

    md: list[str] = []
    if title:
        md.append(f"# {title}")
        md.append("")
    if intro:
        md.append(intro)
        md.append("")

    sections = data.get("sections") or []
    if isinstance(sections, list):
        for s in sections:
            if not isinstance(s, dict):
                continue
            h2 = (s.get("h2") or s.get("heading") or "").strip()
            body = (s.get("text") or "").strip()
            if h2:
                md.append(f"## {h2}")
            if body:
                md.append(body)
            md.append("")

    if concl_t:
        md.append(f"## {concl_h}")
        md.append(concl_t)
        md.append("")

    return "\n".join(md).strip()


def _article_json_image_prompts(data: dict) -> list[str]:
    """Backward-compatible extractor (2 prompts per section).

    This remains the default behavior used by older workflows.
    """

    return _article_json_image_prompts_mode(data, prompts_per_section=2)


def _article_json_norm_key(k: str) -> str:
    import re as _re

    k = (k or "").strip().lower()
    k = k.replace("_", " ")
    k = _re.sub(r"\s+", " ", k).strip()
    return k


def _article_json_get_field_variant(section: dict, base_name: str, idx: int) -> str:
    direct_keys = [f"{base_name}{idx}", f"{base_name}_{idx}"]
    for dk in direct_keys:
        try:
            v = section.get(dk)
            if isinstance(v, str) and v.strip():
                return v.strip()
        except Exception:
            pass

    wanted_suffix = str(idx)
    for k, v in (section or {}).items():
        if not isinstance(k, str):
            continue
        nk = _article_json_norm_key(k)
        if base_name not in nk:
            continue
        if not nk.endswith(wanted_suffix):
            continue
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _article_json_get_prompt_variant(section: dict, idx: int) -> str:
    return _article_json_get_field_variant(section, "prompt", idx)


def _article_json_get_alt_variant(section: dict, idx: int) -> str:
    return _article_json_get_field_variant(section, "alt", idx)


def _article_json_prompt_slot_indices(section: dict, *, prompts_per_section: int = 2) -> list[int]:
    p1 = _article_json_get_prompt_variant(section, 1)
    p2 = _article_json_get_prompt_variant(section, 2)
    pps = 2 if int(prompts_per_section or 2) >= 2 else 1

    if pps == 1:
        if p1:
            return [1]
        if p2:
            return [2]
        return []

    out: list[int] = []
    if p1:
        out.append(1)
    if p2:
        out.append(2)
    return out


def _article_json_image_prompts_mode(data: dict, *, prompts_per_section: int = 2) -> list[str]:
    """Extract ordered per-section image prompts from structured article JSON.

    Modes:
    - prompts_per_section=2 (default): returns [prompt1, prompt2] for each section (if present)
    - prompts_per_section=1: returns ONLY prompt1 per section (fallback to prompt2 if prompt1 missing)

    IMPORTANT:
    - Does NOT include `featured_image`.
    - Defensive against Gemini key variants.
    """

    prompts: list[str] = []
    sections = data.get("sections") or []
    if isinstance(sections, list):
        for s in sections:
            if not isinstance(s, dict):
                continue
            for slot_idx in _article_json_prompt_slot_indices(s, prompts_per_section=prompts_per_section):
                p = _article_json_get_prompt_variant(s, slot_idx)
                if p:
                    prompts.append(p)

    # de-dup (preserve order)
    seen: set[str] = set()
    out: list[str] = []
    for p in prompts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _article_json_prompt_coverage_status(data: dict, *, prompts_per_section: int = 2) -> dict:
    """Check whether the number of image prompts matches the number of sections.

    We expect:
      expected_prompts = len(sections) * prompts_per_section

    Exception:
    - In recipe theme, sparse section prompts are allowed, so expected_prompts
      is treated as "the number of prompt slots that actually exist in JSON".

    We count prompts per section based on the same key-variant logic as
    `_article_json_image_prompts_mode`, but WITHOUT de-duplication (because
    duplicates are still valid prompts).

    Returns a dict:
      {
        ok: bool,
        sections: int,
        prompts_found: int,
        prompts_expected: int,
        missing: int,
      }

    Never raises.
    """

    try:
        sections = data.get("sections") or []
        if not isinstance(sections, list):
            sections = []

        # Count only dict sections (schema requires objects).
        sec_dicts = [s for s in sections if isinstance(s, dict)]
        sec_count = len(sec_dicts)

        found = sum(len(_article_json_prompt_slot_indices(s, prompts_per_section=prompts_per_section)) for s in sec_dicts)

        theme = _normalize_article_theme(st.session_state.get("unif_text_theme"))
        sparse_allowed = (theme == "recipes")
        pps = 2 if int(prompts_per_section or 2) >= 2 else 1
        expected = found if sparse_allowed else (sec_count * pps)
        missing = max(0, int(expected) - int(found))
        ok = (found == expected)

        return {
            "ok": bool(ok),
            "sections": int(sec_count),
            "prompts_found": int(found),
            "prompts_expected": int(expected),
            "missing": int(missing),
            "sparse_allowed": bool(sparse_allowed),
        }
    except Exception:
        return {
            "ok": False,
            "sections": 0,
            "prompts_found": 0,
            "prompts_expected": 0,
            "missing": 0,
        }


def _clean_article_for_gutenberg(text: str) -> str:
    """Clean generated output for pasting into WordPress Gutenberg.

    Removes service lines like:
    - Excerpt: ...
    - Featured Image: ...
    - Prompt 1/2: ...
    - Amazon Search Phrases: ...

    Important: Gemini sometimes outputs H2 headings as plain standalone lines
    (without '##' or 'H2:'). We apply heuristics to detect such lines and
    convert them to Markdown H2 so Gutenberg can preserve structure.
    """
    import re as _re

    t = (text or "").splitlines()
    out: list[str] = []

    drop_prefixes = (
        "excerpt:",
        "featured image:",
        "prompt 1:",
        "prompt 2:",
        "amazon search phrases:",
    )

    for ln in t:
        s = ln.strip()
        if not s:
            out.append("")
            continue

        low = s.lower()
        if any(low.startswith(p) for p in drop_prefixes):
            continue

        # Normalize headings to Markdown so Gutenberg keeps structure on paste.
        m2 = _re.match(r"^h2\s*[:\-]?\s*(.+)$", s, flags=_re.IGNORECASE)
        if m2:
            out.append("## " + m2.group(1).strip())
            continue
        m3 = _re.match(r"^h3\s*[:\-]?\s*(.+)$", s, flags=_re.IGNORECASE)
        if m3:
            out.append("### " + m3.group(1).strip())
            continue

        # Heuristic: Gemini sometimes outputs section headings as numbered lines like "1. Some Heading".
        # Convert such short numbered lines into H2 to avoid Gutenberg turning them into ordered lists.
        mnum = _re.match(r"^(\d+)[\.)]\s+(.+)$", s)
        if mnum:
            candidate = mnum.group(2).strip()
            if len(candidate) <= 140 and candidate.count(".") <= 1:
                out.append("## " + candidate)
                continue

        out.append(ln)

    # Remove excessive blank lines
    cleaned: list[str] = []
    prev_blank = False
    for ln in out:
        is_blank = (ln.strip() == "")
        if is_blank and prev_blank:
            continue
        cleaned.append(ln.rstrip())
        prev_blank = is_blank

    # Ensure first non-empty line is a Markdown H1
    for i, ln in enumerate(cleaned):
        if ln.strip():
            if not ln.lstrip().startswith("#"):
                cleaned[i] = "# " + ln.strip()
            break

    # No heuristic heading detection here: we rely on explicit structure (prefer JSON output).

    return "\n".join(cleaned).strip()


def _article_markdownish_to_gutenberg_blocks(md: str) -> str:
    """Convert markdown-ish text to Gutenberg block markup (most reliable).

    Output is HTML with Gutenberg comments, suitable for pasting into WordPress Code Editor.

    Extra behavior:
    - Insert an empty paragraph block before each H2 except the first one.
      This helps when pasting into Gutenberg so sections are visually separated.
    """
    import html as _html
    import re as _re

    def inline(s: str) -> str:
        return _render_inline_allow_only_anchors(s)

    EMPTY_P = "<!-- wp:paragraph --><p></p><!-- /wp:paragraph -->"

    lines = (md or "").splitlines()
    out: list[str] = []

    ul_items: list[str] = []
    ol_items: list[str] = []

    seen_h2 = False

    def flush_lists():
        nonlocal ul_items, ol_items
        if ul_items:
            li = "\n".join([f"<li>{inline(x)}</li>" for x in ul_items])
            out.append("<!-- wp:list --><ul>\n" + li + "\n</ul><!-- /wp:list -->")
            ul_items = []
        if ol_items:
            li = "\n".join([f"<li>{inline(x)}</li>" for x in ol_items])
            out.append("<!-- wp:list {\"ordered\":true} --><ol>\n" + li + "\n</ol><!-- /wp:list -->")
            ol_items = []

    for raw in lines:
        st = raw.strip()
        if not st:
            flush_lists()
            continue

        if st.startswith("### "):
            flush_lists()
            h = inline(st[4:])
            out.append(f"<!-- wp:heading {{\"level\":3}} --><h3>{h}</h3><!-- /wp:heading -->")
            continue
        if st.startswith("## "):
            flush_lists()
            if seen_h2:
                out.append(EMPTY_P)
            seen_h2 = True
            h = inline(st[3:])
            out.append(f"<!-- wp:heading {{\"level\":2}} --><h2>{h}</h2><!-- /wp:heading -->")
            continue
        if st.startswith("# "):
            flush_lists()
            h = inline(st[2:])
            out.append(f"<!-- wp:heading {{\"level\":1}} --><h1>{h}</h1><!-- /wp:heading -->")
            continue

        m_ol = _re.match(r"^(\d+)\.\s+(.*)$", st)
        if m_ol:
            if ul_items:
                flush_lists()
            ol_items.append(m_ol.group(2).strip())
            continue
        if st.startswith("- ") or st.startswith("* "):
            if ol_items:
                flush_lists()
            ul_items.append(st[2:].strip())
            continue

        flush_lists()
        p = inline(st)
        out.append(f"<!-- wp:paragraph --><p>{p}</p><!-- /wp:paragraph -->")

    flush_lists()
    return "\n\n".join(out).strip()


def _gutenberg_image_block(src: str, *, alt: str = "") -> str:
    """Return a Gutenberg wp:image block for a given image URL/path."""
    import html as _html

    s = (src or "").strip()
    a = (alt or "").strip()
    # Use forward slashes for nicer WP compatibility; keep UNC/URLs intact.
    try:
        s = s.replace("\\\\", "/")
    except Exception:
        pass
    s_esc = _html.escape(s, quote=True)
    a_esc = _html.escape(a, quote=True)
    return (
        "<!-- wp:image {\"sizeSlug\":\"full\",\"linkDestination\":\"none\"} -->"
        f"<figure class=\"wp-block-image size-full\"><img src=\"{s_esc}\" alt=\"{a_esc}\"/></figure>"
        "<!-- /wp:image -->"
    )


def _markdownish_body_to_gutenberg_blocks(body_md: str) -> str:
    """Convert body text (no headings expected) into Gutenberg blocks.

    Supports paragraphs + ordered/unordered lists + **bold**.
    """
    import html as _html
    import re as _re

    def inline(s: str) -> str:
        return _render_inline_allow_only_anchors(s)

    lines = (body_md or "").splitlines()
    out: list[str] = []

    ul_items: list[str] = []
    ol_items: list[str] = []

    def flush_lists():
        nonlocal ul_items, ol_items
        if ul_items:
            li = "\n".join([f"<li>{inline(x)}</li>" for x in ul_items])
            out.append("<!-- wp:list --><ul>\n" + li + "\n</ul><!-- /wp:list -->")
            ul_items = []
        if ol_items:
            li = "\n".join([f"<li>{inline(x)}</li>" for x in ol_items])
            out.append("<!-- wp:list {\"ordered\":true} --><ol>\n" + li + "\n</ol><!-- /wp:list -->")
            ol_items = []

    para_lines: list[str] = []

    def flush_para():
        nonlocal para_lines
        if not para_lines:
            return
        txt = " ".join([x.strip() for x in para_lines if x.strip()]).strip()
        if txt:
            out.append(f"<!-- wp:paragraph --><p>{inline(txt)}</p><!-- /wp:paragraph -->")
        para_lines = []

    for raw in lines:
        st = raw.strip()
        if not st:
            flush_para()
            flush_lists()
            continue

        # Lists
        m_ol = _re.match(r"^(\d+)\.\s+(.*)$", st)
        if m_ol:
            flush_para()
            if ul_items:
                flush_lists()
            ol_items.append(m_ol.group(2).strip())
            continue
        if st.startswith("- ") or st.startswith("* "):
            flush_para()
            if ol_items:
                flush_lists()
            ul_items.append(st[2:].strip())
            continue

        # Paragraph line
        flush_lists()
        para_lines.append(st)

    flush_para()
    flush_lists()

    return "\n\n".join(out).strip()


def _uagb_two_image_container_block(
    img_left: str | None,
    img_right: str | None,
    *,
    alt_left: str = "",
    alt_right: str = "",
    class_name: str = "h2-uagb-no-shortcode-margin",
) -> str:
    """Build a UAGB container block with 2 columns, each containing one image.

    This matches your WordPress Gutenberg workflow where a UAGB container is inserted
    before H2 headings (except the first).

    Notes:
    - We generate random `block_id`s so multiple containers on the page don't clash.
    - We keep markup intentionally minimal; WP/UAGB will normalize attributes on paste.
    """
    import html as _html
    import secrets as _secrets

    def _bid() -> str:
        return _secrets.token_hex(4)

    outer_id = _bid()
    c1_id = _bid()
    c2_id = _bid()
    img1_id = _bid()
    img2_id = _bid()

    def _esc_url(u: str) -> str:
        s = (u or "").strip().replace("\\\\", "/")
        return _html.escape(s, quote=True)

    def _img_block(block_id: str, url: str, *, alt: str = "") -> str:
        u = _esc_url(url)
        a = _html.escape((alt or "").strip(), quote=True)
        return (
            # Keep comment JSON close to what UAGB itself outputs (see gutenberg_block.txt).
            f'<!-- wp:uagb/image {{"block_id":"{block_id}","url":"{u}","urlTablet":"{u}","urlMobile":"{u}"}} -->\n'
            f'<div class="wp-block-uagb-image uagb-block-{block_id} wp-block-uagb-image--layout-default wp-block-uagb-image--effect-static wp-block-uagb-image--align-none">'
            f'<figure class="wp-block-uagb-image__figure"><img src="{u}" alt="{a}" loading="lazy" /></figure></div>\n'
            f'<!-- /wp:uagb/image -->'
        )

    left_html = _img_block(img1_id, img_left, alt=alt_left) if img_left else ""
    right_html = _img_block(img2_id, img_right, alt=alt_right) if img_right else ""

    # If one side is missing, we still keep the 2 containers for layout stability.
    return (
        f'<!-- wp:uagb/container {{"block_id":"{outer_id}","directionDesktop":"row","variationSelected":true,"isBlockRootParent":true,"className":"{_html.escape(class_name, quote=True)}"}} -->\n'
        f'<div class="wp-block-uagb-container {_html.escape(class_name, quote=True)} uagb-block-{outer_id} alignfull uagb-is-root-container">'
        f'<div class="uagb-container-inner-blocks-wrap">\n'
        f'<!-- wp:uagb/container {{"block_id":"{c1_id}","widthDesktop":50,"widthSetByUser":true}} -->\n'
        f'<div class="wp-block-uagb-container uagb-block-{c1_id}">\n{left_html}\n</div>\n'
        f'<!-- /wp:uagb/container -->\n\n'
        f'<!-- wp:uagb/container {{"block_id":"{c2_id}","widthDesktop":50,"widthSetByUser":true}} -->\n'
        f'<div class="wp-block-uagb-container uagb-block-{c2_id}">\n{right_html}\n</div>\n'
        f'<!-- /wp:uagb/container -->\n'
        f'</div></div>\n'
        f'<!-- /wp:uagb/container -->'
    )


def _uagb_one_image_container_block(
    img: str | None,
    *,
    alt: str = "",
    class_name: str = "h2-uagb-inserter-root h2-uagb-no-shortcode-margin",
) -> str:
    """Build a UAGB container block with a single image.

    Matches the layout in `gutenberg_block2.txt`: a root container with zero padding
    and a single UAGB image directly inside.
    """
    import html as _html
    import secrets as _secrets

    def _bid() -> str:
        return _secrets.token_hex(4)

    outer_id = _bid()
    img_id = _bid()

    def _esc_url(u: str) -> str:
        s = (u or "").strip().replace("\\\\", "/")
        return _html.escape(s, quote=True)

    def _img_block(block_id: str, url: str, *, alt: str = "") -> str:
        u = _esc_url(url)
        a = _html.escape((alt or "").strip(), quote=True)
        return (
            f'<!-- wp:uagb/image {{"block_id":"{block_id}","url":"{u}","urlTablet":"{u}","urlMobile":"{u}"}} -->\n'
            f'<div class="wp-block-uagb-image uagb-block-{block_id} wp-block-uagb-image--layout-default wp-block-uagb-image--effect-static wp-block-uagb-image--align-none">'
            f'<figure class="wp-block-uagb-image__figure"><img src="{u}" alt="{a}" loading="lazy" /></figure></div>\n'
            f'<!-- /wp:uagb/image -->'
        )

    img_html = _img_block(img_id, img, alt=alt) if img else ""

    cls = _html.escape(class_name, quote=True)
    # Note: keep padding attributes explicit (as in your saved template); WP/UAGB will normalize.
    return (
        f'<!-- wp:uagb/container {{"block_id":"{outer_id}","topPaddingDesktop":0,"bottomPaddingDesktop":0,'
        f'"leftPaddingDesktop":0,"rightPaddingDesktop":0,"topPaddingTablet":0,"bottomPaddingTablet":0,'
        f'"leftPaddingTablet":0,"rightPaddingTablet":0,"topPaddingMobile":0,"bottomPaddingMobile":0,'
        f'"leftPaddingMobile":0,"rightPaddingMobile":0,"variationSelected":true,"isBlockRootParent":true,'
        f'"className":"{cls}"}} -->\n'
        f'<div class="wp-block-uagb-container {cls} uagb-block-{outer_id} alignfull uagb-is-root-container">'
        f'<div class="uagb-container-inner-blocks-wrap">{img_html}</div></div>\n'
        f'<!-- /wp:uagb/container -->'
    )


def _collect_all_webp_in_run_dir_for_wp(run_dir: str | Path, *, out_dir_name: str = "__all_webp_for_wp") -> tuple[str | None, int, list[str]]:
    """Collect all .webp files under a generation run directory into a single folder.

    Convenience helper for bulk-uploading images to WordPress.

    - Scans recursively under `run_dir`.
    - Copies files into `<run_dir>/<out_dir_name>`.
    - Does NOT delete/move sources.

    Returns: (out_dir_path_or_none, copied_count, errors)
    """

    try:
        from pathlib import Path as _Path
        import re as _re

        src_root = _Path(run_dir).expanduser()
        if not src_root.exists() or not src_root.is_dir():
            return None, 0, [f"Run dir not found: {src_root}"]

        out_dir = src_root / out_dir_name
        out_dir.mkdir(parents=True, exist_ok=True)
        out_dir_resolved = out_dir.resolve()

        # Windows-invalid filename chars: < > : " / \\ | ? *
        invalid_re = _re.compile(r"[<>:\\\"/\\\\|?*]")

        def _sanitize_component(s: str, *, default: str) -> str:
            s = (s or "").strip()
            s = invalid_re.sub("_", s)
            s = _re.sub(r"\s+", "_", s).strip("_ .")
            return s or default

        def _unique_dest_path(dst_dir: _Path, filename: str) -> _Path:
            """Keep original filename; add suffix only if collision inside dst_dir."""
            cand = dst_dir / filename
            if not cand.exists():
                return cand
            stem = cand.stem
            suf = cand.suffix
            for i in range(2, 10000):
                c2 = dst_dir / f"{stem}_{i}{suf}"
                if not c2.exists():
                    return c2
            return dst_dir / f"{stem}_{os.getpid()}{suf}"

        def _is_in_out_dir(p: _Path) -> bool:
            try:
                return p.resolve().is_relative_to(out_dir_resolved)  # type: ignore[attr-defined]
            except Exception:
                try:
                    return str(p.resolve()).startswith(str(out_dir_resolved) + os.sep)
                except Exception:
                    return False

        copied = 0
        errors: list[str] = []

        for p in src_root.rglob("*.webp"):
            try:
                if not p.is_file():
                    continue
                if _is_in_out_dir(p):
                    continue

                # Put images into per-article folders to avoid name collisions
                # while preserving original filenames.
                try:
                    rel_path = p.relative_to(src_root)
                    rel_parts = list(rel_path.parts)
                except Exception:
                    rel_parts = []

                article_dirname = "misc"
                if rel_parts:
                    for part in rel_parts:
                        if isinstance(part, str) and part.lower().startswith("article_"):
                            article_dirname = part
                            break

                safe_article_dirname = _sanitize_component(article_dirname, default="misc")
                article_out_dir = out_dir / safe_article_dirname
                article_out_dir.mkdir(parents=True, exist_ok=True)

                filename = p.name
                # In the extremely rare case of invalid chars in filename, sanitize minimally.
                safe_filename = invalid_re.sub("_", filename)
                if not safe_filename.lower().endswith(".webp"):
                    safe_filename += ".webp"

                dst = _unique_dest_path(article_out_dir, safe_filename)
                shutil.copy2(p, dst)
                copied += 1
            except Exception as e:
                errors.append(f"{p}: {e}")

        return str(out_dir), copied, errors
    except Exception as e:
        return None, 0, [str(e)]



def _get_latest_featured_webp_for_article_idx(idx: int) -> str | None:
    """Return the *current* featured image WebP path for a given article idx.

    Source of truth:
    - Featured tab stores converted WebP paths in `st.session_state.unif_featured_webp` under keys:
        webp_<idx>_<image_rev>
      where image_rev is computed from the current featured image file signature.

    We try to resolve the WebP for the *currently displayed* featured image (latest saved_path),
    and fallback to the newest existing webp for that idx.

    Returns absolute path string or None.
    """
    try:
        import hashlib

        # Defensive: session_state may not have these yet
        webp_map = st.session_state.get("unif_featured_webp")
        if not isinstance(webp_map, dict) or not webp_map:
            return None

        # 1) Prefer the WebP for the *current* featured image shown in the UI.
        saved_path = ""
        try:
            feat_results = st.session_state.get("unif_featured_results") or []
            for r in feat_results:
                try:
                    if int((r or {}).get("idx") or 0) == int(idx):
                        saved_path = str((r or {}).get("saved_path") or "").strip()
                        break
                except Exception:
                    continue
        except Exception:
            saved_path = ""

        def _compute_image_rev(pth: str) -> str:
            try:
                if not pth:
                    return "none"
                p = Path(pth)
                stt = p.stat() if p.exists() else None
                sig = f"{pth}|{getattr(stt, 'st_mtime_ns', '')}|{getattr(stt, 'st_size', '')}"
            except Exception:
                sig = pth or ""
            return hashlib.md5(sig.encode("utf-8")).hexdigest()[:10] if sig else "none"

        if saved_path:
            rev = _compute_image_rev(saved_path)
            key = f"webp_{int(idx)}_{rev}"
            p = str(webp_map.get(key) or "").strip()
            if p and Path(p).exists():
                return str(Path(p).expanduser().resolve())

            # If WebP was created next to the filled image but not recorded, try common adjacent names.
            try:
                cand = Path(saved_path).with_suffix(".webp")
                if cand.exists():
                    return str(cand.expanduser().resolve())
            except Exception:
                pass

        # 2) Fallback: newest existing webp_<idx>_* entry by file mtime.
        cand_paths: list[Path] = []
        prefix = f"webp_{int(idx)}_"
        for k, v in list(webp_map.items()):
            try:
                if not (isinstance(k, str) and k.startswith(prefix)):
                    continue
                vp = str(v or "").strip()
                if not vp:
                    continue
                pp = Path(vp)
                if pp.exists() and pp.is_file():
                    cand_paths.append(pp)
            except Exception:
                continue

        if not cand_paths:
            return None

        cand_paths.sort(key=lambda p: p.stat().st_mtime_ns if p.exists() else 0, reverse=True)
        return str(cand_paths[0].expanduser().resolve())
    except Exception:
        return None


def _get_tab0_local_webp_files_for_article(idx: int) -> list[str] | None:
    """Return ordered local WebP files for an article from the selected Tab0 run folder.

    This is used for WordPress bulk upload JSON generation.

    We deliberately return *local* paths (not URLs): wp_bulk_upload_streamlit.py will upload
    the files and then substitute placeholders in Gutenberg blocks.

    Ordering rule matches `_get_tab0_images_for_article`:
    - group by prompt index parsed from filename prefix `^(\\d+)_`
    - within each prompt index: sort by name
    - prefer `_pro_` images first, then non-pro

    We prefer finalized post-processed WebPs under:
      article_*/postproc/webp_640x1024
      article_*/postproc/webp_no_normalize
    with fallbacks.
    """
    try:
        from pathlib import Path as _Path
        import re as _re

        run_dir = str(st.session_state.get("unif_tab0_run_dir") or "").strip()
        if not run_dir:
            return None

        run_p = _Path(run_dir).expanduser()
        if not run_p.exists() or not run_p.is_dir():
            return None

        # Determine this article position among Tab0 results (idx ascending)
        results = st.session_state.get("unif_text_results") or []
        idxs: list[int] = []
        for rr in results:
            try:
                ii = int((rr or {}).get("idx") or 0)
                if ii:
                    idxs.append(ii)
            except Exception:
                continue
        idxs = sorted(set(idxs))
        if int(idx) not in idxs:
            return None
        pos = idxs.index(int(idx))

        article_dirs = sorted([p for p in run_p.glob("article_*") if p.is_dir()], key=lambda p: p.name.lower())
        if pos >= len(article_dirs):
            return None
        art_dir = article_dirs[pos]

        def _collect_webp(d: _Path) -> list[_Path]:
            try:
                if not d.exists() or not d.is_dir():
                    return []
                return sorted([p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".webp"], key=lambda p: p.name.lower())
            except Exception:
                return []

        preferred_dirs: list[_Path] = [
            art_dir / "postproc" / "webp_640x1024",
            art_dir / "postproc" / "webp_no_normalize",
            art_dir / "postproc" / "webp",
            art_dir / "postproc",
        ]

        files: list[_Path] = []
        for d in preferred_dirs:
            cand = _collect_webp(d)
            if cand:
                files = cand
                break

        if not files:
            # last resort: any webp directly under article dir
            files = _collect_webp(art_dir)

        if not files:
            return None

        def _prompt_idx_from_name(name: str) -> int | None:
            m = _re.match(r"^(\d+)_", name or "")
            if not m:
                return None
            try:
                v = int(m.group(1))
                # 0_* is обычно featured/служебная картинка; секции начинаются с 1_*
                if v <= 0:
                    return None
                return v
            except Exception:
                return None

        by_idx: dict[int, list[_Path]] = {}
        for p in files:
            pi = _prompt_idx_from_name(p.name)
            if pi is None:
                continue
            by_idx.setdefault(pi, []).append(p)

        ordered_pro: list[str] = []
        ordered_plain: list[str] = []
        for pi in sorted(by_idx.keys()):
            group = sorted(by_idx[pi], key=lambda x: x.name.lower())
            for p in group:
                if "_pro_" in p.name.lower():
                    ordered_pro.append(str(p.expanduser().resolve()))
                else:
                    ordered_plain.append(str(p.expanduser().resolve()))

        ordered = ordered_pro + ordered_plain
        return ordered or None
    except Exception:
        return None


def _get_tab0_images_for_article(idx: int, article_json: dict) -> list[list[str]] | None:
    """Map images from a selected local generation run folder to an article's sections.

    Expected folder layout (selected run dir):
      <run_dir>/article_.../\n
    Mapping rule:
    - We sort article subfolders by name and map them to Tab0 posts order (idx ascending).
    - Inside each article folder we take image files in prompt order. For each prompt index,
      we prefer files containing `_pro_` before non-pro.

    Returns:
      list[list[str]] aligned to `article_json['sections']`, each inner list contains
      N image paths (N = unif_text_prompts_per_section, usually 2).
    """
    try:
        from pathlib import Path as _Path
        import re as _re

        run_dir = str(st.session_state.get("unif_tab0_run_dir") or "").strip()
        if not run_dir:
            return None

        run_p = _Path(run_dir)
        if not run_p.exists() or not run_p.is_dir():
            return None

        # Determine this article position among results
        results = st.session_state.get("unif_text_results") or []
        idxs: list[int] = []
        for rr in results:
            try:
                ii = int((rr or {}).get("idx") or 0)
                if ii:
                    idxs.append(ii)
            except Exception:
                continue
        idxs = sorted(set(idxs))
        if idx not in idxs:
            return None
        pos = idxs.index(idx)

        article_dirs = sorted([p for p in run_p.glob("article_*") if p.is_dir()], key=lambda p: p.name.lower())
        if pos >= len(article_dirs):
            return None
        art_dir = article_dirs[pos]

        # Collect image files
        # We prefer optimized WebP outputs generated by the post-processing pipeline.
        # Folder layout (inside each article_* dir), depending on pipeline options:
        #   postproc/webp_640x1024/*.webp        (normalize enabled)
        #   postproc/webp_no_normalize/*.webp    (normalize disabled)
        # Fallback: use images from the article root folder.
        exts_any = {".webp", ".png", ".jpg", ".jpeg"}

        def _collect_images_in_dir(d: _Path, *, only_exts: set[str] | None = None) -> list[_Path]:
            if not d.exists() or not d.is_dir():
                return []
            out: list[_Path] = []
            for pp in d.iterdir():
                if not pp.is_file():
                    continue
                ext = pp.suffix.lower()
                if only_exts is not None:
                    if ext not in only_exts:
                        continue
                else:
                    if ext not in exts_any:
                        continue
                out.append(pp)
            return out

        preferred_dirs: list[_Path] = [
            art_dir / "postproc" / "webp_640x1024",
            art_dir / "postproc" / "webp_no_normalize",
            # Additional fallbacks in case folder naming changes.
            art_dir / "postproc" / "webp",
            art_dir / "postproc",
        ]

        files: list[_Path] = []
        for d in preferred_dirs:
            # In optimized dirs we expect WebP only.
            cand = _collect_images_in_dir(d, only_exts={".webp"})
            if cand:
                files = cand
                break

        if not files:
            files = _collect_images_in_dir(art_dir, only_exts=None)

        if not files:
            return None

        def _prompt_idx_from_name(name: str) -> int | None:
            m = _re.match(r"^(\d+)_", name)
            if m:
                try:
                    v = int(m.group(1))
                    # 0_* is обычно featured/служебная картинка; секции начинаются с 1_*
                    if v <= 0:
                        return None
                    return v
                except Exception:
                    return None
            return None

        # Group by prompt idx
        by_idx: dict[int, list[_Path]] = {}
        for p in files:
            pi = _prompt_idx_from_name(p.name)
            if pi is None:
                continue
            by_idx.setdefault(pi, []).append(p)

        # Build order for insertion into containers:
        #   1) all *_pro_* images first (in prompt idx order)
        #   2) then all non-pro images (in prompt idx order)
        # This way containers are filled like:
        #   [1_pro, 2_pro], [3_pro, 4_pro], ... then [1_, 2_], [3_, 4_], ...
        ordered_pro: list[str] = []
        ordered_plain: list[str] = []

        base_url = str(st.session_state.get("unif_tab0_uploads_base_url") or "").strip()

        # IMPORTANT: Streamlit keeps the text_input value in session_state.
        # Historically, we auto-switched between nestingmuse.com and spaceofmuse.com based on
        # "images per section" mode. This was convenient but also created an unwanted dependency
        # (e.g. you couldn't pick nestingmuse.com with 1 image/section).
        # Now the domain is controlled ONLY by the explicit base_url input in Tab0.
        # We keep the fallback behavior (local paths when base_url is empty) for compatibility.

        def _join_base_url(b: str, filename: str) -> str:
            b = (b or "").strip()
            if not b:
                return filename
            if not b.endswith("/"):
                b = b + "/"
            return b + filename.lstrip("/")

        def _to_url_or_path(p: _Path) -> str:
            if base_url:
                return _join_base_url(base_url, p.name)
            # Fallback: local paths (not recommended for WP paste, but kept for compatibility)
            return str(p).replace("\\", "/")

        for pi in sorted(by_idx.keys()):
            group = by_idx[pi]
            # Stable by name within the same prompt index.
            group_sorted = sorted(group, key=lambda x: x.name.lower())
            for p in group_sorted:
                if "_pro_" in p.name.lower():
                    ordered_pro.append(_to_url_or_path(p))
                else:
                    ordered_plain.append(_to_url_or_path(p))

        ordered: list[str] = ordered_pro + ordered_plain

        if not ordered:
            return None

        sections = article_json.get("sections") or []
        if not isinstance(sections, list):
            return None

        per_sec = int(st.session_state.get("unif_text_prompts_per_section") or 2)
        if per_sec <= 0:
            per_sec = 2

        # Alignment for Gutenberg image containers:
        # We insert containers before each H2 except the first (i>0 in _article_json_to_gutenberg_blocks).
        # Additionally, the "Conclusion" is rendered as an extra H2 after all sections.
        # Prompt-indexed files in the run folder are numbered 1_, 2_, 3_..., which should map to the
        # 2nd, 3rd, 4th H2... and then (if present) to the Conclusion H2.
        # Therefore we shift by 1 and allocate one extra slot:
        #   out[0] -> [] (first H2 skipped)
        #   out[1] -> images for 1_*
        #   out[2] -> images for 2_*
        #   ...
        #   out[len(sections)] -> images for Conclusion (if any)
        out: list[list[str]] = [[] for _ in range(len(sections) + 1)]
        k = 0
        for sec_i in range(1, len(sections) + 1):
            sec = sections[sec_i - 1] if (sec_i - 1) < len(sections) else {}
            prompt_slots = _article_json_prompt_slot_indices(sec, prompts_per_section=per_sec) if isinstance(sec, dict) else []
            need = len(prompt_slots)
            out[sec_i] = ordered[k : k + need] if need > 0 else []
            k += need
        return out
    except Exception:
        return None


def _article_json_to_gutenberg_blocks(
    data: dict,
    *,
    image_paths_by_section: list[list[str]] | None = None,
    image_alts_by_section: list[list[str]] | None = None,
    include_title_h1: bool = True,
) -> str:
    """Render parsed article JSON directly to Gutenberg blocks, optionally with images.

    If `image_paths_by_section` is provided, we insert a UAGB 2-image container BEFORE each H2
    except the first one (per your Gutenberg layout).
    """
    import html as _html

    def esc_inline(s: str) -> str:
        # Reuse markdownish conversion for bold. Headings are handled separately.
        # We'll just escape here for headings.
        return _html.escape(s or "", quote=False)

    EMPTY_P = "<!-- wp:paragraph --><p></p><!-- /wp:paragraph -->"

    title = (data.get("title") or "").strip()
    intro = (data.get("introduction") or "").strip()

    sections = data.get("sections") or []
    if not isinstance(sections, list):
        sections = []

    concl_h = (data.get("conclusion_heading") or "Conclusion").strip() or "Conclusion"
    concl_t = (data.get("conclusion") or "").strip()

    out: list[str] = []

    if include_title_h1 and title:
        out.append(f"<!-- wp:heading {{\"level\":1}} --><h1>{esc_inline(title)}</h1><!-- /wp:heading -->")

    if intro:
        intro_blocks = _markdownish_body_to_gutenberg_blocks(intro)
        if intro_blocks:
            out.append(intro_blocks)

    # Ensure images list alignment
    img_map = image_paths_by_section or []
    alt_map = image_alts_by_section or []

    seen_h2 = False

    for i, s in enumerate(sections):
        if not isinstance(s, dict):
            continue
        h2 = (s.get("h2") or s.get("heading") or "").strip()
        body = (s.get("text") or "").strip()

        # Images for this section (aligned by section index)
        imgs = img_map[i] if i < len(img_map) else []
        alts = alt_map[i] if i < len(alt_map) else []

        # Your layout: before each H2 except the first, insert a UAGB container.
        # - If the section has 2+ images -> 2-column container
        # - If the section has exactly 1 image -> single-image container
        # Important: this container must appear BEFORE the H2 block.
        if h2 and i > 0 and (imgs or []):
            if len(imgs) >= 2:
                left = imgs[0] if len(imgs) > 0 else None
                right = imgs[1] if len(imgs) > 1 else None
                alt_left = alts[0] if len(alts) > 0 else ""
                alt_right = alts[1] if len(alts) > 1 else ""
                out.append(_uagb_two_image_container_block(left, right, alt_left=alt_left, alt_right=alt_right))
            else:
                alt_one = alts[0] if len(alts) > 0 else ""
                out.append(_uagb_one_image_container_block(imgs[0] if imgs else None, alt=alt_one))

        if h2:
            if seen_h2:
                out.append(EMPTY_P)
            seen_h2 = True
            out.append(f"<!-- wp:heading {{\"level\":2}} --><h2>{esc_inline(h2)}</h2><!-- /wp:heading -->")

        if body:
            body_blocks = _markdownish_body_to_gutenberg_blocks(body)
            if body_blocks:
                out.append(body_blocks)

        # Fallback/legacy behavior: if there is *no* H2 for this section, insert images below text.
        # IMPORTANT: do not do this for i==0 when an H2 exists, otherwise the first pair ends up
        # appearing right before the next H2 (and you get a duplicate container + plain images).
        if (not h2) and (imgs or []):
            for j, p in enumerate(imgs or []):
                if p:
                    a = alts[j] if j < len(alts) else ""
                    out.append(_gutenberg_image_block(p, alt=a))

    # Conclusion as an extra H2
    if concl_t:
        # If we have images reserved for Conclusion (extra slot), insert them before the Conclusion H2.
        concl_imgs = img_map[len(sections)] if len(img_map) > len(sections) else []
        concl_alts = alt_map[len(sections)] if len(alt_map) > len(sections) else []
        if concl_imgs:
            if len(concl_imgs) >= 2:
                left = concl_imgs[0] if len(concl_imgs) > 0 else None
                right = concl_imgs[1] if len(concl_imgs) > 1 else None
                alt_left = concl_alts[0] if len(concl_alts) > 0 else ""
                alt_right = concl_alts[1] if len(concl_alts) > 1 else ""
                out.append(_uagb_two_image_container_block(left, right, alt_left=alt_left, alt_right=alt_right))
            else:
                alt_one = concl_alts[0] if len(concl_alts) > 0 else ""
                out.append(_uagb_one_image_container_block(concl_imgs[0] if concl_imgs else None, alt=alt_one))

        if seen_h2:
            out.append(EMPTY_P)
        out.append(f"<!-- wp:heading {{\"level\":2}} --><h2>{esc_inline(concl_h)}</h2><!-- /wp:heading -->")
        concl_blocks = _markdownish_body_to_gutenberg_blocks(concl_t)
        if concl_blocks:
            out.append(concl_blocks)

    return "\n\n".join([x for x in out if (x or "").strip()]).strip()


def _article_markdownish_to_html(md: str) -> str:
    """Convert a simple markdown-ish text into HTML suitable for Gutenberg paste.

    Supports:
    - # / ## / ### headings
    - unordered lists starting with - or *
    - ordered lists like 1. item
    - **bold**

    This is intentionally simple and defensive.
    """
    import html as _html
    import re as _re

    lines = (md or "").splitlines()

    def esc(s: str) -> str:
        return _html.escape(s, quote=False)

    def inline(s: str) -> str:
        # bold **text**
        s = esc(s)
        s = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        return s

    out: list[str] = []
    in_ul = False
    in_ol = False

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    for raw in lines:
        s = raw.rstrip()
        st = s.strip()

        if not st:
            close_lists()
            continue

        if st.startswith("### "):
            close_lists()
            out.append(f"<h3>{inline(st[4:])}</h3>")
            continue
        if st.startswith("## "):
            close_lists()
            out.append(f"<h2>{inline(st[3:])}</h2>")
            continue
        if st.startswith("# "):
            close_lists()
            out.append(f"<h1>{inline(st[2:])}</h1>")
            continue

        m_ol = _re.match(r"^(\d+)\.\s+(.*)$", st)
        if m_ol:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{inline(m_ol.group(2))}</li>")
            continue

        if st.startswith("- ") or st.startswith("* "):
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline(st[2:])}</li>")
            continue

        # paragraph
        close_lists()
        out.append(f"<p>{inline(st)}</p>")

    close_lists()
    return "\n".join(out).strip()


def _extract_image_prompts_from_article(text: str) -> list[str]:


    """Extract image prompts (featured + Prompt 1/2...) from generated article text.

    Expected lines:
    - Featured Image: [...]
    - Prompt 1: [...]
    - Prompt 2: [...]

    Returns prompts in order: featured first (if present), then all Prompt 1/2 occurrences.
    """
    import re as _re

    t = text or ""

    def _grab(label: str) -> list[str]:
        out: list[str] = []
        # Prefer bracket form
        pat_br = _re.compile(rf"^{_re.escape(label)}\s*:\s*\[(.*)\]\s*$", flags=_re.IGNORECASE | _re.MULTILINE)
        for m in pat_br.finditer(t):
            v = (m.group(1) or "").strip()
            if v:
                out.append(v)
        # Fallback: no brackets
        pat_nb = _re.compile(rf"^{_re.escape(label)}\s*:\s*(.+?)\s*$", flags=_re.IGNORECASE | _re.MULTILINE)
        for m in pat_nb.finditer(t):
            v = (m.group(1) or "").strip()
            if v and v not in out:
                out.append(v)
        return out

    # NOTE: Featured Image prompt is handled by a separate 2x1 generation flow.
    # Do NOT include it into the Tab2/Tab3 prompt routing list.
    p1 = _grab("Prompt 1")
    p2 = _grab("Prompt 2")

    prompts: list[str] = []

    # Interleave Prompt 1/2 by occurrence order in the text.
    # We will do a single scan to keep order stable.
    items: list[tuple[int, str]] = []
    for lab in ("Prompt 1", "Prompt 2"):
        pat = _re.compile(rf"^{_re.escape(lab)}\s*:\s*(?:\[(.*)\]|(.+))\s*$", flags=_re.IGNORECASE | _re.MULTILINE)
        for m in pat.finditer(t):
            v = (m.group(1) or m.group(2) or "").strip()
            if v:
                items.append((m.start(), v))
    for _, v in sorted(items, key=lambda x: x[0]):
        prompts.append(v)

    # De-duplicate while preserving order
    seen = set()
    uniq: list[str] = []
    for p in prompts:
        key = p.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


PIN_INTRO_INSTRUCTION = (
    "Use the attached reference image as inspiration for the first section of the article that comes after the introduction. "
    "This section should immediately grab the reader's attention with text that is relevant to the context of the reference image."
    "Do NOT mention the reference image, any platform, or where the reader came from. "
    "Make sure the first section's image prompt is especially scroll-stopping: one strong establishing/hero shot, realistic and coherent with the section's text."
    "Try to provide more details in your generated prompts to ensure the images look creative rather than like generic stock photos, especially for Prompt1."
)

# When we do NOT attach a per-title image, we still want the first section to feel like a strong "hero" opener
# and we want the first section's images to be especially attention-grabbing.
FIRST_SECTION_HOOK_INSTRUCTION = (
    "For the first image of the first section of the article that comes after the introduction: make it a powerful hook that instantly grabs attention. This image should be really special, with creative elements, but still very realistic without unrealistic ultra-bright sun rays or similar fake-looking details. "
    "Ensure the first section's image prompt is the most scroll-stopping in the whole article: an establishing/hero shot that shows the full scene (beautiful, highly realistic, creative, full interior/lifestyle moment) with strong photorealistic composition and details. "
    "The image must be realistic, cohesive with the section text, and contain no text/logos/watermarks. "
    "Try to provide more details in your generated prompts to ensure the image looks creative rather than like generic stock photos, especially for Prompt1."
)


FASHION_TEMPLATE_REPLACEMENTS: list[tuple[str, str]] = [
    (
        "Generate a 'pinterest_title' which is a catchy, short version of the article title for a Pinterest Pin overlay, perfect to advertise listicles or decor guide articles.",
        "Generate a 'pinterest_title' which is a catchy, short version of the article title for a Pinterest Pin overlay, perfect to advertise listicles or fashion/style guide articles.",
    ),
    (
        "(e.g., '8 Soft Vibe Circular Mirror Ideas' or 'Ultimate Pond Fish Guide').",
        "(e.g., '9 Chic Capsule Wardrobe Ideas', or 'Mediterranean Summer Look Guide').",
    ),
    (
        "IMPORTANT: If you add products lists or design elements lists, always use 'Heading phrase:' before such lists, but if you add tips lists I thinks you can skip 'Heading phrase:'.",
        "IMPORTANT: If you add product lists or style/beauty element lists, always use 'Heading phrase:' before such lists, but if you add tips lists I thinks you can skip 'Heading phrase:'.",
    ),
    (
        "Don't just drop a list like 'UV-resistant navy finish; Whiskey barrel style; Lightweight construction' without a proper heading or introductory sentence with ':' at the end.",
        "Don't just drop a list like 'cropped tweed jacket; high-rise wide-leg trousers; kitten-heel slingbacks' without a proper heading or introductory sentence with ':' at the end.",
    ),
    (
        "For each section of the article, I'd like you to create ONE vertical realistic image prompt that reflects the meaning of the section. Also give me a prompt to generate ONE featured image that reflects the whole article and will be used as the thumbnail/cover. IMPORTANT: this featured image must be WIDE HORIZONTAL LANDSCAPE 2:1 (2x1) banner, ONE monolithic seamless scene, and MUST NOT be a collage/diptych/triptych/split-screen/panels/frames/borders, and MUST look like a single seamless photo. Do NOT use the words 'vertical' or 'portrait' in the featured image prompt. I'd like you to understand that I want to place affiliate links under these images, and I want you to immediately provide me with a list of products related to the image (there are similar products in the image) that I can search on Amazon and select the right products for each section of the blog post. I'd like at least three or six product phrases so that I can easily search on Amazon for the blog section image and add about four products from Amazon based on these phrases. The first section of the article should be dedicated to something similar to the pin I gave and described above, but Please don't mention anything about Pinterest or that anyone came from Pinterest to this article or from anywhere else, or that the reader saw that pin before, dont need it. It is also important that you do not generate the images yourself under any circumstances. I only need you to know what is in the image and give the prompts for their generation, and that's all. The article should be self-contained and not too promotional. Also, after the title but before the large image of the post and the introduction, my blog post should have a small annotation (exerpt or, I don't know, a preamble) that briefly describes what's in the article to intrigue readers. It should be a maximum of 200 characters.\n",
        "For each section of the article, I'd like you to create ONE vertical realistic image prompt that reflects the meaning of the section. Also give me a prompt to generate ONE featured image that reflects the whole article and will be used as the thumbnail/cover. For fashion articles, this featured image can be either one stylish fashion/lifestyle scene or a tasteful editorial collage if that suits the concept better. A beautiful fashionable person or model is also completely fine. Prefer a 4:5 fashion/lifestyle cover image. Keep it realistic, polished, and visually striking. Do NOT use the words 'vertical' or 'portrait' in the featured image prompt. I'd like you to understand that I want to place affiliate links under these images, and I want you to immediately provide me with a list of products related to the image (there are similar products in the image) that I can search on Amazon and select the right products for each section of the blog post. I'd like at least three or six product phrases so that I can easily search on Amazon for the blog section image and add about four products from Amazon based on these phrases. The first section of the article should be dedicated to something similar to the pin I gave and described above, but Please don't mention anything about Pinterest or that anyone came from Pinterest to this article or from anywhere else, or that the reader saw that pin before, dont need it. It is also important that you do not generate the images yourself under any circumstances. I only need you to know what is in the image and give the prompts for their generation, and that's all. The article should be self-contained and not too promotional. Also, after the title but before the large image of the post and the introduction, my blog post should have a small annotation (exerpt or, I don't know, a preamble) that briefly describes what's in the article to intrigue readers. It should be a maximum of 200 characters.\n",
    ),
    (
        "  \"featured_image\": \"WIDE HORIZONTAL LANDSCAPE 2:1 (2x1) banner, ONE monolithic seamless scene...\",\n",
        "  \"featured_image\": \"4:5 fashion/lifestyle cover image, stylish editorial scene or tasteful collage...\",\n",
    ),
]

DEFAULT_RECIPE_ARTICLE_TEMPLATE = (
    "Write a 1,500-word SEO article titled \"[article_title]\" that feels warm, helpful, and genuinely human. The article should read like an experienced food blogger talking to a real reader, not like a robotic recipe card or stiff magazine copy.\n\n"
    "Style & Tone Requirements:\n"
    "Conversational and natural:\n"
    "Write in a relaxed, friendly tone. Use plain, everyday language.\n"
    "Keep it human and specific. Avoid generic AI filler, empty hype, or stiff transitions.\n"
    "Personal touch:\n"
    "You can add personal opinions, small observations, or occasional lived-in commentary where it fits naturally. Do not force anecdotes into every section.\n"
    "Active voice:\n"
    "Write in a clear active voice. Keep sentences direct, readable, and useful.\n"
    "Light humor:\n"
    "A little wit is welcome, but keep it subtle and never let it distract from the cooking advice.\n"
    "Formatting & Structure Requirements:\n"
    "Introduction:\n"
    "Open with a short, punchy introduction that makes the reader want the recipe or the roundup immediately. Avoid generic openers.\n"
    "Headings:\n"
    "Use clear H2 headings for major sections. Use H3 headings only when they actually help.\n"
    "Single-recipe articles:\n"
    "If the title is about one specific recipe or one dish, structure the article naturally with sections such as why this recipe works, ingredients, step-by-step method, variations/substitutions, common mistakes, storage or reheating, FAQ, and a short conclusion. You do not need to force every one of these headings if the topic does not need it, but this is the preferred direction.\n"
    "Recipe listicles / roundups:\n"
    "If the title is clearly a listicle or roundup, keep each recipe/item noticeably shorter than a full single-recipe guide. Give enough detail to be useful, but do not turn every list item into a giant recipe card.\n"
    "Lists are welcome:\n"
    "Ingredient lists, step lists, tips, substitutions, and quick notes are all welcome when useful. It is completely fine here if large parts of the article are plain text, bullet points, numbered steps, or FAQ answers.\n"
    "Visual rhythm:\n"
    "Do not make every section the same size. Let ingredients and step-by-step sections breathe if needed, and let practical sections like storage or FAQ stay tighter.\n"
    "SEO & Content Requirements:\n"
    "Naturally include relevant keywords related to \"[article_title]\" without sounding stuffed.\n"
    "Generate a shortened, SEO-friendly 'url_slug' for the article. It should be based on the title, use hyphens, be lowercase, and stay reasonably compact.\n"
    "Generate a 'pinterest_title' that is short, catchy, and suitable for a recipe pin or food roundup overlay. Keep it concise and natural. Example styles: 'Best Crispy Chicken Bites' or '7 Easy Pasta Dinners'.\n"
    "Generate concise, SEO-aware alt text for the featured image and for any section images that you actually include.\n"
    "Image Prompt Requirements:\n"
    "Generate ONE featured image prompt that reflects the whole article. Keep it simple: make it a horizontal 4:3 food cover image where the dish is clearly visible, preferably fairly close to the camera, and looks realistic and appetizing.\n"
    "For section-level images, do NOT force images into every section. Only add image prompts in sections that genuinely benefit from visuals, such as a finished dish reveal, ingredient overview, a key preparation moment, or a serving scene. But it would be good if a single recipe article had at least 3 pictures (3 is the best amount I think), because two or one is somehow not enough.\n"
    "If a section needs an image, include only 'prompt1' and 'alt1' for that section.\n"
    "For sections that do not need images, omit 'prompt1', 'alt1', and 'amazon_search_phrases' entirely.\n"
    "If you include 'amazon_search_phrases', make them practical search phrases for related kitchen tools, serveware, cookware, bakeware, storage items, or other clearly relevant products for that section.\n"
    "Do not generate the images themselves under any circumstances. I only need the prompts.\n"
    "After the title but before the featured image and introduction, include a short excerpt/preamble that briefly teases the article in no more than 200 characters.\n"
    "Detailed Writing Instructions:\n"
    "If this is a single recipe article, prioritize clarity and usefulness over artificial symmetry. The ingredients and method sections can be longer. FAQ answers can be short. Storage guidance should be practical.\n"
    "If this is a listicle, keep momentum high. Each item should be concise, distinct, and appealing, with enough context for the reader to understand why it belongs.\n"
    "Avoid filler phrases like 'dive into' or generic cliches. Every paragraph should earn its place.\n"
    "Conclusion:\n"
    "End with a concise, satisfying wrap-up. If you ask a question, make it feel natural and grounded.\n\n"
    "OUTPUT FORMAT (follow strictly):\n"
    "- Return ONLY valid JSON (no markdown fences, no commentary outside the JSON).\n"
    "- Do NOT include HTML tags like <h1>, <h2>, etc.\n"
    "- Headings must be provided only via JSON fields.\n"
    "- Use EXACT key names from the schema below.\n"
    "- If you include lists inside any \"text\" fields, format them as Markdown lists using '- ' or '1. '.\n\n"
    "The JSON schema must be:\n"
    "{\n"
    "  \"excerpt\": \"...<=200 chars...\",\n"
    "  \"category\": \"...chosen-category-slug...\",\n"
    "  \"url_slug\": \"...shortened-seo-url-slug...\",\n"
    "  \"pinterest_title\": \"...short catchy food overlay title...\",\n"
    "  \"featured_image\": \"appetizing horizontal 4:3 food cover image with the dish clearly visible...\",\n"
    "  \"featured_image_alt\": \"...SEO optimized alt text for featured image...\",\n"
    "  \"title\": \"...article title...\",\n"
    "  \"introduction\": \"...intro text...\",\n"
    "  \"sections\": [\n"
    "    {\n"
    "      \"h2\": \"...section heading...\",\n"
    "      \"text\": \"...section text...\"\n"
    "    }\n"
    "  ],\n"
    "  \"conclusion_heading\": \"Conclusion\",\n"
    "  \"conclusion\": \"...conclusion text...\"\n"
    "}\n"
    "Optional section-level image fields: in sections that truly need visuals, you may additionally include 'prompt1', 'alt1', and optional 'amazon_search_phrases'. Omit those keys entirely in non-visual sections.\n"
)


def _normalize_article_theme(theme: str | None) -> str:
    t = str(theme or "").strip().lower()
    if t in ARTICLE_THEME_LABELS:
        return t
    return "decor"


def _strip_existing_category_instruction(text: str | None) -> str:
    s = str(text or "")
    if not s.strip():
        return s

    lines = s.splitlines()
    cleaned: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == "CATEGORY (follow strictly):":
            i += 1
            while i < len(lines):
                stripped = lines[i].strip()
                if not stripped:
                    i += 1
                    while i < len(lines) and not lines[i].strip():
                        i += 1
                    break
                if stripped.startswith("-"):
                    i += 1
                    continue
                break
            continue
        cleaned.append(lines[i])
        i += 1

    return "\n".join(cleaned).strip()


def _resolve_tab0_target_domain(theme: str | None = None) -> str:
    normalized_theme = _normalize_article_theme(theme or st.session_state.get("unif_text_theme"))

    # For non-decor article themes, the target site is unambiguous.
    # Do NOT let Gutenberg/uploads defaults override the article taxonomy.
    if normalized_theme == "fashion":
        return "glowuproutine.com"
    if normalized_theme == "recipes":
        return "sweethomecookery.com"

    domain = str(st.session_state.get("unif_tab0_uploads_domain") or "").strip().lower()
    if domain in KNOWN_WP_DOMAINS:
        return domain

    base_url = str(st.session_state.get("unif_tab0_uploads_base_url") or "").strip()
    if base_url:
        try:
            import urllib.parse as _urlparse
            host = _urlparse.urlparse(base_url if "://" in base_url else "https://" + base_url).netloc.lower()
            if host in KNOWN_WP_DOMAINS:
                return host
        except Exception:
            pass
    for known_domain, known_theme in SITE_THEME_BY_DOMAIN.items():
        if known_theme == normalized_theme:
            return known_domain

    return ""


def _replace_many(text: str, replacements: list[tuple[str, str]]) -> str:
    out = str(text or "")
    for old, new in replacements:
        out = out.replace(old, new)
    return out


def _build_fashion_template_from_decor(template_text: str) -> str:
    return _replace_many(template_text, FASHION_TEMPLATE_REPLACEMENTS)


def _recipe_pin_intro_instruction(*, prompts_per_section: int = 2) -> str:
    return (
        "Use the attached reference image as inspiration for the first section of the article that comes after the introduction. "
        "This section should immediately grab the reader's attention with text that is relevant to the context of the reference image. "
        "Do NOT mention the reference image, any platform, or where the reader came from. "
        "Make sure the first food image prompt in that section feels especially appetizing, realistic, and coherent with the section text."
    )


def _get_pin_intro_instruction(theme: str, *, prompts_per_section: int = 2) -> str:
    theme = _normalize_article_theme(theme)
    if theme == "recipes":
        return _recipe_pin_intro_instruction(prompts_per_section=prompts_per_section)
    return (
        "Use the attached reference image as inspiration for the first section of the article that comes after the introduction. "
        "This section should immediately grab the reader's attention with text that is relevant to the context of the reference image. "
        "Do NOT mention the reference image, any platform, or where the reader came from. "
        "Make sure the first section's image prompt is especially scroll-stopping: one strong establishing/hero shot, realistic and coherent with the section text. "
        "Try to provide more details in your generated prompt to ensure the image looks creative rather than like generic stock photos."
    )


def _get_first_section_hook_instruction(theme: str, *, prompts_per_section: int = 2) -> str:
    theme = _normalize_article_theme(theme)

    if theme == "fashion":
        return (
            "For the first image prompt of the first section of the article that comes after the introduction: make it a powerful hook that instantly grabs attention. The image should feel stylish, realistic, and visually rich without fake-looking effects. "
            "Ensure this first section image prompt is the most scroll-stopping in the whole article: think a strong fashion/lifestyle hero scene with clear styling details, realistic textures, and no text/logos/watermarks. "
            "Try to provide more details in your generated prompt to ensure the image looks creative rather than like generic stock photos."
        )

    if theme == "recipes":
        return (
            "For the first food image prompt in the first section that comes after the introduction, make it the strongest visual hook in the article. "
            "Aim for an appetizing, realistic hero scene that instantly makes the recipe feel worth trying, with no text/logos/watermarks."
        )

    return (
        "For the first image prompt of the first section of the article that comes after the introduction: make it a powerful hook that instantly grabs attention. This image should be really special, with creative elements, but still very realistic without unrealistic ultra-bright sun rays or similar fake-looking details. "
        "Ensure this first section image prompt is the most scroll-stopping in the whole article: a beautiful, highly realistic, creative full interior/lifestyle moment with strong composition and details. "
        "The image must be realistic, cohesive with the section text, and contain no text/logos/watermarks. "
        "Try to provide more details in your generated prompt to ensure the image looks creative rather than like generic stock photos."
    )


def _get_image_prompt_count_override(theme: str, *, prompts_per_section: int = 2) -> str:
    theme = _normalize_article_theme(theme)
    if theme == "recipes":
        return (
            "\n\nIMAGE COUNT OVERRIDE (higher priority than any earlier wording):\n"
            "- When a recipe section includes an image, generate ONLY one section image prompt.\n"
            "- Use only the keys \"prompt1\" and \"alt1\" for that section.\n"
            "- Do NOT output \"prompt2\" or \"alt2\".\n"
        )
    return (
        "\n\nIMAGE COUNT OVERRIDE (higher priority than any earlier wording):\n"
        "- Generate ONLY one section image prompt per section.\n"
        "- Use only the keys \"prompt1\" and \"alt1\" for section images.\n"
        "- Do NOT output \"prompt2\" or \"alt2\".\n"
    )


def _sanitize_prompt(text: str) -> str:
    """Remove known garbage tails that sometimes get appended to prompts.

    User report: prompt ends with fragments like:
      "from Amazon based on these phrafor being there.\nComparative and Opinion-Ba"

    We keep this very conservative: only remove when we detect these exact markers.
    """

    import re as _re

    s = "" if text is None else str(text)

    # Normalize whitespace a bit so we can match across line breaks.
    s_norm = s

    # If the garbage marker exists, drop everything from it to the end.
    markers = [
        r"\bfrom\s+Amazon\s+based\s+on\s+these\b",
        r"\bComparative\s+and\s+Opinion\-?Ba\b",
        r"\bphrafor\b",
    ]

    # Find earliest marker occurrence (case-insensitive) and truncate.
    cut_at = None
    for m in markers:
        try:
            mm = _re.search(m, s_norm, flags=_re.IGNORECASE)
            if mm:
                cut_at = mm.start() if cut_at is None else min(cut_at, mm.start())
        except Exception:
            continue

    if cut_at is not None and cut_at >= 0:
        s = s[:cut_at].rstrip()

    return s.strip()


def _build_article_prompt_from_title(
    title: str,
    template: str,
    *,
    theme: str | None = None,
    target_domain: str | None = None,
    include_pin_intro_instruction: bool = False,
    prompts_per_section: int | None = None,
    internal_links_block: str | None = None,
) -> str:
    """Build the final prompt for article generation.

    We primarily keep the prompt text "as is" and only substitute the article title.

    Supported placeholders:
    - `[article_title]` (preferred, as in the provided prompt)
    - `{title}` (legacy)

    If `include_pin_intro_instruction=True`, we append an extra instruction to the prompt
    (used when we also attach a per-title "pin" image).
    """
    import re as _re

    t = (title or "").strip()
    tpl = _strip_existing_category_instruction((template or "").strip())
    if not tpl:
        tpl = "Write an article titled \"[article_title]\""

    # Safety: if the user ever pasted an old generated prompt back into the template,
    # we don't want the (pin) instruction to leak into text-only runs.
    if not include_pin_intro_instruction and tpl:
        leak_patterns = [
            # Legacy leaked instruction (older versions) / user-pasted instructions.
            # Make matching robust to punctuation, line breaks, and optional Pinterest mentions.
            # We only remove the pin-related instruction sentences/paragraphs.
            # IMPORTANT: do NOT remove whole paragraphs.
            # If the user pasted the pin-block in the middle of a long paragraph, "remove until blank line"
            # can accidentally delete unrelated useful instructions that follow (length limits, image placement, etc.).
            # We therefore remove ONLY the pin/Pinterest-related sentences/segments.
            r"The first section of the article should be dedicated.*?dont need it\.?\s*",
            r"The first section of the article should be dedicated.*?do not generate the images yourself under any circumstances\.?\s*",
            r"The first section of the article should be dedicated[^\n\r]*?pin I gave[^\n\r]*?(?:\.|$)\s*",
            r"The first section of the article should be dedicated[^\n\r]*?\bpin\b[^\n\r]*?(?:\.|$)\s*",
            r"Please\s+dont\s+mention\s+anything\s+about\s+Pinterest.*?(?:\.|$)\s*",
            r"dont\s+mention\s+anything\s+about\s+Pinterest.*?(?:\.|$)\s*",
            # Current pin-mode instruction text (in case user pasted it into the template)
            r"Use the attached reference image as inspiration for the first section of the article that comes after the introduction\..*?coherent with the section(?:'|\u2019)s text\.?\s*",
        ]
        for pat in leak_patterns:
            try:
                tpl = _re.sub(pat, "", tpl, flags=_re.IGNORECASE | _re.DOTALL).strip()
            except Exception:
                pass

    pps = None
    try:
        if prompts_per_section is not None:
            pps = int(prompts_per_section)
    except Exception:
        pps = None
    if pps is None:
        try:
            pps = int(st.session_state.get("unif_text_prompts_per_section") or 2)
        except Exception:
            pps = 2

    theme = _normalize_article_theme(theme or st.session_state.get("unif_text_theme"))

    out = tpl.replace("[article_title]", t)
    out = out.replace("{title}", t)

    if include_pin_intro_instruction:
        # Append as a separate paragraph to reduce the chance of breaking user-provided templates.
        pin_instruction = _get_pin_intro_instruction(theme, prompts_per_section=pps)
        out = (out.rstrip() + "\n\n" + pin_instruction.strip()).strip()
    else:
        # When there is no attached per-title image, still enforce a strong first-section hook.
        hook_instruction = _get_first_section_hook_instruction(theme, prompts_per_section=pps)
        if hook_instruction.strip() not in out:
            out = (out.rstrip() + "\n\n" + hook_instruction.strip()).strip()

    image_count_override = _get_image_prompt_count_override(theme, prompts_per_section=pps)
    if image_count_override.strip():
        out = (out.rstrip() + image_count_override).strip()

    out = _strip_existing_category_instruction(out)

    # --- Category selection (STRICT) ---
    # We add this as an extra instruction so we don't have to rewrite user templates.

    # Category slugs depend on the target WP site (domain), not on images-per-section.
    _domain = str(target_domain or "").strip().lower() or _resolve_tab0_target_domain(theme)
    _site_rule = SITE_CATEGORY_RULES.get(_domain)

    if _site_rule:
        allowed = str(_site_rule.get("allowed") or "").strip()
        rule = str(_site_rule.get("rule") or "").strip()
    else:
        # Default/fallback: nestingmuse.com taxonomy for known decor sites,
        # otherwise keep the instruction generic and theme-aware.
        if not _domain and theme == "decor":
            _site_rule = SITE_CATEGORY_RULES.get("nestingmuse.com") or {}
            allowed = str(_site_rule.get("allowed") or "").strip()
            rule = str(_site_rule.get("rule") or "").strip()
            category_instruction = (
                "\n\nCATEGORY (follow strictly):\n"
                f"- Add top-level JSON field \"category\".\n"
                f"- Allowed values (choose EXACTLY one, do not invent new): {allowed}.\n"
                f"- {rule}\n"
                "- IMPORTANT: Output the category slug EXACTLY as written (case-sensitive, hyphens).\n"
            )
        else:
            preferred_slug = "style-guides" if theme == "fashion" else ("main-courses" if theme == "recipes" else "interiors")
            category_instruction = (
                "\n\nCATEGORY (follow strictly):\n"
                "- Add top-level JSON field \"category\".\n"
                "- Use a short WordPress-style slug that matches the target site's existing taxonomy.\n"
                f"- If the site uses one broad category for this topic, prefer a simple slug like '{preferred_slug}'.\n"
                "- IMPORTANT: Use lowercase letters and hyphens only.\n"
            )
            allowed = ""
            rule = ""

    if allowed:
        category_instruction = (
            "\n\nCATEGORY (follow strictly):\n"
            f"- Add top-level JSON field \"category\".\n"
            f"- Allowed values (choose EXACTLY one, do not invent new): {allowed}.\n"
            f"- {rule}\n"
            "- IMPORTANT: Output the category slug EXACTLY as written (case-sensitive, hyphens).\n"
        )

    # Also remind schema includes this key.
    out = (out.rstrip() + category_instruction).strip()

    # Optional: contextual internal linking candidates
    if internal_links_block:
        out = (out.rstrip() + "\n\n" + str(internal_links_block).strip()).strip()

    return out.strip()


# ---------------- Featured image generation helpers ----------------

FEATURED_IMAGE_BASE_PATH = "2x1.jpg"
FEATURED_IMAGE_FASHION_BASE_CANDIDATES = ("4x5.png", "4x5.jpg")
FEATURED_IMAGE_RECIPE_BASE_CANDIDATES = ("4x3.jpg", "2x1.jpg")


def _get_featured_image_mode(theme: str | None = None, target_domain: str | None = None) -> dict:
    theme_norm = _normalize_article_theme(theme)
    domain = str(target_domain or "").strip().lower()

    if theme_norm == "fashion" or domain == "glowuproutine.com":
        base_path = next((p for p in FEATURED_IMAGE_FASHION_BASE_CANDIDATES if os.path.exists(p)), FEATURED_IMAGE_FASHION_BASE_CANDIDATES[-1])
        return {
            "kind": "fashion",
            "base_path": base_path,
            "base_name": os.path.basename(base_path),
            "ratio_label": "4:5",
            "ratio_button": "4x5",
        }

    if theme_norm == "recipes" or domain == "sweethomecookery.com":
        base_path = next((p for p in FEATURED_IMAGE_RECIPE_BASE_CANDIDATES if os.path.exists(p)), FEATURED_IMAGE_RECIPE_BASE_CANDIDATES[-1])
        return {
            "kind": "recipes",
            "base_path": base_path,
            "base_name": os.path.basename(base_path),
            "ratio_label": "4:3",
            "ratio_button": "4x3",
        }

    return {
        "kind": "decor",
        "base_path": FEATURED_IMAGE_BASE_PATH,
        "base_name": os.path.basename(FEATURED_IMAGE_BASE_PATH),
        "ratio_label": "2:1",
        "ratio_button": "2x1",
    }


def _normalize_featured_image_prompt(
    featured_prompt: str | None,
    *,
    theme: str | None = None,
    target_domain: str | None = None,
) -> str:
    import re as _re

    s = str(featured_prompt or "").strip()
    if not s:
        return ""

    effective_theme = _normalize_article_theme(theme or st.session_state.get("unif_text_theme"))
    effective_target_domain = str(target_domain or "").strip().lower() or _resolve_tab0_target_domain(effective_theme)
    kind = str(_get_featured_image_mode(effective_theme, effective_target_domain).get("kind") or "decor")

    # Remove frequent legacy prefixes so theme-specific rules can be reapplied cleanly.
    s = _re.sub(r"^\s*SCENE PROMPT:\s*", "", s, flags=_re.IGNORECASE)
    legacy_prefix_patterns = [
        r"^\s*WIDE HORIZONTAL(?: LANDSCAPE)? 2:1(?:\s*\(?2x1\)?)?\s*banner[,.:]?\s*",
        r"^\s*(?:ONE\s+)?monolithic seamless(?: single)? scene[,.:]?\s*",
        r"^\s*(?:and\s*)?(?:MUST NOT be a |no )collage(?:/diptych/triptych/split-screen/panels/frames/borders|/split-screen/panels/frames/borders)?[,.:]?\s*",
        r"^\s*(?:and\s*)?MUST look like a single seamless photo\.?\s*",
    ]
    for _ in range(3):
        before = s
        for pattern in legacy_prefix_patterns:
            s = _re.sub(pattern, "", s, flags=_re.IGNORECASE)
        s = _re.sub(r"^\s*[-,;:.]+\s*", "", s).strip()
        if s == before:
            break

    if kind == "fashion":
        # Bring older fashion prompts in line with the 4:5 cover format.
        s = _re.sub(r"\b4\s*:\s*3\b", "4:5", s, flags=_re.IGNORECASE)
        s = _re.sub(r"\b4x3\b", "4x5", s, flags=_re.IGNORECASE)
        s = _re.sub(r"^\s*(?:horizontal|landscape)\s+", "", s, flags=_re.IGNORECASE).strip()

        must_add = "4:5 fashion/lifestyle cover image"
        if "4:5" not in s and "4x5" not in s.lower():
            return f"{must_add}. {s}".strip()
        if not _re.search(r"\b4:5\b|\b4x5\b|\bcover\b", s, flags=_re.IGNORECASE):
            return f"{must_add}. {s}".strip()
        return s

    # Remove common wrong orientation cues for non-fashion featured images.
    s = _re.sub(r"\bvertical\b", "horizontal", s, flags=_re.IGNORECASE)
    s = _re.sub(r"\bportrait\b", "landscape", s, flags=_re.IGNORECASE)

    if kind == "recipes":
        must_add = "appetizing horizontal 4:3 food cover image with the dish clearly visible, preferably fairly close to the camera"
        if "4:3" not in s and "4x3" not in s.lower():
            return f"{must_add}. {s}".strip()
        if not _re.search(r"\bhorizontal\b|\blandscape\b|\bwide\b|\bcover\b", s, flags=_re.IGNORECASE):
            return f"{must_add}. {s}".strip()
        return s

    must_add = "WIDE HORIZONTAL 2:1 banner, monolithic seamless single scene, no collage/split-screen/panels/frames/borders"
    if "2:1" not in s and "2x1" not in s.lower():
        return f"{must_add}. {s}".strip()
    if not _re.search(r"\bhorizontal\b|\blandscape\b|\bwide\b", s, flags=_re.IGNORECASE):
        return f"{must_add}. {s}".strip()
    return s


def _build_featured_image_full_prompt(
    featured_prompt: str,
    *,
    theme: str | None = None,
    target_domain: str | None = None,
) -> str:
    spec = _get_featured_image_mode(theme, target_domain)
    scene_prompt = _normalize_featured_image_prompt(
        featured_prompt,
        theme=theme,
        target_domain=target_domain,
    )
    kind = str(spec.get("kind") or "decor")

    if kind == "fashion":
        return (
            "Generate one polished fashion/lifestyle cover image in a 4:5 aspect ratio using this prompt.\n\n"
            f"SCENE PROMPT: {scene_prompt}\n\n"
            "HARD REQUIREMENTS (must follow exactly):\n"
            "- Keep the final image in a 4:5 aspect ratio.\n"
            "- Fill the entire 4:5 frame; no blank/white space anywhere.\n"
            "- Keep the result realistic, stylish, and suitable for a fashion article cover.\n"
            "- No text, no logos, no watermarks."
        )

    if kind == "recipes":
        return (
            "Generate one appetizing horizontal food cover image in a 4:3 aspect ratio using this prompt.\n\n"
            f"SCENE PROMPT: {scene_prompt}\n\n"
            "HARD REQUIREMENTS (must follow exactly):\n"
            "- Keep the final image in a horizontal 4:3 aspect ratio.\n"
            "- Fill the entire 4:3 frame; no blank/white space anywhere.\n"
            "- Make the main dish clearly visible, preferably fairly close to the camera.\n"
            "- Keep it realistic, appetizing, and natural-looking.\n"
            "- A simple, clean food/lifestyle composition is perfect.\n"
            "- Avoid text, logos, watermarks, or awkward obvious panel layouts.\n"
            "- Match lighting and color across the whole image for a polished result."
        )

    return (
        "Generate one wide horizontal featured image in a 2:1 landscape aspect ratio using this prompt.\n\n"
        f"SCENE PROMPT: {scene_prompt}\n\n"
        "HARD REQUIREMENTS (must follow exactly):\n"
        "- Keep the final image in a 2:1 horizontal aspect ratio (wide landscape banner).\n"
        "- Fill the entire 2:1 frame; no blank/white space anywhere.\n"
        "- Output must be monolithic and seamless: ONE continuous scene only (single frame).\n"
        "- ABSOLUTELY NO collage/diptych/triptych/split-screen/panels/frames/borders/multiple images.\n"
        "- ABSOLUTELY NO seams: no vertical divider, no horizontal divider, no center split, no gutter, no different scenes on left/right halves.\n"
        "- Do NOT mirror/duplicate the scene across halves. Do NOT make left/right variations.\n"
        "- Avoid any composition that looks like two separate photos stitched together.\n"
        "- No text, no logos, no watermarks.\n"
        "- Match lighting, perspective, and color across the entire image for a natural seamless look."
    )


def _resolve_local_asset_path(path: str | None) -> str:
    raw = str(path or "").strip()
    if not raw:
        return raw

    p = Path(raw).expanduser()
    if p.exists():
        try:
            return str(p.resolve())
        except Exception:
            return str(p)

    try:
        here = Path(__file__).resolve().parent
        p2 = here / raw
        if p2.exists():
            return str(p2.resolve())
    except Exception:
        pass

    return raw


def _featured_attach_image(page, image_path: str) -> bool:
    """Attach the featured base image in classic Gemini.

    The shared helper is kept first. This local fallback is intentionally scoped
    to the optional legacy Featured Images base-upload mode. The default
    automatic flow now matches the bulk/pipeline text-only image generation path.
    """

    try:
        if _attach_image(page, image_path):
            return True
    except Exception:
        pass

    upload_ru = "\u0437\u0430\u0433\u0440\u0443\u0437"
    file_ru = "\u0444\u0430\u0439\u043b"
    attach_ru = "\u043f\u0440\u0438\u043a\u0440\u0435\u043f"
    upload_files_ru = "\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u0444\u0430\u0439\u043b\u044b"
    upload_file_ru = "\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u0444\u0430\u0439\u043b"

    def _set_any_file_input() -> bool:
        selectors = [
            "input[type='file'][accept*='image']",
            "input[type='file']",
        ]
        for sel in selectors:
            try:
                inputs = page.query_selector_all(sel)
            except Exception:
                inputs = []
            for inp in inputs:
                try:
                    inp.set_input_files(image_path)
                    return True
                except Exception:
                    continue
        return False

    if _set_any_file_input():
        return True

    button_selectors = [
        "button[aria-label='Open file upload menu']",
        "button[aria-label*='upload' i]",
        "button[aria-label*='attach' i]",
        "button[aria-label*='file' i]",
        "button[aria-label*='image' i]",
        f"button[aria-label*='{upload_ru}' i]",
        f"button[aria-label*='{file_ru}' i]",
        f"button[aria-label*='{attach_ru}' i]",
        "button:has(mat-icon:has-text('add'))",
        "button:has(mat-icon:has-text('attach_file'))",
        "button:has(mat-icon:has-text('add_photo_alternate'))",
        ".upload-card-button.open",
    ]
    menu_selectors = [
        f"button:has-text('{upload_files_ru}')",
        f"button:has-text('{upload_file_ru}')",
        f"[role='menuitem']:has-text('{upload_files_ru}')",
        f"[role='menuitem']:has-text('{upload_file_ru}')",
        "button:has-text('Upload files')",
        "button:has-text('Upload file')",
        "[role='menuitem']:has-text('Upload files')",
        "[role='menuitem']:has-text('Upload file')",
        ".mat-mdc-menu-item:has-text('Upload')",
    ]

    def _click_upload_menu_item() -> bool:
        if _set_any_file_input():
            return True
        for sel in menu_selectors:
            try:
                loc = page.locator(sel).first
                if not loc or loc.count() == 0:
                    continue
                with page.expect_file_chooser(timeout=2500) as fc_info:
                    loc.click(force=True, timeout=1500)
                fc_info.value.set_files(image_path)
                return True
            except Exception:
                if _set_any_file_input():
                    return True
                continue
        return False

    for sel in button_selectors:
        try:
            btn = page.locator(sel).first
            if not btn or btn.count() == 0:
                continue
            try:
                btn.scroll_into_view_if_needed(timeout=700)
            except Exception:
                pass
            try:
                btn.click(timeout=1200)
            except Exception:
                btn.click(force=True, timeout=1200)
            try:
                page.wait_for_timeout(250)
            except Exception:
                time.sleep(0.25)
            if _click_upload_menu_item():
                return True
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        except Exception:
            continue

    # Last pass: scan visible buttons by actual aria/text. This catches localized
    # Gemini variants without touching the shared helper used by the pipeline.
    try:
        buttons = page.query_selector_all("button")
    except Exception:
        buttons = []
    needles = (upload_ru, file_ru, attach_ru, "upload", "attach")
    for btn in buttons:
        try:
            label = ((btn.get_attribute("aria-label") or "") + " " + (btn.inner_text() or "")).lower()
            if not any(n in label for n in needles):
                continue
            btn.click(force=True)
            try:
                page.wait_for_timeout(250)
            except Exception:
                time.sleep(0.25)
            if _click_upload_menu_item():
                return True
        except Exception:
            continue

    return _set_any_file_input()


def _goto_gemini_resilient(
    page,
    url: str,
    *,
    timeout_ms: int = 120000,
    attempts: int = 3,
    allow_aistudio_fallback: bool = True,
) -> str:
    """Navigate to Gemini UI with retries and less brittle waiting.

    Why: `wait_until="load"` often hangs on Gemini (long-polling, service worker, etc.)
    and causes 30s Playwright timeouts. This function prefers `domcontentloaded`,
    uses a bigger navigation timeout. Some callers may opt into fallback from
    gemini.google.com to aistudio.google.com.

    Returns the URL that succeeded.
    """

    urls: list[str] = [url]
    if allow_aistudio_fallback and (url or "").startswith("https://gemini.google.com/"):
        urls.append("https://aistudio.google.com/app")

    last_err: Exception | None = None

    # Navigation timeout is separate from default timeout.
    try:
        page.set_default_navigation_timeout(timeout_ms)
    except Exception:
        pass

    for u in urls:
        for att in range(1, max(1, int(attempts)) + 1):
            try:
                _assert_page_alive(page)
                # domcontentloaded is usually enough for the Gemini UI shell.
                page.goto(u, wait_until="domcontentloaded")

                # Now wait for the input to be usable.
                _wait_input_ready(page, timeout_ms=min(90000, timeout_ms))
                _dismiss_overlays(page)
                _assert_page_alive(page)
                return u
            except Exception as e:
                last_err = e
                # Small backoff; Gemini can be flaky under load.
                time.sleep(0.8 + 0.6 * att)
                continue

    raise last_err or RuntimeError("Failed to open Gemini UI")


def _featured_image_worker(
    *,
    idx: int,
    title: str,
    featured_prompt: str,
    theme: str | None,
    target_domain: str | None,
    url: str,
    headless: bool,
    executable_path: str | None,
    profile_dir: str,
    model_choice: str,
    timeout_s: int = 180,
    base_dir: str,
) -> dict:
    """Worker: generate a single featured image for an article.
    
    Returns dict with: idx, title, featured_prompt, saved_path, error
    """
    result: dict = {
        "idx": idx,
        "title": title,
        "featured_prompt": featured_prompt,
        "theme": theme,
        "target_domain": target_domain,
        "saved_path": None,
        "error": None,
        "url_used": None,
    }
    
    if not featured_prompt or not featured_prompt.strip():
        result["error"] = "Empty featured_prompt"
        return result
    
    spec = _get_featured_image_mode(theme, target_domain)
    base_image_path = _resolve_local_asset_path(str(spec.get("base_path") or FEATURED_IMAGE_BASE_PATH))
    full_prompt = _build_featured_image_full_prompt(
        featured_prompt,
        theme=theme,
        target_domain=target_domain,
    )
    # Legacy example kept for reference; actual base image now depends on theme/site.
    # A high-resolution, vertical interior photography shot of a sun-drenched French Country bedroom. 
    # The focal point is a vintage sage green wardrobe with a distressed finish. 
    # The background features delicate pink floral wallpaper. Soft linen bedding and a wicker 
    # basket filled with straw hats sit nearby. Cinematic lighting, 8k resolution, hyper-realistic 
    # textures. DO NOT LEAVE BLANK WHITE SPACE, THIS IS IMPORTANT AND KEEP 2x1 RATIO. IMPORTANT: 
    # THE image must be monolithic and seamless, it should not contain two or more separate images 
    # or a collage layout. 

    lk = _get_profile_lock(profile_dir)
    acquired = False
    profile_pref_backups: list[tuple[Path, bytes]] = []
    browser_download_dir: Path | None = None

    try:
        featured_run_dir = Path(base_dir).resolve()
    except Exception:
        featured_run_dir = Path(base_dir)
    download_trace_path = featured_run_dir / "_download_debug" / f"featured_{int(idx)}.log"

    def _download_trace(message: str) -> None:
        try:
            download_trace_path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            with open(download_trace_path, "a", encoding="utf-8") as trace_file:
                trace_file.write(f"{stamp} {message}\n")
        except Exception:
            pass

    try:
        acquired = lk.acquire(timeout=max(30, int(timeout_s)))
        if not acquired:
            result["error"] = f"Profile is busy (locked): {profile_dir}"
            return result

        browser_download_dir = (
            featured_run_dir
            / "_browser_download_tmp"
            / f"featured_{int(idx)}_{time.time_ns()}"
        )
        browser_download_dir.mkdir(parents=True, exist_ok=True)
        _download_trace(f"worker download directory: {browser_download_dir}")
        profile_pref_backups, pref_notes = _set_featured_profile_download_directory_temporarily(
            profile_dir,
            str(browser_download_dir),
        )
        for pref_note in pref_notes:
            _download_trace(pref_note)
        native_watch_dirs = [str(browser_download_dir), *_featured_native_download_watch_dirs(profile_dir)]
        _download_trace(f"native watch directories: {native_watch_dirs}")

        with sync_playwright() as p:
            ctx = _launch_persistent_ctx_with_retries(
                p,
                user_data_dir=profile_dir,
                headless=headless,
                executable_path=executable_path,
                downloads_path=str(browser_download_dir),
            )
            try:
                page = ctx.new_page()
                _download_trace(_pin_featured_chrome_download_directory(page, str(browser_download_dir)))
                # Default timeout for locators/actions (keep 30s); navigation handled separately.
                page.set_default_timeout(30000)

                # Resilient navigation (Gemini often never reaches full "load")
                url_used = _goto_gemini_resilient(
                    page,
                    url,
                    timeout_ms=120000,
                    attempts=3,
                    allow_aistudio_fallback=False,
                )
                result["url_used"] = url_used

                # Start from a clean Gemini chat before doing anything else.
                # This matches the stable image pipeline flow more closely.
                try:
                    _start_new_chat(page)
                except Exception:
                    pass
                
                _wait_input_ready(page, timeout_ms=60000)
                _dismiss_overlays(page)
                
                # Pipeline Stage1/Stage3 now relies on text-only aspect-ratio prompts.
                # Keep the old uploaded-base path available only as an explicit escape hatch,
                # because img2img/upload mode is the part that most often leaves Gemini in a
                # blank generated-image placeholder for featured covers.
                use_featured_base_image = str(os.getenv("UNIF_FEATURED_ATTACH_BASE", "")).strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
                if use_featured_base_image:
                    if not os.path.exists(base_image_path):
                        result["error"] = f"Base image not found: {base_image_path}"
                        return result

                    ok = _featured_attach_image(page, base_image_path)
                    attached_preview = False
                    try:
                        attached_preview = bool(_wait_image_attached(page, timeout_ms=10000))
                    except Exception:
                        attached_preview = False
                    if not ok or not attached_preview:
                        try:
                            ok2 = _featured_attach_image(page, base_image_path)
                            ok = ok or ok2
                            attached_preview = attached_preview or bool(_wait_image_attached(page, timeout_ms=10000))
                        except Exception:
                            pass

                    if not attached_preview:
                        result["error"] = f"Failed to attach {os.path.basename(base_image_path)}"
                        return result

                    try:
                        _dismiss_overlays(page)
                        _wait_input_ready(page, timeout_ms=30000)
                    except Exception:
                        pass

                # Match pipeline order: prepare the chat first, then pick the model,
                # then insert and send the prompt.
                try:
                    gph._pick_model(page, model_choice)
                    _dismiss_overlays(page)
                    _wait_input_ready(page, timeout_ms=30000)
                except Exception:
                    pass

                time.sleep(0.2)
                
                # Type prompt and send. Featured prompts are often long, so verify
                # the Gemini editor before pressing Send.
                def _editor_len() -> int:
                    try:
                        return int(
                            page.evaluate(
                                """() => {
                                  const sels = [
                                    "div.ql-editor.textarea.new-input-ui[contenteditable='true']",
                                    "div.ql-editor[contenteditable='true']",
                                    "[contenteditable='true'][role='textbox']"
                                  ];
                                  for (const sel of sels) {
                                    const el = document.querySelector(sel);
                                    if (!el) continue;
                                    const t = (el.innerText || el.textContent || '');
                                    return (t || '').trim().length;
                                  }
                                  return 0;
                                }"""
                            )
                            or 0
                        )
                    except Exception:
                        return 0

                def _insert_featured_prompt_fallback(text: str) -> bool:
                    selectors = [
                        "div.ql-editor.textarea.new-input-ui[contenteditable='true']",
                        "div.ql-editor[contenteditable='true']",
                        "[contenteditable='true'][role='textbox']",
                    ]

                    editor = None
                    for sel in selectors:
                        try:
                            editor = page.query_selector(sel)
                        except Exception:
                            editor = None
                        if editor:
                            break
                    if not editor:
                        return False

                    try:
                        editor.click()
                    except Exception:
                        try:
                            page.evaluate("el => el.focus()", editor)
                        except Exception:
                            pass

                    try:
                        page.keyboard.press("Control+A")
                        page.keyboard.press("Delete")
                    except Exception:
                        pass

                    try:
                        # Chunked insert_text is slower than the shared helper's DOM path,
                        # but it behaves like real typing and is only used for this flaky featured flow.
                        chunk_size = 450
                        for start in range(0, len(text), chunk_size):
                            page.keyboard.insert_text(text[start:start + chunk_size])
                            try:
                                page.wait_for_timeout(35)
                            except Exception:
                                time.sleep(0.035)
                    except Exception:
                        pass

                    if _editor_len() >= max(20, int(len((text or "").strip()) * 0.75)):
                        return True

                    try:
                        return bool(
                            page.evaluate(
                                """(el, text) => {
                                  try {
                                    el.focus();
                                    const sel = window.getSelection();
                                    if (sel) {
                                      sel.removeAllRanges();
                                      const r = document.createRange();
                                      r.selectNodeContents(el);
                                      sel.addRange(r);
                                    }
                                    let ok = false;
                                    try { ok = document.execCommand('insertText', false, text); } catch(e) { ok = false; }
                                    if (!ok) {
                                      try { el.textContent = text; } catch(e) { try { el.innerText = text; } catch(e2) {} }
                                    }
                                    try { el.dispatchEvent(new InputEvent('input', { bubbles: true })); } catch(e) {
                                      try { const ev = document.createEvent('Event'); ev.initEvent('input', true, true); el.dispatchEvent(ev); } catch(e2) {}
                                    }
                                    try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch(e) {}
                                    return true;
                                  } catch(e) {
                                    return false;
                                  }
                                }""",
                                editor,
                                text,
                            )
                        )
                    except Exception:
                        return False

                exp_len = len((full_prompt or "").strip())
                _type_prompt(page, full_prompt)
                cur_len = _editor_len()
                if exp_len > 50 and cur_len < max(20, int(exp_len * 0.75)):
                    _insert_featured_prompt_fallback(full_prompt)
                    cur_len = _editor_len()
                if exp_len > 50 and cur_len < max(20, int(exp_len * 0.75)):
                    result["error"] = "Featured prompt was not inserted into Gemini input"
                    return result

                if not _click_send_no_stop(page):
                    result["error"] = "Gemini Send button was not triggered for featured prompt"
                    return result

                def _try_featured_hover_download() -> list:
                    """Featured-only fallback for Gemini controls shown only on image hover.

                    The shared downloader is intentionally not changed because the bulk
                    pipelines already use it successfully.  Some Gemini featured-image
                    cards render their Download button only after the image itself is
                    hovered, so the normal button scan can see no actionable control.
                    """
                    image_scope = None
                    for image_sel in (
                        "single-image",
                        ".attachment-container.generated-images",
                        "[data-turn-role='Model'] ms-image-chunk",
                        "[data-turn-role='Model'] img.loaded-image",
                        "img.image.animate.loaded",
                        "img.image.loaded",
                    ):
                        try:
                            loc = page.locator(image_sel)
                            count = int(loc.count() or 0)
                            if count:
                                image_scope = loc.nth(count - 1)
                                break
                        except Exception:
                            continue

                    if image_scope is None:
                        return []

                    # Gemini frequently creates the control only after hover.
                    try:
                        image_scope.scroll_into_view_if_needed(timeout=1200)
                    except Exception:
                        pass
                    try:
                        image_scope.hover(timeout=2500)
                    except Exception:
                        pass
                    try:
                        page.wait_for_timeout(700)
                    except Exception:
                        time.sleep(0.7)

                    button_selectors = (
                        "[data-test-id='download-generated-image-button'] button, "
                        "download-generated-image-button button, "
                        "button[aria-label*='Download image' i], "
                        "button[aria-label*='Download' i], "
                        "button[aria-label*='Скачать изображение' i], "
                        "button[aria-label*='Скачать в полном размере' i], "
                        "button[mattooltip*='Download' i], "
                        "button[mattooltip*='Скачать' i], "
                        "button.download-button"
                    )

                    # The control is now visible, but do not use the old direct
                    # ``expect_download`` path here. Gemini can first create a
                    # native extensionless .tmp and only later emit that event.
                    # Re-enter the shared strict downloader so it performs the
                    # same filesystem confirmation and hand-off as Tab3.
                    for root in (image_scope, page):
                        try:
                            buttons = root.locator(button_selectors)
                            if int(buttons.count() or 0) <= 0:
                                continue
                            return _wait_and_download_generated_images(
                                page,
                                ctx,
                                timeout_s=25,
                                max_images=1,
                                allow_screenshot_fallback=False,
                                request_timeout_ms=60000,
                                require_browser_download=True,
                                browser_download_dir=str(browser_download_dir),
                                native_download_dirs=native_watch_dirs,
                                download_debug_hook=_download_trace,
                                serialize_native_download_click=True,
                                native_download_confirmation_timeout_s=20.0,
                                # The image was just hovered above.  Do not
                                # spend another collection timeout trying to
                                # rediscover its response card: go straight to
                                # the real Download control and the same
                                # filesystem confirmation used by Tab1/Tab3.
                                download_only=True,
                            )
                        except Exception:
                            continue

                    return []

                def _wait_featured_images_like_pipeline() -> list:
                    # The shared downloader can return early when Gemini creates an empty
                    # generated-image placeholder. Keep reopening the collection window in
                    # the SAME chat until the real total budget is exhausted.
                    total_budget_s = max(180, int(timeout_s or 180))
                    deadline_m = time.monotonic() + float(total_budget_s)
                    best_small: list | None = None
                    # Gemini can render the featured image before its Download control is
                    # ready.  Keep a few short, explicit download-only retry windows for
                    # that case.  This is intentionally local to featured images; the
                    # shared downloader and the pipeline flows are left unchanged.
                    visible_download_retry_count = 0
                    visible_download_retry_limit = 3
                    visible_download_retry_delay_ms = 6000

                    while time.monotonic() < deadline_m:
                        remaining_s = max(1.0, deadline_m - time.monotonic())
                        # After the image is already visible, retry the Gemini UI
                        # download quickly instead of waiting through another long
                        # generation window.
                        if visible_download_retry_count:
                            wait_s = int(max(10, min(25, remaining_s)))
                        else:
                            wait_s = int(max(20, min(120, remaining_s)))
                        try:
                            got = _wait_and_download_generated_images(
                                page,
                                ctx,
                                timeout_s=wait_s,
                                max_images=1,
                                allow_screenshot_fallback=False,
                                request_timeout_ms=60000,
                                # A featured image must be confirmed as a real
                                # Chrome download before this worker may close.
                                require_browser_download=True,
                                browser_download_dir=str(browser_download_dir),
                                native_download_dirs=native_watch_dirs,
                                download_debug_hook=_download_trace,
                                serialize_native_download_click=True,
                                native_download_confirmation_timeout_s=20.0,
                            )
                        except Exception:
                            got = []

                        # The normal collector is shared with the working bulk flows.
                        # Only featured cards get this hover-triggered UI fallback.
                        if not got:
                            got = _try_featured_hover_download()

                        if got:
                            try:
                                first0 = got[0]
                                blob0 = first0[1] if isinstance(first0, tuple) else first0
                                size0 = len(blob0 or b"")
                            except Exception:
                                size0 = 0

                            # Match the proven bulk acceptance threshold. Featured covers
                            # are often more compressed than vertical generations, and a
                            # valid 25–50 KB download must not be discarded and turned
                            # into a later "No images generated" error.
                            if size0 >= 25000:
                                return got
                            if size0 >= 10000 and not best_small:
                                best_small = got

                        # A visible generated image paired with no downloaded bytes is
                        # a transient Gemini UI state. Re-enter the downloader a few
                        # times: each call re-finds the latest image and re-clicks its
                        # Download control with a fresh expect_download listener.
                        try:
                            image_visible = bool(_has_generated_images(page))
                        except Exception:
                            image_visible = False
                        if not got and image_visible and visible_download_retry_count < visible_download_retry_limit:
                            visible_download_retry_count += 1
                            try:
                                page.wait_for_timeout(visible_download_retry_delay_ms)
                            except Exception:
                                time.sleep(visible_download_retry_delay_ms / 1000.0)
                            continue

                        # A successful collection attempt, or a page without the image,
                        # starts the next normal wait cycle from scratch.
                        visible_download_retry_count = 0

                        if _gemini_stopped_response_visible(page):
                            break

                        try:
                            page.wait_for_timeout(2000)
                        except Exception:
                            time.sleep(2.0)

                    return best_small or []

                imgs = _wait_featured_images_like_pipeline()

                stopped_resend_used = False
                if not imgs and _gemini_stopped_response_visible(page):
                    stopped_resend_used = True
                    try:
                        _start_new_chat(page)
                    except Exception:
                        pass

                    _wait_input_ready(page, timeout_ms=60000)
                    _dismiss_overlays(page)

                    if use_featured_base_image:
                        ok = _featured_attach_image(page, base_image_path)
                        attached_preview = False
                        try:
                            attached_preview = bool(_wait_image_attached(page, timeout_ms=10000))
                        except Exception:
                            attached_preview = False
                        if not ok or not attached_preview:
                            try:
                                ok2 = _featured_attach_image(page, base_image_path)
                                ok = ok or ok2
                                attached_preview = attached_preview or bool(_wait_image_attached(page, timeout_ms=10000))
                            except Exception:
                                pass
                        if not ok or not attached_preview:
                            result["error"] = f"Failed to re-attach {os.path.basename(base_image_path)} after Gemini stopped"
                            return result

                        try:
                            _dismiss_overlays(page)
                            _wait_input_ready(page, timeout_ms=30000)
                        except Exception:
                            pass

                    try:
                        gph._pick_model(page, model_choice)
                        _dismiss_overlays(page)
                        _wait_input_ready(page, timeout_ms=30000)
                    except Exception:
                        pass

                    _type_prompt(page, full_prompt)
                    cur_len = _editor_len()
                    if exp_len > 50 and cur_len < max(20, int(exp_len * 0.75)):
                        _insert_featured_prompt_fallback(full_prompt)
                        cur_len = _editor_len()
                    if exp_len > 50 and cur_len < max(20, int(exp_len * 0.75)):
                        result["error"] = "Featured prompt was not inserted into Gemini input on stopped-response resend"
                        return result

                    if not _click_send_no_stop(page):
                        result["error"] = "Gemini Send button was not triggered on stopped-response resend"
                        return result

                    imgs = _wait_featured_images_like_pipeline()

                if not imgs:
                    if stopped_resend_used or _gemini_stopped_response_visible(page):
                        result["error"] = "Gemini stopped featured generation; resent once but no image was generated"
                    else:
                        result["error"] = "No images generated"
                    return result
                
                # Save the first image
                os.makedirs(base_dir, exist_ok=True)
                
                # Naming: idx_featured_<sanitized_title>.png
                import re as _re
                title_slug = _re.sub(r'[^\w\s-]', '', title.lower())
                title_slug = _re.sub(r'[\s_-]+', '_', title_slug)[:50]
                
                fname = f"{idx}_featured_{title_slug}.png"
                # Handle both bytes and (mime, bytes) tuple formats
                first_img = imgs[0]
                if isinstance(first_img, tuple):
                    # Format: (mime, bytes)
                    img_data = first_img[1]
                else:
                    # Format: bytes
                    img_data = first_img
                
                fpath = str(_write_bytes_to_new_file(Path(base_dir) / fname, img_data))
                
                result["saved_path"] = fpath
                return result
                
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        result["error"] = str(e)
        return result
    finally:
        for restore_note in _restore_featured_profile_download_directory(profile_pref_backups):
            _download_trace(restore_note)
        if browser_download_dir and result.get("saved_path"):
            try:
                shutil.rmtree(browser_download_dir, ignore_errors=True)
                _download_trace("private download directory removed after confirmed save")
            except Exception:
                pass
        if acquired:
            try:
                lk.release()
            except Exception:
                pass


def _assert_page_alive(page) -> None:
    """Fail fast if page/window was closed.

    `page.is_closed()` is not always enough (depending on how the window was closed),
    so we also do a tiny `evaluate` ping.
    """
    try:
        if page.is_closed():
            raise RuntimeError("Page closed")
    except Exception:
        # If Playwright throws here, treat as closed.
        raise RuntimeError("Page closed")

    try:
        page.evaluate("1")
    except Exception as e:
        if "closed" in str(e).lower() or "target" in str(e).lower():
            raise RuntimeError("Page closed")


def _wait_for_new_text_response(page, prev_count: int, timeout_s: int = 120) -> bool:
    """Wait until Gemini produces a new response container.

    NOTE: For text generation this can happen very early (e.g. an empty container / "thinking" state),
    so this function is only a *first* stage. Final readiness is determined by
    `_wait_for_text_generation_to_finish`.
    """
    deadline = time.time() + max(5, int(timeout_s))
    while time.time() < deadline:
        try:
            _assert_page_alive(page)
            if _count_responses(page) > int(prev_count or 0):
                return True
        except Exception as e:
            # If user closed the window manually, fail fast so caller can retry.
            if "closed" in str(e).lower():
                raise
        time.sleep(0.4)
    return False


def _wait_for_text_generation_to_finish(page, timeout_s: int = 180) -> bool:
    """Wait until Gemini finishes generating text.

    Primary signal: the Stop button disappears.
    Your UI has `aria-label="Остановить генерацию ответа"` while generation is in progress.

    Fallback signal: last response text stops changing for a short stability window.
    """
    timeout_s = max(10, int(timeout_s))
    deadline = time.time() + timeout_s

    stop_locators = [
        'button.send-button.stop[aria-label="Остановить генерацию ответа"]',
        'button[aria-label="Остановить генерацию ответа"]',
        'button.send-button.stop[aria-label="Stop generating response"]',
        'button[aria-label="Stop generating response"]',
    ]

    def _stop_visible() -> bool:
        for sel in stop_locators:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    # Fast path: if stop is visible, wait until it disappears.
    # If we never see Stop at all (some UIs / fast generations), don't wait the full timeout,
    # go to text-stability fallback quickly.
    saw_stop = False
    observe_stop_until = time.time() + 3.0

    while time.time() < deadline:
        try:
            _assert_page_alive(page)

            if _stop_visible():
                saw_stop = True
                time.sleep(0.35)
                continue

            if saw_stop:
                # Stop button is gone => generation likely finished
                time.sleep(0.8)
                return True

            if time.time() > observe_stop_until:
                break
        except Exception as e:
            if "closed" in str(e).lower():
                raise
        time.sleep(0.35)

    # Fallback: stability check (text stops changing)
    stable_for_s = 2.5
    last_txt = None
    last_change = time.time()
    deadline2 = time.time() + 30
    while time.time() < deadline2:
        try:
            _assert_page_alive(page)
            txt = _extract_last_response_text(page, prefer_copy_button=False)
        except Exception as e:
            if "closed" in str(e).lower():
                raise
            txt = ""
        if txt != last_txt:
            last_txt = txt
            last_change = time.time()
        else:
            if (time.time() - last_change) >= stable_for_s and (txt or "").strip():
                return True
        time.sleep(0.4)

    return False


def _grant_clipboard_permissions(page) -> None:
    """Best-effort clipboard permission grant for the current page origin."""
    try:
        from urllib.parse import urlsplit as _urlsplit

        u = str(getattr(page, "url", "") or "").strip()
        if not u:
            page.context.grant_permissions(["clipboard-read", "clipboard-write"])
            return
        p = _urlsplit(u)
        if p.scheme and p.netloc:
            page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=f"{p.scheme}://{p.netloc}")
        else:
            page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    except Exception:
        pass


def _read_browser_clipboard_text(page) -> str:
    """Read clipboard text via the page context when permissions allow it."""
    try:
        _grant_clipboard_permissions(page)
        txt = page.evaluate(
            """async () => {
              try {
                if (!navigator.clipboard || !navigator.clipboard.readText) return '';
                return await navigator.clipboard.readText();
              } catch (e) {
                return '';
              }
            }"""
        )
        return str(txt or "")
    except Exception:
        return ""


def _read_windows_clipboard_text() -> str:
    """Read text from the Windows clipboard as a fallback."""
    try:
        if platform.system() != "Windows":
            return ""
    except Exception:
        return ""

    try:
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        if cp.returncode != 0:
            return ""
        return str(cp.stdout or "")
    except Exception:
        return ""


def _is_probably_model_response_text(text: str) -> bool:
    """Heuristic filter for clipboard payloads copied from Gemini."""
    t = str(text or "").strip()
    if not t:
        return False
    if len(t) >= 120 and not _looks_like_ui_noise(t):
        return True

    strong_markers = (
        "<a ",
        "</a>",
        "```json",
        "\"sections\"",
        "\"title\"",
        "\"excerpt\"",
        "\n## ",
        "\n# ",
    )
    tl = t.lower()
    if any(m in tl for m in strong_markers):
        return True
    if t.startswith("{") or t.startswith("["):
        return True
    return False


def _find_last_response_copy_button(page):
    """Locate the most likely 'Copy response' button for the latest Gemini answer."""
    response_selectors = [
        "message-content",
        ".message-content",
        ".model-response-text",
        ".response-content",
        ".presented-response-container",
        ".response-container-content",
        "structured-content-container",
        ".response-container",
        "model-response",
    ]
    button_selectors = [
        "copy-button button[mattooltip*='ответ']",
        "copy-button button[mattooltip*='response' i]",
        "button[data-test-id='copy-button'][mattooltip*='ответ']",
        "button[data-test-id='copy-button'][mattooltip*='response' i]",
        "copy-button button[data-test-id='copy-button']",
        "button[data-test-id='copy-button']",
        "copy-button button[aria-label*='Копировать']",
        "copy-button button[aria-label*='Copy' i]",
    ]

    for sel in response_selectors:
        try:
            containers = page.locator(sel)
            cnt = containers.count()
        except Exception:
            cnt = 0

        for idx in range(cnt - 1, -1, -1):
            try:
                container = containers.nth(idx)
                for bsel in button_selectors:
                    btns = container.locator(bsel)
                    bcnt = btns.count()
                    for j in range(bcnt - 1, -1, -1):
                        btn = btns.nth(j)
                        try:
                            if btn.is_visible():
                                return btn
                        except Exception:
                            continue
            except Exception:
                continue

    for bsel in button_selectors:
        try:
            btns = page.locator(bsel)
            bcnt = btns.count()
            for j in range(bcnt - 1, -1, -1):
                btn = btns.nth(j)
                try:
                    if btn.is_visible():
                        return btn
                except Exception:
                    continue
        except Exception:
            continue

    return None


def _extract_last_response_text_via_copy_button(page, timeout_ms: int = 4000) -> str:
    """Use Gemini's own Copy button so we get the serialized response, not visual DOM text."""
    try:
        _assert_page_alive(page)
    except Exception:
        return ""

    btn = _find_last_response_copy_button(page)
    if btn is None:
        return ""

    timeout_s = max(1.5, float(timeout_ms) / 1000.0)
    with _CLIPBOARD_READ_LOCK:
        before_browser = _read_browser_clipboard_text(page)
        before_windows = _read_windows_clipboard_text()

        try:
            btn.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass

        clicked = False
        for attempt in (
            lambda: btn.click(timeout=2500),
            lambda: btn.click(timeout=2500, force=True),
            lambda: btn.dispatch_event("click"),
        ):
            try:
                attempt()
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            return ""

        deadline = time.time() + timeout_s
        best = ""
        while time.time() < deadline:
            cur_browser = _read_browser_clipboard_text(page)
            if cur_browser and cur_browser != before_browser and _is_probably_model_response_text(cur_browser):
                return cur_browser.strip()
            if cur_browser and cur_browser != before_browser and len(cur_browser) > len(best):
                best = cur_browser

            cur_windows = _read_windows_clipboard_text()
            if cur_windows and cur_windows != before_windows and _is_probably_model_response_text(cur_windows):
                return cur_windows.strip()
            if cur_windows and cur_windows != before_windows and len(cur_windows) > len(best):
                best = cur_windows

            time.sleep(0.2)

        return best.strip()


def _extract_last_response_text_from_dom(page) -> str:
    """Best-effort extraction of the last assistant response text from Gemini DOM."""
    # Try a "last response container" and take its visible text.
    # Priority order: most specific selectors first to avoid capturing UI elements.
    selectors = [
        "message-content",  # Most specific: actual message content
        ".message-content",
        ".model-response-text",
        ".response-content",
        ".presented-response-container",
        ".response-container-content",
        "structured-content-container",
        ".response-container",
        "model-response",
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel).last
            if not loc or loc.count() == 0:
                continue
            txt = (loc.inner_text() or "").strip()
            # Filter out UI noise: if text starts with typical UI elements, skip it
            if txt and len(txt) > 200 and not _looks_like_ui_noise(txt):
                return txt
        except Exception:
            continue

    # Fallback: try to get all response containers and filter
    try:
        all_responses = page.locator("div[class*='response'], div[class*='message']").all()
        for resp in reversed(all_responses):  # Start from last
            try:
                txt = (resp.inner_text() or "").strip()
                if txt and len(txt) > 200 and not _looks_like_ui_noise(txt):
                    return txt
            except Exception:
                continue
    except Exception:
        pass

    # Try markdown code blocks (Gemini often wraps JSON in ```json blocks)
    try:
        code_blocks = page.locator("pre, code, .code-block").all()
        for block in reversed(code_blocks):
            try:
                txt = (block.inner_text() or "").strip()
                if txt and len(txt) > 200 and not _looks_like_ui_noise(txt):
                    # If it's JSON or looks like article content, return it
                    if txt.startswith('{') or txt.startswith('[') or "sections" in txt.lower():
                        return txt
            except Exception:
                continue
    except Exception:
        pass

    # Last resort fallback: page-level inner_text, but clean it
    try:
        txt = (page.locator("body").inner_text() or "").strip()
        # Try to extract the actual response by filtering out noise
        txt = _clean_extracted_text(txt)
        return txt
    except Exception:
        return ""


def _extract_last_response_text(page, *, prefer_copy_button: bool = True) -> str:
    """Extract Gemini response text, preferring the real Copy button when safe."""
    if prefer_copy_button:
        try:
            copied = _extract_last_response_text_via_copy_button(page)
            if (copied or "").strip():
                return copied.strip()
        except Exception:
            pass
    return _extract_last_response_text_from_dom(page)


def _looks_like_ui_noise(text: str) -> bool:
    """Check if text looks like UI elements rather than actual content."""
    if not text:
        return True
    
    # Noise indicators (common UI text that appears in Gemini UI, not in responses)
    noise_markers = [
        "Перейти на Google AI Plus",
        "Мой контент",
        "Please replace the empty space",
        "Change the white",
        "DO NOT LEAVE BLANK WHITE SPACE",
        "Чат с Gemini",
        "С чего начнем",
        "Здравствуйте",
        "ratio image using this Prompt",
    ]
    
    # Strong indicators that this is UI noise
    for marker in noise_markers:
        if marker in text:
            return True
    
    # If text starts with "Gemini" and is short, likely UI
    first_line = text.split('\n')[0].strip()
    if first_line == "Gemini" or first_line.startswith("Gemini\n"):
        return True
    
    # Check if text looks like a series of image prompts (not article text)
    lines = text.split('\n')
    prompt_keywords = ["Please replace", "Change the white", "DO NOT LEAVE BLANK"]
    prompt_line_count = sum(1 for line in lines if any(kw in line for kw in prompt_keywords))
    
    # If more than 30% of lines are image prompts, this is likely UI noise
    if len(lines) > 5 and prompt_line_count / len(lines) > 0.3:
        return True
    
    return False


def _clean_extracted_text(text: str) -> str:
    """Clean extracted text from Gemini UI by removing common noise patterns."""
    if not text:
        return ""
    
    # Split into lines for processing
    lines = text.split('\n')
    cleaned_lines = []
    
    # Patterns to skip entirely (UI noise)
    skip_patterns = [
        "Gemini",
        "Перейти на Google AI Plus",
        "Google AI Plus",
        "Мой контент",
        "Please replace the empty space",
        "Change the white",
        "DO NOT LEAVE BLANK WHITE SPACE",
        "Чат с Gemini",
        "С чего начнем",
        "Здравствуйте",
        "ratio image using this Prompt",
        "IMPORTANT: THE image must be",
        "Cinematic lighting",
    ]
    
    in_valid_content = False
    valid_content_start_idx = -1
    
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        
        # Skip empty lines at the start
        if not in_valid_content and not line_stripped:
            continue
        
        # Check if this line is noise
        is_noise = any(pattern in line for pattern in skip_patterns)
        
        if is_noise:
            continue
        
        # Detect start of valid content (JSON or article text)
        if not in_valid_content:
            # JSON start
            if line_stripped.startswith('{') or line_stripped.startswith('['):
                in_valid_content = True
                valid_content_start_idx = i
            # Article title pattern
            elif len(line_stripped) > 20 and not any(c in line_stripped for c in ['(', ')', '[', ']']):
                in_valid_content = True
                valid_content_start_idx = i
        
        if in_valid_content:
            cleaned_lines.append(line)
    
    # Join and return
    result = '\n'.join(cleaned_lines).strip()
    
    # If we got very little content, return original (might be a parsing issue)
    if len(result) < 100:
        return text
    
    return result


def _article_text_worker(
    *,
    idx: int,
    title: str,
    prompt: str,
    url: str,
    headless: bool,
    executable_path: str | None,
    profile_dir: str,
    model_choice: str | None,
    timeout_s: int = 180,
    base_image_path: str | None = None,
) -> dict:
    """Worker: open Gemini UI, (optionally) attach an image, send prompt, extract response text."""
    result: dict = {
        "idx": idx,
        "title": title,
        "prompt": prompt,
        "text": "",
        "error": None,
        "profile_dir": profile_dir,
        "base_image_path": base_image_path,
    }

    # Concurrency guard: never drive the same Chrome profile (user-data-dir) from two threads.
    # If this happens, both workers can type/click in the same Gemini tab and you get a
    # duplicate paste/send while generation is running and Gemini shows
    # "Something went wrong".
    lk = _get_profile_lock(profile_dir)
    acquired = False

    try:
        acquired = lk.acquire(timeout=max(30, int(timeout_s)))
        if not acquired:
            result["error"] = f"Profile is busy (locked): {profile_dir}"
            return result

        with sync_playwright() as p:
            ctx = _launch_persistent_ctx_with_retries(
                p,
                user_data_dir=profile_dir,
                headless=headless,
                executable_path=executable_path,
            )
            try:
                page = ctx.new_page()
                page.set_default_timeout(30000)
                page.goto(url, wait_until="load")
                _wait_input_ready(page, timeout_ms=60000)
                _dismiss_overlays(page)

                try:
                    if model_choice:
                        gph._pick_model(page, model_choice)
                except Exception:
                    # Not fatal
                    pass

                try:
                    _start_new_chat(page)
                except Exception:
                    pass

                _wait_input_ready(page, timeout_ms=60000)
                _dismiss_overlays(page)

                # Optionally attach a "pin" image for context before sending the prompt.
                if base_image_path:
                    try:
                        if os.path.exists(base_image_path):
                            ok_img = _attach_image(page, base_image_path)
                            if ok_img:
                                _wait_image_attached(page, timeout_ms=10000)
                        else:
                            # Don't fail the whole run, but surface it.
                            result["error"] = f"Base image not found: {base_image_path}"
                    except Exception as e:
                        # Non-fatal; continue with text-only prompt.
                        result["error"] = f"Image attach failed: {e}"

                prev = 0
                try:
                    prev = _count_responses(page)
                except Exception:
                    prev = 0

                _type_prompt(page, prompt)

                # Guard: sometimes Gemini UI wipes the editor content right after automation inserts it
                # (text flashes for a moment, then disappears). If we press Send with an empty editor,
                # the window will appear "stuck" while we wait for a response that will never come.
                def _editor_len() -> int:
                    try:
                        return int(
                            page.evaluate(
                                """() => {
                                  const sels = [
                                    "div.ql-editor.textarea.new-input-ui[contenteditable='true']",
                                    "div.ql-editor[contenteditable='true']",
                                    "[contenteditable='true'][role='textbox']",
                                  ];
                                  for (const sel of sels){
                                    const el = document.querySelector(sel);
                                    if (el){
                                      const t = (el.innerText || el.textContent || '').trim();
                                      return t.length;
                                    }
                                  }
                                  return 0;
                                }"""
                            )
                            or 0
                        )
                    except Exception:
                        return 0

                exp_len = len((prompt or '').strip())
                cur_len = _editor_len()
                if exp_len > 50 and cur_len < max(20, int(exp_len * 0.75)):
                    # One quick retry
                    _type_prompt(page, prompt)
                    cur_len = _editor_len()

                if exp_len > 50 and cur_len < max(20, int(exp_len * 0.75)):
                    result["error"] = (
                        "Prompt was not reliably inserted into Gemini input (editor content vanished / truncated). "
                        "Please rerun this item or switch to manual paste."
                    )
                    return result

                if not _click_send_no_stop(page):
                    result["error"] = "Gemini Send button was not triggered for article prompt"
                    return result

                ok = _wait_for_new_text_response(page, prev, timeout_s=min(timeout_s, 90))
                if not ok:
                    result["error"] = f"Timeout: не дождались появления ответа за {min(timeout_s, 90)} сек"
                    return result

                finished = _wait_for_text_generation_to_finish(page, timeout_s=timeout_s)
                if not finished:
                    # We'll still attempt extraction, but mark as incomplete.
                    result["error"] = f"Timeout: генерация не завершилась за {timeout_s} сек (пробую забрать текст как есть)"

                txt = _extract_last_response_text(page)
                if not (txt or "").strip():
                    # Keep previous error if any
                    if not result.get("error"):
                        result["error"] = "Ответ получен, но текст не удалось извлечь из DOM"
                elif _is_gemini_stopped_response_text(txt):
                    result["text"] = ""
                    result["error"] = "Gemini reported: response generation was stopped"
                    result["gemini_stopped"] = True
                else:
                    result["text"] = txt
                return result
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        result["error"] = str(e)
        return result
    finally:
        if acquired:
            try:
                lk.release()
            except Exception:
                pass


def _should_retry_article_error(err: str | None) -> bool:
    if not err:
        return False
    e = str(err).lower()
    retry_markers = [
        "timeout",
        "target closed",
        "browser closed",
        "page closed",
        "has been closed",
        "context closed",
        "connection closed",
        "websocket",
        "net::",
    ]
    return any(m in e for m in retry_markers)


def _article_text_worker_with_retries(
    *,
    retries: int,
    idx: int,
    title: str,
    prompt: str,
    url: str,
    headless: bool,
    executable_path: str | None,
    profile_dir: str,
    model_choice: str | None,
    timeout_s: int,
    base_image_path: str | None = None,
) -> dict:
    """Run `_article_text_worker` with automatic retries on timeout / manual window close."""
    attempts = max(1, int(retries) + 1)  # retries=0 => 1 attempt
    last_res: dict | None = None
    stopped_extra_used = False
    attempt = 1
    while attempt <= attempts:
        res = _article_text_worker(
            idx=idx,
            title=title,
            prompt=prompt,
            url=url,
            headless=headless,
            executable_path=executable_path,
            profile_dir=profile_dir,
            model_choice=model_choice,
            timeout_s=timeout_s,
            base_image_path=base_image_path,
        )
        last_res = res

        # Success => stop
        if (res.get("text") or "").strip() and not (res.get("error") or ""):
            res["attempt"] = attempt
            res["attempts_total"] = attempts
            return res

        # Gemini can explicitly return "You stopped this response" / "Вы остановили...".
        # That is not a model/content failure; resend once even when UI retries are set to 0.
        if bool(res.get("gemini_stopped")) and not stopped_extra_used:
            stopped_extra_used = True
            attempts += 1
            time.sleep(1.2)
            attempt += 1
            continue

        # If we still have attempts left, retry aggressively when output is empty.
        # This covers cases where the user manually closed the window but Playwright didn't
        # surface a clear "Target closed" marker in the error.
        if attempt < attempts and not (res.get("text") or "").strip():
            time.sleep(1.2)
            attempt += 1
            continue

        # Otherwise fall back to marker-based retry decision
        err = res.get("error")
        if attempt >= attempts or not _should_retry_article_error(str(err) if err is not None else None):
            res["attempt"] = attempt
            res["attempts_total"] = attempts
            return res

        # Brief backoff before retry
        time.sleep(1.2)
        attempt += 1

    return last_res or {"idx": idx, "title": title, "prompt": prompt, "text": "", "error": "Unknown error"}


st.set_page_config(page_title="Unified: Gemini → Photoshop → WebP", layout="wide")
st.title("Unified workflow: Gemini (Playwright) → Photoshop fill → WebP")

# -------- Query-param import: prefill Tab 1 prompts from Tab 0 article outputs --------
# We use this to open a new browser tab and automatically inject image prompts into Tab 1.

def _get_query_param_value(key: str) -> str | None:
    try:
        v = st.query_params.get(key)
        if isinstance(v, list):
            return v[0] if v else None
        return v
    except Exception:
        try:
            qp = st.experimental_get_query_params()  # type: ignore[attr-defined]
            v = qp.get(key)
            return v[0] if isinstance(v, list) and v else (v if isinstance(v, str) else None)
        except Exception:
            return None


def _clear_query_params() -> None:
    try:
        st.query_params.clear()
    except Exception:
        try:
            st.experimental_set_query_params()  # type: ignore[attr-defined]
        except Exception:
            pass


def _try_import_prompts_from_query() -> None:
    raw = _get_query_param_value("load_img_prompts")
    if not raw:
        return
    try:
        import base64 as _b64
        import json as _json
        payload = _b64.urlsafe_b64decode(raw.encode("utf-8")).decode("utf-8", errors="ignore")
        data = _json.loads(payload)
        fast_prompts: list[str] = []
        pro_prompts: list[str] = []

        if isinstance(data, dict):
            # New format: split prompts by target tab
            pro_prompts = data.get("pro_prompts") or []
            fast_prompts = data.get("fast_prompts") or []
            # Backward compatibility
            if not pro_prompts and not fast_prompts:
                fast_prompts = data.get("prompts") or []
        else:
            # Old format: everything goes to fast tab
            fast_prompts = data

        if not isinstance(fast_prompts, list):
            fast_prompts = []
        if not isinstance(pro_prompts, list):
            pro_prompts = []

        fast_prompts = [str(p).strip() for p in fast_prompts if str(p).strip()]
        pro_prompts = [str(p).strip() for p in pro_prompts if str(p).strip()]

        # Defensive cleanup: sometimes a whole article JSON blob can accidentally end up
        # as a "prompt" (e.g., if a copy/paste or model output got mixed into the list).
        # If we detect such a blob, re-extract the vertical prompts from it.
        def _expand_if_article_blob(maybe_prompt: str) -> list[str]:
            s = (maybe_prompt or "").strip()
            if not s:
                return []

            # Only treat as an "article blob" if it *actually* parses as our article JSON.
            # If parsing fails for any reason, keep the original string as-is (never drop prompts).
            if s.startswith("{") and ("\"excerpt\"" in s or "\"sections\"" in s or "\"featured_image\"" in s):
                try:
                    art = _try_parse_article_json(s)
                    if isinstance(art, dict) and isinstance(art.get("sections"), list):
                        extracted = _article_json_image_prompts(art)
                        if extracted:
                            return extracted
                except Exception:
                    pass

            return [s]

        fast2: list[str] = []
        for p in fast_prompts:
            fast2.extend(_expand_if_article_blob(p))
        pro2: list[str] = []
        for p in pro_prompts:
            pro2.extend(_expand_if_article_blob(p))

        fast_prompts = [p for p in fast2 if p]
        pro_prompts = [p for p in pro2 if p]

        if not fast_prompts and not pro_prompts:
            return

        # Prefill Tab 1 (fast)
        if fast_prompts:
            # IMPORTANT: clear existing widget state for prompt inputs.
            # Otherwise Streamlit will keep stale values for keys like `unif_pw_prompt_0`,
            # which can make the visible fields look "shifted"/skipped after import.
            try:
                for k in list(st.session_state.keys()):
                    if isinstance(k, str) and k.startswith("unif_pw_prompt_"):
                        del st.session_state[k]
            except Exception:
                pass

            st.session_state.unif_pw_prompts = fast_prompts
            # Also explicitly seed widget keys so the first render cannot pick up stray stale values.
            # Streamlit gives widget-key state higher priority than the `value=` argument.
            for i, p in enumerate(fast_prompts):
                st.session_state[f"unif_pw_prompt_{i}"] = p
            # Remove any leftover prompt widget keys beyond the new length
            try:
                n = len(fast_prompts)
                for k in list(st.session_state.keys()):
                    if isinstance(k, str) and k.startswith("unif_pw_prompt_"):
                        try:
                            idx = int(k.split("_")[-1])
                        except Exception:
                            continue
                        if idx >= n:
                            del st.session_state[k]
            except Exception:
                pass

            st.session_state.unif_pw_imported_from_query = True

        # Prefill Tab 2 (pro / Nano Banana)
        if pro_prompts:
            st.session_state.unif_nbp_tasks = [
                {"prompt": p, "user_data_dir": os.path.abspath(".chrome_automation_profile")}
                for p in pro_prompts
            ]
            st.session_state.unif_nbp_imported_from_query = True

        _clear_query_params()

        # Auto-switch to the relevant tab via small JS click.
        # Prefer Pro tab if pro_prompts exist, else Fast tab.
        tab_label = "2) Gemini Generate" if pro_prompts else "1) Gemini Generate"
        components.html(
            f"""
            <script>
            (function(){{
              function clickTab(){{
                const btns = Array.from(window.parent.document.querySelectorAll('button'));
                const target = btns.find(b => (b.innerText||'').includes('{tab_label}'));
                if(target){{ target.click(); return true; }}
                return false;
              }}
              let tries=0;
              const t = setInterval(()=>{{
                tries++;
                if(clickTab() || tries>20){{ clearInterval(t); }}
              }}, 250);
            }})();
            </script>
            """,
            height=0,
        )
    except Exception:
        # Don't break the app if query parsing fails
        try:
            _clear_query_params()
        except Exception:
            pass


_try_import_prompts_from_query()

# Глобальная настройка корня генераций (по умолчанию — локальная папка). Можно указать абсолютный путь на диске.
if "unif_base_root" not in st.session_state:
    # Попробуем использовать Desktop path из Windows-профиля пользователя, если доступен
    try:
        default_root = str((Path("generate automation")).resolve())
    except Exception:
        default_root = "generate automation"
    st.session_state.unif_base_root = default_root

st.session_state.unif_base_root = st.text_input(
    "Базовая папка генераций (корень)",
    value=st.session_state.unif_base_root,
    key="ui_unif_base_root"
)


# Allocate unique run directory per app launch (date-based with incrementing suffix)
from pathlib import Path as _PathAlias

def _get_run_base_dir() -> str:
    if "unif_run_base_dir" in st.session_state and st.session_state.unif_run_base_dir:
        return st.session_state.unif_run_base_dir
    # Use configurable base root from session state
    base_root = st.session_state.get("unif_base_root", "generate automation")
    root = _PathAlias(base_root)
    date_str = datetime.now().strftime("%Y-%m-%d")
    base = root / date_str

    # ``exists()`` followed by a later mkdir is not safe across several
    # Streamlit servers. Claim the directory atomically now, before any image
    # worker starts, so independent ports never share a run folder.
    for i in range(0, 10000):
        cand = base if i == 0 else _PathAlias(f"{base}_{i}")
        try:
            cand.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError(f"Could not allocate a unique run directory under {root}")

    # Cache absolute path
    st.session_state.unif_run_base_dir = str(_PathAlias(cand).resolve())
    return st.session_state.unif_run_base_dir

# Initialize and pin run dir early to avoid double-increment from multiple initial renders
_ = _get_run_base_dir()

# Shared helper

def _get_all_saved_paths() -> list[str]:
   """Return all saved image paths across tabs.

   We keep Tab1 (fast) and Tab2 (pro) outputs in separate session_state keys to prevent
   accidental overwrites/deletes during regeneration.

   This helper provides a unified view for tabs like Photoshop/WebP.
   """
   fast = list(st.session_state.get("unif_fast_saved_paths") or [])
   pro = list(st.session_state.get("unif_pro_saved_paths") or [])
   # Preserve order but avoid duplicates
   out: list[str] = []
   seen: set[str] = set()
   for p in fast + pro:
       if not p:
           continue
       if p in seen:
           continue
       out.append(p)
       seen.add(p)
   return out


def _extract_prompt_idx(name: str) -> int | None:
   """Parse prompt index from filename supporting both old and new naming.
   New:  <idx>_<slug>_<vv>.<ext>
   Old:  <ii>_<vv>_[reN_]<slug>.<ext>
   """
   try:
       import re as _re
       b = os.path.basename(name)
       m = _re.match(r"^(\d+)_((?:re\d+_)?[^.]+)_(\d{2})(?:_filled)?\.(?:png|jpg|jpeg|bin|webp)$", b, flags=_re.IGNORECASE)
       if m:
           return int(m.group(1))
       m2 = _re.match(r"^(\d{2})_\d{2}_(?:re\d+_)?(.+)\.(?:png|jpg|jpeg|bin)$", b, flags=_re.IGNORECASE)
       if m2:
           return int(m2.group(1))
       return None
   except Exception:
       return None

def _cleanup_old_prompt_files(base_dir: str, idx: int, keep_paths: List[str]) -> int:
   """Delete old files for a given prompt index in base_dir, except those in keep_paths.
   Supports both old (ii_vv_[reN_]slug.ext) and new (idx_slug_vv.ext) naming.
   Returns number of files deleted."""
   try:
       import glob
       exts = {'.png', '.jpg', '.jpeg', '.bin'}
       # Normalize keep paths
       keep_set = set(os.path.normcase(os.path.normpath(os.path.abspath(p))) for p in (keep_paths or []))
       deleted = 0
       patterns = [
           os.path.join(base_dir, f"{idx:02d}_??_*.*"),   # old style
           os.path.join(base_dir, f"{idx}_*_*.*"),       # new style
       ]
       seen = set()
       for pat in patterns:
           for fpath in glob.glob(pat):
               try:
                   if os.path.splitext(fpath)[1].lower() not in exts:
                       continue
                   ap = os.path.normcase(os.path.normpath(os.path.abspath(fpath)))
                   if ap in keep_set or ap in seen:
                       continue
                   # Extra guard: ensure this file indeed belongs to this idx for new format
                   base = os.path.basename(ap)
                   import re as _re
                   if not (_re.match(rf"^{idx}_", base) or _re.match(rf"^{idx:02d}_", base)):
                       continue

                   # IMPORTANT: do not delete Nano Banana Pro images from Tab 2.
                   # They use the naming convention: <idx>_pro_<slug>_<vv>.<ext>
                   # Tab 1 regeneration must only clean up NON-pro images.
                   if _re.match(rf"^{idx}_pro_", base) or _re.match(rf"^{idx:02d}_pro_", base):
                       continue

                   os.remove(ap)
                   seen.add(ap)
                   deleted += 1
               except Exception:
                   pass
       return deleted
   except Exception:
       return 0

def _is_cdp_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/json/version", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


tab0, tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "0) Generate Article Texts (Gemini UI / multi-window)",
    "1) Gemini Generate (Playwright)",
    "2) Gemini Generate (Nano Banana Pro / multi-window)",
    "3) Remove watermark in Photoshop",
    "4) Normalize Pins to 640×1024 (crop)",
    "5) Convert to WebP",
    "6) Regenerate Titles & Descriptions (Gemini API)",
    "7) Regenerate Product Images (Gemini UI)",
])

# ---------------- Tab 0: Article texts (Gemini UI / multi-window) ----------------
with tab0:
    st.subheader("Generate article texts via Gemini UI (multi-window)")
    st.caption(
        "Вставьте список заголовков (по одному на строку). Для каждого заголовка откроется Gemini в отдельном профиле "
        "(параллельно) и будет сгенерирован текст статьи. Результаты появятся ниже."
    )

    # --- Recovery: load autosaved Tab0 results back into the UI ---
    with st.expander("Recovery / Load autosave (Tab0)", expanded=False):
        try:
            autosave_root = Path("autosaves") / "app_unified_streamlit" / "tab0_article_texts"
            autosave_files = []
            try:
                autosave_files = sorted(
                    [p for p in autosave_root.glob("**/unif_text_results_*.json") if p.is_file()],
                    key=lambda p: p.stat().st_mtime if p.exists() else 0,
                    reverse=True,
                )
            except Exception:
                autosave_files = []

            last_ptr = Path("autosaves") / "app_unified_streamlit" / "_last_tab0_article_texts_autosave.json"
            last_path = None
            try:
                if last_ptr.exists():
                    last_obj = _json.loads(last_ptr.read_text(encoding="utf-8"))
                    # Backward/forward compatible: older saves may use 'last_autosave'
                    last_path = (last_obj or {}).get("path") or (last_obj or {}).get("last_autosave")
            except Exception:
                last_path = None

            cols_rec = st.columns([1, 3, 1])
            with cols_rec[0]:
                if st.button("Load LAST", key="unif_load_last_autosave_tab0"):
                    if last_path and Path(last_path).exists():
                        st.session_state["unif_text_autosave_file_to_load"] = str(last_path)
                    else:
                        st.warning("No last autosave pointer found.")

            selected = None
            opts = [str(p) for p in autosave_files]
            default_idx = 0
            if last_path and last_path in opts:
                default_idx = opts.index(last_path)
            with cols_rec[1]:
                if opts:
                    selected = st.selectbox(
                        "Select autosave file",
                        options=opts,
                        index=default_idx,
                        key="unif_text_autosave_file_to_load",
                    )
                else:
                    st.caption("No autosave files found yet.")

            with cols_rec[2]:
                if st.button("Load", key="unif_load_selected_autosave_tab0", type="primary"):
                    try:
                        p = Path(st.session_state.get("unif_text_autosave_file_to_load") or "")
                        if not p.exists():
                            st.error(f"File not found: {p}")
                        else:
                            obj = _json.loads(p.read_text(encoding="utf-8"))
                            loaded_results = (obj or {}).get("results")
                            if not isinstance(loaded_results, list):
                                raise ValueError("Autosave JSON has no 'results' list")

                            # Clear UI cache for those idx to avoid stale widget keys
                            try:
                                for rr in loaded_results:
                                    _idx = rr.get("idx")
                                    if _idx is None:
                                        continue
                                    _clear_article_ui_cache(int(_idx))
                            except Exception:
                                pass

                            st.session_state.unif_text_results = loaded_results

                            # Restore optional fields to make the UI look like before
                            if isinstance((obj or {}).get("titles_raw"), str):
                                st.session_state.unif_text_titles_raw = (obj or {}).get("titles_raw")
                            if isinstance((obj or {}).get("template"), str):
                                loaded_template = (obj or {}).get("template")
                                st.session_state.unif_text_template = loaded_template
                                try:
                                    _cur_theme = _normalize_article_theme(st.session_state.get("unif_text_theme"))
                                    _theme_templates = dict(st.session_state.get("unif_text_theme_templates") or {})
                                    _theme_templates[_cur_theme] = str(loaded_template or "")
                                    if _cur_theme == "decor":
                                        _theme_templates["fashion"] = _build_fashion_template_from_decor(str(loaded_template or ""))
                                    st.session_state.unif_text_theme_templates = _theme_templates
                                    st.session_state.unif_text_theme_last = _cur_theme
                                except Exception:
                                    pass
                            if (obj or {}).get("prompts_per_section") in (1, 2):
                                st.session_state.unif_text_prompts_per_section = int((obj or {}).get("prompts_per_section"))

                            # Force fresh widget keys for text areas / copy buttons
                            st.session_state.unif_text_rev_by_idx = {}
                            for rr in loaded_results:
                                try:
                                    _idx = int(rr.get("idx") or 0)
                                    if _idx:
                                        st.session_state.unif_text_rev_by_idx[str(_idx)] = 1
                                except Exception:
                                    continue

                            st.session_state["unif_text_loaded_autosave_path"] = str(p.resolve())
                            st.success(f"Loaded autosave: {p}")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Failed to load autosave: {e}")

            loaded_path = st.session_state.get("unif_text_loaded_autosave_path")
            if loaded_path:
                st.info(f"Currently loaded from: {loaded_path}")
        except Exception as _e_rec:
            st.warning(f"Recovery UI error: {_e_rec}")

    # --- State init ---
    if "unif_text_titles_raw" not in st.session_state:
        st.session_state.unif_text_titles_raw = ""
    if "unif_text_template" not in st.session_state:
        st.session_state.unif_text_template = (
            "Write a 1,500-word SEO article titled \"[article_title]\" that is both engaging and informative. The article must be written as if you are having a friendly, informal conversation with a fellow enthusiast. Ensure that every instruction below is followed precisely, producing a final output that is dynamic, user-friendly, and thoroughly human in its tone and style.\n\n"
            "Style & Tone Requirements:\n"
            "Conversational and Informal:\n"
            "Write as if youre talking to a friend. The tone should be relaxed, engaging, and approachable.\n"
            "Use everyday language; avoid overly formal or academic language.\n"
            "Ensure the narrative flows naturally and doesnt sound scripted or robotic.\n"
            "Occasional Sarcasm & Humor:\n"
            "Inject light sarcasm and humor to keep the reader engaged. Use these elements sparinglyonly enough to maintain a playful tone without overwhelming the content.\n"
            "The humor should be witty and subtle; ensure it does not detract from the main points.\n"
            "Personal Touch and Experience:\n"
            "Include personal opinions or anecdotes where relevant. This adds authenticity and builds trust with the reader.\n"
            "You can mention personal experiences sometimes to make the content more relatable, when describing features or comparing products. IMPORTANT: Use personal touch/anecdotes only where they truly fit; do not include them in every single section to avoid sounding like you have experience with everything on earth.\n"
            "Active Voice Only:\n"
            "Write every sentence in the active voice. For example, use I love this feature instead of This feature is loved by many.\n"
            "Double-check your sentences to avoid any passive constructions.\n"
            "Engagement Through Rhetorical Questions:\n"
            "Insert rhetorical questions throughout the article to engage the reader and provoke thought. For example: Ever wondered why this works so well?\n"
            "These questions should serve as conversation starters and not be overused.\n"
            "Use of Slang & Abbreviations:\n"
            "Use common internet slang and abbreviations (e.g., FYI, IMO) naturally but sparingly. Integrate emojis only when they strictly match the sentiment and context of the message. Avoid random placement or mismatching emotions (e.g., never use a sad emoji for a positive statement). Maintain a balanced, human-like flow.\n"
            "Formatting & Structural Requirements:\n"
            "Introduction:\n"
            "Begin with a short (about 350 characters), punchy introduction that immediately hooks the reader.\n"
            "Avoid generic openers like In todays world.. or dive into\n"
            "The introduction should quickly address the readers needs and set the tone for the rest of the article.\n"
            "Headings and Subheadings:\n"
            "Organize the article using H2 headings for each major section or point.\n"
            "Use H3 headings to break down subtopics within each H2 section when necessary.\n"
            "Ensure the headings are clear and descriptive to guide the reader through the content.\n"
            "Section Paragraphs Structure:\n"
            "CRITICAL: You MUST create a 'jagged' visual rhythm. Every section must have a unique 'visual fingerprint'.\n"
            "- Paragraph Count: Randomly use between 1 and 4 paragraphs per section. I strictly forbid using the same number of paragraphs for more than two sections in a row (e.g., avoid 3-3-3-3 patterns).\n"
            "- Paragraph Length: Vary the length significantly. One paragraph can be a single punchy 80-character sentence, while the next can be a 270-character block.\n"
            "- Visual Goal: Avoid 'monolithic bricks'. The article should look like a natural, organic conversation with a mix of short thoughts and deeper explanations.\n"
            "VERY IMPORTANT: Conclusion and introduction must remain monolithic (one single paragraph each).\n"
            "Bullet Points & Lists:\n"
            "Use bullet points or numbered lists SOMETIMES if appropriate. Use them When presenting technical details, features, or comparisons.\n"
            "Also If appropriate, you can introduce lists with a context-specific heading phrase followed by a colon (e.g., 'Heading phrase:'), but only if it fits the flow naturally.\n"
            "Important: By the way, lists are COMPLETELY OPTIONAL in the article, if they are not needed you can skip them. article may not contain lists at all. but often lists definitely add the value to the article (for example, tips or something like that) if they are in the right place, so, of course, use lists if appropriate. IMPORTANT: If you add products lists or design elements lists, always use 'Heading phrase:' before such lists, but if you add tips lists I thinks you can skip 'Heading phrase:'. I mean avoid inserting random product lists without context. Don't just drop a list like 'UV-resistant navy finish; Whiskey barrel style; Lightweight construction' without a proper heading or introductory sentence with ':' at the end. Please ensure every list is preceded by the necessary context so the reader knows what they are looking at. \n"
            "Bold Key Information:\n"
            "Throughout the article, bold the most important points, features, or pieces of information. You can also bold a specific key sentence within a section sometimes if it highlights the main takeaway and if it make sense and fits naturally of course.\n"
            "Content and SEO Requirements:\n"
            "Conciseness and Clarity:\n"
            "Every sentence should contribute directly to the articles purpose. Avoid filler phrases such as dive into or in modern times.\n"
            "Be clear and directevery point should have a reason for being there.\n"
            "Comparative and Opinion-Based Commentary:\n"
            "When comparing products, techniques, or ideas, include clear and honest comparisons that offer genuine insights.\n"
            "Support your opinions with logical reasoning and, when possible, real-life examples.\n"
            "SEO Optimization:\n"
            "Ensure the content is optimized for SEO by naturally including relevant keywords related to \"[article_title]\".\n"
            "For each image (featured and section images), generate a descriptive, SEO-optimized alt text that includes primary or secondary keywords. Keep alt texts concise but descriptive (ideal length: 100-125 characters).\n"
            "Generate a shortened, SEO-friendly 'url_slug' for the article. It should be based on the title, use hyphens to separate words, be all lowercase, and be approximately 50 characters long (e.g., 'guide-to-perfect-fish-for-garden-pond').\n"
            "Generate a 'pinterest_title' which is a catchy, short version of the article title for a Pinterest Pin overlay, perfect to advertise listicles or decor guide articles. IMPORTANT: It must be very concise to look good as an overlay. It should NOT exceed 4, maximum 5 medium or large 'content' words. Small words like 'a', 'the', 'of', 'in' etc., do not count towards this limit. (e.g., '8 Soft Vibe Circular Mirror Ideas' or 'Ultimate Pond Fish Guide'). Avoid making it too long or bulky.\n"
            "The language should be SEO-friendly without sacrificing readability or the conversational tone.\n"
            "Avoid AI Fluff:\n"
            "Do not include generic, AI-generated fluff such as overly used phrases like dive into or clichés.\n"
            "The writing must be human, direct, and purposeful, ensuring that every word adds value.\n"
            "Detailed Writing Instructions:\n"
            "Introduction Section:\n"
            "Open with a captivating hook. Immediately address the readers needs or concerns related to article title\n"
            "SOMETIMES state your personal connection or experience with the topic if possible.\n"
            "Main Body:\n"
            "- Mandatory Structural Irregularity: You must intentionally break the symmetry. Use a '1-3-2-4-2' or '2-1-4-1-3' pattern for paragraph counts across your sections. Avoid the '3-3-3-3' trap. A 580-character section can be one solid block (if within char limits) or four tiny, fast-paced sentences. Mix them up!\n"
            "Divide the main content into multiple sections, each introduced by an H2 heading.\n"
            "Within each section, use H3 subheadings where necessary to break down complex ideas.\n"
            "Incorporate bullet points or numbered lists for technical details or feature comparisons.\n"
            "Bold important terms, key features, or takeaways to emphasize their importance.\n"
            "Tone and Engagement:\n"
            "Maintain a conversational tone by writing as though youre chatting with a friend.\n"
            "Use rhetorical questions throughout to encourage reader engagement.\n"
            "Occasionally inject a touch of sarcasm or witty humor to make the article enjoyable without undermining its professionalism.\n"
            "Sprinkle in internet slang (e.g., FYI, IMO) and emoticons no more than 23 times in the entire article.\n"
            "Sentence Structure:\n"
            "Write in a clear, active voice. Ensure every sentence is dynamic and direct.\n"
            "Avoid complex, multi-clause sentences that might dilute the clarity of your points.\n"
            "Conclusion:\n"
            "End with a concise summary that reiterates the key points.\n"
            "Offer a final, engaging thought or call to action. If you ask a question (e.g., 'which one are you grabbing first?'), add a phrase like 'let me know in the comments' so it doesn't sound detached.\n"
            "Leave the reader with a memorable final impression, perhaps by reintroducing a humorous or personal touch.\n\n"
            "For each section of the article, I'd like you to create ONE vertical realistic image prompt that reflects the meaning of the section. Also give me a prompt to generate ONE featured image that reflects the whole article and will be used as the thumbnail/cover. IMPORTANT: this featured image must be WIDE HORIZONTAL LANDSCAPE 2:1 (2x1) banner, ONE monolithic seamless scene, and MUST NOT be a collage/diptych/triptych/split-screen/panels/frames/borders, and MUST look like a single seamless photo. Do NOT use the words 'vertical' or 'portrait' in the featured image prompt. I'd like you to understand that I want to place affiliate links under these images, and I want you to immediately provide me with a list of products related to the image (there are similar products in the image) that I can search on Amazon and select the right products for each section of the blog post. I'd like at least three or six product phrases so that I can easily search on Amazon for the blog section image and add about four products from Amazon based on these phrases. The first section of the article should be dedicated to something similar to the pin I gave and described above, but Please don't mention anything about Pinterest or that anyone came from Pinterest to this article or from anywhere else, or that the reader saw that pin before, dont need it. It is also important that you do not generate the images yourself under any circumstances. I only need you to know what is in the image and give the prompts for their generation, and that's all. The article should be self-contained and not too promotional. Also, after the title but before the large image of the post and the introduction, my blog post should have a small annotation (exerpt or, I don't know, a preamble) that briefly describes what's in the article to intrigue readers. It should be a maximum of 200 characters.\n"
            "Images in article sections will be placed below the section text, but not above it. Don't start the article with the phrase \"Let's be real for a second\", be a bit more creative, try something catchy but appropritate and relevant to the context of the text.\n"
            "Try to avoid making articles too long. A 1,500-character intro is a bit much—readers might lose interest! :) PLEASE TRY TO KEEP EACH SECTION AROUND 580 CHARACTERS (i think this is the BEST section length to keep your audience engaged), its important rule, but YOU CAN GO UP TO 680-730 characters if you feel the topic needs more depth or clarity (IMPORTANT: introduction and conclusion should be approximately 350 characters long, as stated above), but heaven forbid any section reaches 850-1000 characters or more. Focus on quality over a strict limit, but again 580 characters is THE MOST OPTIMAL section length, I think this is the 'sweet spot' for readability. Also, try to keep the number of sections to ten UNLESS the title specifically calls for a CERTAIN NUMBER OF SECTIONS like '20 ideas' or '15 accents' etc.\n\n"
            "FINAL STRUCTURAL CHECK: Look at your 'text' fields. If they all look like they have the same number of lines, you have failed the task. I need a 'visual staircase' effect. Ensure that Section A is visibly shorter or longer in paragraph count than Section B. If I see a repetitive '3-paragraph' pattern, I will consider the output robotic. Break the pattern NOW.\n\n"
            "OUTPUT FORMAT (follow strictly):\n"
            "- Return ONLY valid JSON (no markdown, no code fences, no commentary, no 'Would you like me to...' questions outside the JSON).\n"
            "- Do NOT include HTML tags like <h1>, <h2>, etc.\n"
            "- Headings must be provided only via JSON fields (title, sections[].h2, conclusion_heading).\n"
            "- Do NOT number section headings unless the title explicitly requires it.\n"
            "- IMPORTANT: Use EXACT key names from the schema below. Do NOT rename keys, do NOT add alternative keys (e.g. do NOT output 'Featured Image' or 'vertical image prompt 1' as keys).\n"
            "- If you include lists inside any \"text\" fields, format them as Markdown lists (each bullet line must start with '- ' and ordered items must start with '1. ', '2. ', etc.). Do NOT use '•' characters.\n\n"
            "The JSON schema must be:\n"
            "{\n"
            "  \"excerpt\": \"...<=200 chars...\",\n"
            "  \"category\": \"...chosen-category-slug...\",\n"
            "  \"url_slug\": \"...shortened-seo-url-slug...\",\n"
            "  \"pinterest_title\": \"...catchy overlay title (STRICT LIMIT: max 4-5 words)...\",\n"
            "  \"featured_image\": \"WIDE HORIZONTAL LANDSCAPE 2:1 (2x1) banner, ONE monolithic seamless scene...\",\n"
            "  \"featured_image_alt\": \"...SEO optimized alt text for featured image...\",\n"
            "  \"title\": \"...article title...\",\n"
            "  \"introduction\": \"...intro text...\",\n"
            "  \"sections\": [\n"
            "    {\n"
            "      \"h2\": \"...section heading...\",\n"
            "      \"text\": \"...section text...\",\n"
            "      \"prompt1\": \"...vertical image prompt 1...\",\n"
            "      \"alt1\": \"...SEO optimized alt text for image 1...\",\n"
            "      \"amazon_search_phrases\": [\"phrase 1\", \"phrase 2\", \"phrase 3\"]\n"
            "    }\n"
            "  ],\n"
            "  \"conclusion_heading\": \"Conclusion\",\n"
            "  \"conclusion\": \"...conclusion text...\"\n"
            "}\n"
        )
    if "unif_text_results" not in st.session_state:
        st.session_state.unif_text_results = []  # list[dict]
    if "unif_text_theme" not in st.session_state:
        st.session_state.unif_text_theme = "decor"
    st.session_state.unif_text_theme = _normalize_article_theme(st.session_state.get("unif_text_theme"))
    if "unif_text_theme_last" not in st.session_state:
        st.session_state.unif_text_theme_last = st.session_state.unif_text_theme
    if "unif_text_theme_templates" not in st.session_state:
        _decor_tpl = str(st.session_state.get("unif_text_template") or "").strip()
        st.session_state.unif_text_theme_templates = {
            "decor": _decor_tpl,
            "fashion": _build_fashion_template_from_decor(_decor_tpl),
            "recipes": DEFAULT_RECIPE_ARTICLE_TEMPLATE,
        }
    else:
        _theme_templates = dict(st.session_state.get("unif_text_theme_templates") or {})
        _decor_tpl = str(_theme_templates.get("decor") or st.session_state.get("unif_text_template") or "").strip()
        if "decor" not in _theme_templates:
            _theme_templates["decor"] = _decor_tpl
        if "fashion" not in _theme_templates:
            _theme_templates["fashion"] = _build_fashion_template_from_decor(_decor_tpl)
        if "recipes" not in _theme_templates:
            _theme_templates["recipes"] = DEFAULT_RECIPE_ARTICLE_TEMPLATE
        st.session_state.unif_text_theme_templates = _theme_templates

    # --- Gutenberg images: select a generation run folder to inject images into Gutenberg blocks ---
    # Base root requested by user (Windows Desktop path). Falls back to configured unif_base_root.
    try:
        _default_runs_root = Path(r"C:\\myProjects\\generate automation\\generate automation")
    except Exception:
        _default_runs_root = None

    runs_root = _default_runs_root if (_default_runs_root and _default_runs_root.exists()) else Path(str(st.session_state.get("unif_base_root") or "generate automation"))

    if "unif_tab0_run_dir" not in st.session_state:
        st.session_state.unif_tab0_run_dir = ""
    if "unif_tab0_uploads_base_url" not in st.session_state:
        # Default to the *current* year/month uploads folder (WordPress uses YYYY/MM/).
        # You can always override it in the UI input below.
        _ym = datetime.now().strftime("%Y/%m")
        # Keep previous default behavior (nestingmuse.com), but do NOT tie it to images-per-section.
        st.session_state.unif_tab0_uploads_base_url = f"https://nestingmuse.com/wp-content/uploads/{_ym}/"

    with st.expander("Gutenberg: auto-inject images from run folder", expanded=False):
        st.caption(
            "Select a run folder (e.g. 2026-02-27_23). The app will map article_* folders to posts order. "
            "For Gutenberg, it will insert the section image block before each H2 (except the first)."
        )

        # --- Domain selector for Gutenberg/uploads (independent from images-per-section) ---
        if "unif_tab0_uploads_domain" not in st.session_state:
            _base0 = str(st.session_state.get("unif_tab0_uploads_base_url") or "").strip()
            _host0 = ""
            try:
                import urllib.parse as _urlparse
                _host0 = _urlparse.urlparse(_base0 if "://" in _base0 else "https://" + _base0).netloc
            except Exception:
                _host0 = ""
            if _host0 in KNOWN_WP_DOMAINS:
                st.session_state.unif_tab0_uploads_domain = _host0
            else:
                st.session_state.unif_tab0_uploads_domain = "Custom"

        _domain_options = [*KNOWN_WP_DOMAINS, "Custom"]
        _cur_domain = str(st.session_state.get("unif_tab0_uploads_domain") or "Custom")
        if _cur_domain not in _domain_options:
            _cur_domain = "Custom"

        picked_domain = st.selectbox(
            "Domain for Gutenberg/uploads",
            options=_domain_options,
            index=_domain_options.index(_cur_domain),
            key="ui_unif_tab0_uploads_domain",
            help=(
                "Choose a known site to auto-fill the uploads Base URL for the current YYYY/MM. "
                "Choose Custom to edit the URL manually."
            ),
        )
        st.session_state.unif_tab0_uploads_domain = picked_domain

        # Auto-fill base url when a known domain is selected
        if picked_domain != "Custom":
            _ym = datetime.now().strftime("%Y/%m")
            _new_base = f"https://{picked_domain}/wp-content/uploads/{_ym}/"
            st.session_state.unif_tab0_uploads_base_url = _new_base
            # IMPORTANT: text_input stores its own value under the widget key.
            # If we don't update it, the UI may keep showing the old domain.
            st.session_state.ui_unif_tab0_uploads_base_url = _new_base

        # If user manually typed a custom URL earlier, keep UI key in sync with canonical state.
        if "ui_unif_tab0_uploads_base_url" not in st.session_state:
            st.session_state.ui_unif_tab0_uploads_base_url = str(st.session_state.get("unif_tab0_uploads_base_url") or "")

        
        st.session_state.unif_tab0_uploads_base_url = st.text_input(
            "Base URL for uploaded images (used in Gutenberg blocks)",
            value=str(st.session_state.get("ui_unif_tab0_uploads_base_url") or st.session_state.get("unif_tab0_uploads_base_url") or ""),
            key="ui_unif_tab0_uploads_base_url",
            help="Example: https://nestingmuse.com/wp-content/uploads/YYYY/MM/  (the file name from your local folder will be appended)",
        )
        # Keep canonical state synchronized with widget state
        st.session_state.unif_tab0_uploads_base_url = str(st.session_state.get("ui_unif_tab0_uploads_base_url") or "").strip()


        # If user manually edits the URL, update the selector when it matches a known domain.
        try:
            _base_now = str(st.session_state.get("unif_tab0_uploads_base_url") or "").strip()
            import urllib.parse as _urlparse
            _host_now = _urlparse.urlparse(_base_now if "://" in _base_now else "https://" + _base_now).netloc
            if _host_now in KNOWN_WP_DOMAINS:
                st.session_state.unif_tab0_uploads_domain = _host_now
        except Exception:
            pass
        # --- End domain selector ---

        

        try:
            run_dirs = sorted([p for p in runs_root.iterdir() if p.is_dir()], key=lambda p: p.name.lower(), reverse=True)
        except Exception:
            run_dirs = []
        run_opts = [""] + [str(p) for p in run_dirs]
        cur = str(st.session_state.get("unif_tab0_run_dir") or "")
        idx_cur = run_opts.index(cur) if cur in run_opts else 0
        st.session_state.unif_tab0_run_dir = st.selectbox(
            "Run folder",
            options=run_opts,
            index=idx_cur,
            key="ui_unif_tab0_run_dir",
            help=f"Root: {runs_root}",
        )
        if st.session_state.unif_tab0_run_dir:
            st.code(st.session_state.unif_tab0_run_dir)

            col_a, col_b = st.columns([2, 1])
            with col_a:
                if st.button(
                    "Collect ALL .webp into one folder (for WP upload)",
                    key="unif_tab0_collect_all_webp_btn",
                    help=(
                        "Соберёт все .webp из подпапок выбранного run folder в одну папку внутри run folder, "
                        "С‡С‚РѕР±С‹ РјРѕР¶РЅРѕ Р±С‹Р»Рѕ Р±С‹СЃС‚СЂРѕ РІС‹РґРµР»РёС‚СЊ РІСЃРµ Рё Р·Р°РіСЂСѓР·РёС‚СЊ РІ WordPress. РСЃС…РѕРґРЅРёРєРё РЅРµ С‚СЂРѕРіР°СЋС‚СЃСЏ."
                    ),
                ):
                    with st.spinner("Collecting .webp files..."):
                        out_dir, copied, errs = _collect_all_webp_in_run_dir_for_wp(st.session_state.unif_tab0_run_dir)
                    if out_dir and copied:
                        st.success(f"Copied {copied} webp files to: {out_dir}")
                        st.code(out_dir)
                        if errs:
                            st.warning(f"Some files failed to copy: {len(errs)}")
                            with st.expander("Copy errors", expanded=False):
                                st.text("\n".join(errs[:200]))
                    elif out_dir and copied == 0:
                        st.warning(f"No .webp files found under: {st.session_state.unif_tab0_run_dir}")
                    else:
                        st.error("Failed to collect .webp files")
                        if errs:
                            st.text("\n".join(errs[:50]))

            with col_b:
                # Quick open helper
                if st.button("Open run folder", key="unif_tab0_open_run_folder_btn"):
                    _open_folder(st.session_state.unif_tab0_run_dir)

    # --- Settings (separate keys so we don't affect Tab 1/2) ---
    default_url = st.session_state.get("unif_url", DEFAULT_URLS[0])

    if "unif_text_prompts_per_section" not in st.session_state:
        st.session_state.unif_text_prompts_per_section = 1
    # Force one image prompt per section in Tab 0.
    st.session_state.unif_text_prompts_per_section = 1
    st.session_state.ui_unif_text_prompts_per_section = 1
    st.session_state.unif_text_prompts_per_section_ui = 1
        
    if "unif_text_url" not in st.session_state:
        st.session_state.unif_text_url = default_url
    if "unif_text_headless" not in st.session_state:
        st.session_state.unif_text_headless = False
    if "unif_text_exe_path" not in st.session_state:
        st.session_state.unif_text_exe_path = st.session_state.get("unif_exe_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
    if "unif_text_user_data_dir" not in st.session_state:
        st.session_state.unif_text_user_data_dir = os.path.abspath(".chrome_automation_profile")
    if "unif_text_parallelism" not in st.session_state:
        st.session_state.unif_text_parallelism = 8
    if "unif_text_use_numbered_profiles" not in st.session_state:
        st.session_state.unif_text_use_numbered_profiles = True
    if "unif_text_timeout_s" not in st.session_state:
        st.session_state.unif_text_timeout_s = 180
    if "unif_text_retries" not in st.session_state:
        st.session_state.unif_text_retries = 3
    if not bool(st.session_state.get("_unif_text_defaults_20260625_migrated")):
        try:
            if int(st.session_state.get("unif_text_parallelism", 8) or 8) == 5:
                st.session_state.unif_text_parallelism = 8
        except Exception:
            st.session_state.unif_text_parallelism = 8
        try:
            if int(st.session_state.get("unif_text_retries", 3) or 0) == 0:
                st.session_state.unif_text_retries = 3
        except Exception:
            st.session_state.unif_text_retries = 3
        if str(st.session_state.get("unif_text_profile_numbers") or "").strip() in ("", "21,22,23,24,25"):
            st.session_state.unif_text_profile_numbers = "21,22,23,24,25,26,27,28"
        if str(st.session_state.get("feat_img_profile_numbers") or "").strip() in ("", "21,22,23,24,25"):
            st.session_state.feat_img_profile_numbers = "21,22,23,24,25,26,27,28"
        st.session_state["_unif_text_defaults_20260625_migrated"] = True

    st.caption("Tab 0 now uses one section image only: `prompt1` + `alt1`.")

    cols0 = st.columns([2, 2, 2, 1])
    with cols0[0]:
        st.selectbox(
            "URL интерфейса Gemini",
            DEFAULT_URLS,
            index=DEFAULT_URLS.index(st.session_state.unif_text_url) if st.session_state.unif_text_url in DEFAULT_URLS else 0,
            key="unif_text_url",
        )
    with cols0[1]:
        st.checkbox("Headless режим", value=bool(st.session_state.unif_text_headless), key="unif_text_headless")
    with cols0[2]:
        st.number_input(
            "Timeout на статью (сек)",
            min_value=30,
            max_value=900,
            value=int(st.session_state.unif_text_timeout_s),
            step=10,
            key="unif_text_timeout_s",
        )
    with cols0[3]:
        st.number_input(
            "Retries",
            min_value=0,
            max_value=3,
            value=int(st.session_state.unif_text_retries),
            step=1,
            key="unif_text_retries",
            help="Авто-повтор при зависании/таймауте или если окно закрыли вручную.",
        )

    st.text_input(
        "Путь к chrome.exe",
        value=st.session_state.unif_text_exe_path,
        key="unif_text_exe_path",
    )

    st.text_input(
        "Базовый профиль (user-data-dir)",
        value=st.session_state.unif_text_user_data_dir,
        key="unif_text_user_data_dir",
        help="Например: .chrome_automation_profile. Если включены numbered profiles, то будут использованы _1..N.",
    )

    # Profile for single-item regeneration (button "Пересоздать")
    if "unif_text_regen_profile" not in st.session_state:
        # Can be either a number ("1") meaning <base>_1, or a full path to a user-data-dir.
        st.session_state.unif_text_regen_profile = ""
    st.text_input(
        "Профиль для Пересоздать (номер или полный путь)",
        value=st.session_state.unif_text_regen_profile,
        key="unif_text_regen_profile",
        help=(
            "Оставьте пустым: будет выбран _1 (если существует) или будет создана временная копия базового профиля. "
            "Если указать число (например 3) — откроется <Базовый профиль>_3. "
            "Если указать путь — он будет использован как user-data-dir напрямую."
        ),
    )

    cols1 = st.columns([2, 2])
    with cols1[0]:
        st.checkbox(
            "Использовать профили .chrome_automation_profile_1..N (быстрее)",
            value=bool(st.session_state.unif_text_use_numbered_profiles),
            key="unif_text_use_numbered_profiles",
        )
    with cols1[1]:
        st.number_input(
            "Параллельно окон (concurrency)",
            min_value=1,
            max_value=12,
            value=int(st.session_state.unif_text_parallelism),
            step=1,
            key="unif_text_parallelism",
        )
        
        st.text_input(
            "Номера профилей (через запятую)",
            value="21,22,23,24,25,26,27,28",
            key="unif_text_profile_numbers",
            help="Например: 2,4,5 откроет .chrome_automation_profile_2, _4, _5. Оставьте пустым для использования стандартного параллелизма."
        )
        
        st.selectbox(
            "Модель для генерации текстов",
            options=["Быстрая", "Думающая", "Pro"],
            index=1,
            key="unif_text_model_choice",
            help="Выберите модель Gemini для генерации текстов статей"
        )

    # Get selected model
    model_choice_text = st.session_state.get("unif_text_model_choice", "Думающая")

    # ---------------- Contextual internal links (WordPress) ----------------
    if "unif_il_enabled" not in st.session_state:
        st.session_state.unif_il_enabled = True
    if "unif_il_profile" not in st.session_state:
        st.session_state.unif_il_profile = os.getenv("WP_SITE_PROFILE", "nestingmuse.com")
    if st.session_state.unif_il_profile not in WP_SITE_PROFILES:
        st.session_state.unif_il_profile = "nestingmuse.com"
    if "unif_il_limit" not in st.session_state:
        st.session_state.unif_il_limit = 60
    if "unif_il_recent_posts" not in st.session_state:
        st.session_state.unif_il_recent_posts = []
    if "unif_il_recent_posts_ts" not in st.session_state:
        st.session_state.unif_il_recent_posts_ts = None
    if "unif_il_recent_posts_error" not in st.session_state:
        st.session_state.unif_il_recent_posts_error = None
    if "unif_il_include_titles" not in st.session_state:
        st.session_state.unif_il_include_titles = False

    with st.expander("Internal links (contextual, from your WP site)", expanded=False):
        st.checkbox(
            "Enable contextual internal links (1-2 <a> links inside article text)",
            value=bool(st.session_state.unif_il_enabled),
            key="unif_il_enabled",
            help=(
                "If enabled, the app will fetch the last published posts from your site and add them to the prompt. "
                "Gemini will then insert 1-2 internal links *only where contextually relevant* (IMPORTANT)."
            ),
        )

        st.checkbox(
            "Include titles in candidate list (bigger prompt)",
            value=bool(st.session_state.unif_il_include_titles),
            key="unif_il_include_titles",
            help="OFF (default) = only URLs are passed to the model to keep the prompt smaller.",
        )

        prof_names = list(WP_SITE_PROFILES.keys())

        # IMPORTANT: these fields have Streamlit `key=` so they won't update from the `value=` arg
        # after the first render. We must update st.session_state explicitly when the profile changes.
        if "unif_il_profile_applied" not in st.session_state:
            st.session_state.unif_il_profile_applied = None

        def _apply_il_profile(profile_name: str) -> None:
            """Apply a WP site profile to the internal-linking connection fields."""
            st.session_state.unif_il_profile_applied = profile_name

            # When switching sites, cached post candidates must be invalidated.
            # (also when switching to Custom because the user may change fields manually)
            st.session_state.unif_il_recent_posts = []
            st.session_state.unif_il_recent_posts_ts = None
            st.session_state.unif_il_recent_posts_error = None

            # Do not overwrite user-entered values for the Custom profile.
            if profile_name == "Custom":
                return

            prof = WP_SITE_PROFILES.get(profile_name, {}) or {}
            default_base_url = prof.get("base_url") or ""
            default_username = prof.get("username") or ""
            default_app_password = prof.get("app_password") or ""

            # Keep original behavior: env vars / Streamlit secrets override profile defaults.
            st.session_state.unif_il_base_url = os.getenv("WP_BASE_URL", _get_secret("WP_BASE_URL", default_base_url))
            st.session_state.unif_il_username = os.getenv("WP_USERNAME", _get_secret("WP_USERNAME", default_username))
            st.session_state.unif_il_app_password = os.getenv(
                "WP_APP_PASSWORD", _get_secret("WP_APP_PASSWORD", default_app_password)
            )

        def _on_il_profile_change() -> None:
            _apply_il_profile(str(st.session_state.get("unif_il_profile") or ""))

        st.selectbox(
            "Site profile",
            options=prof_names,
            index=prof_names.index(st.session_state.unif_il_profile) if st.session_state.unif_il_profile in prof_names else 0,
            key="unif_il_profile",
            on_change=_on_il_profile_change,
        )

        # On first load (or when the profile was modified programmatically), apply profile defaults once.
        if st.session_state.unif_il_profile_applied != st.session_state.unif_il_profile:
            _apply_il_profile(str(st.session_state.unif_il_profile))

        prof = WP_SITE_PROFILES.get(st.session_state.unif_il_profile, {})
        default_base_url = prof.get("base_url") or "https://nestingmuse.com"
        default_username = prof.get("username") or ""
        default_app_password = prof.get("app_password") or ""

        if "unif_il_base_url" not in st.session_state:
            st.session_state.unif_il_base_url = os.getenv("WP_BASE_URL", _get_secret("WP_BASE_URL", default_base_url))
        if "unif_il_username" not in st.session_state:
            st.session_state.unif_il_username = os.getenv("WP_USERNAME", _get_secret("WP_USERNAME", default_username))
        if "unif_il_app_password" not in st.session_state:
            st.session_state.unif_il_app_password = os.getenv("WP_APP_PASSWORD", _get_secret("WP_APP_PASSWORD", default_app_password))
        if "unif_il_verify_ssl" not in st.session_state:
            st.session_state.unif_il_verify_ssl = (os.getenv("WP_VERIFY_SSL", "1") != "0")

        c1, c2 = st.columns(2)
        with c1:
            st.text_input("WP base_url", value=str(st.session_state.unif_il_base_url), key="unif_il_base_url")
            st.text_input("WP username", value=str(st.session_state.unif_il_username), key="unif_il_username")
        with c2:
            st.text_input(
                "WP application password",
                value=str(st.session_state.unif_il_app_password),
                key="unif_il_app_password",
                type="password",
            )
            st.checkbox("Verify SSL", value=bool(st.session_state.unif_il_verify_ssl), key="unif_il_verify_ssl")

        st.number_input(
            "How many latest posts to fetch",
            min_value=5,
            max_value=100,
            step=5,
            value=int(st.session_state.unif_il_limit),
            key="unif_il_limit",
        )

        col_f1, col_f2 = st.columns([1, 3])
        with col_f1:
            if st.button("Fetch latest posts", key="unif_il_fetch", type="primary"):
                try:
                    posts = _wp_fetch_recent_posts(
                        base_url=str(st.session_state.unif_il_base_url),
                        username=str(st.session_state.unif_il_username),
                        app_password=str(st.session_state.unif_il_app_password),
                        verify_ssl=bool(st.session_state.unif_il_verify_ssl),
                        limit=int(st.session_state.unif_il_limit),
                        timeout_s=30,
                    )
                    st.session_state.unif_il_recent_posts = posts
                    st.session_state.unif_il_recent_posts_ts = datetime.now().isoformat(timespec="seconds")
                    st.session_state.unif_il_recent_posts_error = None
                    st.success(f"Fetched posts: {len(posts)}")
                except Exception as _e_il:
                    st.session_state.unif_il_recent_posts = []
                    st.session_state.unif_il_recent_posts_ts = None
                    st.session_state.unif_il_recent_posts_error = str(_e_il)
                    st.error(f"Failed to fetch posts: {_e_il}")

        with col_f2:
            ts = st.session_state.get("unif_il_recent_posts_ts")
            err = st.session_state.get("unif_il_recent_posts_error")
            cnt = len(st.session_state.get("unif_il_recent_posts") or [])
            if err:
                st.warning(f"Last fetch error: {err}")
            if ts:
                st.caption(f"Cached posts: {cnt} (fetched at {ts})")
            else:
                st.caption(f"Cached posts: {cnt}")

        # Preview (first 15)
        try:
            preview = (st.session_state.get("unif_il_recent_posts") or [])[:15]
            if preview:
                st.json(preview)
        except Exception:
            pass

    def _on_text_theme_change() -> None:
        theme_templates = dict(st.session_state.get("unif_text_theme_templates") or {})
        prev_theme = _normalize_article_theme(st.session_state.get("unif_text_theme_last"))
        current_template = str(st.session_state.get("unif_text_template") or "")
        if prev_theme:
            theme_templates[prev_theme] = current_template

        decor_template = str(
            theme_templates.get("decor")
            or current_template
            or st.session_state.get("unif_text_template")
            or ""
        ).strip()
        if "decor" not in theme_templates:
            theme_templates["decor"] = decor_template
        if "fashion" not in theme_templates:
            theme_templates["fashion"] = _build_fashion_template_from_decor(decor_template)
        if "recipes" not in theme_templates:
            theme_templates["recipes"] = DEFAULT_RECIPE_ARTICLE_TEMPLATE

        new_theme = _normalize_article_theme(st.session_state.get("unif_text_theme"))
        st.session_state.unif_text_template = str(theme_templates.get(new_theme) or "")
        st.session_state.unif_text_theme_templates = theme_templates
        st.session_state.unif_text_theme_last = new_theme

    st.markdown("---")
    st.selectbox(
        "Tema / theme for article prompt",
        options=["decor", "fashion", "recipes"],
        index=["decor", "fashion", "recipes"].index(_normalize_article_theme(st.session_state.get("unif_text_theme"))),
        key="unif_text_theme",
        on_change=_on_text_theme_change,
        format_func=lambda x: ARTICLE_THEME_LABELS.get(str(x), str(x)),
        help="Decor = nestingmuse.com / spaceofmuse.com; Fashion = glowuproutine.com; Recipes = sweethomecookery.com.",
    )
    st.caption(
        "Selected theme controls which prompt template is used. "
        "Decor and fashion stay very close to each other; recipes use a different structure with optional sparse image prompts."
    )
    st.text_area(
        "Заголовки статей (по одному на строку)",
        value=st.session_state.unif_text_titles_raw,
        height=200,
        key="unif_text_titles_raw",
    )

    st.text_area(
        "Шаблон промпта (используйте {title})",
        value=st.session_state.unif_text_template,
        height=220,
        key="unif_text_template",
    )

    # Quick fix button: in case the user accidentally pasted an old generated prompt
    # (with pin/Pinterest-specific instructions) back into the template.
    # This keeps the workflow smooth without forcing a full reset of the whole template.
    cols_tpl = st.columns([1, 3])
    with cols_tpl[0]:
        if st.button("Очистить шаблон от pin/Pinterest текста", key="unif_text_tpl_clean"):
            import re as _re
            tpl0 = st.session_state.get("unif_text_template") or ""
            leak_patterns = [
                # Remove ONLY the pin/Pinterest-related segments, do NOT remove the whole paragraph.
                r"The first section of the article should be dedicated.*?dont need it\.?\s*",
                r"The first section of the article should be dedicated.*?do not generate the images yourself under any circumstances\.?\s*",
                r"Please\s+dont\s+mention\s+anything\s+about\s+Pinterest.*?(?:\.|$)\s*",
                r"dont\s+mention\s+anything\s+about\s+Pinterest.*?(?:\.|$)\s*",
                r"Use the attached reference image as inspiration for the first section of the article that comes after the introduction\..*?coherent with the section(?:'|\u2019)s text\.?\s*",
            ]
            for pat in leak_patterns:
                try:
                    tpl0 = _re.sub(pat, "", tpl0, flags=_re.IGNORECASE | _re.DOTALL).strip()
                except Exception:
                    pass
            st.session_state.unif_text_template = tpl0
            st.success("Готово: убрал pin/Pinterest-инструкции из шаблона")
            st.rerun()
    with cols_tpl[1]:
        st.caption("Если вы когда-то вставляли полный промпт обратно в шаблон, эта кнопка уберёт блоки про pin/Pinterest.")

    # --- Optional mode: attach one image per title ("pin") + add special instruction to prompt ---
    if "unif_text_use_pin_images" not in st.session_state:
        st.session_state.unif_text_use_pin_images = False
    if "unif_text_pin_images_tmp_dir" not in st.session_state:
        st.session_state.unif_text_pin_images_tmp_dir = None
    if "unif_text_pin_image_paths" not in st.session_state:
        st.session_state.unif_text_pin_image_paths = []

    st.markdown("#### Pin image mode (optional)")
    st.checkbox(
        "Attach 1 image per title + add pin-like intro instruction",
        value=bool(st.session_state.unif_text_use_pin_images),
        key="unif_text_use_pin_images",
        help=(
            "When enabled, the script will attach a corresponding image in Gemini UI for each title, "
            "and will append an extra instruction to the prompt so that the first section matches the image context. "
            "Default is OFF (no images, no extra instruction)."
        ),
    )

    uploaded_pin_imgs = st.file_uploader(
        "Upload images (one per title). Best: same order as titles.",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
        key="unif_text_pin_images_upload",
        disabled=not bool(st.session_state.unif_text_use_pin_images),
    )

    # Manual cleanup helper for temp pin-image folder
    if st.session_state.get("unif_text_pin_images_tmp_dir"):
        if st.button("Cleanup uploaded pin images (delete temp folder)", key="unif_text_pin_cleanup"):
            try:
                shutil.rmtree(st.session_state.unif_text_pin_images_tmp_dir, ignore_errors=True)
            except Exception:
                pass
            st.session_state.unif_text_pin_images_tmp_dir = None
            st.session_state.unif_text_pin_image_paths = []
            st.success("Cleaned")

    def _slugify_simple(s: str) -> str:
        import re as _re
        s = (s or "").lower()
        s = _re.sub(r"[^a-z0-9]+", " ", s)
        s = _re.sub(r"\s+", " ", s).strip()
        return s

    def _materialize_uploaded_images(files) -> list[str]:
        if not files:
            return []
        # Cleanup previous tmp dir (best-effort)
        prev = st.session_state.get("unif_text_pin_images_tmp_dir")
        if prev:
            try:
                shutil.rmtree(prev, ignore_errors=True)
            except Exception:
                pass
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp_dir = Path(f"tmp_rovodev_article_pin_images_{ts}")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        out_paths: list[str] = []
        for uf in files:
            try:
                name = Path(uf.name).name
                outp = tmp_dir / name
                with open(outp, "wb") as f:
                    f.write(uf.getbuffer())
                out_paths.append(str(outp.resolve()))
            except Exception:
                continue
        st.session_state.unif_text_pin_images_tmp_dir = str(tmp_dir.resolve())
        st.session_state.unif_text_pin_image_paths = out_paths
        return out_paths

    # Persist uploads to disk (Gemini UI can only attach files by path)
    if st.session_state.unif_text_use_pin_images and uploaded_pin_imgs is not None:
        _materialize_uploaded_images(uploaded_pin_imgs)

    def _map_pin_images_to_titles(titles_local: list[str], img_paths: list[str]) -> dict[int, str]:
        """Return mapping {1-based title index -> image path}."""
        if not titles_local or not img_paths:
            return {}
        # 1) If counts match, map by order.
        if len(img_paths) == len(titles_local):
            return {i + 1: img_paths[i] for i in range(len(titles_local))}

        # 2) Otherwise try match by filename containing slugified title.
        norm_titles = [(_slugify_simple(t), t) for t in titles_local]
        norm_imgs = [(_slugify_simple(Path(p).stem), p) for p in img_paths]
        used = set()
        mapped: dict[int, str] = {}
        for i, (nt, _orig) in enumerate(norm_titles, start=1):
            best = None
            for ni, p in norm_imgs:
                if p in used:
                    continue
                if nt and nt in ni:
                    best = p
                    break
            if best:
                mapped[i] = best
                used.add(best)

        # 3) Fill remaining in order for the rest (up to min lengths)
        if len(mapped) < min(len(titles_local), len(img_paths)):
            remaining = [p for p in img_paths if p not in used]
            for i in range(1, len(titles_local) + 1):
                if i in mapped:
                    continue
                if not remaining:
                    break
                mapped[i] = remaining.pop(0)
        return mapped

    def _parse_titles(raw: str) -> list[str]:
        return [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]

    titles = _parse_titles(st.session_state.unif_text_titles_raw)
    st.write(f"Тайтов к генерации: {len(titles)}")

    # Current pin mapping preview (usable both for generation and single-item re-run)
    use_pin_mode_ui = bool(st.session_state.get("unif_text_use_pin_images"))
    pin_img_paths_ui = list(st.session_state.get("unif_text_pin_image_paths") or [])
    pin_map_ui = _map_pin_images_to_titles(titles, pin_img_paths_ui) if use_pin_mode_ui else {}
    if use_pin_mode_ui:
        st.caption(f"Pin images loaded: {len(pin_img_paths_ui)} | mapped to titles: {len(pin_map_ui)}/{len(titles)}")

    cols_run = st.columns([1.2, 1.8, 3])
    with cols_run[0]:
        run_text_btn = st.button("Сгенерировать тексты", type="primary", key="unif_text_run")

    with cols_run[1]:
        show_manual_fields_btn = st.button(
            "Показать поля для ручного JSON",
            key="unif_text_show_manual_fields",
            help="Не запускает генерацию. Просто создаёт/обновляет список статей и показывает поля ниже, чтобы можно было вставить JSON вручную.",
        )

    with cols_run[2]:
        st.caption("Если Gemini в параллельных окнах упал/завис — нажмите эту кнопку, чтобы сразу появились поля для ручной вставки JSON.")

    if show_manual_fields_btn:
        if not titles:
            st.warning("Добавьте хотя бы один тайтл")
        else:
            # Create/refresh placeholder results so the manual JSON fields appear immediately.
            prev_results = list(st.session_state.get("unif_text_results") or [])
            prev_by_idx: dict[int, dict] = {}
            for rr in prev_results:
                try:
                    ii = int((rr or {}).get("idx") or 0)
                except Exception:
                    ii = 0
                if ii > 0 and isinstance(rr, dict):
                    prev_by_idx[ii] = dict(rr)

            new_results: list[dict] = []
            for i, t in enumerate(titles, start=1):
                base = prev_by_idx.get(i) or {}
                r2 = {
                    "idx": i,
                    "title": str(t),
                    "prompt": str(base.get("prompt") or ""),
                    "text": str(base.get("text") or ""),
                    # Keep empty (no red error). You can paste JSON manually into the text field below.
                    "error": str(base.get("error") or ""),
                }
                new_results.append(r2)

            st.session_state.unif_text_results = new_results

            # Reset revisions + clear any cached widget state so text areas show fresh values.
            st.session_state.unif_text_rev_by_idx = {}
            for rr in new_results:
                try:
                    _idx = int(rr.get("idx") or 0)
                    if _idx > 0:
                        _bump_article_rev(_idx)
                        _clear_article_ui_cache(_idx)
                except Exception:
                    pass

    if run_text_btn:
        if not titles:
            st.warning("Добавьте хотя бы один тайтл")
        else:
            import concurrent.futures

            url_snapshot = st.session_state.unif_text_url
            headless_snapshot = bool(st.session_state.unif_text_headless)
            exe_snapshot = st.session_state.unif_text_exe_path or None
            base_profile_snapshot = _normalize_user_data_dir(st.session_state.unif_text_user_data_dir) or st.session_state.unif_text_user_data_dir
            use_numbered = bool(st.session_state.unif_text_use_numbered_profiles)
            # Parse profile numbers from input
            profile_numbers_str = st.session_state.get("unif_text_profile_numbers", "").strip()
            profile_numbers = []
            if profile_numbers_str:
                try:
                    profile_numbers = [int(x.strip()) for x in profile_numbers_str.split(",") if x.strip()]
                except ValueError:
                    st.error("Ошибка: номера профилей должны быть числами через запятую (например: 2,4,5)")
                    profile_numbers = []
            
            # Concurrency (max simultaneously opened windows)
            desired_parallelism = max(1, min(12, int(st.session_state.unif_text_parallelism)))

            if profile_numbers:
                # Use specific numbered profiles, but still respect concurrency.
                # We can have more profiles in the pool than concurrent workers.
                max_workers = min(len(profile_numbers), desired_parallelism)
            else:
                # Use default parallelism
                max_workers = desired_parallelism
            timeout_s = int(st.session_state.unif_text_timeout_s)
            retries = int(st.session_state.unif_text_retries)

            template_snapshot = st.session_state.unif_text_template
            theme_snapshot = _normalize_article_theme(st.session_state.get("unif_text_theme"))
            target_domain_snapshot = _resolve_tab0_target_domain(theme_snapshot)
            use_pin_mode = use_pin_mode_ui
            pin_map = pin_map_ui

            # Internal links prompt block snapshot (read once in main thread)
            internal_links_block_snapshot = ""
            try:
                if bool(st.session_state.get("unif_il_enabled")):
                    posts = st.session_state.get("unif_il_recent_posts") or []
                    # Auto-fetch if cache is empty
                    if not posts:
                        try:
                            posts = _wp_fetch_recent_posts(
                                base_url=str(st.session_state.get("unif_il_base_url") or ""),
                                username=str(st.session_state.get("unif_il_username") or ""),
                                app_password=str(st.session_state.get("unif_il_app_password") or ""),
                                verify_ssl=bool(st.session_state.get("unif_il_verify_ssl", True)),
                                limit=int(st.session_state.get("unif_il_limit") or 60),
                                timeout_s=30,
                            )
                            st.session_state.unif_il_recent_posts = posts
                            st.session_state.unif_il_recent_posts_ts = datetime.now().isoformat(timespec="seconds")
                            st.session_state.unif_il_recent_posts_error = None
                        except Exception as _e_il_auto:
                            st.session_state.unif_il_recent_posts = []
                            st.session_state.unif_il_recent_posts_ts = None
                            st.session_state.unif_il_recent_posts_error = str(_e_il_auto)
                            posts = []
                    internal_links_block_snapshot = _build_internal_links_block(
                        posts,
                        include_titles=bool(st.session_state.get("unif_il_include_titles")),
                    )
            except Exception:
                internal_links_block_snapshot = ""

            # IMPORTANT: capture UI value in the main thread.
            # Streamlit session_state is not reliable to read from worker threads.
            prompts_per_section_ui = int(st.session_state.get("unif_text_prompts_per_section") or 2)
            if use_pin_mode and not pin_map:
                st.warning("Pin image mode is ON, but no images were mapped. Running in text-only mode.")
                use_pin_mode = False

            # Prepare a pool of profile dirs to avoid profile locks.
            profile_pool: queue.Queue = queue.Queue()
            tmp_profiles: list[str] = []

            def _prepare_profile_slot(slot_idx: int) -> dict:
                """Return {dir,is_temp} for a slot."""
                # Prefer numbered profile
                if use_numbered:
                    cand = f"{base_profile_snapshot}_{slot_idx}"
                    cand_norm = _normalize_user_data_dir(cand) or cand
                    try:
                        if Path(cand_norm).exists():
                            return {"dir": cand_norm, "is_temp": False}
                    except Exception:
                        pass

                # Fallback: clone base profile
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                tmp_dir = str(Path(f"tmp_rovodev_article_profile_{ts}_{slot_idx}").resolve())
                try:
                    _clone_profile_dir(base_profile_snapshot, tmp_dir)
                except Exception:
                    # If clone fails, try to use base directly (may fail if locked)
                    tmp_dir = base_profile_snapshot
                    return {"dir": tmp_dir, "is_temp": False}
                tmp_profiles.append(tmp_dir)
                return {"dir": tmp_dir, "is_temp": True}

            # Create profile pool based on specific numbers or range
            if profile_numbers:
                # Use specific numbered profiles under the configured base user-data-dir.
                base_profile_name = base_profile_snapshot
                for prof_num in profile_numbers:
                    prof_path = f"{base_profile_name}_{prof_num}"
                    prof_path_norm = _normalize_user_data_dir(prof_path) or prof_path
                    
                    # Check if profile exists
                    try:
                        if Path(prof_path_norm).exists():
                            profile_pool.put({"dir": prof_path_norm, "is_temp": False})
                        else:
                            st.warning(f"Профиль {prof_path} не найден, пропускаем")
                    except Exception:
                        st.warning(f"Не удалось проверить профиль {prof_path}")
            else:
                # Use default numbered profiles or temp profiles
                for s in range(1, max_workers + 1):
                    slot = _prepare_profile_slot(s)
                    profile_pool.put(slot)

            def _worker_one(i: int, title_i: str) -> dict:
                slot = profile_pool.get()
                try:
                    base_img = pin_map.get(i) if use_pin_mode else None
                    prompt_i = _build_article_prompt_from_title(
                        title_i,
                        template_snapshot,
                        theme=theme_snapshot,
                        target_domain=target_domain_snapshot,
                        include_pin_intro_instruction=bool(use_pin_mode and base_img),
                        prompts_per_section=prompts_per_section_ui,
                        internal_links_block=internal_links_block_snapshot,
                    )
                    return _article_text_worker_with_retries(
                        retries=retries,
                        idx=i,
                        title=title_i,
                        prompt=prompt_i,
                        url=url_snapshot,
                        headless=headless_snapshot,
                        executable_path=exe_snapshot,
                        profile_dir=slot["dir"],
                        model_choice=model_choice_text,
                        timeout_s=timeout_s,
                        base_image_path=base_img,
                    )
                finally:
                    profile_pool.put(slot)

            results: list[dict] = [{"idx": i + 1, "title": titles[i], "prompt": "", "text": "", "error": "not started"} for i in range(len(titles))]

            st.info(f"Будет одновременно открыто окон: {max_workers} (профилей в пуле: {profile_pool.qsize()})")
            with st.spinner("Генерация текстов... (окна будут открываться параллельно)"):
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                    futs = {ex.submit(_worker_one, i + 1, t): (i + 1) for i, t in enumerate(titles)}
                    for fut in concurrent.futures.as_completed(futs):
                        idx_done = futs[fut]
                        try:
                            res = fut.result()
                        except Exception as e:
                            res = {"idx": idx_done, "title": titles[idx_done - 1], "prompt": "", "text": "", "error": str(e)}
                        if 1 <= idx_done <= len(results):
                            results[idx_done - 1] = res

            # Cleanup temp profiles created for this run
            for d in tmp_profiles:
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass

            # Enrich results with parsed JSON fields (pinterest_title) for downstream autosaves.
            try:
                for _r in results:
                    try:
                        if not isinstance(_r, dict):
                            continue
                        txt0 = str(_r.get("text") or "").strip()
                        if not txt0:
                            continue
                        art = _try_parse_article_json(
                            txt0,
                            theme=theme_snapshot,
                            target_domain=target_domain_snapshot,
                        )
                        if isinstance(art, dict):
                            _r["text"] = _format_repaired_article_json_text(
                                txt0,
                                theme=theme_snapshot,
                                target_domain=target_domain_snapshot,
                            )
                            pt = str(art.get("pinterest_title") or "").strip()
                            if pt:
                                _r["pinterest_title"] = pt
                    except Exception:
                        continue
            except Exception:
                pass

            st.session_state.unif_text_results = results

            # --- Autosave generated article JSON/texts to disk (crash-safe) ---
            _autosave_tab0_article_texts(results)

            # Bump per-article revisions so Streamlit widget keys change and never
            # reuse stale values from previous runs.
            for _r in results:
                try:
                    _idx = int(_r.get("idx") or 0)
                    if _idx > 0:
                        _bump_article_rev(_idx)
                        _clear_article_ui_cache(_idx)
                except Exception:
                    pass

            st.success("Готово")

    # ---- Results ----
    results = st.session_state.get("unif_text_results") or []
    if results:
        st.markdown("---")
        st.subheader("Результаты")

        # Autosave status (so you can recover after crashes/reboots)
        last_autosave_path = st.session_state.get("unif_text_last_autosave_path")
        last_autosave_err = st.session_state.get("unif_text_last_autosave_error")
        if last_autosave_path:
            st.info(f"Autosaved tab0 results to: {last_autosave_path}")
        elif last_autosave_err:
            st.warning(f"Tab0 autosave failed: {last_autosave_err}")

        # Manual export: build/save the same JSON as autosave, but from CURRENT UI fields
        # (useful when you manually paste/edit article JSON in the text areas).
        try:
            import json as _json_export

            def _tab0_results_with_current_widget_texts() -> list[dict]:
                """Return Tab0 results but with the CURRENT text_area values applied.

                Also adds a bit of robustness for manual workflows:
                - If user pasted a full article JSON into the text field, but `title` is empty,
                  we try to extract `title` from that JSON so Tab3 (Stage3) table shows titles.
                - Tab3 skips rows where `error` is truthy. If the text is present, we clear
                  the error so the item is usable downstream.
                """

                base_results = st.session_state.get("unif_text_results") or []
                out: list[dict] = []

                def _guess_title_from_text(s: str) -> str:
                    s = (s or "").strip()
                    if not s:
                        return ""
                    # First non-empty line, then first sentence-like chunk
                    first = (s.splitlines()[0] if s.splitlines() else s).strip()
                    if not first:
                        return ""
                    # If JSON, first line can be '{' -> ignore
                    if first in ("{", "["):
                        # try next line
                        for ln in s.splitlines()[1:10]:
                            ln = (ln or "").strip()
                            if ln and ln not in ("{", "["):
                                first = ln
                                break
                    # remove obvious JSON key prefix
                    first = first.lstrip("{[").strip()
                    # keep short
                    if len(first) > 120:
                        first = first[:117].rstrip() + "..."
                    return first

                for rr in (base_results or []):
                    try:
                        r2 = dict(rr or {})
                    except Exception:
                        # fallback (shouldn't happen) - but keep it usable
                        r2 = rr if isinstance(rr, dict) else {"text": str(rr)}

                    try:
                        _idx = int((r2 or {}).get("idx") or 0)
                    except Exception:
                        _idx = 0

                    # Apply current widget value (edited text)
                    if _idx > 0:
                        _rev = _get_article_rev(_idx)
                        _key = f"unif_text_out_{_idx}_{_rev}"
                        if _key in st.session_state:
                            r2["text"] = str(st.session_state.get(_key) or "")

                    txt = str((r2 or {}).get("text") or "")
                    txt_stripped = txt.strip()

                    # If text exists, ensure Stage3 won't skip it due to stale `error`.
                    if txt_stripped:
                        if (r2 or {}).get("error"):
                            r2["error"] = None

                    # If title missing/empty, try to extract from pasted JSON article text
                    try:
                        title0 = str((r2 or {}).get("title") or "").strip()
                    except Exception:
                        title0 = ""

                    if txt_stripped:
                        try:
                            _theme_for_repair = _normalize_article_theme(st.session_state.get("unif_text_theme"))
                            _domain_for_repair = _resolve_tab0_target_domain(_theme_for_repair)
                            art = _try_parse_article_json(
                                txt_stripped,
                                theme=_theme_for_repair,
                                target_domain=_domain_for_repair,
                            )
                            if isinstance(art, dict):
                                r2["text"] = _format_repaired_article_json_text(
                                    txt_stripped,
                                    theme=_theme_for_repair,
                                    target_domain=_domain_for_repair,
                                )
                                # Title: only fill if missing
                                if not title0:
                                    t2 = str(art.get("title") or "").strip()
                                    if t2:
                                        r2["title"] = t2
                                    else:
                                        t3 = _guess_title_from_text(txt_stripped)
                                        if t3:
                                            r2["title"] = t3

                                # pinterest_title: always extract if present
                                try:
                                    pt = str(art.get("pinterest_title") or "").strip()
                                    if pt:
                                        r2["pinterest_title"] = pt
                                except Exception:
                                    pass
                            elif not title0:
                                t3 = _guess_title_from_text(txt_stripped)
                                if t3:
                                    r2["title"] = t3
                        except Exception:
                            if not title0:
                                t3 = _guess_title_from_text(txt_stripped)
                                if t3:
                                    r2["title"] = t3

                    out.append(r2)

                return out

            cols_export = st.columns([1, 1, 2])
            with cols_export[0]:
                if st.button("💾 Сохранить JSON статей", key="unif_tab0_manual_save_articles_json"):
                    try:
                        cur_results = _tab0_results_with_current_widget_texts()
                        _autosave_tab0_article_texts(cur_results)
                        st.success(f"Saved: {st.session_state.get('unif_text_last_autosave_path')}")
                    except Exception as _e_manual_save:
                        st.error(f"Manual save failed: {_e_manual_save}")

            with cols_export[1]:
                # Open autosave folder (best-effort)
                if st.button("📂 Открыть папку", key="unif_tab0_open_articles_autosave_folder"):
                    try:
                        p_last = str(st.session_state.get("unif_text_last_autosave_path") or "").strip()
                        if p_last:
                            _open_folder(str(Path(p_last).parent))
                        else:
                            _open_folder(str(Path("autosaves") / "app_unified_streamlit" / "tab0_article_texts"))
                    except Exception:
                        _open_folder(str(Path("autosaves") / "app_unified_streamlit" / "tab0_article_texts"))

            with cols_export[2]:
                try:
                    # Prepare on-the-fly download payload using current widget texts
                    ts_export = datetime.now().strftime("%Y%m%d_%H%M%S")
                    date_export = datetime.now().strftime("%Y-%m-%d")
                    payload_export = {
                        "ts": ts_export,
                        "date": date_export,
                        "source": "tab0_article_texts",
                        "prompts_per_section": int(st.session_state.get("unif_text_prompts_per_section") or 2),
                        "titles_raw": str(st.session_state.get("unif_text_titles_raw") or ""),
                        "template": str(st.session_state.get("unif_text_template") or ""),
                        "results": _tab0_results_with_current_widget_texts(),
                    }
                    st.download_button(
                        "⬇️ Скачать JSON статей",
                        data=_json_export.dumps(payload_export, ensure_ascii=False, indent=2).encode("utf-8"),
                        file_name=f"unif_text_results_{ts_export}.json",
                        mime="application/json",
                        key="unif_tab0_download_articles_json",
                    )
                except Exception as _e_dl:
                    st.caption(f"Download JSON unavailable: {_e_dl}")

            # --- WordPress bulk upload JSON (for wp_bulk_upload_streamlit.py) ---
            with st.expander("WordPress: подготовить JSON для bulk upload", expanded=False):
                st.caption(
                    "Собирает JSON в формате wp_bulk_upload_streamlit.py: posts[].media + content_template с плейсхолдерами {{media:key:url}}. "
                    "РСЃРїРѕР»СЊР·СѓСЋС‚СЃСЏ РўРћР›Р¬РљРћ С„РёРЅР°Р»СЊРЅС‹Рµ .webp РёР· postproc (Р±РµР· РІР°С‚РµСЂРјР°СЂРєРё) Рё Featured .webp РёР· РІРєР»Р°РґРєРё Featured (РµСЃР»Рё РєРѕРЅРІРµСЂС‚РёСЂРѕРІР°Р»Рё)."
                )

                # Let user choose draft/publish later (default draft)
                wp_status = st.selectbox(
                    "WP post status",
                    options=["draft", "publish"],
                    index=0,
                    key="unif_wp_bulk_status",
                    help="Рекомендуется draft. publish используйте только если уверены.",
                )

                def _build_wp_bulk_payload_from_current_tab0() -> dict:
                    cur_results = _tab0_results_with_current_widget_texts()
                    posts: list[dict] = []

                    for rr in cur_results or []:
                        if not isinstance(rr, dict):
                            continue
                        try:
                            idx = int(rr.get("idx") or 0)
                        except Exception:
                            idx = 0
                        if idx <= 0:
                            continue

                        text = str(rr.get("text") or "")
                        if not text.strip():
                            continue
                        if rr.get("error"):
                            # Skip errored items (same behavior as other pipelines)
                            continue

                        title = str(rr.get("title") or "").strip()
                        article_json = _try_parse_article_json(text)
                        if isinstance(article_json, dict):
                            if not title:
                                title = str(article_json.get("title") or "").strip()

                        if not title:
                            title = f"Article {idx}"

                        excerpt_val = ""
                        if isinstance(article_json, dict):
                            excerpt_val = str(article_json.get("excerpt") or "").strip()

                        # Optional: custom permalink slug (WordPress uses the field name `slug`)
                        slug_val = ""
                        if isinstance(article_json, dict):
                            slug_val = str(article_json.get("url_slug") or "").strip()

                        media: dict[str, dict] = {}
                        featured_key: str | None = None

                        # Featured image (prefer converted WebP from Featured tab)
                        feat_webp = _get_latest_featured_webp_for_article_idx(idx)
                        feat_alt = ""
                        if isinstance(article_json, dict):
                            feat_alt = str(article_json.get("featured_image_alt") or "").strip()
                        if feat_webp:
                            media["featured"] = {"path": feat_webp, "alt": feat_alt}
                            featured_key = "featured"

                        # Inline images: build placeholders and media spec from run folder webp
                        blocks_for_wp = ""
                        if isinstance(article_json, dict):
                            sections = article_json.get("sections") or []
                            if not isinstance(sections, list):
                                sections = []

                            ordered_files = _get_tab0_local_webp_files_for_article(idx) or []
                            try:
                                per_sec = int(st.session_state.get("unif_text_prompts_per_section") or 2)
                            except Exception:
                                per_sec = 2
                            if per_sec <= 0:
                                per_sec = 2

                            # Align with _get_tab0_images_for_article: slot 0 empty, slot 1..N for prompts, slot N for conclusion
                            local_paths_by_slot: list[list[str]] = [[] for _ in range(len(sections) + 1)]
                            prompt_slots_by_slot: list[list[int]] = [[] for _ in range(len(sections) + 1)]
                            k = 0
                            for slot_i in range(1, len(sections) + 1):
                                sec = sections[slot_i - 1] if (slot_i - 1) < len(sections) else {}
                                prompt_slots = _article_json_prompt_slot_indices(sec, prompts_per_section=per_sec) if isinstance(sec, dict) else []
                                prompt_slots_by_slot[slot_i] = list(prompt_slots)
                                need = len(prompt_slots)
                                chunk = ordered_files[k : k + need]
                                local_paths_by_slot[slot_i] = [str(p) for p in chunk]
                                k += need

                            placeholder_by_slot: list[list[str]] = [[] for _ in range(len(sections) + 1)]
                            alt_placeholder_by_slot: list[list[str]] = [[] for _ in range(len(sections) + 1)]

                            img_counter = 0
                            for slot_i, paths in enumerate(local_paths_by_slot):
                                for j, _p in enumerate(paths):
                                    img_counter += 1
                                    mkey = f"img{img_counter}"

                                    # Map alt-text from article_json sections[].alt1/alt2.
                                    alt_val = ""
                                    try:
                                        if slot_i > 0 and (slot_i - 1) < len(sections):
                                            sec = sections[slot_i - 1]
                                            if isinstance(sec, dict):
                                                slot_indices = prompt_slots_by_slot[slot_i] if slot_i < len(prompt_slots_by_slot) else []
                                                prompt_idx = slot_indices[j] if j < len(slot_indices) else (1 if j == 0 else 2)
                                                alt_val = _article_json_get_alt_variant(sec, prompt_idx)
                                    except Exception:
                                        alt_val = ""

                                    media[mkey] = {"path": _p, "alt": alt_val}
                                    placeholder_by_slot[slot_i].append(f"{{{{media:{mkey}:url}}}}")
                                    alt_placeholder_by_slot[slot_i].append(f"{{{{media:{mkey}:alt}}}}")

                            blocks_for_wp = _article_json_to_gutenberg_blocks(
                                article_json,
                                image_paths_by_section=placeholder_by_slot,
                                image_alts_by_section=alt_placeholder_by_slot,
                                include_title_h1=False,
                            )
                        else:
                            cleaned_for_wp = _clean_article_for_gutenberg(text)
                            blocks_for_wp = _article_markdownish_to_gutenberg_blocks(cleaned_for_wp)

                        post: dict = {
                            "title": title,
                            "excerpt": excerpt_val,
                            "status": str(wp_status or "draft"),
                            "content_template": blocks_for_wp,
                            "media": media,
                        }

                        # Category slug from article JSON (must match WP category slug exactly)
                        cat_slug = ""
                        if isinstance(article_json, dict):
                            cat_slug = str(article_json.get("category") or "").strip()

                        # Safety: ensure category_slug matches the selected target domain taxonomy.
                        # If LLM returned a slug that belongs to the other site, map it.
                        _target_domain = _resolve_tab0_target_domain(st.session_state.get("unif_text_theme"))

                        if _target_domain == "spaceofmuse.com":
                            # spaceofmuse taxonomy
                            if cat_slug in ("interiors", "interior"):
                                cat_slug = "home-interiors"
                            elif cat_slug in ("exteriors", "exterior"):
                                cat_slug = "outdoor-spaces"
                        elif _target_domain == "glowuproutine.com":
                            if cat_slug in ("style", "styles", "fashion-style", "fashion_style", "fashion", "fashion-guides"):
                                cat_slug = "style-guides"
                            elif cat_slug in ("outfit", "outfits", "look", "looks", "lookbook"):
                                cat_slug = "outfit-ideas"
                            elif cat_slug in ("trend", "trends", "seasonal", "season", "seasonal-style", "seasonal-style-guides"):
                                cat_slug = "seasonal-trends"
                            elif cat_slug in ("wardrobe", "wardrobe-basics", "capsule-wardrobe", "essentials"):
                                cat_slug = "wardrobe-essentials"
                        elif _target_domain == "sweethomecookery.com":
                            if cat_slug in ("drink", "drinks"):
                                cat_slug = "beverages"
                            elif cat_slug in ("breakfast", "brunch"):
                                cat_slug = "breakfast-brunch"
                            elif cat_slug in ("dessert", "desserts"):
                                cat_slug = "desserts"
                            elif cat_slug in ("healthy", "healthy-eating"):
                                cat_slug = "healthy-diet"
                            elif cat_slug in ("main-course", "entree", "entrees"):
                                cat_slug = "main-courses"
                            elif cat_slug in ("quick", "easy", "quick-and-easy"):
                                cat_slug = "quick-easy"
                            elif cat_slug in ("seasonal", "seasonal-food", "seasonal-recipes"):
                                cat_slug = "seasonal-eats"
                            elif cat_slug in ("slow-cooker", "air-fryer", "slow-cooker-recipes", "air-fryer-recipes"):
                                cat_slug = "slow-cooker-air-fryer"
                            elif cat_slug in ("snacks", "apps", "appetizers", "snack-apps"):
                                cat_slug = "snacks-apps"
                            elif cat_slug in ("soups", "salads", "soup", "salad"):
                                cat_slug = "soups-salads"
                            elif cat_slug in ("world", "world-cuisine", "international", "international-cuisine"):
                                cat_slug = "world-kitchen"
                        else:
                            # nestingmuse taxonomy (default)
                            if cat_slug in ("home-interiors", "home_interiors"):
                                cat_slug = "interiors"
                            elif cat_slug in ("outdoor-spaces", "outdoor_spaces"):
                                cat_slug = "exteriors"

                        if cat_slug:
                            post["category_slug"] = cat_slug
        
                        if slug_val:
                            post["slug"] = slug_val
                        if featured_key:
                            post["featured_key"] = featured_key

                        posts.append(post)

                    return {
                        "base_dir": ".",
                        "posts": posts,
                    }

                if st.button("🧩 Сгенерировать JSON для wp_bulk_upload", key="unif_wp_bulk_build_btn", type="primary"):
                    try:
                        st.session_state["unif_wp_bulk_payload"] = _build_wp_bulk_payload_from_current_tab0()
                        st.success(f"Готово. Постов: {len((st.session_state['unif_wp_bulk_payload'] or {}).get('posts') or [])}")
                    except Exception as _e_wpjson:
                        st.error(f"Не удалось собрать JSON: {_e_wpjson}")

                payload = st.session_state.get("unif_wp_bulk_payload")
                if isinstance(payload, dict) and payload.get("posts"):
                    ts_wp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    st.download_button(
                        "⬇️ Скачать wp_bulk_upload JSON",
                        data=_json_export.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
                        file_name=f"wp_bulk_posts_{ts_wp}.json",
                        mime="application/json",
                        key=f"unif_wp_bulk_download_{ts_wp}",
                    )
                    with st.expander("Preview payload", expanded=False):
                        st.json(payload)

        except Exception:
            pass

        # Bulk fast-image generation: one interface for ALL articles (Tab1 prompts only)
        try:
            import json as _json
            import socket as _socket
            import urllib.parse

            def _get_free_port() -> int:
                with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                    s.bind(("127.0.0.1", 0))
                    return int(s.getsockname()[1])

            def _is_port_open(port: int) -> bool:
                try:
                    with _socket.create_connection(("127.0.0.1", int(port)), timeout=0.25):
                        return True
                except Exception:
                    return False

            def _extract_all_img_prompts_from_article_text(text_blob: str) -> list[str]:
                """Extract ALL image prompts from article text (raw list, unwrapped).

                We use this once per article and then split into:
                - pro: first 4 prompts
                - fast: prompts after the first 4

                This guarantees both payload sections are consistent.
                """
                current_text = (text_blob or "").strip()
                if not current_text:
                    return []

                article_json = _try_parse_article_json(current_text)
                if isinstance(article_json, dict):
                    try:
                        img_prompts = _article_json_image_prompts(article_json)
                    except Exception:
                        img_prompts = []
                else:
                    try:
                        img_prompts = _extract_image_prompts_from_article(current_text)
                    except Exception:
                        img_prompts = []

                img_prompts = [str(p).strip() for p in (img_prompts or []) if str(p).strip()]
                # de-dup preserve order
                seen: set[str] = set()
                out: list[str] = []
                for p in img_prompts:
                    if p in seen:
                        continue
                    seen.add(p)
                    out.append(p)
                return out

            # Build payload grouped by article
            # - bulk_articles: fast prompts (AFTER first 4) to be used in bulk fast app/tab1
            # - bulk_pro_articles: first 4 prompts (RAW, no wrapper) to be used in bulk pro tab
            bulk_articles: list[dict] = []
            bulk_pro_articles: list[dict] = []

            for rr in results:
                try:
                    aidx = int(rr.get("idx") or 0)
                except Exception:
                    aidx = 0
                atitle = str(rr.get("title") or "").strip()
                atext = str(rr.get("text") or "")

                # Pro = prompts for the first 2 sections.
                # In legacy mode (2 prompts/section) => 4 prompts.
                # In one-image mode (1 prompt/section) => 2 prompts.
                prompts_per_section_ui = int(st.session_state.get("unif_text_prompts_per_section") or 2)
                pro_prompt_count = 4 if prompts_per_section_ui >= 2 else 2

                # IMPORTANT: do NOT extract prompts via regex-first here.
                # Regex extraction returns prompts in the order they appear in text, so in one-image mode
                # first 2 items may become (section1.prompt1, section1.prompt2) which is wrong.
                # Prefer structured JSON parsing and then apply prompts_per_section mode.
                article_json = _try_parse_article_json(atext)
                if isinstance(article_json, dict):
                    try:
                        img_prompts = _article_json_image_prompts_mode(article_json, prompts_per_section=prompts_per_section_ui)
                    except Exception:
                        img_prompts = []
                else:
                    img_prompts = _extract_all_img_prompts_from_article_text(atext)

                if not img_prompts:
                    continue

                pro_raw = [_sanitize_prompt(p) for p in img_prompts[:pro_prompt_count] if str(p).strip()]
                if pro_raw:
                    bulk_pro_articles.append({"idx": aidx, "title": atitle, "prompts": pro_raw})

                # Fast = prompts AFTER first 4, wrapped like Tab1
                pre = NBP_PROMPT_PRE
                post = NBP_PROMPT_POST
                fast_raw = img_prompts[pro_prompt_count:]
                fast_wrapped = [f"{pre}{_sanitize_prompt(p)}{post}" for p in (fast_raw or []) if str(p).strip()]
                if fast_wrapped:
                    bulk_articles.append({"idx": aidx, "title": atitle, "prompts": fast_wrapped})

            total_fast = sum(len(a.get("prompts") or []) for a in bulk_articles)

            if bulk_articles and total_fast:
                st.info(f"Bulk fast prompts: {len(bulk_articles)} статей / {total_fast} промптов (только Tab1)")

                col_bulk_a, col_bulk_b = st.columns([1, 1])

                def _start_or_get_bulk_fast_server(payload_path: str) -> str | None:
                    # Reuse existing server if still up
                    try:
                        port = int(st.session_state.get("unif_bulk_fast_port") or 0)
                    except Exception:
                        port = 0
                    if port and _is_port_open(port):
                        return f"http://localhost:{port}/?load_file={urllib.parse.quote(payload_path)}"

                    # Start new server
                    port = _get_free_port()
                    try:
                        # Keep process handle so it's not GC'ed too early
                        cmd = [
                            sys.executable,
                            "-m",
                            "streamlit",
                            "run",
                            "app_bulk_fast_images_streamlit.py",
                            "--server.headless",
                            "true",
                            "--server.port",
                            str(port),
                            "--server.address",
                            "127.0.0.1",
                        ]
                        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        st.session_state["unif_bulk_fast_proc_pid"] = int(proc.pid)
                        st.session_state["unif_bulk_fast_port"] = int(port)
                    except Exception as e:
                        st.error(f"Не удалось запустить bulk fast app: {e}")
                        return None

                    # Wait briefly for startup
                    deadline = time.time() + 6
                    while time.time() < deadline:
                        if _is_port_open(port):
                            break
                        time.sleep(0.25)

                    return f"http://localhost:{port}/?load_file={urllib.parse.quote(payload_path)}"

                with col_bulk_a:
                    if st.button("🖼️ Открыть BULK генерацию картинок (fast/tab1)", key="unif_open_bulk_fast", type="primary"):
                        try:
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            payload_path = str(Path(f"tmp_rovodev_bulk_fast_payload_{ts}.json").resolve())
                            prompts_per_section_ui = int(st.session_state.get("unif_text_prompts_per_section") or 2)
                            pro_prompt_count = 4 if prompts_per_section_ui >= 2 else 2
                            payload = {
                                "base_root": st.session_state.get("unif_base_root", "generate automation"),
                                "prompts_per_section": prompts_per_section_ui,
                                "pro_prompt_count": pro_prompt_count,
                                # fast prompts (after the pro slice)
                                "articles": bulk_articles,
                                # pro prompts (first N, raw)
                                "pro_articles": bulk_pro_articles,
                            }
                            Path(payload_path).write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

                            url2 = _start_or_get_bulk_fast_server(payload_path)
                            if url2:
                                # Open in a new browser tab
                                components.html(
                                    f"""
                                    <script>
                                      window.open({ _json.dumps(url2) }, '_blank');
                                    </script>
                                    """,
                                    height=0,
                                )
                                st.success("Открываю bulk fast интерфейс в новой вкладке...")
                                st.session_state["unif_bulk_fast_last_url"] = url2
                        except Exception as e:
                            st.error(f"Ошибка подготовки bulk fast payload: {e}")

                with col_bulk_b:
                    last_url = st.session_state.get("unif_bulk_fast_last_url")
                    if last_url:
                        st.markdown(f"<a href=\"{last_url}\" target=\"_blank\">Открыть ещё раз bulk fast интерфейс</a>", unsafe_allow_html=True)
        except Exception:
            pass

        for r in results:
            idx = int(r.get("idx") or 0)
            title = r.get("title") or ""
            prompt = r.get("prompt") or ""
            text = r.get("text") or ""
            err = r.get("error")

            with st.container(border=True):
                st.markdown(f"**#{idx}. {title}**")
                try:
                    _att = int(r.get("attempt") or 0)
                    _tot = int(r.get("attempts_total") or 0)
                    if _att and _tot and _tot > 1:
                        st.caption(f"Попытка: {_att}/{_tot}")
                except Exception:
                    pass
                if err and str(err) not in {"None", ""}:
                    st.error(str(err))
                # Prompt shown/copyable should reflect current UI settings (e.g., prompts_per_section)
                # because saved prompts in `results` may be from an older run.
                with st.expander("Промпт", expanded=False):
                    try:
                        template_snapshot = str(st.session_state.get("unif_text_template") or "")
                        include_pin_intro_instruction = False
                        try:
                            if bool(st.session_state.get("unif_text_use_pin_images")) and isinstance(pin_map_ui, dict):
                                include_pin_intro_instruction = bool(pin_map_ui.get(idx))
                        except Exception:
                            include_pin_intro_instruction = False

                        _il_block = ""
                        try:
                            if bool(st.session_state.get("unif_il_enabled")):
                                _il_block = _build_internal_links_block(
                                    st.session_state.get("unif_il_recent_posts") or [],
                                    include_titles=bool(st.session_state.get("unif_il_include_titles")),
                                )
                        except Exception:
                            _il_block = ""

                        rebuilt_prompt = _build_article_prompt_from_title(
                            str(title or ""),
                            template_snapshot,
                            theme=_normalize_article_theme(st.session_state.get("unif_text_theme")),
                            target_domain=_resolve_tab0_target_domain(st.session_state.get("unif_text_theme")),
                            include_pin_intro_instruction=include_pin_intro_instruction,
                            prompts_per_section=int(st.session_state.get("unif_text_prompts_per_section") or 2),
                            internal_links_block=_il_block,
                        )
                        prompt_saved = str(prompt or "").strip()
                        prompt_rebuilt = str(rebuilt_prompt or "").strip()

                        if prompt_saved and prompt_saved != prompt_rebuilt:
                            st.caption("Current prompt from the active UI settings:")
                            st.code(prompt_rebuilt)
                            st.caption("Prompt actually used for this result:")
                            st.code(prompt_saved)
                        else:
                            st.code(prompt_rebuilt)
                    except Exception as _e_prompt:
                        # Fallback to saved prompt if rebuild fails
                        st.code(prompt or "")

                # Manual fallback helpers: choose Chrome profile, copy full prompt, open profile window.
                # This helps when automated multi-window generation fails and you want to generate/paste JSON manually.
                try:
                    template_snapshot = str(st.session_state.get("unif_text_template") or "")

                    # If Tab0 pin-mode is enabled and this idx has a mapped pin image, we should build a prompt
                    # consistent with the automated flow (includes pin intro instruction).
                    include_pin_intro_instruction = False
                    try:
                        if bool(st.session_state.get("unif_text_use_pin_images")) and isinstance(pin_map_ui, dict):
                            include_pin_intro_instruction = bool(pin_map_ui.get(idx))
                    except Exception:
                        include_pin_intro_instruction = False

                    _il_block = ""
                    try:
                        if bool(st.session_state.get("unif_il_enabled")):
                            posts = st.session_state.get("unif_il_recent_posts") or []
                            # Auto-fetch if cache is empty (same as bulk run)
                            if not posts:
                                try:
                                    posts = _wp_fetch_recent_posts(
                                        base_url=str(st.session_state.get("unif_il_base_url") or ""),
                                        username=str(st.session_state.get("unif_il_username") or ""),
                                        app_password=str(st.session_state.get("unif_il_app_password") or ""),
                                        verify_ssl=bool(st.session_state.get("unif_il_verify_ssl", True)),
                                        limit=int(st.session_state.get("unif_il_limit") or 60),
                                        timeout_s=30,
                                    )
                                    st.session_state.unif_il_recent_posts = posts
                                    st.session_state.unif_il_recent_posts_ts = datetime.now().isoformat(timespec="seconds")
                                    st.session_state.unif_il_recent_posts_error = None
                                except Exception as _e_il_auto_manual:
                                    st.session_state.unif_il_recent_posts = []
                                    st.session_state.unif_il_recent_posts_ts = None
                                    st.session_state.unif_il_recent_posts_error = str(_e_il_auto_manual)
                                    posts = []

                            _il_block = _build_internal_links_block(
                                posts,
                                include_titles=bool(st.session_state.get("unif_il_include_titles")),
                            )
                    except Exception:
                        _il_block = ""

                    manual_prompt = str(
                        _build_article_prompt_from_title(
                            str(title or ""),
                            template_snapshot,
                            theme=_normalize_article_theme(st.session_state.get("unif_text_theme")),
                            target_domain=_resolve_tab0_target_domain(st.session_state.get("unif_text_theme")),
                            include_pin_intro_instruction=include_pin_intro_instruction,
                            prompts_per_section=int(st.session_state.get("unif_text_prompts_per_section") or 2),
                            internal_links_block=_il_block,
                        )
                        or ""
                    ).strip()

                    if manual_prompt:
                        b0, b1, b2, _sp = st.columns([0.8, 1.25, 1.55, 6.4], gap="small", vertical_alignment="bottom")

                        with b0:
                            # Default: round-robin through configured profiles (unif_text_profile_numbers)
                            profile_numbers_str = str(st.session_state.get("unif_text_profile_numbers", "") or "").strip()
                            _profile_numbers: list[int] = []
                            if profile_numbers_str:
                                try:
                                    _profile_numbers = [int(x.strip()) for x in profile_numbers_str.split(",") if x.strip()]
                                except Exception:
                                    _profile_numbers = []

                            _fallback_prof = 1
                            if _profile_numbers:
                                _fallback_prof = _profile_numbers[(int(idx) - 1) % len(_profile_numbers)]
                            else:
                                try:
                                    _fallback_prof = max(1, int(idx) or 1)
                                except Exception:
                                    _fallback_prof = 1

                            prof_key = f"unif_text_manual_profile_num_{idx}"
                            if prof_key not in st.session_state:
                                st.session_state[prof_key] = _fallback_prof

                            st.number_input(
                                "Profile",
                                min_value=1,
                                max_value=999,
                                step=1,
                                key=prof_key,
                                help="Chrome profile number to use for manual Gemini generation (user-data-dir = <base>_<N>).",
                                label_visibility="collapsed",
                            )

                        with b1:
                            _clipboard_copy_text_button(
                                label="Copy prompt",
                                text=manual_prompt,
                                key=f"unif_text_manual_copy_prompt_{idx}",
                                help_text="Copies the full prompt for manual Gemini article JSON generation.",
                            )

                        with b2:
                            if st.button("Open profile", key=f"unif_text_manual_open_profile_{idx}"):
                                try:
                                    prof_num = int(st.session_state.get(prof_key, 1) or 1)
                                except Exception:
                                    prof_num = 1

                                base_profile_name = _normalize_user_data_dir(st.session_state.unif_text_user_data_dir) or st.session_state.unif_text_user_data_dir
                                prof_path = f"{base_profile_name}_{prof_num}"
                                prof_path_norm = _normalize_user_data_dir(prof_path) or prof_path

                                _open_chrome_window_with_profile(
                                    executable_path=st.session_state.unif_text_exe_path or None,
                                    user_data_dir=str(prof_path_norm),
                                    url=st.session_state.unif_text_url,
                                )
                except Exception:
                    pass

                rev = _get_article_rev(idx)

                edited_text = st.text_area(
                    "Текст статьи",
                    value=text,
                    height=260,
                    key=f"unif_text_out_{idx}_{rev}",
                )

                # Button to refresh prompts after manual text edits
                if st.button("🔄 Обновить промпты", key=f"unif_refresh_prompts_{idx}_{rev}", help="Пересчитать количество промптов после редактирования текста"):
                    # Update the text in session state with edited value
                    for j, rr in enumerate(st.session_state.unif_text_results):
                        if int(rr.get("idx") or 0) == idx:
                            st.session_state.unif_text_results[j]["text"] = edited_text
                            break
                    # Bump revision to force UI update
                    _bump_article_rev(idx)
                    st.rerun()

                # Open Tab 1 (image generation) in a new browser tab and prefill prompts.
                # Prefer structured JSON output (no guessing). Fallback to regex extraction.
                # Use edited_text to ensure we're parsing the current (possibly edited) version
                current_text = edited_text if edited_text else text
                prompts_per_section_ui = int(st.session_state.get("unif_text_prompts_per_section") or 2)
                article_json = _try_parse_article_json(current_text)
                if isinstance(article_json, dict):
                    try:
                        img_prompts = _article_json_image_prompts_mode(article_json, prompts_per_section=prompts_per_section_ui)
                    except Exception:
                        img_prompts = []
                else:
                    try:
                        img_prompts = _extract_image_prompts_from_article(current_text)
                    except Exception:
                        img_prompts = []

                # Gutenberg/preview helper block should show for every successful text,
                # even if we failed to parse image prompts.
                try:
                    import base64 as _b64
                    import json as _json

                    # 1) Optional link to image generation (only when we have prompts)
                    if img_prompts:
                        # Prompt routing rules:
                        # - Standard mode: first 4 prompts -> Pro tab, rest -> Fast tab
                        # - Pin image mode: Pro tab gets prompts 2-4 (skip prompt #1),
                        #   Fast tab gets prompts 5..end (prompt #1 is discarded)
                        use_pin_for_this_article = bool(st.session_state.get("unif_text_use_pin_images"))
                        if use_pin_for_this_article:
                            pro_prompts = img_prompts[1:4]
                            fast_prompts = img_prompts[4:]
                        else:
                            pro_prompts = img_prompts[:4]
                            fast_prompts = img_prompts[4:]

                        payload = _json.dumps(
                            {"title": title, "pro_prompts": pro_prompts, "fast_prompts": fast_prompts},
                            ensure_ascii=False,
                        )
                        encoded = _b64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
                        href = f"?load_img_prompts={encoded}"
                        # Show prompt/section consistency next to the prompt count.
                        status = None
                        try:
                            if isinstance(article_json, dict):
                                status = _article_json_prompt_coverage_status(
                                    article_json,
                                    prompts_per_section=int(st.session_state.get("unif_text_prompts_per_section") or 2),
                                )
                        except Exception:
                            status = None

                        badge_html = ""
                        try:
                            if isinstance(status, dict) and status.get("prompts_expected") is not None:
                                ok = bool(status.get("ok"))
                                sec_n = int(status.get("sections") or 0)
                                exp_n = int(status.get("prompts_expected") or 0)
                                found_n = int(status.get("prompts_found") or 0)
                                missing_n = int(status.get("missing") or 0)
                                color = "#2e7d32" if ok else "#c62828"
                                label = "OK" if ok else "НЕ СХОДИТСЯ"
                                extra = f"; не хватает: {missing_n}" if (not ok and missing_n) else ""
                                badge_html = (
                                    f"<span style=\"margin-left:10px;font-size:13px;color:{color};\">"
                                    f"{label} — секций: {sec_n}, промптов: {found_n}/{exp_n}{extra}"
                                    f"</span>"
                                )
                            elif isinstance(article_json, dict):
                                badge_html = "<span style=\"margin-left:10px;font-size:13px;color:#666;\">(проверка секций недоступна)</span>"
                        except Exception:
                            badge_html = ""

                        # Prefer non-dedup count from JSON status for the label (more intuitive).
                        prompt_count_label = len(img_prompts)
                        try:
                            if isinstance(status, dict) and status.get("prompts_found") is not None:
                                prompt_count_label = int(status.get("prompts_found") or 0)
                        except Exception:
                            prompt_count_label = len(img_prompts)

                        st.markdown(
                            f"<div><a href=\"{href}\" target=\"_blank\">Открыть генерацию картинок для статьи (промптов: {prompt_count_label})</a>{badge_html}</div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.caption("Не нашёл строки Featured Image / Prompt 1 / Prompt 2 в тексте (генератор картинок не откроется)")

                    # 2) Gutenberg helper (always)
                    # Domain should not depend on "images per section".
                    # Use the domain from the uploads base URL input (Tab0), fallback to nestingmuse.com.
                    _base = str(st.session_state.get("unif_tab0_uploads_base_url") or "").strip()
                    _gb_domain = "nestingmuse.com"
                    try:
                        import urllib.parse as _urlparse
                        host = _urlparse.urlparse(_base if "://" in _base else "https://" + _base).netloc
                        if host:
                            _gb_domain = host
                    except Exception:
                        pass
                    gutenberg_url = f"https://{_gb_domain}/wp-admin/post-new.php"

                    # Domain is controlled in Tab0 via "Domain for Gutenberg/uploads" selector + Base URL.
                    # (No per-article selector here to avoid duplicated controls.)

                    # Recompute in case base URL was changed above
                    _base = str(st.session_state.get("unif_tab0_uploads_base_url") or "").strip()
                    _gb_domain = "nestingmuse.com"
                    try:
                        import urllib.parse as _urlparse
                        host = _urlparse.urlparse(_base if "://" in _base else "https://" + _base).netloc
                        if host:
                            _gb_domain = host
                    except Exception:
                        pass
                    gutenberg_url = f"https://{_gb_domain}/wp-admin/post-new.php"

                    
                    st.markdown(
                        f"<a href=\"{gutenberg_url}\" target=\"_blank\">Открыть Gutenberg (новый пост)</a>",
                        unsafe_allow_html=True,
                    )
                    # (rendered above)

                    # Continue rendering the rest of the article UI
                    

                    if isinstance(article_json, dict):
                        cleaned_for_wp = _article_json_to_markdown(article_json)
                    else:
                        cleaned_for_wp = _clean_article_for_gutenberg(current_text)

                    # Reliable clipboard copy: must happen inside a real user gesture (HTML button click).
                    # Streamlit button click is not considered a clipboard "user gesture" by most browsers.
                    b64_txt = _b64.b64encode(cleaned_for_wp.encode("utf-8")).decode("ascii")
                    html_for_wp = _article_markdownish_to_html(cleaned_for_wp)
                    html_for_wp_wrapped = f"<div>{html_for_wp}</div>"
                    b64_html = _b64.b64encode(html_for_wp_wrapped.encode("utf-8")).decode("ascii")

                    # Gutenberg blocks:
                    # - Prefer structured JSON renderer (lets us inject images per section)
                    # - Fallback to markdown-ish renderer
                    blocks_for_wp = ""
                    try:
                        if isinstance(article_json, dict):
                            # Optional: inject local image paths into Gutenberg blocks (per-section)
                            imgs_by_sec = None
                            try:
                                imgs_by_sec = _get_tab0_images_for_article(idx, article_json)
                            except Exception:
                                imgs_by_sec = None
                            blocks_for_wp = _article_json_to_gutenberg_blocks(
                                article_json,
                                image_paths_by_section=imgs_by_sec,
                                include_title_h1=False,
                            )
                        else:
                            blocks_for_wp = _article_markdownish_to_gutenberg_blocks(cleaned_for_wp)
                    except Exception:
                        blocks_for_wp = _article_markdownish_to_gutenberg_blocks(cleaned_for_wp)

                    b64_blocks = _b64.b64encode(blocks_for_wp.encode("utf-8")).decode("ascii")

                    st.download_button(
                        "Скачать текст статьи для Gutenberg (.txt)",
                        data=cleaned_for_wp,
                        file_name=f"article_{idx}.txt",
                        mime="text/plain",
                        key=f"unif_wp_dl_{idx}_{rev}",
                    )

                    # Excerpt copy (prefer JSON)
                    excerpt_val = ""
                    if isinstance(article_json, dict):
                        excerpt_val = str(article_json.get("excerpt") or "").strip()
                    if not excerpt_val:
                        # fallback for non-JSON outputs
                        try:
                            import re as _re
                            m = _re.search(r"^Excerpt:\s*\[(.*)\]\s*$", text or "", flags=_re.IGNORECASE | _re.MULTILINE)
                            if m:
                                excerpt_val = (m.group(1) or "").strip()
                        except Exception:
                            pass

                    b64_excerpt = _b64.b64encode(excerpt_val.encode("utf-8")).decode("ascii")

                    components.html(
                        f"""
                        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                          <button id="wp_copy_btn_{idx}_{rev}" style="padding:6px 10px;">Copy (HTML formatting)</button>
                          <button id="wp_copy_blocks_btn_{idx}_{rev}" style="padding:6px 10px;">Copy (Gutenberg Blocks)</button>
                          <button id="wp_copy_excerpt_btn_{idx}_{rev}" style="padding:6px 10px;">Copy Excerpt</button>
                          <span id="wp_copy_status_{idx}_{rev}" style="font-size:13px;"></span>
                        </div>
                        <script>
                        (function(){{
                          const btn = document.getElementById('wp_copy_btn_{idx}_{rev}');
                          const btnBlocks = document.getElementById('wp_copy_blocks_btn_{idx}_{rev}');
                          const btnExcerpt = document.getElementById('wp_copy_excerpt_btn_{idx}_{rev}');
                          const st = document.getElementById('wp_copy_status_{idx}_{rev}');
                          const b64Text = '{b64_txt}';
                          const b64Html = '{b64_html}';
                          const b64Blocks = '{b64_blocks}';
                          const b64Excerpt = '{b64_excerpt}';
                          function b64ToUtf8(b64){{
                            const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
                            return new TextDecoder('utf-8').decode(bytes);
                          }}

                          btn.addEventListener('click', async () => {{
                            try {{
                              const txt = b64ToUtf8(b64Text);
                              const html = b64ToUtf8(b64Html);

                              if (navigator.clipboard && navigator.clipboard.write && window.ClipboardItem) {{
                                const item = new ClipboardItem({{
                                  'text/plain': new Blob([txt], {{type: 'text/plain'}}),
                                  'text/html': new Blob([html], {{type: 'text/html'}})
                                }});
                                await navigator.clipboard.write([item]);
                              }} else {{
                                await navigator.clipboard.writeText(txt);
                              }}

                              st.textContent = 'Copied (HTML). Paste in Gutenberg (Ctrl+V).';
                            }} catch(e) {{
                              st.textContent = 'Clipboard blocked. Use preview/download.';
                            }}
                          }});

                          btnBlocks.addEventListener('click', async () => {{
                            try {{
                              const blocks = b64ToUtf8(b64Blocks);
                              await navigator.clipboard.writeText(blocks);
                              st.textContent = 'Copied (Gutenberg Blocks). In WP: open Code editor, paste, then switch back to Visual.';
                            }} catch(e) {{
                              st.textContent = 'Clipboard blocked. Use preview/download.';
                            }}
                          }});

                          btnExcerpt.addEventListener('click', async () => {{
                            try {{
                              const ex = b64ToUtf8(b64Excerpt);
                              await navigator.clipboard.writeText(ex);
                              st.textContent = 'Copied excerpt. Paste into WP excerpt field.';
                            }} catch(e) {{
                              st.textContent = 'Clipboard blocked. Copy excerpt from preview.';
                            }}
                          }});
                        }})();
                        </script>
                        """,
                        height=60,
                    )

                    if excerpt_val:
                        st.caption(f"Excerpt: {excerpt_val}")

                    with st.expander("Чистая статья для Gutenberg (preview)", expanded=False):
                        st.text_area(
                            "",
                            value=cleaned_for_wp,
                            height=260,
                            key=f"unif_wp_clean_preview_{idx}_{rev}",
                        )
                except Exception:
                    # Don't break the whole article card if any helper fails
                    if img_prompts:
                        st.caption(f"Промптов для картинок: {len(img_prompts)}")

                # Regenerate single title (sync)
                if st.button(f"Пересоздать #{idx}", key=f"unif_text_regen_{idx}"):
                    url_snapshot = st.session_state.unif_text_url
                    headless_snapshot = bool(st.session_state.unif_text_headless)
                    exe_snapshot = st.session_state.unif_text_exe_path or None
                    base_profile_snapshot = _normalize_user_data_dir(st.session_state.unif_text_user_data_dir) or st.session_state.unif_text_user_data_dir
                    timeout_s = int(st.session_state.unif_text_timeout_s)
                    retries = int(st.session_state.unif_text_retries)
                    template_snapshot = st.session_state.unif_text_template

                    # Profile selection for single-item regeneration
                    # - empty: prefer <base>_1 if exists, otherwise clone base
                    # - number N: use <base>_N if exists, otherwise fallback to <base>_1/clone
                    # - path: use the given user-data-dir directly (no suffixing)
                    regen_profile_raw = str(st.session_state.get("unif_text_regen_profile", "") or "").strip()

                    def _resolve_regen_profile_dir(base_profile: str, raw: str) -> tuple[str, str | None]:
                        """Return (profile_dir, tmp_dir_to_cleanup)."""
                        # Raw path: if it looks like a path (contains slash or drive) and exists, use it.
                        raw_norm = _normalize_user_data_dir(raw) if raw else None
                        if raw_norm:
                            try:
                                if Path(raw_norm).exists():
                                    return raw_norm, None
                            except Exception:
                                pass

                        # Raw number: treat as suffix
                        num = None
                        if raw:
                            try:
                                num = int(raw)
                            except Exception:
                                num = None

                        # Prefer requested numbered profile
                        if num is not None:
                            try:
                                cand = _normalize_user_data_dir(f"{base_profile}_{num}") or f"{base_profile}_{num}"
                                if Path(cand).exists():
                                    return cand, None
                            except Exception:
                                pass

                        # Fallback to _1
                        try:
                            cand = _normalize_user_data_dir(f"{base_profile}_1") or f"{base_profile}_1"
                            if Path(cand).exists():
                                return cand, None
                        except Exception:
                            pass

                        # Last resort: clone base profile (avoids locks)
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        tmp_dir_local = str(Path(f"tmp_rovodev_article_regen_{ts}_{idx}").resolve())
                        try:
                            _clone_profile_dir(base_profile, tmp_dir_local)
                            return tmp_dir_local, tmp_dir_local
                        except Exception:
                            return base_profile, None

                    prof, tmp_dir = _resolve_regen_profile_dir(base_profile_snapshot, regen_profile_raw)

                    base_img = pin_map_ui.get(idx) if use_pin_mode_ui else None
                    _il_block = ""
                    try:
                        if bool(st.session_state.get("unif_il_enabled")):
                            _il_block = _build_internal_links_block(
                                st.session_state.get("unif_il_recent_posts") or [],
                                include_titles=bool(st.session_state.get("unif_il_include_titles")),
                            )
                    except Exception:
                        _il_block = ""

                    prompt_i = _build_article_prompt_from_title(
                        title,
                        template_snapshot,
                        theme=_normalize_article_theme(st.session_state.get("unif_text_theme")),
                        target_domain=_resolve_tab0_target_domain(st.session_state.get("unif_text_theme")),
                        include_pin_intro_instruction=bool(use_pin_mode_ui and base_img),
                        prompts_per_section=int(st.session_state.get("unif_text_prompts_per_section") or 2),
                        internal_links_block=_il_block,
                    )
                    res = _article_text_worker_with_retries(
                        retries=retries,
                        idx=idx,
                        title=title,
                        prompt=prompt_i,
                        url=url_snapshot,
                        headless=headless_snapshot,
                        executable_path=exe_snapshot,
                        profile_dir=prof,
                        model_choice=model_choice_text,
                        timeout_s=timeout_s,
                        base_image_path=base_img,
                    )
                    try:
                        _theme_for_repair = _normalize_article_theme(st.session_state.get("unif_text_theme"))
                        _domain_for_repair = _resolve_tab0_target_domain(_theme_for_repair)
                        txt_res = str(res.get("text") or "").strip()
                        if txt_res and isinstance(
                            _try_parse_article_json(
                                txt_res,
                                theme=_theme_for_repair,
                                target_domain=_domain_for_repair,
                            ),
                            dict,
                        ):
                            res["text"] = _format_repaired_article_json_text(
                                txt_res,
                                theme=_theme_for_repair,
                                target_domain=_domain_for_repair,
                            )
                    except Exception:
                        pass

                    # Cleanup
                    if tmp_dir:
                        try:
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                        except Exception:
                            pass

                    # Update in-place
                    for j, rr in enumerate(st.session_state.unif_text_results):
                        if int(rr.get("idx") or 0) == idx:
                            st.session_state.unif_text_results[j] = res
                            break

                    # Refresh crash-safe autosave so regenerated article is included
                    _autosave_tab0_article_texts(list(st.session_state.unif_text_results or []))

                    # Bump revision + clear any cached widget values for this idx
                    _bump_article_rev(idx)
                    _clear_article_ui_cache(idx)
                    st.rerun()

    # ---- Featured Image Generation ----
    if results:
        st.markdown("---")
        st.subheader("Генерация Featured Images")
        _feat_theme_ui = _normalize_article_theme(st.session_state.get("unif_text_theme"))
        _feat_domain_ui = _resolve_tab0_target_domain(_feat_theme_ui)
        _feat_spec_ui = _get_featured_image_mode(_feat_theme_ui, _feat_domain_ui)
        if _feat_spec_ui.get("kind") == "fashion":
            st.caption("Сгенерируйте featured images в формате 4x5 для статей, у которых есть промпт Featured Image.")
        else:
            st.caption("Сгенерируйте featured images в формате 2x1 для статей, у которых есть промпт Featured Image.")
        
        # Extract articles with featured image prompts
        articles_with_featured = []
        _current_theme = _normalize_article_theme(st.session_state.get("unif_text_theme"))
        _current_target_domain = _resolve_tab0_target_domain(_current_theme)
        for r in results:
            idx = int(r.get("idx") or 0)
            title = r.get("title") or ""
            text = r.get("text") or ""
            
            # Try JSON first
            article_json = _try_parse_article_json(text)
            if isinstance(article_json, dict):
                feat_prompt = (article_json.get("featured_image") or article_json.get("featuredImage") or "").strip()
            else:
                # Fallback to regex extraction
                try:
                    img_prompts = _extract_image_prompts_from_article(text)
                    feat_prompt = img_prompts[0] if img_prompts else ""
                except Exception:
                    feat_prompt = ""
            
            if feat_prompt:
                articles_with_featured.append(
                    {
                        "idx": idx,
                        "title": title,
                        "featured_prompt": feat_prompt,
                        "theme": _current_theme,
                        "target_domain": _current_target_domain,
                    }
                )
        
        if articles_with_featured:
            st.write(f"Статей с Featured Image промптом: {len(articles_with_featured)}")
            
            # Settings for featured image generation
            feat_cols = st.columns([2, 2, 2, 3])
            with feat_cols[0]:
                feat_parallelism = st.number_input(
                    "Параллельно окон",
                    min_value=1,
                    max_value=12,
                    value=3,
                    key="feat_img_parallelism"
                )
            with feat_cols[1]:
                feat_timeout = st.number_input(
                    "Timeout (сек)",
                    min_value=30,
                    max_value=300,
                    value=180,
                    key="feat_img_timeout"
                )
            with feat_cols[2]:
                if not st.session_state.get("_feat_img_model_default_migrated"):
                    if st.session_state.get("feat_img_model_choice") in (None, "", "Думающая"):
                        st.session_state.feat_img_model_choice = "Быстрая"
                    st.session_state["_feat_img_model_default_migrated"] = True
                feat_model = st.selectbox(
                    "Модель",
                    options=["Быстрая", "Думающая", "Pro"],
                    index=0,
                    key="feat_img_model_choice"
                )
            with feat_cols[3]:
                feat_profile_numbers = st.text_input(
                    "Номера профилей (через запятую)",
                    value="21,22,23,24,25,26,27,28",
                    key="feat_img_profile_numbers",
                    help="Например: 2,4,5 откроет .chrome_automation_profile_2, _4, _5"
                )
            
            if st.button("🖼️ Сгенерировать Featured Images", type="primary", key="generate_featured_btn"):
                import concurrent.futures
                
                url_snap = st.session_state.unif_text_url
                headless_snap = bool(st.session_state.unif_text_headless)
                exe_snap = st.session_state.unif_text_exe_path or None
                
                # Parse profile numbers from input
                profile_numbers_str = feat_profile_numbers.strip()
                profile_numbers = []
                if profile_numbers_str:
                    try:
                        seen_profile_numbers: set[int] = set()
                        for x in profile_numbers_str.split(","):
                            x = x.strip()
                            if not x:
                                continue
                            n = int(x)
                            if n in seen_profile_numbers:
                                continue
                            seen_profile_numbers.add(n)
                            profile_numbers.append(n)
                    except ValueError:
                        st.error("Ошибка: номера профилей должны быть числами через запятую (например: 2,4,5)")
                        st.stop()
                
                if not profile_numbers:
                    st.error("Укажите хотя бы один номер профиля")
                    st.stop()
                
                # Determine base directory for saving featured images
                base_dir_featured = _get_run_base_dir()
                
                # Prepare profile pool with specific numbered profiles
                profile_pool_feat: queue.Queue = queue.Queue()
                base_profile_name = ".chrome_automation_profile"
                
                for prof_num in profile_numbers:
                    prof_path = f"{base_profile_name}_{prof_num}"
                    prof_path_norm = _normalize_user_data_dir(prof_path) or prof_path
                    
                    # Check if profile exists
                    try:
                        if not Path(prof_path_norm).exists():
                            st.warning(f"Профиль {prof_path} не найден, пропускаем")
                            continue
                    except Exception:
                        pass
                    
                    profile_pool_feat.put({"dir": prof_path_norm, "is_temp": False, "num": prof_num})
                
                if profile_pool_feat.empty():
                    st.error("Не найдено ни одного указанного профиля")
                    st.stop()
                
                num_profiles = profile_pool_feat.qsize()
                max_workers_feat = min(int(feat_parallelism), num_profiles)
                st.info(f"Доступно профилей: {num_profiles}, будет использовано окон: {max_workers_feat}")
                
                def _worker_feat(art: dict) -> dict:
                    slot = profile_pool_feat.get()
                    try:
                        result = _featured_image_worker(
                            idx=art["idx"],
                            title=art["title"],
                            featured_prompt=art["featured_prompt"],
                            theme=art.get("theme"),
                            target_domain=art.get("target_domain"),
                            url=url_snap,
                            headless=headless_snap,
                            executable_path=exe_snap,
                            profile_dir=slot["dir"],
                            model_choice=feat_model,
                            timeout_s=int(feat_timeout),
                            base_dir=base_dir_featured,
                        )
                        # Preserve the featured_prompt in the result for display and regeneration
                        result["featured_prompt"] = art["featured_prompt"]
                        result["title"] = art["title"]
                        result["theme"] = art.get("theme")
                        result["target_domain"] = art.get("target_domain")
                        return result
                    finally:
                        profile_pool_feat.put(slot)
                
                feat_results: list[dict] = []
                with st.spinner(f"Генерация featured images ({len(articles_with_featured)} шт.)..."):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers_feat) as ex:
                        futs = {ex.submit(_worker_feat, a): a["idx"] for a in articles_with_featured}
                        for fut in concurrent.futures.as_completed(futs):
                            try:
                                res = fut.result()
                                feat_results.append(res)
                            except Exception as e:
                                idx_err = futs[fut]
                                # Try to preserve title/prompt for the failed item so user can regenerate it.
                                art = next((a for a in articles_with_featured if a.get("idx") == idx_err), None)
                                feat_results.append(
                                    {
                                        "idx": idx_err,
                                        "title": (art or {}).get("title", ""),
                                        "featured_prompt": (art or {}).get("featured_prompt", ""),
                                        "theme": (art or {}).get("theme"),
                                        "target_domain": (art or {}).get("target_domain"),
                                        "error": str(e),
                                    }
                                )
                
                # Store results in session state
                if "unif_featured_results" not in st.session_state:
                    st.session_state.unif_featured_results = []
                st.session_state.unif_featured_results = feat_results
                
                st.success(f"Готово! Сгенерировано {len([r for r in feat_results if r.get('saved_path')])} featured images.")
                st.rerun()
            
            # Show featured image results
            if "unif_featured_results" in st.session_state and st.session_state.unif_featured_results:
                st.markdown("### Результаты генерации Featured Images")
                
                # Initialize webp conversion state
                if "unif_featured_webp" not in st.session_state:
                    st.session_state.unif_featured_webp = {}

                def _featured_image_rev(saved_path_value) -> str:
                    """Stable UI revision for the current featured image file."""
                    import hashlib

                    saved_path_str = str(saved_path_value or "")
                    if not saved_path_str:
                        return "none"
                    try:
                        p = Path(saved_path_str)
                        stt = p.stat() if p.exists() else None
                        sig = f"{saved_path_str}|{getattr(stt, 'st_mtime_ns', '')}|{getattr(stt, 'st_size', '')}"
                    except Exception:
                        sig = saved_path_str
                    return hashlib.md5(sig.encode("utf-8")).hexdigest()[:10] if sig else "none"

                def _featured_margin1_key(idx_value, image_rev_value: str) -> str:
                    return f"feat_wm_margin1_{idx_value}_{image_rev_value}"

                def _unique_featured_webp_path(base: Path) -> Path:
                    """Return a unique WebP path by adding a timestamp suffix if needed."""
                    if not base.exists():
                        return base
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    candidate = base.with_name(f"{base.stem}_{ts}{base.suffix}")
                    if not candidate.exists():
                        return candidate
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    return base.with_name(f"{base.stem}_{ts}{base.suffix}")

                def _featured_watermark_margins(saved_path_value, use_coords1: bool) -> tuple[int, int, str]:
                    """Featured-only hardcoded Photoshop margins.

                    coords 1:
                    - 1024x506-ish images -> 65/65
                    - 1456x720-ish images -> 90/90
                    coords 2:
                    - 1024x506-ish images -> 22/22
                    - otherwise 25/25
                    """
                    if not bool(use_coords1):
                        try:
                            from PIL import Image as _PILImage

                            with _PILImage.open(saved_path_value) as im:
                                w2, h2 = int(im.width), int(im.height)
                            if abs(w2 - 1024) <= 40 and abs(h2 - 506) <= 40:
                                return 22, 22, f"координаты 2: 22/22 ({w2}x{h2})"
                            return 25, 25, f"координаты 2: 25/25 ({w2}x{h2})"
                        except Exception:
                            pass
                        return 25, 25, "координаты 2: 25/25"

                    dims: tuple[int, int] | None = None
                    try:
                        from PIL import Image as _PILImage

                        with _PILImage.open(saved_path_value) as im:
                            dims = (int(im.width), int(im.height))
                    except Exception:
                        dims = None

                    if dims:
                        w, h = dims
                        if (abs(w - 1456) <= 40 and abs(h - 720) <= 40) or (w >= 1400 and h >= 650):
                            return 90, 90, f"координаты 1: 90/90 ({w}x{h})"
                        if abs(w - 1024) <= 40 and abs(h - 506) <= 40:
                            return 65, 65, f"координаты 1: 65/65 ({w}x{h})"
                        return 65, 65, f"координаты 1: 65/65 ({w}x{h})"

                    return 65, 65, "координаты 1: 65/65"

                def _convert_featured_saved_to_webp(idx_value, saved_path_value, image_rev_value: str, use_coords1: bool) -> tuple[bool, str, str | None]:
                    from convert_to_webp import convert_image_to_webp

                    current_saved_path = Path(saved_path_value)
                    margin_left, margin_bottom, margin_label = _featured_watermark_margins(current_saved_path, use_coords1)
                    filled_path, ps_err = _remove_watermark_with_photoshop_single(
                        current_saved_path,
                        size_or_scale=13,
                        out_format="PNG",
                        mode="scale",
                        margin_left=margin_left,
                        margin_bottom=margin_bottom,
                        force=True,
                        unique=True,
                    )
                    if not filled_path:
                        return False, f"Ошибка удаления watermark ({margin_label}): {ps_err}", None

                    webp_out_path = _unique_featured_webp_path(filled_path.with_suffix(".webp"))
                    success, err = convert_image_to_webp(
                        filled_path,
                        webp_out_path,
                        quality=85,
                        lossless=False,
                        keep_metadata=True,
                        method=6,
                        icc_profile=True,
                    )
                    if not success:
                        return False, f"Ошибка конвертации ({margin_label}): {err}", None

                    webp_key = f"webp_{idx_value}_{image_rev_value}"
                    st.session_state.unif_featured_webp[webp_key] = str(webp_out_path)
                    return True, f"{margin_label} -> {webp_out_path.name}", str(webp_out_path)

                featured_bulk_items: list[dict] = []
                for _res in st.session_state.unif_featured_results:
                    try:
                        _idx = _res.get("idx")
                        _saved = str(_res.get("saved_path") or "").strip()
                        if not _saved or not Path(_saved).exists():
                            continue
                        _rev = _featured_image_rev(_saved)
                        _use_coords1 = bool(st.session_state.get(_featured_margin1_key(_idx, _rev), True))
                        featured_bulk_items.append(
                            {
                                "idx": _idx,
                                "title": _res.get("title", ""),
                                "saved_path": _saved,
                                "image_rev": _rev,
                                "use_coords1": _use_coords1,
                            }
                        )
                    except Exception:
                        continue

                if featured_bulk_items:
                    coords1_count = sum(1 for item in featured_bulk_items if bool(item.get("use_coords1")))
                    st.caption(
                        f"Featured watermark bulk: координаты 1 = {coords1_count} | "
                        f"координаты 2 = {len(featured_bulk_items) - coords1_count}"
                    )
                    if st.button(
                        "🧽 Убрать watermark и конвертировать в WebP для ВСЕХ Featured Images",
                        type="primary",
                        key="feat_bulk_webp_all",
                    ):
                        prog_feat_webp = st.progress(0)
                        status_feat_webp = st.empty()
                        ok_count = 0
                        bulk_errors: list[str] = []
                        for n, item in enumerate(featured_bulk_items, start=1):
                            status_feat_webp.text(f"{n}/{len(featured_bulk_items)}: Featured #{item.get('idx')} watermark -> WebP...")
                            ok, msg, _out_path = _convert_featured_saved_to_webp(
                                item.get("idx"),
                                item.get("saved_path"),
                                str(item.get("image_rev") or "none"),
                                bool(item.get("use_coords1")),
                            )
                            if ok:
                                ok_count += 1
                            else:
                                bulk_errors.append(f"#{item.get('idx')}: {msg}")
                            prog_feat_webp.progress(int(n / max(1, len(featured_bulk_items)) * 100))

                        status_feat_webp.text("Featured watermark -> WebP: готово")
                        st.success(f"Featured WebP готово: {ok_count}/{len(featured_bulk_items)}")
                        if bulk_errors:
                            st.warning("Есть ошибки при bulk-обработке Featured Images")
                            with st.expander("Ошибки Featured bulk", expanded=False):
                                for err in bulk_errors[:100]:
                                    st.write("- ", err)
                
                for res in st.session_state.unif_featured_results:
                    idx = res.get("idx")
                    title = res.get("title", "")
                    saved_path = res.get("saved_path")
                    error = res.get("error")
                    
                    with st.container(border=True):
                        st.markdown(f"**#{idx}. {title}**")

                        # We want the user to be able to regenerate even if the generation failed.
                        feat_prompt = (res.get("featured_prompt") or "").strip()

                        if error:
                            st.error(f"Ошибка: {error}")

                        if saved_path:
                            st.success(f"✅ Сохранено: {saved_path}")
                            try:
                                st.image(saved_path, width=400)
                            except Exception:
                                pass
                            _card_image_rev = _featured_image_rev(saved_path)
                            _card_margin1_key = _featured_margin1_key(idx, _card_image_rev)
                            use_featured_coords1 = st.checkbox(
                                "Обрезать watermark по координатам 1",
                                value=True,
                                key=_card_margin1_key,
                                help="Если включено: для 1024x506 используется 65/65, для 1456x720 используется 90/90. Если выключено: для 1024x506 используется 22/22, иначе 25/25.",
                            )
                            _ml_preview, _mb_preview, _margin_preview_label = _featured_watermark_margins(
                                saved_path,
                                bool(use_featured_coords1),
                            )
                            st.caption(f"Watermark crop: {_margin_preview_label}")

                        # Show the prompt used (also in error state)
                        if feat_prompt:
                            with st.expander("Промпт", expanded=False):
                                st.text_area(
                                    "Featured Image Prompt",
                                    value=feat_prompt,
                                    height=100,
                                    key=f"feat_prompt_view_{idx}",
                                    disabled=True,
                                )

                        # Manual mode helper buttons (like bulk Pro tab): copy full prompt, copy the current base image, open profile
                        if feat_prompt:
                            try:
                                res_theme = res.get("theme") or _normalize_article_theme(st.session_state.get("unif_text_theme"))
                                res_target_domain = res.get("target_domain") or _resolve_tab0_target_domain(res_theme)
                                feat_spec = _get_featured_image_mode(res_theme, res_target_domain)
                                full_manual_prompt = _build_featured_image_full_prompt(
                                    feat_prompt,
                                    theme=res_theme,
                                    target_domain=res_target_domain,
                                )
                            except Exception:
                                full_manual_prompt = feat_prompt
                                feat_spec = _get_featured_image_mode(
                                    _normalize_article_theme(st.session_state.get("unif_text_theme")),
                                    _resolve_tab0_target_domain(st.session_state.get("unif_text_theme")),
                                )

                            # Per-article profile selection (so you can open multiple Gemini windows in parallel)
                            b0, b1, b2, b3, b4, _sp = st.columns([0.8, 1.25, 1.1, 1.15, 1.55, 4.15], gap="small", vertical_alignment="bottom")
                            with b0:
                                # Default: round-robin through configured featured profiles (feat_img_profile_numbers)
                                feat_profile_numbers_str = st.session_state.get("feat_img_profile_numbers", "21,22,23,24,25,26,27,28")
                                try:
                                    _profile_numbers = [int(x.strip()) for x in str(feat_profile_numbers_str).split(",") if x.strip()]
                                except Exception:
                                    _profile_numbers = []
                                _fallback_prof = 2
                                if _profile_numbers:
                                    _fallback_prof = _profile_numbers[(idx - 1) % len(_profile_numbers)]

                                prof_key = f"feat_manual_profile_num_{idx}"
                                if prof_key not in st.session_state:
                                    st.session_state[prof_key] = _fallback_prof
                                st.number_input(
                                    "Profile",
                                    min_value=1,
                                    max_value=999,
                                    step=1,
                                    key=prof_key,
                                    help="Chrome profile number to use for manual Gemini generation (user-data-dir = .chrome_automation_profile_<N>).",
                                    label_visibility="collapsed",
                                )

                            with b1:
                                _clipboard_copy_text_button(
                                    label="Copy prompt",
                                    text=str(full_manual_prompt or ""),
                                    key=f"feat_manual_copy_prompt_{idx}",
                                    help_text="Copies the full prompt (instructions + your scene prompt) for manual Gemini Pro generation.",
                                )
                            with b2:
                                _copy_label = str(feat_spec.get("ratio_button") or "2x1")
                                _base_path = str(feat_spec.get("base_path") or FEATURED_IMAGE_BASE_PATH)
                                if st.button(f"Copy {_copy_label}", key=f"feat_manual_copy_base_{idx}"):
                                    ok = False
                                    try:
                                        if os.name == "nt" and os.path.exists(_base_path):
                                            ok = _copy_image_to_windows_clipboard_via_powershell(_base_path)
                                    except Exception:
                                        ok = False
                                    if not ok:
                                        st.toast("Copy failed", icon=None)
                            with b3:
                                if st.button("Open profile", key=f"feat_manual_open_profile_{idx}"):
                                    # Use per-article selected profile number (manual parallel work)
                                    try:
                                        prof_num = int(st.session_state.get(f"feat_manual_profile_num_{idx}", 2) or 2)
                                    except Exception:
                                        prof_num = 2
                                    base_profile_name = ".chrome_automation_profile"
                                    prof_path = f"{base_profile_name}_{prof_num}"
                                    prof_path_norm = _normalize_user_data_dir(prof_path) or prof_path

                                    # Make manual downloads deterministic per card (avoid multiple cards reading the same ~/Downloads)
                                    try:
                                        manual_dl_dir = str(Path("manual_downloads") / "featured" / f"idx_{idx}" / f"profile_{prof_num}")
                                        os.makedirs(manual_dl_dir, exist_ok=True)
                                        _ensure_chrome_profile_download_dir(str(prof_path_norm), manual_dl_dir)
                                        st.session_state[f"feat_manual_download_dir_{idx}"] = manual_dl_dir
                                    except Exception:
                                        pass

                                    _open_chrome_window_with_profile(
                                        executable_path=st.session_state.unif_text_exe_path or None,
                                        user_data_dir=str(prof_path_norm),
                                        url=st.session_state.unif_text_url,
                                    )
                                    # Remember when/where we opened the profile so we can refresh the downloaded image later.
                                    try:
                                        st.session_state[f"feat_manual_open_profile_ts_{idx}"] = time.time()
                                        st.session_state[f"feat_manual_profile_dir_{idx}"] = str(prof_path_norm)
                                    except Exception:
                                        pass
                                    st.caption(f"Opened profile: {prof_path_norm}")

                            with b4:
                                if st.button(
                                    "Refresh",
                                    key=f"feat_manual_refresh_{idx}",
                                    help="After you manually download an image in the opened profile, click to pull the latest image from that profile's Downloads and show it here.",
                                ):
                                    try:
                                        import re as _re
                                        import shutil

                                        # Resolve profile dir (prefer the one we actually opened)
                                        feat_prof_dir = st.session_state.get(f"feat_manual_profile_dir_{idx}")
                                        if not feat_prof_dir:
                                            # Fallback: use per-article selected profile number
                                            try:
                                                prof_num = int(st.session_state.get(f"feat_manual_profile_num_{idx}", 2) or 2)
                                            except Exception:
                                                prof_num = 2
                                            base_profile_name = ".chrome_automation_profile"
                                            feat_prof_dir = _normalize_user_data_dir(f"{base_profile_name}_{prof_num}") or f"{base_profile_name}_{prof_num}"

                                        # Prefer the deterministic manual downloads directory for this card
                                        dl_dir = st.session_state.get(f"feat_manual_download_dir_{idx}") or _get_profile_downloads_dir(str(feat_prof_dir))
                                        if not dl_dir:
                                            st.warning("Could not resolve Downloads directory")
                                        else:
                                            dlp = Path(dl_dir)
                                            if not dlp.exists():
                                                st.warning(f"Downloads dir not found: {dl_dir}")
                                            else:
                                                # Find newest downloaded image (optionally after the time we opened the profile)
                                                ts0 = st.session_state.get(f"feat_manual_open_profile_ts_{idx}")
                                                cutoff = float(ts0) - 3.0 if ts0 else None

                                                exts = {".png", ".jpg", ".jpeg", ".webp"}
                                                cand = []
                                                for p in dlp.glob("*"):
                                                    try:
                                                        if p.suffix.lower() not in exts:
                                                            continue
                                                        stt = p.stat()
                                                        if cutoff is not None and stt.st_mtime < cutoff:
                                                            continue
                                                        cand.append((stt.st_mtime, p))
                                                    except Exception:
                                                        continue

                                                if not cand:
                                                    st.warning("No recent images found in Downloads (try downloading again or open profile first)")
                                                else:
                                                    cand.sort(key=lambda x: x[0], reverse=True)
                                                    newest = cand[0][1]

                                                    # Save into the same run dir as auto-featured images
                                                    base_dir_featured = _get_run_base_dir()
                                                    os.makedirs(base_dir_featured, exist_ok=True)

                                                    # Keep naming consistent with the worker (but preserve extension)
                                                    title_slug = _re.sub(r"[^\\w\\s-]", "", str(title).lower())
                                                    title_slug = _re.sub(r"[\\s_-]+", "_", title_slug)[:50]
                                                    dest_base = Path(base_dir_featured) / f"{idx}_featured_{title_slug}{newest.suffix.lower()}"
                                                    dest = _unique_path_if_exists(dest_base)

                                                    try:
                                                        shutil.copy2(str(newest), str(dest))
                                                    except Exception:
                                                        # fallback: try raw copy
                                                        dest.write_bytes(newest.read_bytes())

                                                    # Update current result in session_state
                                                    try:
                                                        for ii, rr in enumerate(st.session_state.unif_featured_results):
                                                            if rr.get("idx") == idx:
                                                                rr = dict(rr)
                                                                rr["saved_path"] = str(dest)
                                                                rr["error"] = None
                                                                st.session_state.unif_featured_results[ii] = rr
                                                                break
                                                    except Exception:
                                                        pass

                                                    st.success(f"Updated from Downloads: {dest}")
                                                    st.rerun()
                                    except Exception as e:
                                        st.warning(f"Refresh failed: {e}")

                        def _regen_featured_image_now():
                            """Regenerate featured image for this idx using the stored prompt.

                            We keep it available even when the previous generation failed.
                            """

                            if not feat_prompt:
                                st.warning("Нет промпта для пересоздания")
                                return

                            url_snap = st.session_state.unif_text_url
                            headless_snap = bool(st.session_state.unif_text_headless)
                            exe_snap = st.session_state.unif_text_exe_path or None
                            base_dir_featured = _get_run_base_dir()

                            # Get first available profile from the configured list
                            feat_profile_numbers_str = st.session_state.get("feat_img_profile_numbers", "21,22,23,24,25,26,27,28")
                            try:
                                profile_numbers = [int(x.strip()) for x in feat_profile_numbers_str.split(",") if x.strip()]
                                prof_num = profile_numbers[0] if profile_numbers else 21
                            except Exception:
                                prof_num = 21

                            base_profile_name = ".chrome_automation_profile"
                            prof_path = f"{base_profile_name}_{prof_num}"
                            prof_path_norm = _normalize_user_data_dir(prof_path) or prof_path

                            # Get model choice
                            feat_model_local = st.session_state.get("feat_img_model_choice", "Думающая")
                            feat_timeout_local = int(st.session_state.get("feat_img_timeout", 180))

                            with st.spinner(f"Пересоздаю featured image #{idx}..."):
                                res_theme = res.get("theme") or _normalize_article_theme(st.session_state.get("unif_text_theme"))
                                res_target_domain = res.get("target_domain") or _resolve_tab0_target_domain(res_theme)
                                new_result = _featured_image_worker(
                                    idx=idx,
                                    title=title,
                                    featured_prompt=feat_prompt,
                                    theme=res_theme,
                                    target_domain=res_target_domain,
                                    url=url_snap,
                                    headless=headless_snap,
                                    executable_path=exe_snap,
                                    profile_dir=prof_path_norm,
                                    model_choice=feat_model_local,
                                    timeout_s=feat_timeout_local,
                                    base_dir=base_dir_featured,
                                )

                            # Update result in session state
                            for i, r in enumerate(st.session_state.unif_featured_results):
                                if r.get("idx") == idx:
                                    new_result["featured_prompt"] = feat_prompt
                                    new_result["title"] = title
                                    new_result["theme"] = res_theme
                                    new_result["target_domain"] = res_target_domain
                                    st.session_state.unif_featured_results[i] = new_result

                                    # Clear derived WebP for this idx (any rev)
                                    keys_to_delete = [
                                        k
                                        for k in list(st.session_state.unif_featured_webp.keys())
                                        if isinstance(k, str) and k.startswith(f"webp_{idx}_")
                                    ]
                                    for k in keys_to_delete:
                                        del st.session_state.unif_featured_webp[k]
                                    break

                            st.rerun()

                        # WebP conversion button (only makes sense when we have an image)
                        if saved_path:
                            # WebP conversion button
                            # IMPORTANT: make keys depend on the current image path ("revision") so that after regeneration
                            # we don't keep stale WebP/copy widgets.
                            image_rev = _featured_image_rev(saved_path)
                            webp_key = f"webp_{idx}_{image_rev}"
                            webp_path = st.session_state.unif_featured_webp.get(webp_key)
                            
                            col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 1])

                            with col_btn1:
                                # Always allow reconvert for the CURRENT image (use rev in the button key)
                                if st.button(
                                    "🧽 Убрать watermark и конвертировать в WebP",
                                    key=f"btn_webp_{idx}_{image_rev}",
                                ):
                                    ok, msg, _webp_out = _convert_featured_saved_to_webp(
                                        idx,
                                        saved_path,
                                        image_rev,
                                        bool(use_featured_coords1),
                                    )
                                    if ok:
                                        st.rerun()
                                    else:
                                        st.error(msg)
                                elif webp_path:
                                    st.success(f"✅ WebP: {Path(webp_path).name}")
                            
                            with col_btn2:
                                if webp_path and Path(webp_path).exists():
                                    # Copy path button with JavaScript
                                    # Make DOM ids depend on image_rev to avoid stale JS listeners after regeneration.
                                    safe_path = webp_path.replace(chr(92), chr(92) + chr(92))
                                    copy_html = f"""
                                    <div style=\"margin-top: 0px;\">
                                        <span style=\"display:none\">rev:{image_rev}</span>
                                        <button id=\"copy_btn_{idx}_{image_rev}\" 
                                                style=\"background-color: #ff4b4b; color: white; border: none; 
                                                       padding: 8px 16px; border-radius: 4px; cursor: pointer;
                                                       font-size: 14px; font-family: sans-serif;\">
                                            📋 Скопировать путь
                                        </button>
                                        <span id=\"copy_status_{idx}_{image_rev}\" style=\"margin-left: 10px; color: green;\"></span>
                                    </div>
                                    <script>
                                    (function() {{
                                        const btn = document.getElementById('copy_btn_{idx}_{image_rev}');
                                        const status = document.getElementById('copy_status_{idx}_{image_rev}');
                                        const path = '{safe_path}';

                                        if (!btn) return;

                                        // Ensure we don't accumulate multiple listeners across reruns
                                        btn.onclick = async function() {{
                                            try {{
                                                await navigator.clipboard.writeText(path);
                                                status.textContent = '✅ Скопировано!';
                                                setTimeout(() => {{ status.textContent = ''; }}, 2000);
                                            }} catch (err) {{
                                                status.textContent = '❌ Ошибка: ' + err.message;
                                            }}
                                        }};
                                    }})();
                                    </script>
                                    """
                                    # Some Streamlit versions don't support the `key` argument for components.html
                                    try:
                                        st.components.v1.html(copy_html, height=50, key=f"copy_html_{idx}_{image_rev}")
                                    except TypeError:
                                        st.components.v1.html(copy_html, height=50)
                            
                            with col_btn3:
                                # Regenerate featured image (available when we have an image)
                                if feat_prompt and st.button("🔄 Пересоздать промпт", key=f"regen_feat_prompt_{idx}"):
                                    _regen_featured_image_now()
                        else:
                            st.warning("Не сгенерировано")
                            # Even when generation failed, allow re-generation from the stored prompt.
                            if feat_prompt and st.button("🔄 Пересоздать промпт", key=f"regen_feat_prompt_{idx}"):
                                _regen_featured_image_now()
        else:
            st.info("Нет статей с featured image промптами")


# ---------------- Tab 1: Gemini Generate (defaults reused) ----------------
with tab1:
    st.subheader("Gemini generation via browser (Playwright)")

    # Выбор модели Gemini (отображается выбор перед автозаполнением промпта)
    model_choice = st.selectbox(
        "Модель Gemini для автозапросов",
        ["Быстрая", "Думающая"],
        index=0,
        key="unif_model_choice",
        help="Будет выбрана в интерфейсе перед вставкой промпта."
    )

    url = st.selectbox("URL интерфейса", DEFAULT_URLS, index=0, key="unif_url")
    headless = st.checkbox("Headless режим", value=False, key="unif_headless")
    use_auto_profile = st.checkbox("Отдельный профиль для автоматики (рекомендуется)", value=True, key="unif_auto_profile")
    # Поддержка обновления user-data-dir через временное состояние
    if "_tmp_new_uddir" in st.session_state:
        st.session_state["unif_user_data_dir"] = st.session_state.pop("_tmp_new_uddir")
    user_data_dir = st.text_input("Путь к профилю (user-data-dir)", value=st.session_state.get("unif_user_data_dir", os.path.abspath(".chrome_automation_profile")), key="unif_user_data_dir")
    resolved_user_data_dir = _normalize_user_data_dir(user_data_dir) or os.path.abspath(os.path.expanduser(user_data_dir))
    st.caption("Подсказка: по умолчанию используется .chrome_automation_profile. Можете дописать _1 (например, .chrome_automation_profile_1) для второго аккаунта.")
    st.caption(f"Реально используется: {resolved_user_data_dir}")
    # Используем абсолютный путь дальше по коду
    user_data_dir = resolved_user_data_dir

    # Если пользователь ввёл путь с опечаткой (часто: пропущен `\\` перед .chrome_automation_profile_*),
    # аккуратно «применяем» исправление через временный ключ и rerun.
    # Нельзя напрямую менять st.session_state['unif_user_data_dir'] после создания виджета.
    try:
        raw_input = (st.session_state.get("unif_user_data_dir") or "").strip()
        if raw_input and resolved_user_data_dir and _normalize_user_data_dir(raw_input) != resolved_user_data_dir:
            st.session_state["_tmp_new_uddir"] = resolved_user_data_dir
            st.rerun()
    except Exception:
        pass
    executable_path = st.text_input("Путь к chrome.exe (по умолчанию C:/Program Files/Google/Chrome/Application/chrome.exe)", value=r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", key="unif_exe_path")
    # CDP removed from Tab 1 (simplifies behavior and avoids confusion)
    use_cdp = False
    cdp_url = ""


    # Multiprompt UI
    # Default Prompt #1: random word (stable for this Streamlit session)
    if "unif_pw_prompt_default_word" not in st.session_state:
        import random
        import string

        st.session_state.unif_pw_prompt_default_word = "".join(random.choice(string.ascii_lowercase) for _ in range(8))

    if "unif_pw_prompts" not in st.session_state:
        st.session_state.unif_pw_prompts = [str(st.session_state.unif_pw_prompt_default_word or "")]

    if st.session_state.get("unif_pw_imported_from_query"):
        st.success("Промпты импортированы из статьи (Tab 0) и вставлены в поля ниже")
        # Show only once
        st.session_state.unif_pw_imported_from_query = False

    st.markdown("**Промпты**")
    nprompts = []
    for i, val in enumerate(st.session_state.unif_pw_prompts):
        nv = st.text_input(f"Промпт #{i+1}", value=val, key=f"unif_pw_prompt_{i}")
        nprompts.append(nv)
    col_add, col_rem = st.columns([1,1])
    with col_add:
        if st.button("+ Добавить поле", key="unif_pw_add"):
            st.session_state.unif_pw_prompts.append("")
            st.rerun()
    with col_rem:
        if len(st.session_state.unif_pw_prompts) > 1 and st.button("− Убрать последнее", key="unif_pw_rem"):
            st.session_state.unif_pw_prompts = st.session_state.unif_pw_prompts[:-1]
            st.rerun()
    st.session_state.unif_pw_prompts = nprompts

    def _poll_regen_jobs():
        # Apply completed futures to session_state (must run in main thread)
        jobs = st.session_state.get("unif_regen_jobs", {})
        pending = st.session_state.get("unif_regen_pending", {})  # idx -> list[path]
        changed = False

        for job_id, job in list(jobs.items()):
            fut = job.get("future")
            if not fut or job.get("status") in {"done", "error"}:
                continue
            if fut.done():
                # Release reserved profile (if any)
                try:
                    reserved = job.get("reserved_profile_dir")
                    if reserved:
                        in_use = set(st.session_state.get("unif_regen_profile_in_use") or set())
                        if reserved in in_use:
                            in_use.remove(reserved)
                            st.session_state["unif_regen_profile_in_use"] = in_use
                except Exception:
                    pass

                try:
                    res = fut.result()
                    job["status"] = "done"
                    job["result"] = res
                    idx = int(res.get("idx"))
                    new_saved = res.get("new_saved") or []

                    # Batch mode: don't update UI images immediately.
                    # Store latest result per idx; final commit happens when ALL running jobs finish.
                    pending[str(idx)] = list(new_saved)
                    changed = True
                except Exception as e:
                    job["status"] = "error"
                    job["error"] = str(e)
                    changed = True

        st.session_state["unif_regen_jobs"] = jobs
        st.session_state["unif_regen_pending"] = pending

        # Prune finished jobs to avoid unbounded growth (otherwise each rerun becomes slower over time).
        # Keep running jobs + small tail of recent completed jobs for debugging.
        try:
            _keep_tail = 15
            _running = {k: v for k, v in (jobs or {}).items() if v.get("status") == "running"}
            if _running:
                # when running, keep everything (so we can still read results as they complete)
                pass
            else:
                _done = [(k, v) for k, v in (jobs or {}).items() if v.get("status") in {"done", "error"}]
                # sort by created_at if present
                def _key(it):
                    try:
                        return (it[1].get("created_at") or "")
                    except Exception:
                        return ""
                _done_sorted = sorted(_done, key=_key)
                _done_tail = dict(_done_sorted[-_keep_tail:])
                jobs = {**_done_tail}  # keep only tail
        except Exception:
            pass

        # If nothing is running anymore, commit all pending results at once.
        has_running = any(j.get("status") == "running" for j in (jobs or {}).values())
        if (not has_running) and pending:
            cur = st.session_state.get("unif_fast_saved_paths", [])
            cur_kept = list(cur)

            # Remove all idx that we are going to update
            for idx_s in list(pending.keys()):
                try:
                    idxi = int(idx_s)
                except Exception:
                    continue
                cur_kept = [p for p in cur_kept if _extract_prompt_idx(p) != idxi]

            # Add new files + cleanup old on disk per idx
            for idx_s, new_saved in list(pending.items()):
                try:
                    idxi = int(idx_s)
                except Exception:
                    continue
                try:
                    if new_saved:
                        _cleanup_old_prompt_files(os.path.dirname(new_saved[0]), idxi, list(new_saved))
                except Exception:
                    pass
                cur_kept.extend(list(new_saved))

            st.session_state.unif_fast_saved_paths = cur_kept
            # Keep global last-run list for other tabs (Photoshop/WebP)
            # NOTE: keep fast/pro saved paths separate to avoid Tab1 regen touching Tab2 (pro) outputs.
            # st.session_state.unif_saved_paths = cur_kept
            # Update displayed snapshot only once, after ALL regen jobs finished.
            st.session_state["unif_fast_display_saved_paths"] = list(cur_kept)
            st.session_state["unif_regen_pending"] = {}
            # a few extra reruns so images repaint (autorefresh will stop after counter reaches 0)
            st.session_state["unif_regen_dirty_counter"] = 3

    def _submit_regen_job(idx: int, force_sync: bool = False):
        # Enqueue a regeneration job; safe to call from button click
        with st.session_state["unif_regen_lock"]:
            st.session_state.unif_regen_job_seq += 1
            job_id = f"job_{st.session_state.unif_regen_job_seq}_{idx}"

        # Tab 1 regen should use fast prompts (placeholders depend on this list)
        final_prompts = st.session_state.get("unif_fast_final_prompts") or st.session_state.get("unif_final_prompts", [])
        if not final_prompts:
            st.error("Нет final_prompts. Сначала сгенерируйте картинки (кнопка '2) Сгенерировать все промпты').")
            return
        if idx < 1 or idx > len(final_prompts):
            st.error(f"Некорректный индекс промпта: {idx}")
            return

        # Tab 1: regeneration is always queued/parallel (non-blocking) to allow multiple clicks (multi-window style).
        regen_parallel = True
        if force_sync:
            regen_parallel = True
        # CDP removed from Tab 1, so no CDP gating here.

        if regen_parallel and not st.session_state.get("unif_auto_profile", True):
            st.error("Параллельная перегенерация требует user-data-dir. Включите 'Отдельный профиль для автоматики'.")
            return

        user_data_dir = st.session_state.get("unif_user_data_dir")
        if not user_data_dir:
            st.error("Не задан user-data-dir")
            return

        base_dir = st.session_state.get("unif_last_base_dir", _get_run_base_dir())
        url = st.session_state.get("unif_url", DEFAULT_URLS[0])
        headless = bool(st.session_state.get("unif_headless", False))
        executable_path = st.session_state.get("unif_exe_path") or None
        model_choice = st.session_state.get("unif_model_choice")
        prompt_text = final_prompts[idx - 1]

        # Regen prompt variation: prepend a rotating "polite" word to avoid sending identical prompt to Gemini
        # (Gemini may return identical images for identical prompts).
        try:
            prefixes = ["будь добр", "пожалуйста", "плиз", "please"]
            # remove existing prefix if present
            low = prompt_text.strip().lower()
            for pref in prefixes:
                if low.startswith(pref + " ") or low == pref:
                    prompt_text = prompt_text.strip()[len(pref):].lstrip()
                    break
            counts = st.session_state.get("unif_regen_prefix_counts", {})
            c = int(counts.get(str(idx), 0) or 0)
            pref = prefixes[c % len(prefixes)]
            counts[str(idx)] = c + 1
            st.session_state["unif_regen_prefix_counts"] = counts
            prompt_text = f"{pref} {prompt_text}".strip()
        except Exception:
            pass

        max_workers = int(st.session_state.get("unif_pw_parallelism", 3) or 3)
        max_workers = max(1, min(12, max_workers))

        # If parallel regen is disabled, run synchronously (old behavior) and update UI immediately.
        if not regen_parallel:
            try:
                # Determine output dir for this prompt idx (if possible)
                _saved = st.session_state.get("unif_fast_saved_paths", [])
                _dirs = [os.path.dirname(p) for p in _saved if _extract_prompt_idx(p) == idx]
                base_dir_eff = (_dirs[0] if _dirs else base_dir)

                # Close any existing persistent context to release Chrome profile lock before we re-launch it.
                try:
                    _ctx0 = st.session_state.get("unif_pw_ctx")
                    if _ctx0 is not None:
                        try:
                            _ctx0.close()
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    _pw0 = st.session_state.get("unif_pw_browser")
                    if _pw0 is not None:
                        try:
                            _pw0.stop()
                        except Exception:
                            pass
                except Exception:
                    pass
                st.session_state.unif_pw_ctx = None
                st.session_state.unif_pw_page = None
                st.session_state.unif_pw_browser = None

                # Prefer using already-open Playwright context/page from the main tab.
                # This avoids Chrome profile locks and Windows PermissionDenied on locked DB/cache files.
                # NOTE: We do NOT reuse Playwright objects saved in session_state here.
                # They are thread/greenlet-affine and may crash with:
                # "cannot switch to a different thread (which happens to have exited)".
                # Instead we regenerate using a fresh persistent context on the SAME user-data-dir.
                is_alive = False

                if is_alive:
                    try:
                        try:
                            cur_url = page_live.url or ""
                        except Exception:
                            cur_url = ""
                        if ("gemini.google.com" not in cur_url) and ("aistudio.google.com" not in cur_url):
                            try:
                                page_live.goto(url, wait_until="load")
                            except Exception:
                                pass

                        try:
                            _start_new_chat(page_live)
                        except Exception:
                            pass
                        _wait_input_ready(page_live, timeout_ms=30000)
                        _dismiss_overlays(page_live)

                        try:
                            if model_choice:
                                gph._pick_model(page_live, model_choice)
                        except Exception:
                            pass

                        _, new_saved = _regenerate_prompt(
                            page_live,
                            ctx_live,
                            prompt_text,
                            idx,
                            base_dir_eff,
                            max_images=1,
                            attach_base=False,
                            base_image_path=None,
                            model_choice=model_choice,
                        )
                    except Exception as e:
                        # Playwright objects are thread-affine. If the page/context were created in a different thread
                        # (e.g., during parallel generation), using them here may fail with:
                        # "cannot switch to a different thread (which happens to have exited)".
                        # In that case we silently fall back to an isolated Playwright run below.
                        msg = str(e)
                        if "cannot switch to a different thread" in msg:
                            is_alive = False
                        else:
                            st.error(f"Ошибка при перегенерации: {e}")
                            return

                    if is_alive:
                        # Update session_state list and return
                        if new_saved:
                            try:
                                deleted = _cleanup_old_prompt_files(os.path.dirname(new_saved[0]), idx, new_saved)
                            except Exception:
                                deleted = 0
                            cur = st.session_state.get("unif_fast_saved_paths", [])
                            new_list = [p for p in cur if _extract_prompt_idx(p) != idx]
                            new_list.extend(list(new_saved))
                            st.session_state.unif_fast_saved_paths = new_list
                            # st.session_state.unif_saved_paths = new_list
                            st.success(
                                f"Готово: пересоздано {len(new_saved)} файлов для промпта #{idx}. Удалено старых: {deleted}"
                            )
                        else:
                            st.warning("Перегенерация не вернула файлов")
                        return new_saved

                # Fallback: if there is no live page/context, try isolated run (may fail on Windows if profile is locked).
                result = {"new_saved": [], "error": None}

                def _regen_worker_sync():
                    try:
                        from playwright.sync_api import sync_playwright as _sp
                        p = _sp().start()
                        try:
                            # IMPORTANT: do not reuse the main user-data-dir here.
                            # If the main Tab already has a persistent context open, Chrome profile is locked and
                            # Playwright will fail with "Target page, context or browser has been closed".
                            # Use a lightweight temporary copy of the profile.
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            tmp_root = Path(f"tmp_rovodev_regen_sync_profile_{ts}_{idx}").resolve()

                            def _ignore_profile(dirpath: str, names: list[str]):
                                skip_exact = {
                                    "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
                                    "Crashpad", "Crash Reports",
                                }
                                skip_prefix = ("Singleton",)
                                ignored = []
                                for n in names:
                                    if n in skip_exact or any(n.startswith(p) for p in skip_prefix):
                                        ignored.append(n)
                                        continue
                                    if n.lower() in {"service worker", "serviceworker"}:
                                        ignored.append(n)
                                        continue
                                return ignored

                            # Use the real profile directory (same login/cookies). Before this worker runs we
                            # close any previous persistent context that might be holding a lock.

                            ctx_local = _launch_persistent_ctx_with_retries(
                                p,
                                user_data_dir=user_data_dir,
                                headless=headless,
                                executable_path=executable_path,
                            )
                            pg = ctx_local.new_page()

                            pg.set_default_timeout(30000)
                            try:
                                cur_url = pg.url or ""
                            except Exception:
                                cur_url = ""
                            if ("gemini.google.com" not in cur_url) and ("aistudio.google.com" not in cur_url):
                                try:
                                    pg.goto(url, wait_until="load")
                                except Exception:
                                    pass

                            try:
                                _start_new_chat(pg)
                            except Exception:
                                pass
                            _wait_input_ready(pg, timeout_ms=30000)
                            _dismiss_overlays(pg)

                            try:
                                if model_choice:
                                    gph._pick_model(pg, model_choice)
                            except Exception:
                                pass

                            _, ns = _regenerate_prompt(pg, ctx_local, prompt_text, idx, base_dir_eff, max_images=1, attach_base=False, base_image_path=None)
                            result["new_saved"] = ns or []
                        finally:
                            try:
                                p.stop()
                            except Exception:
                                pass
                            # no tmp profile to cleanup
                            pass
                    except Exception as e:
                        result["error"] = str(e)

                t = threading.Thread(target=_regen_worker_sync, daemon=True)
                t.start()
                t.join()

                if result["error"]:
                    st.error(f"Ошибка при перегенерации: {result['error']}")
                    return

                new_saved = result["new_saved"]
                if new_saved:
                    try:
                        deleted = _cleanup_old_prompt_files(os.path.dirname(new_saved[0]), idx, new_saved)
                    except Exception:
                        deleted = 0
                    cur = st.session_state.get("unif_fast_saved_paths", [])
                    new_list = []
                    for p in cur:
                        if _extract_prompt_idx(p) == idx:
                            continue
                        new_list.append(p)
                    new_list.extend(list(new_saved))
                    st.session_state.unif_fast_saved_paths = new_list
                    # st.session_state.unif_saved_paths = new_list
                    st.success(f"Готово: пересоздано {len(new_saved)} файлов для промпта #{idx}. Удалено старых: {deleted}")
                else:
                    st.warning("Перегенерация не вернула файлов")
                return new_saved
            except Exception as e:
                st.error(f"Ошибка при перегенерации: {e}")
                return

        # Parallel regen enabled: queue job(s)
        # Use at least 2 workers for regen, независимо от unif_pw_parallelism (иначе параллельность "ломается").
        # Regen parallelism follows the UI setting "Параллельно окон (concurrency)".
        # If you set concurrency=1, regen jobs will queue and run one-by-one.
        regen_workers = max(1, int(st.session_state.get("unif_pw_parallelism", 3) or 3))
        st.session_state["unif_regen_executor_target"] = regen_workers

        ex0 = st.session_state.get("unif_regen_executor")
        jobs0 = st.session_state.get("unif_regen_jobs", {})
        has_running0 = any(j.get("status") == "running" for j in (jobs0 or {}).values())

        if ex0 is None:
            st.session_state.unif_regen_executor = ThreadPoolExecutor(max_workers=regen_workers)
        else:
            try:
                cur_w = int(getattr(ex0, "_max_workers", regen_workers))
            except Exception:
                cur_w = regen_workers

            # Recreate executor if concurrency changed (up or down). Only safe when no running jobs.
            if cur_w != regen_workers and not has_running0:
                try:
                    ex0.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                st.session_state.unif_regen_executor = ThreadPoolExecutor(max_workers=regen_workers)

        # Clone the chosen profile to avoid Chrome profile-lock in parallel windows (same approach as multi-window).
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp_root = Path(f"tmp_rovodev_regen_profile_{ts}_{job_id}").resolve()

        def _ignore_profile(dirpath: str, names: list[str]):
            skip_exact = {
                "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
                "Crashpad", "Crash Reports",
                "DawnGraphiteCache", "DawnWebGPUCache", "GraphiteDawnCache",
            }
            skip_prefix = ("Singleton",)
            ignored = []
            for n in names:
                if n in skip_exact or any(n.startswith(p) for p in skip_prefix):
                    ignored.append(n)
                    continue
                if n.lower() in {"service worker", "serviceworker"}:
                    ignored.append(n)
                    continue
                if n.upper() == "LOCK" or n.lower() in {"lockfile", "devtoolsactiveport"}:
                    ignored.append(n)
                    continue
            return ignored

        # Worker: run regenerate in a dedicated profile.
        # If `profile_dir` is a numbered profile (direct), we do NOT delete it.
        # If it is a temporary clone, we clean it up.
        def _worker(profile_dir: str, *, is_temp_clone: bool):
            try:
                with sync_playwright() as p:
                    ctx = _launch_persistent_ctx_with_retries(
                        p,
                        user_data_dir=profile_dir,
                        headless=headless,
                        executable_path=executable_path,
                    )
                    try:
                        page = ctx.new_page()
                        page.set_default_timeout(30000)
                        # Gemini often never reaches full "load"; use resilient navigation.
                        try:
                            _goto_gemini_resilient(page, url, timeout_ms=120000, attempts=3)
                        except Exception:
                            # Fallback to the old behavior
                            try:
                                page.goto(url, wait_until="domcontentloaded")
                            except Exception:
                                page.goto(url, wait_until="load")
                        # Avoid heavy DOM debugging here; it can be slow/hangy on some UI variants.
                        _wait_input_ready(page, timeout_ms=60000)

                        try:
                            if model_choice:
                                gph._pick_model(page, model_choice)
                        except Exception:
                            pass

                        imgs, new_saved = _regenerate_prompt(
                            page,
                            ctx,
                            prompt_text,
                            idx,
                            base_dir,
                            max_images=1,
                            attach_base=False,
                            base_image_path=None,
                        )
                        return {"idx": idx, "new_saved": new_saved, "imgs": imgs, "profile_dir": profile_dir}
                    finally:
                        try:
                            ctx.close()
                        except Exception:
                            pass
            finally:
                if is_temp_clone:
                    shutil.rmtree(profile_dir, ignore_errors=True)

        # Prefer numbered profiles for regen (round-robin). Fallback to a temporary clone if none available.
        reserved_profile_dir = _pick_regen_profile_round_robin()
        if reserved_profile_dir:
            fut = st.session_state.unif_regen_executor.submit(
                _worker,
                reserved_profile_dir,
                is_temp_clone=False,
            )
            profile_dir_for_job = reserved_profile_dir
        else:
            # Fallback: clone base profile (slower but always works)
            try:
                shutil.copytree(user_data_dir, Path(tmp_root), dirs_exist_ok=False, ignore=_ignore_profile)
            except Exception:
                # if copy fails, still try to run directly on the base profile
                pass
            fut = st.session_state.unif_regen_executor.submit(
                _worker,
                str(tmp_root),
                is_temp_clone=True,
            )
            profile_dir_for_job = str(tmp_root)

        st.session_state.unif_regen_jobs[job_id] = {
            "job_id": job_id,
            "idx": idx,
            "status": "running",
            "future": fut,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "profile_dir": profile_dir_for_job,
            "reserved_profile_dir": reserved_profile_dir,
        }

    # Poll completed regen jobs on every rerun
    _poll_regen_jobs()

    # If user changed concurrency during regen, rebuild executor after jobs finish.
    try:
        tgt = int(st.session_state.get("unif_regen_executor_target", 0) or 0)
        ex0 = st.session_state.get("unif_regen_executor")
        jobs0 = st.session_state.get("unif_regen_jobs", {})
        has_running0 = any(j.get("status") == "running" for j in (jobs0 or {}).values())
        if ex0 is not None and tgt > 0 and not has_running0:
            cur_w = int(getattr(ex0, "_max_workers", tgt))
            if cur_w != tgt:
                try:
                    ex0.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                st.session_state.unif_regen_executor = ThreadPoolExecutor(max_workers=tgt)
    except Exception:
        pass

    # Auto-rerun UI (NO page reload) while there are running regen jobs,
    # and a few extra reruns after completion to ensure images update.
    #
    # We use client-side postMessage timer because Streamlit won't rerun on background completion by itself.
    jobs = st.session_state.get("unif_regen_jobs", {})
    has_running = any(j.get("status") == "running" for j in (jobs or {}).values())
    dirty_counter = int(st.session_state.get("unif_regen_dirty_counter", 0) or 0)

    # Decrease counter once jobs are no longer running
    if (not has_running) and dirty_counter > 0:
        st.session_state["unif_regen_dirty_counter"] = dirty_counter - 1
        dirty_counter = dirty_counter - 1

    # Client-side autorefresh while jobs are running.
    #
    # IMPORTANT: `components.html` runs inside a sandboxed iframe (origin "null"), so it cannot reliably
    # trigger Streamlit reruns via postMessage in all environments. The `streamlit-autorefresh` component
    # triggers reruns from the main app and works reliably.
    pending = st.session_state.get("unif_regen_pending", {})
    if has_running or dirty_counter > 0 or bool(pending):
        try:
            from streamlit_autorefresh import st_autorefresh

            # Reduce visible "flicker" during long-running regen: poll slower while running, fast after finish.
            interval = 4000 if has_running else 1000
            st_autorefresh(interval=interval, key="unif_regen_autorefresh")
        except Exception:
            st.warning(
                "Автообновление результатов перегенерации недоступно (не установлен streamlit-autorefresh). "
                "Установите: pip install streamlit-autorefresh. Пока что обновление появится после любого клика в UI."
            )

    with st.sidebar:
        st.markdown("### Gemini image ratio")
        st.caption("10:16 задаётся в тексте промпта; белую картинку больше не прикрепляем.")
        # Блок перегенерации как в оригинале — использует session_state
        st.markdown("---")
        st.markdown("#### Перегенерация")

        st.caption("Перегенерация работает параллельно по умолчанию: кнопки 'Пересоздать промпт' ставят задачи в очередь и открывают отдельные окна (копии профиля).")
        st.session_state["unif_regen_parallel"] = True
        # Сайдбар-перегенерацию убрали: используйте кнопки "Пересоздать промпт #..." под каждой картинкой в табе.
        st.caption("Перегенерация доступна под каждой картинкой в основной области (кнопки 'Пересоздать промпт').")

    st.markdown("---")
    st.markdown("**Параллельная генерация (несколько окон)**")
    st.caption(
        "Важно: один и тот же Chrome user-data-dir нельзя открыть параллельно в нескольких окнах. "
        "Поэтому для параллельности используются временные *копии* выбранного профиля. "
        "Логин/куки обычно сохраняются, но если ваш профиль очень большой — копирование может занять время."
    )
    pw_parallel = st.checkbox(
        "Включить параллельную генерацию (multi-window)",
        value=True,
        key="unif_pw_parallel",
    )
    pw_use_numbered_profiles = st.checkbox(
        "Использовать профили .chrome_automation_profile_1..N (быстро, без копирования)",
        value=True,
        key="unif_pw_use_numbered_profiles",
        help=(
            "Если включено, то для Prompt #1 будет использован .chrome_automation_profile_1, "
            "для Prompt #2 — .chrome_automation_profile_2 и т.д. Это быстрее, чем копировать профиль. "
            "Если какого-то профиля не хватает — для него будет создана временная копия (fallback)."
        ),
    )
    pw_parallelism = st.number_input(
        "Параллельно окон (concurrency)",
        min_value=1,
        max_value=12,
        value=3,
        step=1,
        key="unif_pw_parallelism",
        help="Сколько окон запускать одновременно."
    )

    pw_profile_start = st.number_input(
        "Стартовый номер профиля (.chrome_automation_profile_N)",
        min_value=1,
        max_value=10_000,
        value=int(st.session_state.get("unif_pw_profile_start", 1) or 1),
        step=1,
        key="unif_pw_profile_start",
        help=(
            "Если включено 'использовать пронумерованные профили', то вместо диапазона _1..N будет использоваться "
            "_START..(_START+N-1). Например, START=4 и concurrency=3 => профили _4, _5, _6."
        ),
    )

    colA, colB = st.columns([1,1])
    with colA:
        open_btn = st.button("1) Открыть/подключиться (Playwright)", type="secondary", key="unif_open")
    with colB:
        go_btn = st.button("2) Сгенерировать все промпты", type="primary", key="unif_generate")

    if "unif_pw_ctx" not in st.session_state:
        st.session_state.unif_pw_ctx = None
        st.session_state.unif_pw_browser = None
        st.session_state.unif_pw_page = None
        st.session_state.unif_prev_resp_count_pw = 0

    # Parallel regeneration job manager (Tab 1)
    st.session_state.setdefault("unif_regen_jobs", {})  # job_id -> dict
    st.session_state.setdefault("unif_regen_job_seq", 0)
    st.session_state.setdefault("unif_regen_executor", None)
    st.session_state.setdefault("unif_regen_lock", threading.Lock())
    # Snapshot of displayed results to avoid UI "jumping" during autorefresh reruns
    st.session_state.setdefault(
        "unif_fast_display_saved_paths",
        list(st.session_state.get("unif_fast_saved_paths", []) or []),
    )
    st.session_state.setdefault("unif_regen_profile_in_use", set())
    st.session_state.setdefault("unif_regen_rr_idx", 0)

    # Profile pool for parallel regeneration: use pre-created profiles (.chrome_automation_profile_1, _2, ...)
    if "unif_regen_profile_pool" not in st.session_state:
        profs = []
        try:
            for name in sorted(os.listdir(".")):
                if name.startswith(".chrome_automation_profile_") and os.path.isdir(name):
                    profs.append(str(Path(name).resolve()))
        except Exception:
            profs = []
        st.session_state["unif_regen_profile_pool"] = profs

    def _pick_regen_profile_round_robin() -> str | None:
        """Pick a numbered profile for regen jobs.

        Uses round-robin ordering and respects `unif_regen_profile_in_use` so the same
        user-data-dir is never used concurrently.

        Returns profile_dir or None if no suitable profile is available.
        """
        try:
            pool = list(st.session_state.get("unif_regen_profile_pool") or [])
            if not pool:
                # Re-scan lazily
                try:
                    pool = []
                    for name in sorted(os.listdir(".")):
                        if name.startswith(".chrome_automation_profile_") and os.path.isdir(name):
                            pool.append(str(Path(name).resolve()))
                    st.session_state["unif_regen_profile_pool"] = pool
                except Exception:
                    pool = []
            if not pool:
                return None

            in_use = set(st.session_state.get("unif_regen_profile_in_use") or set())
            start = int(st.session_state.get("unif_regen_rr_idx", 0) or 0)
            for off in range(len(pool)):
                i = (start + off) % len(pool)
                cand = pool[i]
                if cand in in_use:
                    continue
                # reserve
                in_use.add(cand)
                st.session_state["unif_regen_profile_in_use"] = in_use
                st.session_state["unif_regen_rr_idx"] = (i + 1) % len(pool)
                return cand
            return None
        except Exception:
            return None

    def _close_unif_pw_handles():
        """Best-effort cleanup for the single-window Playwright session stored in session_state."""
        try:
            ctx0 = st.session_state.get("unif_pw_ctx")
            if ctx0 is not None:
                try:
                    ctx0.close()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            pw0 = st.session_state.get("unif_pw_browser")
            if pw0 is not None:
                try:
                    pw0.stop()
                except Exception:
                    pass
        except Exception:
            pass
        st.session_state.unif_pw_ctx = None
        st.session_state.unif_pw_page = None
        st.session_state.unif_pw_browser = None

    # Open/connect
    if open_btn:
        try:
            # If user clicks Open multiple times, do not keep stacking Chrome instances.
            _close_unif_pw_handles()
            if use_auto_profile:
                os.makedirs(user_data_dir, exist_ok=True)
            playwright = sync_playwright().start()
            browser = _launch_persistent_ctx_with_retries(
                playwright,
                user_data_dir=user_data_dir if use_auto_profile else None,
                headless=headless,
                executable_path=executable_path or None,
            )
            page = browser.new_page()
            page.set_default_timeout(30000)
            page.goto(url, wait_until="load")
            st.session_state.unif_pw_ctx = browser
            st.session_state.unif_pw_browser = playwright
            st.session_state.unif_pw_page = page
            try:
                _wait_input_ready(page, timeout_ms=10000)
                st.success("Поле ввода найдено. Можно сразу нажимать '2) Сгенерировать все промпты'.")
            except Exception:
                st.info("Если требуется — войдите в аккаунт Google в открывшемся окне, затем нажмите '2) Сгенерировать все промпты'.")
            st.session_state.unif_prev_resp_count_pw = _count_responses(page)
        except Exception as e:
            st.error(f"Ошибка при открытии: {e}")

    # Generate
    if go_btn:
        try:
            # If we are going to run non-parallel generation, make sure we don't leak old windows.
            # (parallel mode uses isolated contexts per worker and cleans them up itself)
            if not bool(st.session_state.get("unif_pw_parallel", False)):
                _close_unif_pw_handles()

            # Snapshot prompts first
            pre = NBP_PROMPT_PRE
            post = NBP_PROMPT_POST
            final_prompts = [f"{pre}{_sanitize_prompt((p or '').strip())}{post}" for p in st.session_state.unif_pw_prompts if (p or '').strip()]

            if pw_parallel:
                if not use_auto_profile:
                    st.error("Параллельный режим требует выбранный user-data-dir. Включите 'Отдельный профиль для автоматики'.")
                    raise RuntimeError("Parallel mode requires user-data-dir")
                if not final_prompts:
                    st.warning("Нет заполненных промптов")
                    raise RuntimeError("No prompts")
                base_dir = _get_run_base_dir()
                try:
                    os.makedirs(base_dir, exist_ok=True)
                except Exception:
                    pass
                st.session_state.unif_last_base_dir = str(_PathAlias(base_dir).resolve())

                max_workers = int(st.session_state.get("unif_pw_parallelism", 3) or 3)
                max_workers = max(1, min(12, max_workers, len(final_prompts)))

                # Prefer using existing numbered profiles (.chrome_automation_profile_<n>) to avoid slow copying.
                # Fallback: if a numbered profile is missing, we create a lightweight temp copy for that slot.
                start_profile_num = int(st.session_state.get("unif_pw_profile_start", 1) or 1)
                start_profile_num = max(1, start_profile_num)

                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                tmp_root = Path(f"tmp_rovodev_pw_profiles_{ts}").resolve()
                tmp_root.mkdir(parents=True, exist_ok=True)

                def _ignore_profile(dirpath: str, names: list[str]):
                    skip_exact = {
                        "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
                        "Crashpad", "Crash Reports",
                    }
                    skip_prefix = ("Singleton",)
                    ignored = []
                    for n in names:
                        if n in skip_exact or any(n.startswith(p) for p in skip_prefix):
                            ignored.append(n)
                            continue
                        if n.lower() in {"service worker", "serviceworker"}:
                            ignored.append(n)
                            continue
                    return ignored

                # NOTE: We don't pre-create copies anymore.
                # In parallel mode, each prompt runs in its own profile (.chrome_automation_profile_1..N).
                # If a numbered profile is missing, we will clone the base profile on-demand for that prompt.

                import concurrent.futures

                # Profile pool: only use slots 1..max_workers. This allows reusing profile_1/profile_2/... when
                # you have more prompts than concurrency (e.g. 3 prompts with concurrency=2).
                profile_pool: queue.Queue = queue.Queue()

                def _prepare_profile_slot(profile_num: int, worker_slot_idx: int) -> str:
                    """Return a user-data-dir for one worker slot.

                    * profile_num: number used for .chrome_automation_profile_<profile_num>
                    * worker_slot_idx: internal slot index (1..max_workers), used only for tmp folder names
                    """
                    # Try numbered profile
                    if bool(st.session_state.get("unif_pw_use_numbered_profiles", True)):
                        cand = Path(f".chrome_automation_profile_{profile_num}")
                        if cand.exists() and cand.is_dir():
                            return str(cand.resolve())
                    # Fallback: clone base profile ONCE per worker slot
                    dst = tmp_root / f"slot_{worker_slot_idx}"
                    if dst.exists():
                        shutil.rmtree(dst, ignore_errors=True)
                    shutil.copytree(user_data_dir, dst, dirs_exist_ok=False, ignore=_ignore_profile)
                    return str(dst)

                for worker_slot_idx in range(1, max_workers + 1):
                    profile_num = start_profile_num + (worker_slot_idx - 1)
                    profile_pool.put(_prepare_profile_slot(profile_num, worker_slot_idx))

                def _run_one_prompt(prompt_idx: int, fp: str, orig_prompt: str):
                    """Run a single prompt in a dedicated Chrome profile slot.

                    Important behavior: if the window/page gets closed prematurely (common flaky case on Windows)
                    or if we time out without images, we retry by re-opening a fresh persistent context for the
                    SAME prompt (and same profile slot) instead of silently moving on to the next prompt.
                    """

                    local_saved: list[str] = []
                    local_errors: list[str] = []
                    profile_dir = None

                    def _is_retryable_error(msg: str) -> bool:
                        m = (msg or "").lower()
                        return any(
                            k in m
                            for k in [
                                "target page, context or browser has been closed",
                                "has been closed",
                                "browser has disconnected",
                                "browser.getwindowfortarget",
                                "timeout",
                                "timed out",
                            ]
                        )

                    try:
                        # Take a free profile slot, run the prompt, then return it to the pool.
                        profile_dir = profile_pool.get()

                        # Retry full browser/session for this prompt if Chrome window closes too early.
                        max_session_attempts = 2
                        for session_attempt in range(1, max_session_attempts + 1):
                            try:
                                with sync_playwright() as p:
                                    ctx = _launch_persistent_ctx_with_retries(
                                        p,
                                        user_data_dir=profile_dir,
                                        headless=headless,
                                        executable_path=executable_path or None,
                                    )
                                    try:
                                        page = ctx.new_page()
                                        page.set_default_timeout(30000)
                                        page.goto(url, wait_until="load")
                                        _debug_dom(page)
                                        _wait_input_ready(page, timeout_ms=60000)

                                        try:
                                            _start_new_chat(page)
                                        except Exception:
                                            pass

                                        imgs = []
                                        for _attempt in range(1, 2):
                                            _dismiss_overlays(page)
                                            try:
                                                if model_choice:
                                                    gph._pick_model(page, model_choice)
                                            except Exception:
                                                pass
                                            try:
                                                _type_prompt(page, fp)
                                            except Exception as e:
                                                raise RuntimeError(f"Prompt insert failed: {e}")
                                            _click_send(page)

                                            # Main wait. If Gemini is slow, we don't kill the whole run; we'll retry
                                            # this prompt by reopening the context (see outer session_attempt loop).
                                            imgs = _filter_imgs_min_bytes(
                                                _wait_and_download_generated_images(
                                                    page,
                                                    ctx,
                                                    timeout_s=180,
                                                    max_images=6,
                                                    allow_screenshot_fallback=False,
                                                )
                                            )
                                            if not imgs and _has_generated_images(page):
                                                imgs = _filter_imgs_min_bytes(
                                                    _wait_and_download_generated_images(
                                                        page,
                                                        ctx,
                                                        timeout_s=25,
                                                        max_images=6,
                                                        allow_screenshot_fallback=False,
                                                    )
                                                )
                                            if imgs:
                                                break

                                        if not imgs:
                                            raise RuntimeError("No images were collected")

                                        import re as _re
                                        import hashlib as _hashlib

                                        base_slug = _re.sub(r"[^a-zA-Z0-9_-]+", "_", (orig_prompt or "").strip())
                                        base_slug = _re.sub(r"_+", "_", base_slug).strip("_")
                                        if not base_slug:
                                            base_slug = f"prompt_{prompt_idx}"
                                        MAX_BASENAME = 110
                                        prefix_len = len(str(prompt_idx)) + 1
                                        suffix_len = 1 + 2
                                        allowed_slug_len = max(1, MAX_BASENAME - prefix_len - suffix_len)
                                        slug = base_slug[:allowed_slug_len]

                                        unique_imgs = []
                                        seen_hash = set()
                                        seen_phashes: list[int] = []

                                        def _phash64(_blob: bytes) -> int | None:
                                            try:
                                                from PIL import Image
                                                import io as _io

                                                img = Image.open(_io.BytesIO(_blob)).convert("L").resize((8, 8))
                                                pixels = list(img.getdata())
                                                avg = sum(pixels) / len(pixels)
                                                bits = 0
                                                for ii, pxx in enumerate(pixels):
                                                    if pxx >= avg:
                                                        bits |= (1 << ii)
                                                return bits
                                            except Exception:
                                                return None

                                        def _hamming(a: int, b: int) -> int:
                                            return (a ^ b).bit_count()

                                        for (mime, blob) in imgs or []:
                                            # Exact bytes hash
                                            try:
                                                h = _hashlib.sha256(blob).hexdigest()
                                            except Exception:
                                                h = None
                                            if h and h in seen_hash:
                                                continue

                                            # Perceptual hash to catch near-duplicates
                                            ph = _phash64(blob)
                                            if ph is not None:
                                                if any(_hamming(ph, prev) <= 2 for prev in seen_phashes):
                                                    continue

                                            if h:
                                                seen_hash.add(h)
                                            if ph is not None:
                                                seen_phashes.append(ph)

                                            unique_imgs.append((mime, blob))

                                        for j, (mime, blob) in enumerate(unique_imgs, 1):
                                            ext = "png" if mime == "image/png" else ("jpg" if mime == "image/jpeg" else "bin")
                                            fname = f"{prompt_idx}_{slug}_{j:02d}.{ext}"
                                            fpath = os.path.join(base_dir, fname)
                                            with open(fpath, "wb") as f:
                                                f.write(blob)
                                            local_saved.append(fpath)

                                        # Success
                                        break
                                    finally:
                                        try:
                                            ctx.close()
                                        except Exception:
                                            pass
                            except Exception as e:
                                # If this was a known flaky/timeout case, retry by reopening context.
                                msg = str(e)
                                if session_attempt < max_session_attempts and _is_retryable_error(msg):
                                    local_errors.append(
                                        f"Prompt #{prompt_idx}: attempt {session_attempt} failed ({msg}); retrying with a fresh window"
                                    )
                                    continue
                                local_errors.append(f"Prompt #{prompt_idx}: {e}")
                                break

                    finally:
                        # Return profile slot back to the pool for the next prompt
                        try:
                            if profile_dir:
                                profile_pool.put(profile_dir)
                        except Exception:
                            pass

                    return {"idx": prompt_idx, "saved": local_saved, "errors": local_errors}

                # Prepare prompt list (preserve indexing)
                orig_prompts = [((p or "").strip()) for p in (st.session_state.unif_pw_prompts or []) if (p or "").strip()]
                tasks = []
                for idx, fp in enumerate(final_prompts, 1):
                    op = orig_prompts[idx - 1] if idx - 1 < len(orig_prompts) else ""
                    tasks.append((idx, fp, op))

                # Inform user if some numbered profiles are missing (we'll fall back to cloning for them)
                if bool(st.session_state.get("unif_pw_use_numbered_profiles", True)):
                    missing = []
                    for profile_num in range(start_profile_num, start_profile_num + max_workers):
                        cand = Path(f".chrome_automation_profile_{profile_num}")
                        if not (cand.exists() and cand.is_dir()):
                            missing.append(profile_num)
                    if missing:
                        st.warning(
                            "Не найдены профили: "
                            + ", ".join([f".chrome_automation_profile_{i}" for i in missing])
                            + ". Для них будут созданы временные копии (медленнее)."
                        )

                status = st.empty()
                progress = st.progress(0)
                done = 0
                total = len(tasks)
                all_saved: list[str] = []
                all_errors: list[str] = []

                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                    futs = [ex.submit(_run_one_prompt, idx, fp, op) for (idx, fp, op) in tasks]
                    for fut in concurrent.futures.as_completed(futs):
                        r = fut.result() or {}
                        all_saved.extend(r.get("saved") or [])
                        all_errors.extend(r.get("errors") or [])
                        done += 1
                        progress.progress(min(1.0, done / max(1, total)))

                progress.progress(1.0)
                status.write("Готово")

                # Cleanup only temporary cloned profiles
                with st.spinner("Удаляю временные копии профиля..."):
                    shutil.rmtree(tmp_root, ignore_errors=True)

                if all_errors:
                    st.error("Ошибки:\n" + "\n".join(all_errors))

                # Persist for sidebar regeneration
                st.session_state.unif_fast_saved_paths = list(dict.fromkeys(all_saved))
                # st.session_state.unif_saved_paths = list(dict.fromkeys(all_saved))  # keep separate fast/pro lists
                st.session_state.unif_final_prompts = final_prompts
                # Tab 1 UI uses this to show placeholders + regen buttons for missing prompt indices
                st.session_state.unif_fast_final_prompts = final_prompts
                if all_saved:
                    st.success(f"Сохранено файлов: {len(all_saved)} в папку: {base_dir}")
                    try:
                        st.session_state.ps_src_dir = st.session_state.unif_last_base_dir
                    except Exception:
                        pass
                else:
                    st.warning("Не удалось сохранить файлы")

            else:
                # Non-parallel: use a single persistent context and keep it in session_state for later regeneration.
                # IMPORTANT: this block must stay inside the `else` branch; otherwise a new window can be opened
                # on each rerun and performance degrades over time.
                playwright = sync_playwright().start()
                browser = _launch_persistent_ctx_with_retries(
                    playwright,
                    user_data_dir=user_data_dir if use_auto_profile else None,
                    headless=headless,
                    executable_path=executable_path or None,
                )
                ctx = browser
                page = browser.new_page()
                page.set_default_timeout(30000)
                # Persist handles in session_state for later 'Пересоздать'
                st.session_state.unif_pw_ctx = ctx
                st.session_state.unif_pw_page = page
                st.session_state.unif_pw_browser = playwright
                page.goto(url, wait_until="load")
                _debug_dom(page)
                _wait_input_ready(page, timeout_ms=60000)

            pre = NBP_PROMPT_PRE
            post = NBP_PROMPT_POST
            final_prompts = [f"{pre}{_sanitize_prompt((p or '').strip())}{post}" for p in st.session_state.unif_pw_prompts if (p or '').strip()]

            base_dir = _get_run_base_dir()
            try:
                os.makedirs(base_dir, exist_ok=True)
            except Exception:
                pass
            # Обновляем unif_last_base_dir сразу на новый абсолютный путь
            st.session_state.unif_last_base_dir = str(_PathAlias(base_dir).resolve())

            progress = st.progress(0)
            status = st.empty()
            saved_paths: List[str] = []

            for idx, fp in enumerate(final_prompts, 1):
                try:
                    _start_new_chat(page)
                except Exception:
                    pass

                imgs = []
                err = None

                for attempt in range(1, 2):
                    _dismiss_overlays(page)
                    # Попробовать выбрать модель из UI
                    try:
                        mc = st.session_state.get('unif_model_choice')
                        if mc:
                            gph._log(f"[menu] (main) вызываю _pick_model: {mc}", force=True)
                            picked = gph._pick_model(page, mc)
                            gph._log(f"[menu] (main) результат _pick_model: {picked}", force=True)
                    except Exception as e:
                        gph._log(f"[menu] (main) ошибка _pick_model: {e}", force=True)
                    try:
                        _type_prompt(page, fp)
                    except Exception as e:
                        err = f"Prompt insert failed: {e}"
                        continue
                    _click_send(page)
                    imgs = _filter_imgs_min_bytes(
                        _wait_and_download_generated_images(
                            page,
                            ctx,
                            timeout_s=150,
                            max_images=6,
                            allow_screenshot_fallback=False,
                        )
                    )
                    if not imgs and _has_generated_images(page):
                        imgs = _filter_imgs_min_bytes(
                            _wait_and_download_generated_images(
                                page,
                                ctx,
                                timeout_s=15,
                                max_images=6,
                                allow_screenshot_fallback=False,
                            )
                        )
                    if imgs:
                        break

                # Save images to base_dir with a prompt slug
                # Build slug from the original prompt (without service pre/post), prefixed by prompt index like "1_..."
                import re as _re
                try:
                    orig_prompt = (st.session_state.unif_pw_prompts[idx-1] or "").strip()
                except Exception:
                    orig_prompt = (fp or "").strip()
                # Build safe slug: alnum, dash, underscore; collapse repeats; trim
                base_slug = _re.sub(r"[^a-zA-Z0-9_-]+", "_", orig_prompt)
                base_slug = _re.sub(r"_+", "_", base_slug).strip("_")
                if not base_slug:
                    base_slug = f"prompt_{idx}"
                # Compute a max basename length that leaves room for later "_filled" and extension
                # We target basename (without extension) up to 200 chars: <idx>_<slug>_<nn>
                MAX_BASENAME = 110
                prefix_len = len(str(idx)) + 1  # "{idx}_"
                suffix_len = 1 + 2  # "_" + 2 digits
                allowed_slug_len = max(1, MAX_BASENAME - prefix_len - suffix_len)
                slug = base_slug[:allowed_slug_len]
                # De-duplicate identical or near-identical images
                import hashlib as _hashlib
                unique_imgs = []
                _seen_hashes = set()
                _seen_phashes = []  # list of int bitmasks (perceptual hashes)
                def _phash64(_blob: bytes) -> int | None:
                    try:
                        from PIL import Image
                        import io as _io
                        img = Image.open(_io.BytesIO(_blob)).convert("L").resize((8,8))
                        pixels = list(img.getdata())
                        avg = sum(pixels) / len(pixels)
                        bits = 0
                        for i, p in enumerate(pixels):
                            if p >= avg:
                                bits |= (1 << i)
                        return bits
                    except Exception:
                        return None
                def _hamming(a: int, b: int) -> int:
                    return (a ^ b).bit_count()
                for (mime, blob) in imgs:
                    # First, exact bytes hash
                    h = None
                    try:
                        h = _hashlib.sha256(blob).hexdigest()
                    except Exception:
                        pass
                    if h and h in _seen_hashes:
                        continue
                    # Then, perceptual hash to catch near-duplicates
                    ph = _phash64(blob)
                    if ph is not None:
                        is_dup = any(_hamming(ph, prev) <= 2 for prev in _seen_phashes)
                        if is_dup:
                            continue
                    # Accept
                    if h:
                        _seen_hashes.add(h)
                    if ph is not None:
                        _seen_phashes.append(ph)
                    unique_imgs.append((mime, blob))

                for j, (mime, blob) in enumerate(unique_imgs, 1):
                    ext = "png" if mime == "image/png" else ("jpg" if mime == "image/jpeg" else "bin")
                    fname = f"{idx}_{slug}_{j:02d}.{ext}"
                    fpath = os.path.join(base_dir, fname)
                    try:
                        with open(fpath, "wb") as f:
                            f.write(blob)
                        saved_paths.append(fpath)
                    except Exception as e:
                        st.error(f"Ошибка сохранения {fname}: {e}")

                progress.progress(int(idx/ max(1, len(final_prompts)) * 100))
                status.text(f"{idx}/{len(final_prompts)} prompts processed…")

            if saved_paths:
                # Ensure unique file paths (avoid duplicates in UI)
                _seen_paths = set()
                _unique_saved = []
                for _p in saved_paths:
                    if _p not in _seen_paths:
                        _unique_saved.append(_p)
                        _seen_paths.add(_p)
                st.success(f"Сохранено файлов: {len(_unique_saved)} в папку: {base_dir}")
                # Сохраняем в session_state, чтобы не пропадало после перерендера
                st.session_state.unif_fast_saved_paths = _unique_saved
                # st.session_state.unif_saved_paths = _unique_saved  # keep separate fast/pro lists
                # Keep display snapshot in sync for initial generation output
                st.session_state.unif_fast_display_saved_paths = list(_unique_saved)
                st.session_state.unif_last_base_dir = str(_PathAlias(base_dir).resolve())
                st.session_state.unif_final_prompts = final_prompts
                # Tab 1 UI uses this to show placeholders + regen buttons for missing prompt indices
                st.session_state.unif_fast_final_prompts = final_prompts
                # Обновляем дефолт для Photoshop вкладки, чтобы она показывала именно текущую генерацию
                try:
                    st.session_state.ps_src_dir = st.session_state.unif_last_base_dir
                except Exception:
                    pass
            else:
                st.warning("Не удалось сохранить изображения. Проверьте логин/генерацию.")
        except Exception as e:
            # Best-effort cleanup for parallel Tab 1 temp profiles if we failed before reaching cleanup
            try:
                _tr = locals().get("tmp_root")
                if _tr and isinstance(_tr, Path) and _tr.name.startswith("tmp_rovodev_pw_profiles_"):
                    shutil.rmtree(_tr, ignore_errors=True)
            except Exception:
                pass
            st.error(f"Ошибка генерации: {e}")

    # Render a stable snapshot during autorefresh so the results section doesn't "jump" every second.
    _disp = st.session_state.get("unif_fast_display_saved_paths")
    saved_paths = _disp if _disp else st.session_state.get("unif_fast_saved_paths", [])
    base_dir = st.session_state.get("unif_last_base_dir", _get_run_base_dir())

    # Regen status (used to tune autorefresh rate)
    jobs0 = st.session_state.get("unif_regen_jobs", {})
    has_running0 = any(j.get("status") == "running" for j in (jobs0 or {}).values())

    final_prompts_snapshot = (st.session_state.get("unif_fast_final_prompts") or st.session_state.get("unif_final_prompts") or [])
    if saved_paths or final_prompts_snapshot:
        with st.expander(
            "Показать сохранённые файлы (кликните для раскрытия)",
            expanded=True,
        ):
            # Group existing saved files by prompt index
            by_prompt: dict[int, list[str]] = {}
            for p in (saved_paths or []):
                idxp = _extract_prompt_idx(p)
                if idxp is None:
                    by_prompt.setdefault(0, []).append(p)
                else:
                    by_prompt.setdefault(int(idxp), []).append(p)

            # Expected prompt indices are driven by final_prompts, so we can show "empty slots"
            # for prompts that did not produce images (e.g., browser closed / generation failed).
            expected_n = len(final_prompts_snapshot)
            indices: list[int]
            if expected_n > 0:
                indices = list(range(1, expected_n + 1))
            else:
                # Fallback: if we don't have prompts, show whatever we can infer from filenames
                indices = sorted(k for k in by_prompt.keys() if k != 0)

            for idx in indices:
                st.write(f"Промпт #{idx}")

                paths_for_idx = sorted(by_prompt.get(idx, []))
                cols = st.columns(4)

                if paths_for_idx:
                    for i, pth in enumerate(paths_for_idx):
                        with cols[i % 4]:
                            slot = st.empty()
                            try:
                                try:
                                    _mtime = os.stat(pth).st_mtime_ns
                                except Exception:
                                    _mtime = None
                                with open(pth, "rb") as _f:
                                    _b = _f.read()
                                slot.image(
                                    _b,
                                    caption=f"{os.path.basename(pth)}" + (f" (v={_mtime})" if _mtime else ""),
                                    use_container_width=True,
                                )
                            except Exception:
                                slot.write(os.path.basename(pth))
                else:
                    # Keep a visible placeholder so the prompt isn't "lost" in UI.
                    with cols[0]:
                        st.warning("Нет результата (картинка не была сохранена)")

                # Button is always available, even if there is no image
                if st.button(f"Пересоздать промпт #{idx}", key=f"regen_inline_{idx}"):
                    # Non-blocking: enqueue regeneration job (parallel windows via cloned profile)
                    st.session_state["unif_regen_last_user_action"] = time.time()
                    _submit_regen_job(idx)
                    # Start/ensure autorefresh is armed in this same run (without forcing an immediate rerun).
                    try:
                        from streamlit_autorefresh import st_autorefresh
                        st_autorefresh(interval=1000, key="unif_regen_autorefresh")
                    except Exception:
                        pass
                    st.info("Задача перегенерации поставлена в очередь…")

            # Show files with unknown prompt index (if any)
            unknown_paths = sorted(by_prompt.get(0, []))
            if unknown_paths:
                st.markdown("---")
                st.write("Файлы без распознанного номера промпта")
                cols = st.columns(4)
                for i, pth in enumerate(unknown_paths):
                    with cols[i % 4]:
                        try:
                            with open(pth, "rb") as _f:
                                _b = _f.read()
                            st.image(_b, caption=os.path.basename(pth), use_container_width=True)
                        except Exception:
                            st.write(os.path.basename(pth))
                if False:
                    try:
                        playwright = sync_playwright().start()
                        # Жёсткий сценарий: полностью закрываем существующий браузер CDP, затем поднимаем новый
                        exe_state = st.session_state.get("unif_exe_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
                        udd_state = st.session_state.get("unif_user_data_dir", r"C:\\temp\\chrome-debug")
                        cdp_url_state = st.session_state.get("unif_cdp_url", "http://127.0.0.1:9222")

                        def _is_cdp_up(url: str) -> bool:
                            try:
                                with urllib.request.urlopen(url + "/json/version", timeout=1) as resp:
                                    return resp.status == 200
                            except Exception:
                                return False

                        # DEPRECATED: старые попытки перезапуска браузера удалены. Дальше используем только текущую сессию как в оригинале.
                        # В Streamlit колбэки выполняются в другом потоке, поэтому объекты Playwright
                        # из предыдущего шага использовать нельзя. Подключаемся заново к CDP и забираем существующую вкладку Gemini.
                        # Выполняем всю работу перегенерации в отдельном потоке с отдельным sync Playwright,
                        # чтобы избежать конфликтов с asyncio event loop Streamlit.
                        exe_state = st.session_state.get("unif_exe_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
                        udd_state = st.session_state.get("unif_user_data_dir", r"C:\\temp\\chrome-debug")
                        cdp_url_state = st.session_state.get("unif_cdp_url", "http://127.0.0.1:9222")

                        final_prompts = st.session_state.get("unif_final_prompts", [])
                        if 1 <= idx <= len(final_prompts):
                            pt = final_prompts[idx-1]
                        else:
                            pt = final_prompts[0] if final_prompts else ""
                        base_dir_state = st.session_state.get("unif_last_base_dir", _get_run_base_dir())

                        result = {"new_saved": [], "error": None}

                        def _regen_worker():
                            try:
                                from playwright.sync_api import sync_playwright as _sp
                                import urllib.request as _ul
                                p = _sp().start()
                                def _is_cdp_up(url: str) -> bool:
                                    try:
                                        with _ul.urlopen(url + "/json/version", timeout=1) as resp:
                                            return resp.status == 200
                                    except Exception:
                                        return False
                                if not _is_cdp_up(cdp_url_state):
                                    cmd = [exe_state, f"--remote-debugging-port={cdp_url_state.split(':')[-1]}", f"--user-data-dir={udd_state}", "--lang=ru-RU"]
                                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                    deadline = time.time() + 10
                                    while time.time() < deadline and not _is_cdp_up(cdp_url_state):
                                        time.sleep(0.3)
                                b = p.chromium.connect_over_cdp(cdp_url_state)
                                c = b.contexts[0] if b.contexts else b.new_context()
                                pg = c.pages[0] if c.pages else c.new_page()
                                pg.set_default_timeout(30000)
                                # Ensure Gemini page is open only if not already on a Gemini page
                                url_state = st.session_state.get("unif_url", DEFAULT_URLS[0])
                                try:
                                    cur_url = pg.url or ""
                                except Exception:
                                    cur_url = ""
                                if ("gemini.google.com" not in cur_url) and ("aistudio.google.com" not in cur_url):
                                    try:
                                        pg.goto(url_state, wait_until="load")
                                    except Exception:
                                        pass
                                # как в оригинале: новый чат, готовность поля
                                try:
                                    _start_new_chat(pg)
                                except Exception:
                                    pass
                                try:
                                    _wait_input_ready(pg, timeout_ms=30000)
                                    _dismiss_overlays(pg)
                                except Exception:
                                    try:
                                        pg.reload(wait_until="load")
                                    except Exception:
                                        pass
                                    try:
                                        _start_new_chat(pg)
                                    except Exception:
                                        pass
                                    _wait_input_ready(pg, timeout_ms=30000)
                                    _dismiss_overlays(pg)
                                _, ns = _regenerate_prompt(pg, c, pt, idx, base_dir_state, max_images=1, attach_base=False, base_image_path=None)
                                result["new_saved"] = ns or []
                                try:
                                    p.stop()
                                except Exception:
                                    pass
                            except Exception as _e:
                                result["error"] = str(_e)
                        import threading
                        t = threading.Thread(target=_regen_worker, daemon=True)
                        t.start()
                        t.join()
                        if result["error"]:
                            raise RuntimeError(result["error"]) 
                        new_saved = result["new_saved"]

                        if new_saved:
                            # Удаляем старые файлы для этого промпта в папке, оставляем только новые
                            try:
                                deleted = _cleanup_old_prompt_files(os.path.dirname(new_saved[0]), idx, new_saved)
                            except Exception:
                                deleted = 0
                            # Заменяем предыдущие пути в session_state на новые reN-файлы
                            cur = st.session_state.get("unif_fast_saved_paths", [])
                            import re as _re
                            new_list = []
                            for p in cur:
                                _idxp = _extract_prompt_idx(p)
                                if _idxp == idx:
                                    continue
                                new_list.append(p)
                            new_list.extend(list(new_saved))
                            st.session_state.unif_fast_saved_paths = new_list
                            # st.session_state.unif_saved_paths = new_list
                            st.success(f"Готово: пересоздано {len(new_saved)} файлов для промпта #{idx}. Удалено старых: {deleted}")
                            st.rerun()
                        else:
                            st.warning("Перегенерация не вернула файлов")
                    except Exception as e:
                        st.error(f"Ошибка при перегенерации: {e}")

# ---------------- Tab 2: Gemini Generate (Nano Banana Pro / multi-window) ----------------
with tab2:
    st.subheader("Gemini generation via browser (Nano Banana Pro / отдельные окна)")
    st.caption(
        "Эта вкладка делает то же, что и Tab 1, но: для каждого промпта можно выбрать свой Chrome user-data-dir, "
        "и при запуске для каждого заполненного промпта открывается отдельное окно Chrome. "
        "В каждом окне будет выбран режим/model 'Nano Banana Pro' (если он доступен в меню моделей)."
    )

    nbp_url = st.selectbox(
        "URL интерфейса",
        DEFAULT_URLS,
        index=DEFAULT_URLS.index(st.session_state.get("unif_url", DEFAULT_URLS[0])) if st.session_state.get("unif_url", DEFAULT_URLS[0]) in DEFAULT_URLS else 0,
        key="unif_nbp_url",
    )
    nbp_headless = st.checkbox(
        "Headless режим (не рекомендуется для UI-логина)",
        value=False,
        key="unif_nbp_headless",
    )
    nbp_executable_path = st.text_input(
        "Путь к chrome.exe",
        value=st.session_state.get("unif_exe_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"),
        key="unif_nbp_exe_path",
    )

    st.markdown("**Задания (промпт + профиль)**")
    if "unif_nbp_tasks" not in st.session_state:
        # Each item: {"prompt": str, "user_data_dir": str}
        st.session_state.unif_nbp_tasks = [{"prompt": "", "user_data_dir": os.path.abspath(".chrome_automation_profile")}]

    if st.session_state.get("unif_nbp_imported_from_query"):
        st.success("Первые 3 промпта импортированы из статьи (Tab 0) и вставлены в Nano Banana Pro")
        st.session_state.unif_nbp_imported_from_query = False

    tasks_new = []
    for i, t in enumerate(st.session_state.unif_nbp_tasks):
        st.markdown(f"#### Prompt #{i+1}")
        pval = st.text_input(f"Промпт #{i+1}", value=t.get("prompt", ""), key=f"unif_nbp_prompt_{i}")

        # --- Buttons under the prompt (manual mode convenience) ---
        # Keep buttons compact (left-aligned) by adding a large spacer column.
        # Make the first 4 columns a bit wider to avoid label wrapping.
        c1, c2, c3, _spacer = st.columns([1.35, 1.05, 1.2, 7.05], gap="small", vertical_alignment="bottom")

        # 1) Copy full Gemini UI prompt (10:16 is text-only now)
        final_prompt_preview = f"{NBP_PROMPT_PRE}{_sanitize_prompt(pval or '').strip()}{NBP_PROMPT_POST}" if (pval or '').strip() else ""
        with c1:
            _clipboard_copy_text_button(
                label="Copy prompt",
                text=final_prompt_preview,
                key=f"nbp_copy_full_prompt_{i}",
            )

        # 2) Copy generated image filename for this prompt
        # Prefer actual latest saved file; otherwise predict filename using same rules as the saver.
        _latest_path = _get_latest_nbp_saved_image_path(i + 1)
        _pred_name = _predict_nbp_image_basename(i + 1, pval, j=1, ext="png")
        _name_to_copy = str(Path(_latest_path).name) if _latest_path else _pred_name
        with c2:
            try:
                _clipboard_copy_text_button(
                    label="Copy name",
                    text=_name_to_copy,
                    key=f"nbp_copy_image_name_{i}",
                )
            except Exception:
                st.caption("(copy failed)")

        # 3) Open Chrome window with this prompt's profile (user-data-dir)
        _ud_snapshot = (t.get("user_data_dir") or os.path.abspath(".chrome_automation_profile"))
        _ud_snapshot = st.session_state.get(f"unif_nbp_uddir_{i}", _ud_snapshot)
        with c3:
            if st.button("Open profile", key=f"nbp_open_profile_{i}"):
                _open_chrome_window_with_profile(
                    executable_path=st.session_state.get("unif_nbp_exe_path") or None,
                    user_data_dir=_ud_snapshot,
                    url=st.session_state.get("unif_nbp_url", DEFAULT_URLS[0]),
                )

        default_ud = t.get("user_data_dir") or os.path.abspath(".chrome_automation_profile")
        ud = st.text_input(
            f"Chrome user-data-dir для промпта #{i+1}",
            value=default_ud,
            key=f"unif_nbp_uddir_{i}",
            help="Например: .chrome_automation_profile, .chrome_automation_profile_1, ... (разные аккаунты/сессии).",
        )
        tasks_new.append({"prompt": pval, "user_data_dir": _normalize_user_data_dir(ud) or os.path.abspath(os.path.expanduser(ud))})
        st.markdown("---")
    st.session_state.unif_nbp_tasks = tasks_new

    col_add, col_rem = st.columns([1, 1])
    with col_add:
        if st.button("+ Добавить поле", key="unif_nbp_add"):
            st.session_state.unif_nbp_tasks.append({"prompt": "", "user_data_dir": os.path.abspath(".chrome_automation_profile")})
            st.rerun()
    with col_rem:
        if len(st.session_state.unif_nbp_tasks) > 1 and st.button("− Убрать последнее", key="unif_nbp_rem"):
            st.session_state.unif_nbp_tasks = st.session_state.unif_nbp_tasks[:-1]
            st.rerun()

    keep_windows_open = st.checkbox(
        "Оставить окна Chrome открытыми после запуска",
        value=True,
        key="unif_nbp_keep_open",
        help="Если включено, окна останутся открытыми (для ручной проверки). Важно: Streamlit не сможет их автоматически закрыть."
    )

    nbp_parallelism = st.number_input(
        "Параллельно окон (concurrency)",
        min_value=1,
        max_value=12,
        value=3,
        step=1,
        help="Сколько окон/профилей запускать одновременно. Для параллельного режима нужно выключить 'Оставить окна открытыми'.",
        key="unif_nbp_parallelism",
    )

    nbp_retries = st.number_input(
        "Повторов (retry) на один промпт",
        min_value=0,
        max_value=5,
        value=2,
        step=1,
        help="Если Gemini UI пишет 'Something went wrong / Что-то пошло не так' или картинки не успели появиться — попробуем повторить с паузой.",
        key="unif_nbp_retries",
    )

    nbp_run = st.button("Сгенерировать картинки (Nano Banana Pro)", type="primary", key="unif_nbp_run")

    if nbp_run:
        # IMPORTANT: do not access st.session_state from background threads.
        # We snapshot all inputs first, then run Playwright work synchronously.
        base_dir = _get_run_base_dir()
        try:
            os.makedirs(base_dir, exist_ok=True)
        except Exception:
            pass

        tasks_snapshot = [
            {
                "prompt": (t.get("prompt") or "").strip(),
                "user_data_dir": _normalize_user_data_dir((t.get("user_data_dir") or "").strip()),
            }
            for t in (st.session_state.get("unif_nbp_tasks") or [])
        ]
        url_snapshot = st.session_state.get("unif_nbp_url", DEFAULT_URLS[0])
        headless_snapshot = bool(st.session_state.get("unif_nbp_headless", False))
        exe_snapshot = st.session_state.get("unif_nbp_exe_path") or None
        keep_open_snapshot = bool(st.session_state.get("unif_nbp_keep_open", True))
        retries_snapshot = int(st.session_state.get("unif_nbp_retries", 2) or 0)

        tasks_snapshot = [t for t in tasks_snapshot if t["prompt"]]
        if not tasks_snapshot:
            st.warning("Нет заполненных промптов. Добавьте хотя бы один промпт и повторите.")
        else:
            result = {"saved": [], "errors": []}

            # Keep references in session_state if user wants windows open, otherwise they may close immediately
            if keep_open_snapshot:
                st.session_state.setdefault("unif_nbp_keepalive", [])

            import contextlib

            _use_status = hasattr(st, "status")
            if _use_status:
                _cm = st.status("Запуск Nano Banana Pro (multi-window)...", expanded=True)
                status = _cm.__enter__()
            else:
                # Fallback for older Streamlit: show a spinner + a text area for logs
                _cm = st.spinner("Запуск Nano Banana Pro (multi-window)...")
                _cm.__enter__()
                status = st.empty()

            try:
                from playwright.sync_api import sync_playwright as _sp

                # Parallel mode: only safe when keep_open is False (we must close contexts inside workers).
                if keep_open_snapshot:
                    status.write("⚠️ Параллельный режим отключён, потому что включено 'Оставить окна открытыми'.\n"
                                 "Выключите эту опцию, чтобы окна и отправка шли параллельно.")

                    def _launch_ctx_with_retries(pw, *, user_data_dir: str | None):
                        last_err: Exception | None = None
                        for attempt in range(1, 4):
                            try:
                                return _launch_persistent_ctx_with_retries(
                                    pw,
                                    user_data_dir=user_data_dir,
                                    headless=headless_snapshot,
                                    executable_path=exe_snapshot,
                                )
                            except Exception as e:
                                last_err = e
                                # On Windows, Chrome launch can be flaky while OS is still creating the window.
                                # Retry a couple of times.
                                time.sleep(0.7 * attempt)
                        raise last_err or RuntimeError("Failed to launch persistent context")

                    p = _sp().start()
                    st.session_state.unif_nbp_keepalive.append(p)

                    used_udirs: set[str] = set()
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

                    for idx, item in enumerate(tasks_snapshot, 1):
                        prompt_raw = item["prompt"]
                        udir = (item.get("user_data_dir") or "").strip() or None
                        udir = _normalize_user_data_dir(udir) if udir else None

                        # In keep-open mode we might need multiple windows using the same base profile.
                        # Chrome can't open the same user-data-dir twice, so we auto-clone when repeated.
                        effective_udir = udir
                        cloned_dir = None
                        try:
                            if effective_udir:
                                udir_key = os.path.normcase(os.path.normpath(effective_udir))
                                if udir_key in used_udirs:
                                    cloned_dir = str(Path(f"tmp_rovodev_nbp_profile_{ts}_{idx}").resolve())
                                    try:
                                        _clone_profile_dir(effective_udir, cloned_dir)
                                        effective_udir = cloned_dir
                                    except Exception:
                                        # If cloning fails, fall back to original and let Chrome decide.
                                        effective_udir = udir
                                used_udirs.add(os.path.normcase(os.path.normpath(effective_udir or "")))
                        except Exception:
                            pass

                        status.write(f"Окно #{idx}: профиль={effective_udir or '(без профиля)'}")

                        if effective_udir:
                            try:
                                os.makedirs(effective_udir, exist_ok=True)
                            except Exception:
                                pass

                        ctx = _launch_ctx_with_retries(p, user_data_dir=effective_udir)
                        st.session_state.unif_nbp_keepalive.append(ctx)
                        if cloned_dir:
                            st.session_state.unif_nbp_keepalive.append({"tmp_profile_dir": cloned_dir})

                        page = ctx.new_page()
                        page.set_default_timeout(30000)
                        try:
                            page.goto(url_snapshot, wait_until="load")
                            # _debug_dom() is very heavy and can stall on some Gemini UI variants.
                            # Keep it disabled by default.
                            # _debug_dom(page)
                            _wait_input_ready(page, timeout_ms=60000)

                            try:
                                _start_new_chat(page)
                            except Exception:
                                pass

                            _dismiss_overlays(page)

                            try:
                                gph._pick_model(page, "Nano Banana Pro")
                            except Exception as e:
                                result["errors"].append(f"Prompt #{idx}: не удалось выбрать модель Nano Banana Pro ({e})")

                            pre = NBP_PROMPT_PRE
                            post = NBP_PROMPT_POST
                            final_prompt = f"{pre}{_sanitize_prompt(prompt_raw)}{post}"

                            # Retry loop: Nano Banana Pro can get stuck in "Generating" for automation.
                            max_attempts = 1 + int(retries_snapshot or 0)
                            last_exc: Exception | None = None
                            imgs = []

                            for attempt in range(1, max_attempts + 1):
                                try:
                                    if attempt > 1:
                                        # Exponential backoff + jitter
                                        try:
                                            time.sleep(1.2 * (2 ** (attempt - 2)) + random.random() * 1.0)
                                        except Exception:
                                            pass

                                        try:
                                            page.reload(wait_until="load")
                                        except Exception:
                                            pass
                                        try:
                                            _wait_input_ready(page, timeout_ms=60000)
                                        except Exception:
                                            pass
                                        try:
                                            _start_new_chat(page)
                                        except Exception:
                                            pass
                                        _dismiss_overlays(page)
                                        try:
                                            gph._pick_model(page, "Nano Banana Pro")
                                        except Exception:
                                            pass

                                    # Small jitter before typing (more human-like)
                                    try:
                                        time.sleep(0.15 + random.random() * 0.55)
                                    except Exception:
                                        pass

                                    _type_prompt(page, final_prompt)
                                    _click_send(page)

                                    # Give the UI a tiny moment after Send (helps Pro stabilize)
                                    try:
                                        time.sleep(0.25 + random.random() * 0.75)
                                    except Exception:
                                        pass

                                    imgs = _filter_imgs_min_bytes(
                                        _wait_and_download_generated_images(
                                            page,
                                            ctx,
                                            timeout_s=170,
                                            max_images=6,
                                            allow_screenshot_fallback=False,
                                        )
                                    )
                                    if not imgs and _has_generated_images(page):
                                        imgs = _filter_imgs_min_bytes(
                                            _wait_and_download_generated_images(
                                                page,
                                                ctx,
                                                timeout_s=25,
                                                max_images=6,
                                                allow_screenshot_fallback=False,
                                            )
                                        )

                                    if imgs:
                                        break

                                    # Pro sometimes needs an extra nudge
                                    try:
                                        for _ in range(2):
                                            try:
                                                page.keyboard.press("Escape")
                                            except Exception:
                                                pass
                                            time.sleep(0.12 + random.random() * 0.25)
                                        try:
                                            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                                        except Exception:
                                            pass
                                    except Exception:
                                        pass

                                    last_exc = RuntimeError("Empty result (no images)")
                                except Exception as e:
                                    last_exc = e
                                    continue

                            if not imgs and last_exc:
                                result["errors"].append(f"Prompt #{idx}: {last_exc}")
                                continue

                            import re as _re
                            import hashlib as _hashlib

                            base_slug = _re.sub(r"[^a-zA-Z0-9_-]+", "_", prompt_raw)
                            base_slug = _re.sub(r"_+", "_", base_slug).strip("_")
                            if not base_slug:
                                base_slug = f"prompt_{idx}"

                            MAX_BASENAME = 110
                            prefix = f"{idx}_pro_"
                            prefix_len = len(prefix)
                            suffix_len = 1 + 2
                            allowed_slug_len = max(1, MAX_BASENAME - prefix_len - suffix_len)
                            slug = base_slug[:allowed_slug_len]

                            seen = set()
                            uniq = []
                            for mime, blob in imgs or []:
                                try:
                                    h = _hashlib.sha256(blob).hexdigest()
                                except Exception:
                                    h = None
                                if h and h in seen:
                                    continue
                                if h:
                                    seen.add(h)
                                uniq.append((mime, blob))

                            saved_local = []
                            for j, (mime, blob) in enumerate(uniq, 1):
                                ext = "png" if mime == "image/png" else ("jpg" if mime == "image/jpeg" else "bin")
                                fname = f"{idx}_pro_{slug}_{j:02d}.{ext}"
                                fpath = os.path.join(base_dir, fname)
                                with open(fpath, "wb") as f:
                                    f.write(blob)
                                saved_local.append(fpath)

                            result["saved"].extend(saved_local)
                            status.write(f"Окно #{idx}: сохранено {len(saved_local)} файлов")
                        except Exception as e:
                            result["errors"].append(f"Prompt #{idx}: {e}")

                else:
                    import concurrent.futures

                    max_workers = int(st.session_state.get("unif_nbp_parallelism", 3) or 3)
                    max_workers = max(1, min(12, max_workers))

                    progress = st.progress(0)
                    done = 0
                    total = len(tasks_snapshot)

                    def _run_one(task_idx: int, prompt_raw: str, udir: str | None, *, clone_profile: bool, retries: int):
                        # No Streamlit calls here.
                        local_result = {"idx": task_idx, "saved": [], "errors": []}
                        tmp_profile_dir = None
                        effective_udir = _normalize_user_data_dir(udir) if udir else None

                        def _jitter_sleep(base: float, spread: float):
                            try:
                                time.sleep(max(0.0, base + (random.random() * spread)))
                            except Exception:
                                pass

                        try:
                            # In parallel mode, multiple tasks must not share the same Chrome user-data-dir.
                            # We only clone when we detected duplicates.
                            if effective_udir and clone_profile:
                                try:
                                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                                    tmp_profile_dir = str(Path(f"tmp_rovodev_nbp_profile_{ts}_{task_idx}").resolve())
                                    _clone_profile_dir(effective_udir, tmp_profile_dir)
                                    effective_udir = tmp_profile_dir
                                except Exception:
                                    # If cloning fails, fall back to original and hope it isn't shared.
                                    tmp_profile_dir = None

                            if effective_udir:
                                try:
                                    os.makedirs(effective_udir, exist_ok=True)
                                except Exception:
                                    pass

                            with _sp() as p:
                                # Launch with a couple retries (Windows flakiness)
                                last_err: Exception | None = None
                                ctx = None
                                for attempt in range(1, 4):
                                    try:
                                        ctx = _launch_persistent_ctx_with_retries(
                                            p,
                                            user_data_dir=effective_udir,
                                            headless=headless_snapshot,
                                            executable_path=exe_snapshot,
                                        )
                                        break
                                    except Exception as e:
                                        last_err = e
                                        time.sleep(0.7 * attempt)
                                if ctx is None:
                                    raise last_err or RuntimeError("Failed to launch persistent context")

                                try:
                                    page = ctx.new_page()
                                    page.set_default_timeout(30000)
                                    page.goto(url_snapshot, wait_until="load")
                                    # _debug_dom() is very heavy and can stall on some Gemini UI variants.
                                    # Keep it disabled by default.
                                    # _debug_dom(page)
                                    _wait_input_ready(page, timeout_ms=60000)

                                    try:
                                        _start_new_chat(page)
                                    except Exception:
                                        pass

                                    # Stagger starts a bit to avoid request bursts.
                                    _jitter_sleep(0.25 * (task_idx % 5), 0.6)

                                    _dismiss_overlays(page)

                                    try:
                                        gph._pick_model(page, "Nano Banana Pro")
                                    except Exception as e:
                                        local_result["errors"].append(f"не удалось выбрать модель Nano Banana Pro ({e})")

                                    pre = NBP_PROMPT_PRE
                                    post = NBP_PROMPT_POST
                                    final_prompt = f"{pre}{_sanitize_prompt(prompt_raw)}{post}"

                                    max_attempts = 1 + int(retries or 0)

                                    last_exc: Exception | None = None
                                    imgs = []
                                    for attempt in range(1, max_attempts + 1):
                                        try:
                                            if attempt > 1:
                                                # Exponential backoff + jitter
                                                _jitter_sleep(1.2 * (2 ** (attempt - 2)), 1.0)
                                                try:
                                                    page.reload(wait_until="load")
                                                except Exception:
                                                    pass
                                                try:
                                                    _wait_input_ready(page, timeout_ms=60000)
                                                except Exception:
                                                    pass
                                                try:
                                                    _start_new_chat(page)
                                                except Exception:
                                                    pass
                                                _dismiss_overlays(page)

                                            try:
                                                _type_prompt(page, final_prompt)
                                            except Exception as e:
                                                last_exc = RuntimeError(f"Prompt insert failed: {e}")
                                                continue
                                            _click_send(page)

                                            # Give the UI a tiny moment after Send (helps Pro stabilize)
                                            _jitter_sleep(0.25, 0.65)

                                            imgs = _filter_imgs_min_bytes(
                                                _wait_and_download_generated_images(
                                                    page,
                                                    ctx,
                                                    timeout_s=170,
                                                    max_images=6,
                                                    allow_screenshot_fallback=False,
                                                )
                                            )
                                            if not imgs and _has_generated_images(page):
                                                imgs = _filter_imgs_min_bytes(
                                                    _wait_and_download_generated_images(
                                                        page,
                                                        ctx,
                                                        timeout_s=25,
                                                        max_images=6,
                                                        allow_screenshot_fallback=False,
                                                    )
                                                )

                                            if imgs:
                                                break

                                            # Watchdog: if Pro gets stuck in "Generating" forever, try to recover
                                            # with a few realistic user actions before retrying.
                                            try:
                                                for _ in range(2):
                                                    try:
                                                        page.keyboard.press("Escape")
                                                    except Exception:
                                                        pass
                                                    _jitter_sleep(0.12, 0.25)

                                                try:
                                                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                                                except Exception:
                                                    pass

                                                # If a Stop/Cancel button exists, click it.
                                                stop_sels = [
                                                    "button:has-text('Stop')",
                                                    "button:has-text('Cancel')",
                                                    "button:has-text('Остановить')",
                                                    "button:has-text('Отмена')",
                                                    "button[aria-label*='Stop' i]",
                                                    "button[aria-label*='Cancel' i]",
                                                ]
                                                for sel in stop_sels:
                                                    try:
                                                        b = page.locator(sel).first
                                                        if b and b.count() and b.is_visible():
                                                            try:
                                                                b.click(timeout=1500)
                                                            except Exception:
                                                                b.click(force=True, timeout=1500)
                                                            _jitter_sleep(0.2, 0.6)
                                                            break
                                                    except Exception:
                                                        continue

                                                # Try to re-open model picker once (Pro sometimes resets)
                                                try:
                                                    gph._pick_model(page, "Nano Banana Pro")
                                                except Exception:
                                                    pass
                                            except Exception:
                                                pass

                                            last_exc = RuntimeError("Пустой результат (нет картинок)")
                                        except Exception as e:
                                            last_exc = e
                                            # continue retry loop
                                            continue

                                    if not imgs and last_exc:
                                        raise last_exc

                                    import re as _re
                                    import hashlib as _hashlib

                                    base_slug = _re.sub(r"[^a-zA-Z0-9_-]+", "_", prompt_raw)
                                    base_slug = _re.sub(r"_+", "_", base_slug).strip("_")
                                    if not base_slug:
                                        base_slug = f"prompt_{task_idx}"

                                    MAX_BASENAME = 110
                                    prefix = f"{task_idx}_pro_"
                                    prefix_len = len(prefix)
                                    suffix_len = 1 + 2
                                    allowed_slug_len = max(1, MAX_BASENAME - prefix_len - suffix_len)
                                    slug = base_slug[:allowed_slug_len]

                                    seen = set()
                                    uniq = []
                                    for mime, blob in imgs or []:
                                        try:
                                            h = _hashlib.sha256(blob).hexdigest()
                                        except Exception:
                                            h = None
                                        if h and h in seen:
                                            continue
                                        if h:
                                            seen.add(h)
                                        uniq.append((mime, blob))

                                    for j, (mime, blob) in enumerate(uniq, 1):
                                        ext = "png" if mime == "image/png" else ("jpg" if mime == "image/jpeg" else "bin")
                                        fname = f"{task_idx}_pro_{slug}_{j:02d}.{ext}"
                                        fpath = os.path.join(base_dir, fname)
                                        with open(fpath, "wb") as f:
                                            f.write(blob)
                                        local_result["saved"].append(fpath)
                                finally:
                                    try:
                                        ctx.close()
                                    except Exception:
                                        pass
                        except Exception as e:
                            local_result["errors"].append(str(e))
                        finally:
                            if tmp_profile_dir:
                                shutil.rmtree(tmp_profile_dir, ignore_errors=True)
                        return local_result

                    # Detect duplicated profiles (same user-data-dir used by multiple tasks)
                    _ud_norm = []
                    for it in tasks_snapshot:
                        try:
                            v = (it.get("user_data_dir") or "").strip() or None
                            v = _normalize_user_data_dir(v) if v else None
                            if v:
                                v = os.path.normcase(os.path.normpath(v))
                        except Exception:
                            v = None
                        _ud_norm.append(v)
                    dup_udirs = {v for v in _ud_norm if v and _ud_norm.count(v) > 1}

                    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                        futs = []
                        for idx, item in enumerate(tasks_snapshot, 1):
                            prompt_raw = item["prompt"]
                            udir = (item.get("user_data_dir") or "").strip() or None
                            udir_norm = _normalize_user_data_dir(udir) if udir else None
                            udir_key = os.path.normcase(os.path.normpath(udir_norm)) if udir_norm else None
                            need_clone = bool(udir_key and udir_key in dup_udirs)
                            futs.append(ex.submit(_run_one, idx, prompt_raw, udir, clone_profile=need_clone, retries=retries_snapshot))

                        # Hard safety: don't let Streamlit wait forever if a worker hangs inside Playwright.
                        # We are NOT adding extra retries; we only cap total waiting time.
                        overall_wait_s = max(60, int(240 * max(1, total)))
                        try:
                            for fut in concurrent.futures.as_completed(futs, timeout=overall_wait_s):
                                r = fut.result()
                                done += 1
                                progress.progress(done / max(1, total))

                                if r.get("errors"):
                                    for e in r["errors"]:
                                        result["errors"].append(f"Prompt #{r['idx']}: {e}")
                                if r.get("saved"):
                                    result["saved"].extend(r["saved"])
                                    status.write(f"Окно #{r['idx']}: сохранено {len(r['saved'])} файлов")
                                else:
                                    status.write(f"Окно #{r['idx']}: файлов не сохранено")
                        except concurrent.futures.TimeoutError:
                            # Some futures are still running/hung. We won't wait forever.
                            for fut in futs:
                                if not fut.done():
                                    try:
                                        fut.cancel()
                                    except Exception:
                                        pass
                            raise TimeoutError(
                                f"Nano Banana Pro: exceeded overall wait limit ({overall_wait_s}s). "
                                "Likely a hung Chrome/Gemini tab."
                            )

                if _use_status:
                    try:
                        status.update(label="Готово", state="complete", expanded=True)
                    except Exception:
                        pass
            except Exception as e:
                if _use_status:
                    try:
                        status.update(label=f"Ошибка: {e}", state="error", expanded=True)
                    except Exception:
                        pass
                result["errors"].append(str(e))
            finally:
                try:
                    _cm.__exit__(None, None, None)
                except Exception:
                    pass

            if result.get("errors"):
                st.error("Ошибки:\n" + "\n".join(result["errors"]))
            if result.get("saved"):
                st.success(f"Сохранено файлов: {len(result['saved'])} в папку: {base_dir}")
                # Store Pro outputs separately so Tab1 regeneration can't overwrite/delete them.
                st.session_state.unif_pro_saved_paths = list(dict.fromkeys(result["saved"]))
                st.session_state.unif_last_base_dir = str(_PathAlias(base_dir).resolve())
                try:
                    st.session_state.ps_src_dir = st.session_state.unif_last_base_dir
                except Exception:
                    pass

# ---------------- Tab 3: Photoshop watermark removal (batch) ----------------
with tab3:
    st.subheader("Photoshop: удалить ватермарк (Content-Aware Fill нижний правый)")
    st.caption("Параметры по умолчанию: 13, Все форматы (как в исходнике), scale. Координаты 1: 82/82, координаты 2: 25/25.")

    # Определяем папку текущей генерации:
    # 1) по сохранённым файлам (dirname последнего сохранённого);
    # 2) иначе из session_state.unif_last_base_dir;
    # 3) иначе сегодняшняя дата в "generate automation".
    saved_paths = _get_all_saved_paths()
    last_gen_dir = None
    if saved_paths:
        try:
            last_gen_dir = os.path.dirname(saved_paths[-1])
        except Exception:
            last_gen_dir = None
    if not last_gen_dir:
        last_gen_dir = st.session_state.get("unif_last_base_dir")
    if not last_gen_dir:
        last_gen_dir = str((Path("generate automation") / datetime.now().strftime("%Y-%m-%d")).resolve())
    # Автоподстановка актуальной папки (можно отключить чекбоксом)
    if "ps_follow_latest" not in st.session_state:
        st.session_state.ps_follow_latest = True

    def _ps_src_dir_changed():
        # A manual edit should stick; otherwise the "follow latest" rerun rewrites it.
        st.session_state.ps_follow_latest = False

    # Если режим авто включён, или поле ещё не задано — обновляем на последнюю папку генерации
    if st.session_state.ps_follow_latest or ("ps_src_dir" not in st.session_state):
        st.session_state.ps_src_dir = str(Path(last_gen_dir).expanduser())

    st.checkbox("Автоматически подставлять последнюю папку генерации", key="ps_follow_latest")

    # Поле ввода для ручного изменения папки. По умолчанию — текущая генерация
    src_dir_text = st.text_input(
        "Папка с изображениями для Photoshop",
        key="ps_src_dir",
        on_change=_ps_src_dir_changed,
    )
    st.button("Открыть папку", on_click=_open_folder, args=(src_dir_text,), key="btn_open_ps_src")

    # Сканирование папки и выбор файлов (по умолчанию — все)
    folder = Path(src_dir_text).expanduser()
    exts = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".psd")
    found_images: List[Path] = []
    if folder.exists() and folder.is_dir():
        try:
            found_images = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts]
            # Если в корне мало файлов, попробуем углублённый поиск
            if not found_images:
                found_images = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in exts]
        except Exception:
            found_images = []
    else:
        st.info("Укажите существующую папку с изображениями.")

    st.write(f"Найдено файлов: {len(found_images)}")

    # Опции выбора
    pick_manually = st.checkbox("Выбрать файлы вручную", value=False, key="ps_pick_manual")
    selected_files: List[str] = []
    if pick_manually and found_images:
        # Покажем мультивыбор по именам файлов
        names = [f.name for f in found_images]
        chosen = st.multiselect("Выберите файлы для обработки", options=names, default=names)
        name_to_path = {f.name: str(f) for f in found_images}
        selected_files = [name_to_path[n] for n in chosen]
    else:
        selected_files = [str(p) for p in found_images]

    # Альтернатива: перетаскивание и загрузка файлов напрямую
    st.markdown("— или —")
    uploads = st.file_uploader(
        "Перетащите сюда изображения (PNG, JPG, JPEG, TIF, TIFF, WEBP, BMP, PSD)",
        type=["png", "jpg", "jpeg", "tif", "tiff", "webp", "bmp", "psd"],
        accept_multiple_files=True,
        key="ps_uploads"
    )
    uploaded_temp_paths: List[str] = []
    uploaded_temp_dir = None
    if uploads:
        # Сохраним загруженные файлы во временную папку для текущего запуска
        import time as _t

        upload_sig_parts: list[str] = []
        for uf in uploads:
            try:
                uf_size = getattr(uf, "size", None)
                if uf_size is None:
                    uf_size = len(uf.getbuffer())
                upload_sig_parts.append(f"{uf.name}:{uf_size}")
            except Exception:
                upload_sig_parts.append(str(getattr(uf, "name", "upload")))
        upload_sig = "|".join(upload_sig_parts)

        prev_sig = st.session_state.get("ps_upload_batch_sig")
        prev_dir = st.session_state.get("ps_upload_batch_dir")
        if prev_sig == upload_sig and prev_dir and Path(prev_dir).exists():
            uploaded_temp_dir = Path(prev_dir)
        else:
            ts = _t.strftime("%Y%m%d_%H%M%S")
            uploaded_temp_dir = Path(f"tmp_rovodev_ps_uploads_{ts}")
            st.session_state["ps_upload_batch_sig"] = upload_sig
            st.session_state["ps_upload_batch_dir"] = str(uploaded_temp_dir)

        try:
            uploaded_temp_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        for uf in uploads:
            try:
                # uf is UploadedFile; write to disk preserving name
                outp = uploaded_temp_dir / uf.name
                with open(outp, "wb") as fh:
                    fh.write(uf.getbuffer())
                uploaded_temp_paths.append(str(outp))
            except Exception:
                continue
        if uploaded_temp_paths:
            selected_files = uploaded_temp_paths
            st.info(f"Загружено файлов: {len(uploaded_temp_paths)}. Будут обработаны именно они.")
    # Запомним временную папку в состоянии, чтобы убрать после обработки
    st.session_state["ps_uploaded_temp_dir"] = str(uploaded_temp_dir) if uploaded_temp_dir else None

    # One-time migration for browser sessions that still hold the old 75/75 default.
    if not bool(st.session_state.get("ps_coord1_default_82_migrated")):
        for _k in ("ps_ml_1", "ps_mb_1"):
            try:
                if int(st.session_state.get(_k, 75) or 75) == 75:
                    st.session_state[_k] = 82
            except Exception:
                st.session_state[_k] = 82
        st.session_state["ps_coord1_default_82_migrated"] = True

    size_or_scale = st.number_input("size_or_scale (для scale = делитель, 13 => 1/13 меньшей стороны)", min_value=1.0, max_value=512.0, value=13.0, step=1.0, key="ps_size")
    out_format = st.selectbox("Формат вывода", ["PNG", "JPEG"], index=0, key="ps_fmt")
    mode = st.selectbox("Режим", ["scale", "fixed"], index=0, key="ps_mode")
    ps_coord_cols = st.columns(4)
    with ps_coord_cols[0]:
        margin_left_1 = st.number_input("Отступ слева 1, px", min_value=0, max_value=999, value=82, step=1, key="ps_ml_1")
    with ps_coord_cols[1]:
        margin_bottom_1 = st.number_input("Отступ снизу 1, px", min_value=0, max_value=999, value=82, step=1, key="ps_mb_1")
    with ps_coord_cols[2]:
        margin_left_2 = st.number_input("Отступ слева 2, px", min_value=0, max_value=999, value=25, step=1, key="ps_ml_2")
    with ps_coord_cols[3]:
        margin_bottom_2 = st.number_input("Отступ снизу 2, px", min_value=0, max_value=999, value=25, step=1, key="ps_mb_2")

    def _ps_path_identity(path_str: str) -> str:
        try:
            return str(Path(str(path_str)).expanduser().resolve()).lower()
        except Exception:
            return str(path_str or "").strip().lower()

    def _ps_coord1_key(path_str: str) -> str:
        import hashlib

        ident = _ps_path_identity(path_str)
        return "ps_coord1_" + hashlib.md5(ident.encode("utf-8", errors="ignore")).hexdigest()[:16]

    if selected_files:
        st.caption(
            "Отметьте картинки для координат 1. Неотмеченные картинки будут обработаны по координатам 2."
        )
        with st.expander("Выбор координат watermark для Photoshop", expanded=True):
            cols = st.columns(4)
            for i, img_path in enumerate(selected_files):
                with cols[i % 4]:
                    try:
                        st.image(img_path, caption=os.path.basename(img_path), use_container_width=True)
                    except Exception:
                        st.write(os.path.basename(str(img_path)))
                    st.checkbox(
                        "Координаты 1",
                        value=True,
                        key=_ps_coord1_key(img_path),
                    )

        ps_coord1_ids = {
            _ps_path_identity(p)
            for p in selected_files
            if bool(st.session_state.get(_ps_coord1_key(p), True))
        }
        st.caption(
            f"Photoshop groups: координаты 1 = {len(ps_coord1_ids)} | "
            f"координаты 2 = {max(0, len(selected_files) - len(ps_coord1_ids))}"
        )
    else:
        ps_coord1_ids = set()

    run_btn = st.button("Запустить Photoshop обработку (batch)", type="primary", key="ps_run")

    if run_btn:
        folder = Path(src_dir_text).expanduser()
        if not folder.exists() or not folder.is_dir():
            st.error("Папка не существует или это не директория")
        else:
            images = selected_files
            if not images:
                st.warning("Не найдено изображений для обработки.")
            else:
                images = list(dict.fromkeys([str(p) for p in images if p]))
                coord1_images = [p for p in images if _ps_path_identity(p) in ps_coord1_ids]
                coord2_images = [p for p in images if _ps_path_identity(p) not in ps_coord1_ids]

                st.info(f"Запускаю обработку файлов: {len(images)}…")
                st.write(
                    f"Photoshop groups to process: координаты 1 = {len(coord1_images)} | "
                    f"координаты 2 = {len(coord2_images)}"
                )

                fmt_arg = out_format

                def _run_ps_group(group_images: List[str], label: str, margin_left_value: int, margin_bottom_value: int) -> tuple[bool, str]:
                    if not group_images:
                        return True, ""
                    cmd = [
                        sys.executable,
                        "photoshop_crop_bottom_right.py",
                        *group_images,
                        str(int(size_or_scale)),
                        fmt_arg,
                        mode,
                        str(int(margin_left_value)),
                        str(int(margin_bottom_value)),
                    ]
                    proc = subprocess.run(cmd, capture_output=True, text=True)
                    if proc.returncode == 0:
                        return True, ""
                    details = f"Photoshop {label} вернул код {proc.returncode}. STDERR: {proc.stderr}\nSTDOUT: {proc.stdout}"
                    return False, details

                try:
                    ps_errors: List[str] = []
                    for group_images, label, ml, mb in (
                        (coord1_images, "координаты 1", margin_left_1, margin_bottom_1),
                        (coord2_images, "координаты 2", margin_left_2, margin_bottom_2),
                    ):
                        if not group_images:
                            continue
                        st.caption(f"Photoshop {label}: {len(group_images)} файлов, отступы {int(ml)}/{int(mb)}")
                        ok_group, err_group = _run_ps_group(group_images, label, int(ml), int(mb))
                        if not ok_group:
                            ps_errors.append(err_group)

                    if ps_errors:
                        st.warning("Photoshop обработка завершилась с ошибками в одной или нескольких группах.")
                        with st.expander("Ошибки Photoshop", expanded=True):
                            for err in ps_errors:
                                st.code(err, language=None)
                    else:
                        st.success("Photoshop обработка завершена. Файлы с суффиксом _filled сохранены рядом с исходными.")

                    if not ps_errors:
                        # Запомним фактическую исходную папку, использованную при запуске Photoshop
                        try:
                            st.session_state["ps_run_src_dir_final"] = str(Path(src_dir_text).expanduser())
                        except Exception:
                            pass
                        # Если файлы были загружены через drag&drop (во временную папку),
                        # автоматически перенесём результаты *_filled.* в указанную папку src_dir_text
                        try:
                            from pathlib import Path as _P
                            import shutil as _sh
                            dest_dir = _P(src_dir_text).expanduser()
                            up_tmp = st.session_state.get("ps_uploaded_temp_dir")
                            moved = 0
                            if up_tmp:
                                tmp_dir = _P(up_tmp)
                                if tmp_dir.exists() and dest_dir.exists():
                                    for q in sorted(tmp_dir.glob("*_filled.*")):
                                        if q.suffix.lower() not in (".png", ".jpg", ".jpeg"):
                                            continue
                                        target = dest_dir / q.name
                                        # Разрешение конфликтов имени: добавим (1), (2), ... если нужно
                                        if target.exists():
                                            stem = target.stem
                                            suff = target.suffix
                                            k = 1
                                            while True:
                                                cand = dest_dir / f"{stem}({k}){suff}"
                                                if not cand.exists():
                                                    target = cand
                                                    break
                                                k += 1
                                        try:
                                            _sh.move(str(q), str(target))
                                            moved += 1
                                        except Exception:
                                            # попробуем копировать как fallback
                                            try:
                                                _sh.copy2(str(q), str(target))
                                                moved += 1
                                            except Exception:
                                                pass
                            if moved:
                                st.info(f"Перенесено файлов в папку для Photoshop: {moved} → {dest_dir}")
                        except Exception:
                            pass
                        # Покажем, где искать результаты и выведем превью
                        try:
                            from pathlib import Path as _P
                            import re as _re
                            out_dirs = sorted(set(_P(p).parent for p in images))
                            up_tmp = st.session_state.get("ps_uploaded_temp_dir")
                            # Если был перенос из временной папки — показываем целевую папку
                            if up_tmp:
                                out_dirs = [ _P(src_dir_text).expanduser() ]
                            else:
                                if up_tmp:
                                    out_dirs = list(dict.fromkeys(out_dirs + [_P(up_tmp)]))
                            total_found = 0
                            for d in out_dirs:
                                if not d or not _P(d).exists():
                                    continue
                                filled = [q for q in _P(d).glob("*_filled.*") if q.suffix.lower() in (".png", ".jpg", ".jpeg")]
                                if not filled:
                                    continue
                                st.markdown(f"**Папка результатов:** {str(d)}")
                                cols = st.columns(4)
                                for i, q in enumerate(sorted(filled)):
                                    total_found += 1
                                    with cols[i % 4]:
                                        try:
                                            st.image(str(q), caption=q.name, use_container_width=True)
                                        except Exception:
                                            st.write(q.name)
                                        st.code(str(q), language=None)
                            if total_found == 0:
                                st.info("Пока не найдено файлов *_filled рядом с исходниками. Проверьте права записи и исходное расширение.")
                        except Exception as _e:
                            st.info(f"Не удалось отобразить результаты: {_e}")
                except Exception as e:
                    st.error(f"Ошибка запуска Photoshop: {e}")

# ---------------- Tab 4: Normalize Pins to fixed size before WebP ----------------
with tab4:
    st.subheader("Normalize Pins to 640×1024 (crop to fit)")
    st.caption("Этап перед WebP. По умолчанию берём *_filled изображения из последней генерации и приводим к размеру 640×1024, обрезая лишнее по длинной стороне. Можно также перетащить дополнительные файлы любого формата.")

    from PIL import Image
    import io as _io

    # РСЃС‚РѕС‡РЅРёРє РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ: С‚Р° Р¶Рµ, С‡С‚Рѕ РёСЃРїРѕР»СЊР·РѕРІР°Р»Р°СЃСЊ РЅР° РІРєР»Р°РґРєРµ Photoshop / РїРѕСЃР»РµРґРЅСЏСЏ РіРµРЅРµСЂР°С†РёСЏ
    def _guess_today_latest_dir() -> str | None:
        try:
            import re as _re
            root = Path("generate automation").expanduser()
            today = datetime.now().strftime("%Y-%m-%d")
            if not root.exists():
                return None
            best = None
            best_n = -1
            for d in root.iterdir():
                if not d.is_dir():
                    continue
                name = d.name
                if name == today:
                    n = 0
                else:
                    m = _re.match(rf"^{today}_(\d+)$", name)
                    if not m:
                        continue
                    n = int(m.group(1))
                if n >= best_n:
                    best_n = n
                    best = d
            return str(best.expanduser()) if best is not None else None
        except Exception:
            return None

    # Вычисляем актуальную папку по тем же приоритетам, что и Photoshop, плюс fallback на самую свежую сегодняшнюю
    latest_default_dir = None
    latest_default_dir = st.session_state.get("ps_run_src_dir_final") or st.session_state.get("ps_src_dir") or None
    if not latest_default_dir:
        latest_default_dir = st.session_state.get("unif_last_base_dir") or None
    if not latest_default_dir:
        saved_paths = _get_all_saved_paths()
        if saved_paths:
            try:
                latest_default_dir = os.path.dirname(saved_paths[-1])
            except Exception:
                latest_default_dir = None
    if not latest_default_dir:
        latest_default_dir = _guess_today_latest_dir()

    # Режим авто-подстановки, как во вкладке Photoshop
    if "norm_follow_latest" not in st.session_state:
        st.session_state.norm_follow_latest = True
    if st.session_state.norm_follow_latest or ("norm_src_dir" not in st.session_state):
        st.session_state["norm_src_dir"] = latest_default_dir or ""

    col_l, col_r = st.columns([2,1])
    with col_l:
        src_dir_text = st.text_input("Папка с исходными изображениями", value=st.session_state.get("norm_src_dir", latest_default_dir or ""), key="norm_src_dir")
        st.button("Открыть папку", on_click=_open_folder, args=(src_dir_text,), key="btn_open_norm_src")
        include_filled_only = st.checkbox("Только *_filled (после Photoshop)", value=True, key="norm_only_filled")
        # drag&drop дополнительные картинки любого формата
        uploaded_files = st.file_uploader("Дополнительно перетащите файлы (png/jpg/webp и др.)", accept_multiple_files=True)
    with col_r:
        target_w = st.number_input("Ширина", min_value=64, max_value=4096, value=640, step=1)
        target_h = st.number_input("Высота", min_value=64, max_value=4096, value=1024, step=1)
        try_match_size = st.checkbox("Стараться сохранить размер файла (JPEG/WebP)", value=True, help="Подбор качества для достижения веса, близкого к исходному (±10%).")

    # Куда сохраняем
    # Формируем папку сохранения из текущего поля источника (оно уже показывает верный путь с суффиксом _N)
    computed_outdir = str(Path(src_dir_text).expanduser() / f"normalized_{target_w}x{target_h}") if src_dir_text else ""
    # Если включён режим авто-подстановки — синхронизируем путь сохранения с источником и размером
    if st.session_state.get("norm_follow_latest", True):
        st.session_state["norm_out_dir"] = computed_outdir
    base_outdir = st.text_input(
        "Папка для сохранения",
        value=st.session_state.get("norm_out_dir", computed_outdir),
        key="norm_out_dir",
    )
    st.button("Открыть папку", on_click=_open_folder, args=(base_outdir,), key="btn_open_norm_out")
    if base_outdir:
        try:
            Path(base_outdir).expanduser().mkdir(parents=True, exist_ok=True)
        except Exception as e:
            st.error(f"Не удалось создать папку для сохранения: {e}")
    st.caption(f"РСЃС‚РѕС‡РЅРёРє: {src_dir_text or 'вЂ”'} в†’ РЎРѕС…СЂР°РЅРµРЅРёРµ: {base_outdir or 'вЂ”'}")

    def _iter_src_files() -> list[Path]:
        paths: list[Path] = []
        # из папки
        if src_dir_text:
            pdir = Path(src_dir_text).expanduser()
            if pdir.is_dir():
                exts = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
                # только файлы верхнего уровня; жёсткий фильтр по окончанию стема на _filled
                pat = "*_filled.*" if include_filled_only else "*"
                total_scanned = 0
                skipped_non_filled = 0
                for p in pdir.glob(pat):
                    if not p.is_file():
                        continue
                    total_scanned += 1
                    if p.suffix.lower() in exts:
                        stem_low = p.stem.lower()
                        if include_filled_only and (not stem_low.endswith("_filled")):
                            skipped_non_filled += 1
                            continue
                        paths.append(p)
                # Отобразим краткую статистику отбора
                st.caption(f"Найдено в папке: {len(paths)} файлов для обработки (просканировано: {total_scanned}, пропущено (не *_filled): {skipped_non_filled})")
        # из upload'а
        for uf in uploaded_files or []:
            try:
                # Загруженные файлы всегда принимаем (по вашему требованию), независимо от *_filled
                tmp_name = f"tmp_upload_{uf.name}"
                tmp_base = Path(base_outdir).expanduser() if base_outdir else Path('.')
                tmp_base.mkdir(parents=True, exist_ok=True)
                tmp_path = tmp_base / tmp_name
                with open(tmp_path, "wb") as f:
                    f.write(uf.getbuffer())
                paths.append(tmp_path)
            except Exception as e:
                st.warning(f"Не удалось принять загруженный файл {uf.name}: {e}")
        return paths

    def _crop_to_fit(img: Image.Image, tw: int, th: int) -> Image.Image:
        src_w, src_h = img.size
        scale = max(tw / src_w, th / src_h)
        new_w, new_h = int(round(src_w * scale)), int(round(src_h * scale))
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        # центрируем crop
        left = max(0, (new_w - tw) // 2)
        top = max(0, (new_h - th) // 2)
        right = left + tw
        bottom = top + th
        return resized.crop((left, top, right, bottom))

    def _save_match_size(img: Image.Image, out_path: Path, orig_size: int) -> None:
        ext = out_path.suffix.lower()
        if ext in (".jpg", ".jpeg", ".webp"):
            # бинарный поиск качества, чтобы попасть в ±10% от исходника
            lo, hi = 30, 95
            target = orig_size
            best = None
            for _ in range(8):
                q = (lo + hi) // 2
                buf = _io.BytesIO()
                params = {}
                if ext in (".jpg", ".jpeg"):
                    params = dict(format="JPEG", quality=q, optimize=True, subsampling="4:2:0")
                else:
                    params = dict(format="WEBP", quality=q, method=6)
                try:
                    img.save(buf, **params)
                    size = buf.tell()
                except Exception:
                    size = 10**12
                best = (q, buf.getvalue(), size)
                # условие приближения
                if abs(size - target) <= max(1, int(target * 0.10)):
                    break
                if size > target:
                    hi = q - 1
                else:
                    lo = q + 1
            # финальная запись
            if best is not None:
                with open(out_path, "wb") as f:
                    f.write(best[1])
            else:
                img.save(out_path)
        else:
            # PNG/BMP: просто сохраняем, без гарантии веса
            try:
                if ext == ".png":
                    img.save(out_path, format="PNG", optimize=True)
                else:
                    img.save(out_path)
            except Exception:
                img.save(out_path)

    run_norm = st.button("Нормализовать", type="primary")

    if run_norm:
        src_files = _iter_src_files()
        if not src_files:
            st.warning("Нет входных файлов. Укажите папку или загрузите файлы.")
        else:
            prog = st.progress(0)
            done = 0
            out_paths: list[str] = []
            for p in src_files:
                try:
                    with Image.open(p) as im:
                        im = im.convert("RGB") if im.mode not in ("RGB", "RGBA") else im
                        out = _crop_to_fit(im, int(target_w), int(target_h))
                        out_name = p.stem + f"_{int(target_w)}x{int(target_h)}" + p.suffix
                        out_path = Path(base_outdir) / out_name if base_outdir else Path(out_name)
                        if try_match_size:
                            # Взвешенно подбираем качество. Для входных PNG/BMP при сохранении в PNG просто сохраняем без гарантии веса.
                            try:
                                orig_size = p.stat().st_size if p.exists() else len(open(p, 'rb').read())
                            except Exception:
                                try:
                                    with open(p, 'rb') as _fh:
                                        orig_size = len(_fh.read())
                                except Exception:
                                    orig_size = 0
                            _save_match_size(out, out_path, orig_size)
                        else:
                            # сохранить в исходный формат с разумными параметрами
                            ext = p.suffix.lower()
                            if ext in (".jpg", ".jpeg"):
                                out.save(out_path, format="JPEG", quality=90, subsampling="4:2:0", optimize=True)
                            elif ext == ".png":
                                out.save(out_path, format="PNG", optimize=True)
                            elif ext == ".webp":
                                out.save(out_path, format="WEBP", quality=80, method=6)
                            else:
                                out.save(out_path)
                        out_paths.append(str(out_path))
                except Exception as e:
                    st.error(f"Ошибка обработки {p}: {e}")
                done += 1
                prog.progress(int(done / max(1, len(src_files)) * 100))
            st.success(f"Готово. Сохранено: {len(out_paths)} файлов в {base_outdir}")
            # Запомним папку для следующей вкладки WebP как дефолт
            st.session_state["normalized_last_outdir"] = str(Path(base_outdir).resolve())
            # Покажем предпросмотр
            if out_paths:
                st.markdown("---")
                st.subheader("Результаты нормализации")
                cols = st.columns(4)
                for i, pth in enumerate(sorted(out_paths)):
                    with cols[i % 4]:
                        try:
                            st.image(pth, caption=os.path.basename(pth), use_container_width=True)
                        except Exception:
                            st.write(os.path.basename(pth))

# ---------------- Tab 5: WebP conversion (embed existing UI) ----------------
with tab5:
    st.subheader("Convert images to WebP")
    # По умолчанию берём результаты нормализации, если есть; иначе последнюю базовую папку генерации
    default_webp_dir = st.session_state.get("normalized_last_outdir") or st.session_state.get("unif_last_base_dir")
    if not default_webp_dir:
        try:
            from pathlib import Path as _P
            from datetime import datetime as _Dt
            default_webp_dir = str((_P("generate automation") / _Dt.now().strftime("%Y-%m-%d")).expanduser())
        except Exception:
            default_webp_dir = ""

    # Строим базовый outdir и имя сессии по аналогии с предыдущей реализацией
    try:
        gen_dir = Path(default_webp_dir).expanduser()
        suffix = ""
        try:
            name = gen_dir.name
            import re as _re
            m = _re.search(r"_([0-9]+)$", name)
            if m:
                suffix = m.group(0)  # like _9
        except Exception:
            pass
        default_outdir = str(gen_dir)
        default_session = f"webp{suffix or ''}"
    except Exception:
        default_outdir = None
        default_session = None

    # Встраиваем UI конвертера WebP
    convert_to_webp.run_streamlit_app(
        embed=True,
        default_folder=default_webp_dir,
        only_filled_default=False,
        default_base_outdir=default_outdir,
        default_session_name=default_session,
        follow_latest_defaults=True,
    )
    st.button("Открыть папку", on_click=_open_folder, args=(default_webp_dir,), key="btn_open_webp_src")

# ---------------- Tab 6: Titles & Descriptions regeneration (Gemini API) ----------------
with tab6:
    st.subheader("Regenerate product Titles & Descriptions (Gemini API)")
    st.caption("Эта вкладка использует только модель Gemini Flash для генерации (models/gemini-2.5-flash). При ошибках будет показана подробная причина от Google API.")

    import json as _json
    from collections import deque as _deque
    import time
    import random

    # --- Gemini API config (mirrors generate_pinterest_texts.py, text-only variant) ---
    GEMINI_API_KEYS = [
        "Your_api_key_here",
    ]
    MODELS = [
        "models/gemini-2.5-flash",
    ]
    RATE_LIMITS = {
        "models/gemini-2.5-flash": 10,
    }

    if "g_current_key_idx" not in st.session_state:
        st.session_state.g_current_key_idx = 0
    if "g_current_model_idx" not in st.session_state:
        st.session_state.g_current_model_idx = 0
    if "g_rate_windows" not in st.session_state:
        st.session_state.g_rate_windows = {i: _deque() for i in range(len(MODELS))}
    if "g_last_429_at" not in st.session_state:
        st.session_state.g_last_429_at = {i: 0.0 for i in range(len(MODELS))}

    def _g_get_current_url():
        model = MODELS[st.session_state.g_current_model_idx]
        key = GEMINI_API_KEYS[st.session_state.g_current_key_idx]
        return f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent?key={key}"

    def _g_wait_for_rate_slot(model_idx: int):
        name = MODELS[model_idx]
        rpm = RATE_LIMITS.get(name, 15)
        dq = st.session_state.g_rate_windows[model_idx]
        while True:
            now = time.time()
            while dq and (now - dq[0]) >= 60:
                dq.popleft()
            if len(dq) < rpm:
                dq.append(now)
                return
            sleep_for = max(1, int(60 - (now - dq[0]) + 1))
            time.sleep(sleep_for)

    def _g_call_gemini_text(prompt_text: str, timeout_sec: int = 90, max_duration_sec: int = 180, log=None) -> tuple[str | None, str | None]:
        # Text-only version of generateContent, with model/key rotation and rate limiting.
        # Returns (result_text, error_message). If generation fails within max_duration_sec, result_text is None
        # and error_message contains the last seen Google API error (status + body) or a fallback reason.
        import re as _re
        start_ts = time.time()
        last_error: str | None = None
        while True:
            # Stop if exceeded max duration
            if (time.time() - start_ts) > max_duration_sec:
                if last_error is None:
                    last_error = "Generation timed out without a specific error message."
                return None, last_error

            if st.session_state.g_current_model_idx >= len(MODELS):
                st.session_state.g_current_model_idx = 0
                st.session_state.g_current_key_idx += 1
                if st.session_state.g_current_key_idx >= len(GEMINI_API_KEYS):
                    st.session_state.g_current_key_idx = 0
                    time.sleep(60)
                st.session_state.g_last_429_at = {i: 0.0 for i in range(len(MODELS))}
                continue

            url = _g_get_current_url()
            model_name = MODELS[st.session_state.g_current_model_idx]
            key_idx = st.session_state.g_current_key_idx
            if log:
                log(f"Using model={model_name}, key_idx={key_idx}. Waiting for rate slot...")
            else:
                print(f"[TD] Using model={model_name}, key_idx={key_idx}. Waiting for rate slot...")
            _g_wait_for_rate_slot(st.session_state.g_current_model_idx)

            # Extra jitter delay to reduce Gemini API rate-limit bursts
            jitter_s = random.uniform(4.0, 8.0)
            if log:
                log(f"Jitter sleep {jitter_s:.1f}s before request (anti rate-limit)...")
            time.sleep(jitter_s)

            headers = {"Content-Type": "application/json"}
            payload = {
                "contents": [{"parts": [{"text": prompt_text}]}]
            }
            try:
                if log:
                    log("Sending request to Google API...")
                else:
                    print("[TD] Sending request to Google API...")
                resp = requests.post(url, json=payload, headers=headers, timeout=timeout_sec)
                if log:
                    log(f"Received response: status={resp.status_code}")
                else:
                    print(f"[TD] Received response: status={resp.status_code}")
                # Keep raw body for better error reporting if raise_for_status fails later
                raw_text = None
                try:
                    raw_text = resp.text
                except Exception:
                    raw_text = None
                resp.raise_for_status()
                data = resp.json() or {}
                pf = data.get("promptFeedback") or {}
                if "blockReason" in pf:
                    # Surface block reason to UI
                    br = pf.get("blockReason")
                    details = pf.get("blockReasonMessage") or pf.get("safetyRatings")
                    last_error = f"Prompt blocked by Google: {br}. Details: {details}"
                    return None, last_error
                cand = (data.get("candidates") or [])
                if cand:
                    content = cand[0].get("content") or {}
                    parts = content.get("parts") or []
                    if parts and "text" in parts[0]:
                        return parts[0]["text"], None
                # Unknown structure -> switch model but keep trace
                last_error = f"Unexpected response structure: {data!r}"
                if log:
                    log("Unexpected response structure. Will rotate model or retry.")
                st.session_state.g_current_model_idx += 1
                time.sleep(5)
                continue
            except requests.exceptions.RequestException as e:
                resp = getattr(e, 'response', None)
                status_code = resp.status_code if resp is not None else None
                body = None
                try:
                    body = resp.text if resp is not None else None
                except Exception:
                    body = None
                last_error = f"HTTP error from Google: status={status_code}, body={body}"
                if log:
                    log(f"HTTP error: status={status_code}. Body snippet: {str(body)[:400] if body else 'None'}")
                else:
                    print(f"[TD] HTTP error: status={status_code}. Body snippet: {str(body)[:200] if body else 'None'}")
                if status_code == 429:
                    now = time.time()
                    last = st.session_state.g_last_429_at[st.session_state.g_current_model_idx]
                    if (now - last) > 120:
                        st.session_state.g_last_429_at[st.session_state.g_current_model_idx] = now
                        time.sleep(61)
                    else:
                        st.session_state.g_current_model_idx += 1
                    continue
                elif status_code in (400, 404):
                    st.session_state.g_current_model_idx += 1
                    continue
                elif status_code in (401, 403):
                    st.session_state.g_current_key_idx += 1
                    if st.session_state.g_current_key_idx >= len(GEMINI_API_KEYS):
                        st.session_state.g_current_key_idx = 0
                        time.sleep(60)
                    st.session_state.g_current_model_idx = 0
                    time.sleep(3)
                    continue
                elif status_code and status_code >= 500:
                    time.sleep(15)
                    continue
                else:
                    time.sleep(10)
                    continue
            except Exception as e:
                last_error = f"Unexpected client error: {e}"
                time.sleep(10)
                continue

    # --- Dynamic pairs UI ---
    if "td_pairs" not in st.session_state:
        st.session_state.td_pairs = [{"title": "", "desc": ""}]

    st.markdown("Введите пары исходного Title и полного Description для регенерации. Можно добавлять больше пар.")

    new_pairs = []
    for i, pair in enumerate(st.session_state.td_pairs):
        st.markdown(f"#### Пара #{i+1}")
        t = st.text_input(f"Title #{i+1}", value=pair.get("title", ""), key=f"td_title_{i}")
        d = st.text_area(f"Full description #{i+1}", value=pair.get("desc", ""), key=f"td_desc_{i}", height=140)
        new_pairs.append({"title": t, "desc": d})
        st.markdown("---")

    col_add, col_rem = st.columns([1,1])
    with col_add:
        if st.button("+ Добавить пару", key="td_add"):
            st.session_state.td_pairs.append({"title": "", "desc": ""})
            st.rerun()
    with col_rem:
        if len(st.session_state.td_pairs) > 1 and st.button("− Убрать последнюю", key="td_rem"):
            st.session_state.td_pairs = st.session_state.td_pairs[:-1]
            st.rerun()
    st.session_state.td_pairs = new_pairs

    run_btn = st.button("Сгенерировать для всех пар", type="primary", key="td_run")

    def _build_prompt(src_title: str, src_full_desc: str) -> str:
        # Force strict JSON and valid HTML tags in long_description_html
        return (
            "You will rewrite and regenerate product marketing content from a provided original title and a full description.\n" \
            "Rules:\n" \
            "- Language: English only.\n" \
            "- This is an affiliate page: do NOT say 'our product' or imply ownership.\n" \
            "- Output EXACTLY one JSON object (no markdown fences, no extra text). Use valid JSON with double quotes for keys and strings.\n" \
            "- Provide these keys:\n" \
            "  \"short_title\": Shorten the original title, keeping the core product type and optionally the brand.\n" \
            "  \"short_description\": One paragraph, persuasive sales-style, no lists, suitable to appear at the right of the product image on a store page.\n" \
            "  \"long_description_html\": Valid HTML string. Start with <h2>Why you should buy this product.</h2>. Use <p> for paragraphs. If you add subheadings, use <h5> only.\n" \
            "Return only the JSON object.\n\n" \
            f"Original title to shorten: \"{src_title.strip()}\"\n" \
            f"Original full description:\n{src_full_desc.strip()}\n"
        )

    if run_btn:
        results = []
        status = st.status("Processing pairs...", expanded=True)
        with status:
            for i, pair in enumerate(st.session_state.td_pairs, 1):
                st.write(f"Queueing pair #{i} ...")
            st.write("Starting generation...")

        for i, pair in enumerate(st.session_state.td_pairs, 1):
            with st.spinner(f"Generating pair #{i} ..."):
                log_lines = []
                log_box = st.empty()
                def _log(msg: str):
                    ts = time.strftime('%H:%M:%S')
                    line = f"[{ts}] {msg}"
                    log_lines.append(line)
                    try:
                        log_box.caption("\n".join(log_lines))
                    except Exception:
                        pass
                title_in = (pair.get("title") or "").strip()
                desc_in = (pair.get("desc") or "").strip()
                if not title_in and not desc_in:
                    results.append({"index": i, "error": "Empty inputs"})
                    continue
                prompt = _build_prompt(title_in, desc_in)
                text, g_error = _g_call_gemini_text(prompt, timeout_sec=45, max_duration_sec=90, log=_log)
                if log_lines:
                    log_box.caption("\n".join(log_lines))
                if not text:
                    # Record Google error message to show in UI
                    results.append({"index": i, "error": g_error or "Unknown error from Google API"})
                    continue
                item = {"index": i, "raw": text}
                # Try parse JSON robustly
                parsed = None
                s = text.strip()
                # strip code fences or leading/trailing junk
                if s.startswith("```"):
                    try:
                        s = s.split("```", 2)[1]
                    except Exception:
                        pass
                # sometimes models prepend words like 'json' or explanations
                for prefix in ("json", "JSON", "Result:"):
                    if s.lower().startswith(prefix.lower()):
                        s = s[len(prefix):].lstrip(':\n ')
                # attempt to find first {...} json object
                try:
                    first_brace = s.find('{')
                    last_brace = s.rfind('}')
                    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                        s_obj = s[first_brace:last_brace+1]
                        parsed = _json.loads(s_obj)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    item.update({
                        "short_title": parsed.get("short_title"),
                        "short_description": parsed.get("short_description"),
                        "long_description_html": parsed.get("long_description_html"),
                    })
                else:
                    # try to salvage by splitting lines that look like key: value
                    try:
                        tmp = {}
                        for line in s.splitlines():
                            if '"short_title"' in line or 'short_title' in line:
                                tmp['short_title'] = line.split(':',1)[1].strip().strip('",')
                            if '"short_description"' in line or 'short_description' in line:
                                tmp['short_description'] = line.split(':',1)[1].strip().strip('",')
                            if '"long_description_html"' in line or 'long_description_html' in line:
                                tmp['long_description_html'] = line.split(':',1)[1].strip()
                        if tmp:
                            item.update(tmp)
                    except Exception:
                        pass
                results.append(item)

        status.update(label="Done", state="complete")

        # Render
        for item in results:
            st.markdown(f"### Result for pair #{item.get('index', '?')}")
            if item.get("error"):
                st.error(item["error"]) 
                continue
            st.write("Shortened title:")
            st.success(item.get("short_title") or "—")
            st.write("Short description:")
            st.write(item.get("short_description") or "—")
            st.write("Long description (HTML):")
            html = item.get("long_description_html") or item.get("raw") or ""
            st.markdown(html, unsafe_allow_html=True)
            st.markdown("---")

# ---------------- Tab 7: Regenerate product images (Gemini UI) ----------------
with tab7:
    st.subheader("Regenerate product images (Gemini UI)")
    st.caption(
        "Эта вкладка открывает Chrome с выбранным профилем и создаёт до 6 вкладок Gemini. "
        "В каждой вкладке она просто вставляет промпт (без отправки), чтобы вы могли быстро нажать Generate/Send вручную."
    )

    # Параметры запуска браузера: максимально похоже на Tab 1
    # (используем отдельные ключи, но подставляем значения из Tab 1 по умолчанию)
    regen_url = st.selectbox(
        "URL интерфейса Gemini",
        DEFAULT_URLS,
        index=DEFAULT_URLS.index(st.session_state.get("unif_url", DEFAULT_URLS[0])) if st.session_state.get("unif_url", DEFAULT_URLS[0]) in DEFAULT_URLS else 0,
        key="unif_regen_url",
    )
    regen_headless = st.checkbox(
        "Headless режим",
        value=st.session_state.get("unif_headless", False),
        key="unif_regen_headless",
    )
    regen_use_auto_profile = st.checkbox(
        "Отдельный профиль для автоматики (рекомендуется)",
        value=st.session_state.get("unif_auto_profile", True),
        key="unif_regen_auto_profile",
    )

    if "_tmp_new_uddir_regen" in st.session_state:
        st.session_state["unif_regen_user_data_dir"] = st.session_state.pop("_tmp_new_uddir_regen")

    regen_user_data_dir = st.text_input(
        "Путь к профилю (user-data-dir)",
        value=st.session_state.get(
            "unif_regen_user_data_dir",
            st.session_state.get("unif_user_data_dir", os.path.abspath(".chrome_automation_profile")),
        ),
        key="unif_regen_user_data_dir",
    )
    regen_user_data_dir = os.path.abspath(os.path.expanduser(regen_user_data_dir))
    st.caption(f"Реально используется: {regen_user_data_dir}")

    regen_executable_path = st.text_input(
        "Путь к chrome.exe",
        value=st.session_state.get("unif_exe_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"),
        key="unif_regen_exe_path",
    )

    regen_use_cdp = st.checkbox(
        "Запуск и подключение по CDP (рекомендуется)",
        value=st.session_state.get("unif_use_cdp", True),
        key="unif_regen_use_cdp",
    )

    if "_tmp_new_cdp_regen" in st.session_state:
        st.session_state["unif_regen_cdp_url"] = st.session_state.pop("_tmp_new_cdp_regen")

    regen_cdp_url = st.text_input(
        "CDP URL",
        value=st.session_state.get("unif_regen_cdp_url", st.session_state.get("unif_cdp_url", "http://127.0.0.1:9222")),
        key="unif_regen_cdp_url",
    )

    # ----- UI: titles + pasted image per item -----
    if "unif_regen_items" not in st.session_state:
        st.session_state.unif_regen_items = [{"title": "", "image_dataurl": None}]

    st.markdown("### Products (Title + Image)")
    st.caption(
        "Для каждого товара можно вставить картинку через Ctrl+V (скопируйте картинку на сайте → кликните в область вставки → Ctrl+V). "
        "При запуске картинка будет прикреплена в Gemini вместе с текстом."
    )

    new_items = []
    for i, item in enumerate(st.session_state.unif_regen_items):
        st.markdown(f"#### Item #{i+1}")
        # Paste image block
        img_dataurl = _paste_image(key=f"unif_regen_paste_{i}")
        # Preserve previous value if component returns None on reruns
        prev_img = item.get("image_dataurl")
        if img_dataurl is None:
            img_dataurl = prev_img
        t = st.text_input(f"Title #{i+1}", value=item.get("title", ""), key=f"unif_regen_title_{i}")
        new_items.append({"title": t, "image_dataurl": img_dataurl})
        st.markdown("---")

    col_add, col_rem = st.columns([1, 1])
    with col_add:
        if st.button("+ Добавить поле", key="unif_regen_add"):
            st.session_state.unif_regen_items.append({"title": "", "image_dataurl": None})
            st.rerun()
    with col_rem:
        if len(st.session_state.unif_regen_items) > 1 and st.button("− Убрать последнее", key="unif_regen_rem"):
            st.session_state.unif_regen_items = st.session_state.unif_regen_items[:-1]
            st.rerun()

    st.session_state.unif_regen_items = new_items

    st.markdown("---")

    run_regen_btn = st.button("Открыть Gemini и вставить промпты", type="primary", key="unif_regen_run")

    def _build_regen_image_prompt(title: str) -> str:
        title = (title or "").strip()
        return (
            f"мне нужен этот же самый ({title}) но важно чтобы было с другим ракурсом и фоном "
            "но при этом важно чтобы ты старался не искажать товар а просто показал с другого ракурса и другим фоном. "
            "создай такое изображение пожалуйста. изображение должно быть максимально реалистичным. "
            "это для карточки товаров в интернет магазине поэтому товар должен быть отчетливо виден. "
            "СМЕНА РАКУРСА ОЧЕНЬ ВАЖНА"
        )

    if run_regen_btn:
        items_clean = [
            {"title": (it.get("title") or "").strip(), "image_dataurl": it.get("image_dataurl")}
            for it in st.session_state.unif_regen_items
            if (it.get("title") or "").strip()
        ]
        if not items_clean:
            st.error("Добавьте хотя бы один Title.")
        else:
            # Открываем вкладок ровно по количеству заполненных (непустых) тайтлов
            items_to_process = items_clean

            result = {"ok": False, "error": None, "tabs": 0}

            def _worker():
                try:
                    from playwright.sync_api import sync_playwright as _sp
                    import urllib.request as _ul
                    def _is_up(url: str) -> bool:
                        try:
                            with _ul.urlopen(url + "/json/version", timeout=1) as resp:
                                return resp.status == 200
                        except Exception:
                            return False

                    p = _sp().start()
                    ctx = None

                    if regen_use_cdp:
                        # Поднимем Chrome с remote debugging если ещё не поднят
                        if not _is_up(regen_cdp_url):
                            cmd = [
                                regen_executable_path,
                                f"--remote-debugging-port={regen_cdp_url.split(':')[-1]}",
                                f"--user-data-dir={regen_user_data_dir}",
                                "--lang=ru-RU",
                            ]
                            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            deadline = time.time() + 10
                            while time.time() < deadline and not _is_up(regen_cdp_url):
                                time.sleep(0.3)

                        browser = p.chromium.connect_over_cdp(regen_cdp_url)
                        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                    else:
                        # Persistent context
                        if regen_use_auto_profile:
                            os.makedirs(regen_user_data_dir, exist_ok=True)
                        browser = _launch_persistent_ctx_with_retries(
                            p,
                            user_data_dir=regen_user_data_dir if regen_use_auto_profile else None,
                            headless=regen_headless,
                            executable_path=regen_executable_path or None,
                        )
                        ctx = browser

                    # Создаём вкладки и прикрепляем картинку + вставляем текст
                    for i, item in enumerate(items_to_process, 1):
                        title = (item.get("title") or "").strip()
                        image_dataurl = item.get("image_dataurl")
                        page = ctx.new_page()
                        page.set_default_timeout(30000)
                        try:
                            page.goto(regen_url, wait_until="load")
                        except Exception:
                            # если load завис — пробуем domcontentloaded
                            try:
                                page.goto(regen_url, wait_until="domcontentloaded")
                            except Exception:
                                pass

                        # Входим в новый чат и ждём поле ввода
                        try:
                            _start_new_chat(page)
                        except Exception:
                            pass
                        try:
                            _wait_input_ready(page, timeout_ms=60000)
                            _dismiss_overlays(page)
                        except Exception:
                            # fallback: перезагрузка
                            try:
                                page.reload(wait_until="load")
                            except Exception:
                                pass
                            try:
                                _start_new_chat(page)
                            except Exception:
                                pass
                            _wait_input_ready(page, timeout_ms=60000)
                            _dismiss_overlays(page)

                        # Если есть картинка (вставленная в Streamlit) — прикрепляем её в Gemini
                        if image_dataurl:
                            try:
                                import base64 as _b64
                                import tempfile as _tf
                                import re as _re
                                m = _re.match(r"^data:(image/[^;]+);base64,(.+)$", image_dataurl)
                                if m:
                                    mime = m.group(1)
                                    b64 = m.group(2)
                                    ext = ".png"
                                    if "jpeg" in mime or "jpg" in mime:
                                        ext = ".jpg"
                                    elif "webp" in mime:
                                        ext = ".webp"
                                    raw = _b64.b64decode(b64)
                                    tmp = _tf.NamedTemporaryFile(prefix="tmp_rovodev_paste_", suffix=ext, delete=False)
                                    try:
                                        tmp.write(raw)
                                        tmp.flush()
                                    finally:
                                        tmp.close()
                                    ok = _attach_image(page, tmp.name)
                                    _wait_image_attached(page, timeout_ms=10000)
                                    try:
                                        os.unlink(tmp.name)
                                    except Exception:
                                        pass
                                    if ok:
                                        _dismiss_overlays(page)
                            except Exception:
                                pass

                        # Выбираем модель "Быстрая" по умолчанию (как в Tab 1)
                        try:
                            gph._pick_model(page, "Быстрая")
                        except Exception:
                            pass

                        if (title or "").strip():
                            _type_prompt(page, _build_regen_image_prompt(title))
                            # Отправляем запрос
                            # Send ONLY via the explicit send button helper.
                            # Enter fallback caused accidental sends / typing into the wrong area on some accounts.
                            try:
                                _click_send(page)
                            except Exception:
                                pass
                            # Небольшая пауза, чтобы запрос успел уйти
                            time.sleep(0.8)
                        # Переходим к следующей вкладке (следующий item)

                    result["ok"] = True
                    result["tabs"] = len(items_to_process)
                    try:
                        p.stop()
                    except Exception:
                        pass
                except Exception as e:
                    result["error"] = str(e)

            import threading
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join()

            if result.get("error"):
                st.error(f"Ошибка: {result['error']}")
            else:
                st.success(f"Готово: открыто вкладок и вставлено промптов: {result.get('tabs', 0)}")
                st.info("Теперь можно перейти в Chrome и нажать Generate/Send в каждой вкладке.")

