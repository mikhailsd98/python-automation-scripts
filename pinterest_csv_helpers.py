# -*- coding: utf-8 -*-
"""Pinterest CSV helper functions.

This module centralizes logic for:
- normalizing output of generate_pinterest_texts.generate_pinterest_assets()
- composing Pinterest "bulk uploader" CSV rows
- (optional) uploading local images to imgbb to fill Media URL

It is intentionally Streamlit-agnostic: UI code stays in Streamlit apps.
"""

from __future__ import annotations

import base64
import csv
import io
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests

# Pinterest bulk uploader header (comma-separated, no BOM)
DEFAULT_IMGBB_API_KEY = "9e3dce4c0abe60088d719cb2e3bf11e4"


def get_default_imgbb_api_key() -> str:
    """Return default IMGBB API key.

    Priority:
    1) environment variable IMGBB_API_KEY
    2) built-in fallback (kept for backward compatibility with existing apps)
    """

    return (os.getenv("IMGBB_API_KEY") or DEFAULT_IMGBB_API_KEY).strip()


PINTEREST_BULK_HEADER: list[str] = [
    "Title",
    "Media URL",
    "Pinterest board",
    "Thumbnail",
    "Description",
    "Link",
    "Publish date",
    "Keywords",
]


def load_board_names(path: str = "board_names.txt") -> list[str]:
    """Load Pinterest board names from a text file (one board per line).

    Returns a list that always contains an empty option as the first element.
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


def imgbb_upload_bytes(image_bytes: bytes, api_key: str, filename: str | None = None, timeout_sec: int = 90) -> str:
    """Upload image bytes to imgbb and return hosted image URL."""

    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("IMGBB API key is empty")

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data: dict[str, str] = {"key": api_key, "image": b64}

    if filename:
        base = os.path.splitext(os.path.basename(filename))[0]
        if base:
            data["name"] = base

    resp = requests.post("https://api.imgbb.com/1/upload", data=data, timeout=timeout_sec)
    resp.raise_for_status()
    j = resp.json() or {}
    if not isinstance(j, dict) or not j.get("success"):
        err = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else None
        raise RuntimeError(f"imgbb upload failed: {err or 'unknown error'}")

    d = j.get("data") or {}
    url = d.get("url") or d.get("display_url")
    if not url:
        raise RuntimeError("imgbb upload failed: response has no url")
    return str(url)


def build_description(pin: dict[str, Any], include_hashtags: bool) -> str:
    """Build Pinterest description, optionally appending hashtags."""

    desc = (pin.get("description") or "").strip()

    ht = pin.get("hashtags")
    hashtags_str = ""
    if isinstance(ht, str):
        hashtags_str = ht.strip()
    elif isinstance(ht, list):
        cleaned: list[str] = []
        for h in ht:
            s = str(h or "").strip()
            if not s:
                continue
            cleaned.append(s if s.startswith("#") else f"#{s.lstrip('#')}")
        hashtags_str = " ".join(cleaned).strip()

    if include_hashtags and hashtags_str:
        return (desc + "\n\n" + hashtags_str).strip()
    return desc


def extract_first_title(pin: dict[str, Any]) -> str:
    """Pick the first title from title_options (or a fallback title field)."""

    titles = pin.get("title_options")
    if isinstance(titles, list) and titles:
        return str(titles[0] or "").strip()

    t = pin.get("title")
    if isinstance(t, str) and t.strip():
        return t.strip()

    return ""


def _strip_markdown_emphasis(s: str) -> str:
    s = (s or "")
    # remove bold/italic markers (best-effort)
    s = s.replace("**", "")
    s = s.replace("*", "")
    return s


def _parse_pinterest_raw_text(raw: str) -> dict[str, Any]:
    """Parse non-JSON Gemini Pinterest response into structured fields.

    Best-effort tolerant parser for Markdown-ish output.
    """

    txt = (raw or "").strip()
    if not txt:
        return {}

    t = txt.replace("\r\n", "\n")

    def _clean_heading(s: str) -> str:
        s = (s or "").strip()
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = _strip_markdown_emphasis(s)
        return s.strip()

    def _strip_list_prefix(s: str) -> str:
        s = (s or "").strip()
        s = _strip_markdown_emphasis(s)
        s = re.sub(r"^[\-\u2022]+\s+", "", s)
        s = re.sub(r"^\d+\s*[\.\)\-:]\s+", "", s)

        # tolerate JSON-ish list items: "...",
        s = s.strip().rstrip(",")
        if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
            s = s[1:-1]

        return s.strip()

    lines = t.split("\n")
    cur: str | None = None  # 'title'|'desc'|'hashtags'|'three'
    buf: dict[str, list[str]] = {"title": [], "desc": [], "hashtags": [], "three": []}

    for ln in lines:
        raw_ln = ln
        ln = (ln or "").rstrip()

        if not ln.strip():
            if cur == "desc":
                buf["desc"].append("")
            continue

        # Support pseudo-JSON lines like:
        # "DESCRIPTION": "..."
        # "HASHTAGS": "tag1 tag2"
        m_inline = re.match(r"^\s*\"?([A-Za-z0-9 _\-]+)\"?\s*:\s*(.*?)\s*$", ln)
        if m_inline:
            k = (m_inline.group(1) or "").strip().lower()
            v = (m_inline.group(2) or "").strip().rstrip(",")
            # strip surrounding quotes
            if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
                v = v[1:-1]

            if k.startswith("description"):
                buf["desc"].append(v)
                cur = "desc"
                continue
            if k.startswith("hashtags"):
                buf["hashtags"].append(v)
                cur = "hashtags"
                continue
            if k.startswith("title"):
                # value might be empty, keep cur to collect subsequent lines
                if v and v not in ("[", "{"):
                    buf["title"].append(v)
                cur = "title"
                continue
            if k.startswith("three") and ("keyword" in k or "word" in k):
                if v and v not in ("[", "{"):
                    buf["three"].append(v)
                cur = "three"
                continue

        head = _clean_heading(ln)
        head_low = head.lower()

        if re.match(r"^title(\s+options)?\b", head_low):
            cur = "title"
            continue
        if re.match(r"^description\b", head_low) or head_low.startswith("description and"):
            cur = "desc"
            continue
        if re.match(r"^hashtags\b", head_low):
            cur = "hashtags"
            continue
        if re.match(r"^(three[- ]word\s+keywords|three\s+word\s+keywords)\b", head_low):
            cur = "three"
            continue

        if "description" in head_low and head_low.endswith(":"):
            cur = "desc"
            continue
        if "hashtags" in head_low and head_low.endswith(":"):
            cur = "hashtags"
            continue
        if ("three" in head_low and "keyword" in head_low) and head_low.endswith(":"):
            cur = "three"
            continue
        if "title" in head_low and head_low.endswith(":"):
            cur = "title"
            continue

        if cur is None:
            continue

        buf[cur].append(raw_ln)

    out: dict[str, Any] = {}

    # Titles
    title_lines = [_strip_list_prefix(_clean_heading(x)) for x in buf["title"]]
    title_lines = [x for x in title_lines if x]
    title_lines = [x for x in title_lines if not x.lower().startswith(("here are", "explanation"))]

    long_titles = [x for x in title_lines if ("|" in x) or (len(x.split()) >= 5)]
    titles = long_titles or title_lines

    seen_t: set[str] = set()
    dedup_titles: list[str] = []
    for x in titles:
        x = _strip_markdown_emphasis(x).strip()
        key = " ".join(x.split())
        if key and key not in seen_t:
            dedup_titles.append(x.strip())
            seen_t.add(key)

    if dedup_titles:
        out["title_options"] = dedup_titles

    # Description
    desc_text = "\n".join(buf["desc"]).strip()
    if desc_text:
        paras = [p.strip() for p in re.split(r"\n\s*\n", desc_text) if p.strip()]
        if paras:
            chosen = ""
            for p in paras:
                p_clean = _clean_heading(p)
                if p_clean.lower().startswith(("hashtags", "title", "explanation")):
                    continue
                chosen = p_clean
                break
            if not chosen:
                chosen = _clean_heading(paras[0])
            if chosen:
                out["description"] = _strip_markdown_emphasis(chosen).strip()

    # Hashtags
    ht_text = "\n".join(buf["hashtags"]).strip()
    if ht_text:
        tags = re.findall(r"#[_A-Za-z0-9]+", ht_text)
        if tags:
            out["hashtags"] = " ".join(tags)
        else:
            flat = ht_text.replace("\n", " ")
            parts = [p.strip() for p in re.split(r"[\s,]+", flat) if p.strip()]
            cleaned: list[str] = []
            for p in parts:
                cleaned.append(p if p.startswith("#") else f"#{p.lstrip('#')}")
            if cleaned:
                out["hashtags"] = " ".join(cleaned)

    # Three-word keywords
    three_text = "\n".join(buf["three"]).strip()
    if three_text:
        lines = [ln.strip() for ln in three_text.split("\n") if ln.strip()]
        cleaned_three: list[str] = []
        for ln in lines:
            ln = _strip_list_prefix(_clean_heading(ln))
            if ln:
                cleaned_three.append(" ".join(ln.split()))
        if cleaned_three:
            out["three_word_keywords"] = cleaned_three[:10]

    return out


def normalize_pinterest_pin(pin: Any) -> dict[str, Any]:
    """Best-effort normalize pin dict returned by generate_pinterest_assets().

    Notes:
    - We preserve the original model output in `_raw` so Streamlit UIs can show it.
    - If `_raw` is missing, we provide a fallback textual representation.
    """

    if not isinstance(pin, dict):
        return {}

    raw_original = pin.get("_raw")
    if not (isinstance(raw_original, str) and raw_original.strip()):
        # Fallback: make something human-readable.
        try:
            import json as _json

            raw_original = _json.dumps(pin, ensure_ascii=False, indent=2)
        except Exception:
            raw_original = str(pin)

    def _is_blank(v: Any) -> bool:
        if v is None:
            return True
        if isinstance(v, str) and not v.strip():
            return True
        if isinstance(v, list) and len(v) == 0:
            return True
        return False

    def _merge_from(src: dict[str, Any], dst: dict[str, Any]) -> dict[str, Any]:
        aliases: dict[str, list[str]] = {
            "description": ["description", "Description", "desc", "Desc"],
            "hashtags": ["hashtags", "Hashtags", "hashTags", "hash_tags", "tags", "Tags"],
            "three_word_keywords": [
                "three_word_keywords",
                "threeWordKeywords",
                "three_word_keys",
                "threeWordKeys",
                "keywords",
                "Keywords",
            ],
            "title_options": [
                "title_options",
                "titleOptions",
                "Title options",
                "Title Options",
                "titles",
                "Titles",
            ],
        }

        for canon, keys in aliases.items():
            if not _is_blank(dst.get(canon)):
                continue
            for k in keys:
                if k in src and not _is_blank(src.get(k)):
                    dst[canon] = src.get(k)
                    break

        return dst

    out: dict[str, Any] = {
        "description": pin.get("description"),
        "hashtags": pin.get("hashtags"),
        "three_word_keywords": pin.get("three_word_keywords"),
        "title_options": pin.get("title_options"),
        "_raw": raw_original,
    }

    # 1) Merge from pin itself (handles wrong casing)
    _merge_from(pin, out)

    # 2) If everything empty, try to parse _raw
    raw = pin.get("_raw")
    if isinstance(raw, str) and raw.strip():
        parsed = _parse_pinterest_raw_text(raw)
        _merge_from(parsed, out)

    # 3) Normalize types
    if isinstance(out.get("hashtags"), str):
        # split by spaces for optional list usage elsewhere, but keep as string for CSV
        out["hashtags"] = out["hashtags"].strip()

    if isinstance(out.get("title_options"), str):
        # if model returned a single string
        out["title_options"] = [out["title_options"].strip()]

    if isinstance(out.get("three_word_keywords"), str):
        # tolerate newline separated keywords
        parts = [p.strip() for p in re.split(r"[\n,]+", out["three_word_keywords"]) if p.strip()]
        out["three_word_keywords"] = parts

    return out


def build_keywords_field(pin: dict[str, Any]) -> str:
    """Build a CSV-friendly 'Keywords' field.

    Pinterest bulk uploader accepts a free-form keywords cell.
    We fill it from:
    - three_word_keywords (preferred)
    - hashtags (without '#') as a fallback
    """

    import re

    three = pin.get("three_word_keywords")
    parts: list[str] = []

    if isinstance(three, list):
        for x in three:
            s = " ".join(str(x or "").split()).strip()
            if s:
                parts.append(s)
    elif isinstance(three, str) and three.strip():
        parts.extend([p.strip() for p in re.split(r"[\n,]+", three) if p.strip()])

    if not parts:
        ht = pin.get("hashtags")
        ht_str = ""
        if isinstance(ht, str):
            ht_str = ht
        elif isinstance(ht, list):
            ht_str = " ".join(str(x or "") for x in ht)
        tags = re.findall(r"#([_A-Za-z0-9]+)", ht_str or "")
        parts.extend([t for t in tags if t])

    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        key = p.lower()
        if key and key not in seen:
            out.append(p)
            seen.add(key)

    return ", ".join(out)


def default_publish_date_iso(dt: datetime | None = None) -> str:
    dt = dt or datetime.now()
    # Keep the same format as existing app: date + fixed time.
    return dt.strftime("%Y-%m-%d") + "T08:00:00"


def build_publish_schedule_iso(
    n: int,
    *,
    max_per_day: int = 10,
    start_date: object | None = None,
) -> list[str]:
    """Build a publish schedule (local naive ISO strings) with a daily cap.

    Parameters:
    - n: number of pins
    - max_per_day: daily cap
    - start_date: optional start day for the schedule (date/datetime/ISO string).
      If provided, schedule starts on that day at 08:00 (local time) instead of "now".

    Behavior:
    - If start_date is not provided:
      - Day 0: first chunk (<= max_per_day) is spread evenly from *now* (rounded to minute)
        until next midnight.
      - Day 1..k: next chunks are spread evenly from 08:00 to next midnight of that day.

    Returns ISO strings like 'YYYY-MM-DDTHH:MM:SS'.
    """

    if n <= 0:
        return []

    max_per_day = int(max_per_day)
    if max_per_day <= 0:
        max_per_day = 1

    now = datetime.now().replace(second=0, microsecond=0)

    # Resolve optional start_date
    resolved_start: datetime | None = None
    if start_date is not None:
        try:
            # date_input returns datetime.date
            from datetime import date as _date

            if isinstance(start_date, datetime):
                resolved_start = start_date.replace(second=0, microsecond=0)
            elif isinstance(start_date, _date):
                resolved_start = datetime.combine(start_date, datetime.min.time()).replace(hour=8, minute=0, second=0, microsecond=0)
            elif isinstance(start_date, str) and start_date.strip():
                s = start_date.strip()
                # accept YYYY-MM-DD or ISO datetime
                if len(s) == 10:
                    d = datetime.strptime(s, "%Y-%m-%d").date()
                    resolved_start = datetime.combine(d, datetime.min.time()).replace(hour=8, minute=0, second=0, microsecond=0)
                else:
                    s_norm = s.replace(" ", "T")
                    resolved_start = datetime.strptime(s_norm[:19], "%Y-%m-%dT%H:%M:%S").replace(second=0, microsecond=0)
        except Exception:
            resolved_start = None

    base = resolved_start or now

    def _even_spread(start: datetime, end: datetime, count: int) -> list[datetime]:
        if count <= 0:
            return []
        if count == 1:
            return [start]
        total_seconds = max(0.0, (end - start).total_seconds())
        step = total_seconds / float(max(1, count - 1))
        return [start + timedelta(seconds=step * i) for i in range(count)]

    out: list[str] = []
    remaining = n
    day_offset = 0

    while remaining > 0:
        chunk = min(remaining, max_per_day)

        if day_offset == 0:
            start = base
            end = (base + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            day = (base + timedelta(days=day_offset)).date()
            start = datetime.combine(day, datetime.min.time()).replace(hour=8, minute=0, second=0, microsecond=0)
            end = (start + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        for dt in _even_spread(start, end, chunk):
            out.append(dt.strftime("%Y-%m-%dT%H:%M:%S"))

        remaining -= chunk
        day_offset += 1

    return out


def build_default_export_rows(
    pin_map: dict[str, Any],
    *,
    include_hashtags: bool,
    publish_date: str | None = None,
    publish_schedule: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build default rows for Streamlit data_editor (includes local_image_path)."""

    publish_date = publish_date or default_publish_date_iso()
    publish_schedule = publish_schedule or []
    rows: list[dict[str, Any]] = []

    for idx, (img_path, pin) in enumerate(pin_map.items()):
        pin_d = normalize_pinterest_pin(pin)
        rows.append(
            {
                "local_image_path": img_path,
                "Title": extract_first_title(pin_d),
                "Media URL": "",
                "Pinterest board": "",
                "Thumbnail": "",
                "Description": build_description(pin_d, include_hashtags=bool(include_hashtags)),
                "Link": "",
                "Publish date": publish_schedule[idx] if idx < len(publish_schedule) else publish_date,
                "Keywords": build_keywords_field(pin_d),
            }
        )

    return rows


def fill_media_urls_from_cache(
    export_rows: list[dict[str, Any]],
    *,
    urls_cache: dict[str, str],
) -> None:
    """Fill empty 'Media URL' cells from urls_cache by local_image_path."""

    for row in export_rows:
        p = str(row.get("local_image_path") or "").strip()
        if p and not (row.get("Media URL") or "").strip():
            row["Media URL"] = urls_cache.get(p, "")


def list_needed_local_paths(export_rows: Iterable[dict[str, Any]]) -> list[str]:
    paths = [str(r.get("local_image_path") or "").strip() for r in (export_rows or [])]
    return [p for p in paths if p]


def validate_local_paths_exist(paths: Iterable[str]) -> list[str]:
    missing = [p for p in paths if p and not os.path.exists(p)]
    return missing


def _normalize_publish_date_for_pinterest_csv(publish_date: str) -> str:
    """Normalize 'Publish date' so Pinterest schedules at the intended local time.

    Pinterest bulk CSV importer commonly interprets timezone-less ISO datetimes as UTC.
    When we type local time (e.g. 11:04) into the table, Pinterest may shift it by the
    local UTC offset (e.g. schedule at 14:04 for UTC+3).

    Strategy (same as pinterest_post_texts_streamlit.py):
    - If the string already contains an explicit timezone ('Z', '+03:00', etc.) we keep it.
    - If it's a timezone-less ISO like 'YYYY-MM-DDTHH:MM:SS', we treat it as *local time*
      and convert to a timezone-less UTC string.
    """

    s = (publish_date or "").strip()
    if not s:
        return ""

    # If user already provided timezone information, don't touch it.
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
        return s

    local_tz = datetime.now().astimezone().tzinfo
    if local_tz is None:
        return s_norm

    dt_local = dt_local_naive.replace(tzinfo=local_tz)
    dt_utc_naive = dt_local.astimezone(timezone.utc).replace(tzinfo=None)
    return dt_utc_naive.strftime("%Y-%m-%dT%H:%M:%S")


def build_pinterest_bulk_csv_bytes(export_rows: Iterable[dict[str, Any]]) -> bytes:
    """Build CSV bytes for Pinterest bulk uploader (UTF-8, comma-separated, CRLF).

    Note: we apply Pinterest timezone fix at export time so the editable tables can
    continue to show local wall-clock times.
    """

    out = io.StringIO()
    writer = csv.writer(out, delimiter=",", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(PINTEREST_BULK_HEADER)

    for row in (export_rows or []):
        writer.writerow(
            [
                (row.get("Title") or "").strip(),
                (row.get("Media URL") or "").strip(),
                (row.get("Pinterest board") or "").strip(),
                (row.get("Thumbnail") or "").strip(),
                (row.get("Description") or "").strip(),
                (row.get("Link") or "").strip(),
                _normalize_publish_date_for_pinterest_csv((row.get("Publish date") or "").strip()),
                (row.get("Keywords") or "").strip(),
            ]
        )

    return out.getvalue().encode("utf-8")
