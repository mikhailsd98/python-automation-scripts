import base64
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List

import requests
import streamlit as st
from requests.auth import HTTPBasicAuth


PLACEHOLDER_RE = re.compile(r"\{\{\s*media:([a-zA-Z0-9_\-]+):(url|id|alt)\s*\}\}")


def _get_secret(key: str, default: str = "") -> str:
    """Safely read Streamlit secrets.

    If no secrets.toml exists, Streamlit raises StreamlitSecretNotFoundError on access.
    We treat that case as "no secret" and return default.
    """
    try:
        return str(st.secrets.get(key, default))
    except Exception:
        return default


@dataclass
class MediaInfo:
    key: str
    path: str
    alt: str = ""
    title: str = ""


class WordPressClient:
    def __init__(self, base_url: str, username: str, app_password: str, verify_ssl: bool = True, timeout_s: int = 60):
        self.base_url = base_url.rstrip("/")
        self.api_base = f"{self.base_url}/wp-json/wp/v2"
        self.auth = HTTPBasicAuth(username, app_password)
        self.verify_ssl = verify_ssl
        self.timeout_s = timeout_s

    def _request(self, method: str, path: str, *, params: Optional[dict] = None, json_body: Optional[dict] = None, headers: Optional[dict] = None, files=None, data=None):
        url = f"{self.api_base}{path}"
        resp = requests.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers,
            files=files,
            data=data,
            auth=self.auth,
            verify=self.verify_ssl,
            timeout=self.timeout_s,
        )
        if not resp.ok:
            raise RuntimeError(
                f"WordPress API error {resp.status_code} for {method} {url}: {resp.text[:2000]}"
            )
        if resp.status_code == 204:
            return None
        return resp.json()

    def upload_media(self, file_path: str, *, alt_text: str = "", title: str = "") -> Dict[str, Any]:
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"Media file not found: {file_path}")

        filename = os.path.basename(file_path)
        mime, _ = mimetypes.guess_type(filename)
        if not mime:
            mime = "application/octet-stream"

        with open(file_path, "rb") as f:
            content = f.read()

        # WordPress supports both multipart and raw upload.
        # Multipart is more compatible with various servers.
        files = {"file": (filename, content, mime)}
        created = self._request("POST", "/media", files=files)

        media_id = created.get("id")
        if media_id and (alt_text or title):
            update_body = {}
            if alt_text:
                update_body["alt_text"] = alt_text
            if title:
                update_body["title"] = title
            # WP allows POST to /media/{id} to update fields
            try:
                self._request("POST", f"/media/{media_id}", json_body=update_body)
            except Exception:
                # alt_text update may be blocked by permissions/plugins; ignore, media is still uploaded.
                pass

        return created

    def get_current_user(self) -> Dict[str, Any]:
        return self._request("GET", "/users/me")

    def update_post_meta(self, post_id: int, meta: Dict[str, Any]) -> Dict[str, Any]:
        # Requires meta keys to be registered with show_in_rest=true
        body: Dict[str, Any] = {"meta": meta}
        return self._request("POST", f"/posts/{int(post_id)}", json_body=body)

    def get_categories(self, *, slug: Optional[str] = None, per_page: int = 100) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"per_page": int(per_page)}
        if slug:
            params["slug"] = str(slug)
        data = self._request("GET", "/categories", params=params)
        if isinstance(data, list):
            return data
        return []

    def resolve_category_id_by_slug(self, slug: str) -> Optional[int]:
        slug = str(slug or "").strip()
        if not slug:
            return None
        try:
            items = self.get_categories(slug=slug, per_page=100)
            if not items:
                return None
            cid = items[0].get("id")
            return int(cid) if cid else None
        except Exception:
            return None

    def create_post(
        self,
        *,
        title: str,
        content: str,
        excerpt: str = "",
        status: str = "draft",
        slug: Optional[str] = None,
        featured_media: Optional[int] = None,
        category_ids: Optional[list[int]] = None,
        tag_ids: Optional[list[int]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "title": title,
            "content": content,
            "excerpt": excerpt,
            "status": status,
        }
        if slug:
            body["slug"] = str(slug)
        if featured_media:
            body["featured_media"] = int(featured_media)
        if category_ids:
            body["categories"] = [int(x) for x in category_ids]
        if tag_ids:
            body["tags"] = [int(x) for x in tag_ids]

        return self._request("POST", "/posts", json_body=body)


def resolve_path(base_dir: str, path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(base_dir, path))


def normalize_wp_slug(value: Any) -> str:
    """Normalize a user/model-provided slug to something WordPress accepts.

    WordPress slugs are generally lowercase with hyphens.
    We keep it conservative: [a-z0-9-], collapse repeats, trim.
    """
    s = str(value or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9-]+", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    # Practical safety limit (WP supports longer, but keep URLs sane)
    return s[:200]


def apply_media_placeholders(content_template: str, media_map: Dict[str, Dict[str, Any]]) -> str:
    def repl(match: re.Match) -> str:
        key = match.group(1)
        field = match.group(2)
        if key not in media_map:
            raise KeyError(f"Content references media key '{key}', but it's not present in post.media")
        if field == "url":
            url = media_map[key].get("source_url") or media_map[key].get("guid", {}).get("rendered")
            if not url:
                raise KeyError(f"Media '{key}' has no source_url")
            return str(url)
        if field == "id":
            mid = media_map[key].get("id")
            if not mid:
                raise KeyError(f"Media '{key}' has no id")
            return str(mid)
        if field == "alt":
            import html as _html

            a = media_map[key].get("alt_text") or media_map[key].get("alt") or ""
            return _html.escape(str(a), quote=True)
        raise ValueError("Unexpected placeholder field")

    return PLACEHOLDER_RE.sub(repl, content_template)


def _list_pin_root_dirs(base_dir: str) -> List[str]:
    try:
        cand = []
        if base_dir and os.path.isdir(base_dir):
            for name in os.listdir(base_dir):
                p = os.path.join(base_dir, name)
                if os.path.isdir(p) and re.match(r"^\d{4}-\d{2}-\d{2}", name):
                    cand.append(name)
        return sorted(cand)
    except Exception:
        return []


def _discover_pin_groups(pin_dir: str) -> Dict[str, List[str]]:
    """Group pin files by common base name.

    Example filename:
      8_Cozy_Sunroom_Ideas_post4_p1_w3_01_filled.png
    We want group key ~ everything up to _w<digit>... (and without extension).
    """
    groups: Dict[str, List[str]] = {}
    if not pin_dir or not os.path.isdir(pin_dir):
        return groups

    exts = {".png", ".jpg", ".jpeg", ".webp"}
    for fn in os.listdir(pin_dir):
        p = os.path.join(pin_dir, fn)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(fn)[1].lower()
        if ext not in exts:
            continue

        base = os.path.splitext(fn)[0]
        # remove trailing variant like _w1_01_filled
        m = re.match(r"^(.*)_w\d+_.*$", base)
        key = m.group(1) if m else base
        groups.setdefault(key, []).append(p)

    # stable ordering inside group
    for k in list(groups.keys()):
        groups[k] = sorted(groups[k])

    return dict(sorted(groups.items(), key=lambda kv: kv[0].lower()))


def _parse_post_num_from_pin_group(group_key: str) -> Optional[int]:
    # NOTE: do NOT use \b here because filenames use underscores, and '_' is a word char in regex.
    # We want to match patterns like: ..._post6_p1..., ..._post10..., etc.
    m = re.search(r"_post(\d+)(?:_|$)", group_key)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _normalize_category_slug(value: Any) -> str:
    return str(value or "").strip()


def upload_one_post(
    wp: WordPressClient,
    post: Dict[str, Any],
    *,
    base_dir: str,
    pin_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    title = post["title"]
    excerpt = post.get("excerpt", "")
    status = post.get("status", "draft")
    slug = normalize_wp_slug(post.get("slug") or post.get("url_slug"))

    # Category: prefer explicit IDs, else resolve by slug
    category_ids = post.get("category_ids")
    if not category_ids:
        cat_slug = _normalize_category_slug(post.get("category_slug") or post.get("category"))
        if cat_slug:
            cid = wp.resolve_category_id_by_slug(cat_slug)
            if cid:
                category_ids = [cid]

    media_spec: Dict[str, Any] = post.get("media", {})
    featured_key = post.get("featured_key")

    uploaded_media: Dict[str, Dict[str, Any]] = {}

    # Upload all media first, so we can substitute into content.
    for key, spec in media_spec.items():
        path = resolve_path(base_dir, spec["path"])
        alt = spec.get("alt", "")
        title_media = spec.get("title", "")
        created = wp.upload_media(path, alt_text=alt, title=title_media)
        # Preserve original alt locally (the POST /media response may not include updated alt_text).
        try:
            created["alt"] = alt
        except Exception:
            pass
        uploaded_media[key] = created

    content_template = post.get("content_template", "")
    if not content_template:
        raise ValueError("post.content_template is required")

    content = apply_media_placeholders(content_template, uploaded_media)

    featured_media_id: Optional[int] = None
    if featured_key:
        featured = uploaded_media.get(featured_key)
        if not featured:
            raise KeyError(f"featured_key='{featured_key}' not found in post.media")
        featured_media_id = featured.get("id")

    created_post = wp.create_post(
        title=title,
        content=content,
        excerpt=excerpt,
        status=status,
        slug=slug,
        featured_media=featured_media_id,
        category_ids=category_ids,
        tag_ids=post.get("tag_ids"),
    )

    # Optional: upload and attach Pinterest pins via pinterest-pin-tracker meta.
    pins_uploaded: List[Dict[str, Any]] = []
    if pin_paths:
        for pp in pin_paths:
            try:
                created_pin = wp.upload_media(pp, alt_text="", title=os.path.basename(pp))
                pins_uploaded.append(created_pin)
            except Exception as e:
                # Continue; we still want the post created.
                pins_uploaded.append({"error": str(e), "path": pp})

        # Attach pin IDs to post meta in the format expected by the plugin: [{id, last_used_ymd:""}, ...]
        pin_items = []
        for it in pins_uploaded:
            mid = (it or {}).get("id")
            if mid:
                pin_items.append({"id": int(mid), "last_used_ymd": ""})

        if pin_items:
            try:
                wp.update_post_meta(int(created_post.get("id")), {"_ppt_pins": pin_items})
            except Exception as e:
                # Don't fail the whole upload if meta update fails
                pins_uploaded.append({"error": f"meta_update_failed: {e}"})

    return {
        "post": created_post,
        "uploaded_media": uploaded_media,
        "pins_uploaded": pins_uploaded,
    }


def main():
    st.set_page_config(page_title="WP Bulk Upload (Drafts)", layout="wide")
    st.title("WordPress bulk uploader → creates Draft posts")

    with st.expander("API настройки (REST)", expanded=True):
        # --- Site profiles ---
        # Edit passwords here once, and then just switch the profile in UI.
        WP_SITE_PROFILES: Dict[str, Dict[str, str]] = {
            "nestingmuse.com": {
                "base_url": "https://nestingmuse.com",
                "username": "mikeSD",
                # Existing hardcoded default (you already use it):
                "app_password": "iWVS f00D TLip bOVo pUcS pEPK",
            },
            "spaceofmuse.com": {
                "base_url": "https://spaceofmuse.com",
                "username": "Michaela_Shaffer",
                # TODO: Paste the Application Password for Michaela_Shaffer here (same 'name/label' as on nestingmuse).
                # Example format: "abcd efgh ijkl mnop qrst uvwx"
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

        profile_names = list(WP_SITE_PROFILES.keys())
        default_profile = os.getenv("WP_SITE_PROFILE", "nestingmuse.com")
        if default_profile not in WP_SITE_PROFILES:
            default_profile = "nestingmuse.com"

        selected_profile = st.selectbox(
            "Site profile",
            options=profile_names,
            index=profile_names.index(default_profile),
            help="Select a site to auto-fill base URL, username and application password.",
        )

        # Defaults from profile, but allow env/secrets to override if set.
        prof = WP_SITE_PROFILES.get(selected_profile, {})
        default_base_url = prof.get("base_url") or "https://nestingmuse.com"
        default_username = prof.get("username") or "mikeSD"
        default_app_password = prof.get("app_password") or ""

        col1, col2 = st.columns(2)
        with col1:
            base_url = st.text_input(
                "Site base URL",
                value=os.getenv("WP_BASE_URL", _get_secret("WP_BASE_URL", default_base_url)),
                help="Пример: https://your-site.com (без /wp-admin). Можно без https:// — добавим автоматически.",
            )
            username = st.text_input(
                "Username (WP login)",
                value=os.getenv("WP_USERNAME", _get_secret("WP_USERNAME", default_username)),
                help="Это именно логин пользователя WordPress, а не имя/label application password.",
            )
        with col2:
            app_password = st.text_input(
                "Application Password (for that user)",
                # If you want profile switching to work, keep this hardcoded mapping above.
                value=os.getenv("WP_APP_PASSWORD", _get_secret("WP_APP_PASSWORD", default_app_password)),
                type="password",
                help=(
                    "Application Password из профиля пользователя (Users → Profile → Application Passwords).\n"
                    "При выборе профиля сайта поля подставляются автоматически."
                ),
            )
            verify_ssl = st.checkbox("Verify SSL", value=(os.getenv("WP_VERIFY_SSL", "1") != "0"))

    st.caption(
        "JSON формат: объект с полем posts: [ ... ]. Внутри content_template используй плейсхолдеры "
        "{{media:<key>:url}} и {{media:<key>:id}}."
    )

    json_file = st.file_uploader("Выбери JSON файл с постами", type=["json"])
    if not json_file:
        st.info("Загрузи JSON (пример в workspace: tmp_rovodev_wp_sample_posts.json)")
        return

    try:
        payload = json.loads(json_file.read().decode("utf-8"))
    except Exception as e:
        st.error(f"Не удалось прочитать JSON: {e}")
        return

    posts = payload.get("posts")
    if not isinstance(posts, list) or not posts:
        st.error("JSON должен содержать posts: [ ... ]")
        return

    base_dir = payload.get("base_dir") or os.getcwd()
    st.write(f"Base dir for relative media paths: `{base_dir}`")

    # Pinterest pins (optional): attach creatives to each created post for the pinterest-pin-tracker plugin.
    with st.expander("Pinterest pins (optional)", expanded=False):
        attach_pins = st.checkbox(
            "Attach Pinterest pin creatives to each post (pinterest-pin-tracker)",
            value=False,
            help=(
                "If enabled, the uploader will upload pin images from a selected folder to the WP Media Library "
                "and then attach them to the created post via meta _ppt_pins (so they appear like 'Добавить пин (из медиатеки)')."
            ),
        )

        script_dir = os.path.dirname(os.path.abspath(__file__))
        nested_project_dir = os.path.join(script_dir, "generate automation")
        pin_root_default = nested_project_dir if os.path.isdir(nested_project_dir) else script_dir
        pin_root = st.text_input(
            "Pins root folder (contains run dirs like 2026-03-16_1)",
            value=os.getenv("PINS_ROOT_DIR", pin_root_default),
        )

        run_dirs = _list_pin_root_dirs(pin_root)
        default_run = run_dirs[-1] if run_dirs else ""
        pin_run_dir_name = st.selectbox(
            "Select pins run folder",
            options=run_dirs if run_dirs else [""],
            index=(len(run_dirs) - 1) if run_dirs else 0,
        )
        pin_dir = os.path.join(pin_root, pin_run_dir_name) if pin_run_dir_name else ""

        pin_groups = _discover_pin_groups(pin_dir) if (attach_pins and pin_dir) else {}
        st.caption(f"Pin groups found: {len(pin_groups)}")
        if attach_pins and pin_groups:
            # show a small preview
            preview_keys = list(pin_groups.keys())[:15]
            st.write("Preview groups (first 15):")
            st.json({k: [os.path.basename(x) for x in pin_groups[k][:3]] for k in preview_keys})

        pins_map_by_post_idx: Dict[int, List[str]] = {}
        if attach_pins and pin_groups:
            # 1) Collect pins by the `postN` number found in pin group key.
            pins_by_pin_postnum: Dict[int, List[str]] = {}
            for gk, paths in pin_groups.items():
                n = _parse_post_num_from_pin_group(gk)
                if not n:
                    continue
                pins_by_pin_postnum.setdefault(int(n), []).extend(paths)

            # 2) Map JSON post order (1..len(posts)) to pin post numbers (postN) by a starting offset.
            auto_start = min(pins_by_pin_postnum.keys()) if pins_by_pin_postnum else 1
            start_pin_postnum = st.number_input(
                "Pins starting post number (postN)",
                min_value=1,
                value=int(auto_start),
                step=1,
                help=(
                    "If your pin files are named like *_post6_* .. *_post10_* but your JSON posts are 1..5, set this to 6. "
                    "Then Post 1 → post6, Post 2 → post7, etc."
                ),
            )

            for json_i in range(1, len(posts) + 1):
                pin_postnum = int(start_pin_postnum) + (json_i - 1)
                paths = pins_by_pin_postnum.get(pin_postnum) or []
                if paths:
                    pins_map_by_post_idx[json_i] = sorted(set(paths))

            st.write("Auto-mapped pins (JSON order → pin postN):")
            if pins_map_by_post_idx:
                preview = []
                for json_i in range(1, min(len(posts), 20) + 1):
                    pin_postnum = int(start_pin_postnum) + (json_i - 1)
                    title_i = str((posts[json_i - 1] or {}).get("title") or "")
                    preview.append(
                        {
                            "post_idx": json_i,
                            "pin_postN": pin_postnum,
                            "pins": len(pins_map_by_post_idx.get(json_i) or []),
                            "title": title_i,
                        }
                    )
                st.json(preview)
            else:
                st.warning(
                    "Auto-mapping resulted in 0 pins attached. Check that your pin filenames contain `_postN` "
                    "and that 'Pins starting post number' matches your folder (e.g. start=6 for post6..post10)."
                )

            # Optional: allow manual override per JSON post index by selecting group keys (not every file)
            st.write("Optional manual override (select group keys, not every file):")
            manual_map: Dict[int, List[str]] = {}
            for json_i in range(1, min(len(posts), 20) + 1):
                pin_postnum = int(start_pin_postnum) + (json_i - 1)
                post_title = str((posts[json_i - 1] or {}).get("title") or "")
                label = f"Post {json_i} → post{pin_postnum} — {post_title}" if post_title else f"Post {json_i} → post{pin_postnum}"
                default_group_keys = [gk for gk in pin_groups.keys() if _parse_post_num_from_pin_group(gk) == pin_postnum]
                selected = st.multiselect(
                    f"{label}: pin group keys",
                    options=list(pin_groups.keys()),
                    default=default_group_keys,
                )
                if selected:
                    pp: List[str] = []
                    for gk in selected:
                        pp.extend(pin_groups.get(gk, []))
                    manual_map[json_i] = sorted(set(pp))

            if manual_map:
                pins_map_by_post_idx = manual_map

    colA, colB = st.columns([1, 1])

    if not base_url.startswith("http://") and not base_url.startswith("https://"):
        normalized_base_url = "https://" + base_url.lstrip("/")
    else:
        normalized_base_url = base_url

    with colA:
        if st.button("Test connection (users/me)"):
            if not (normalized_base_url and username and app_password):
                st.error("Заполни base_url, username, app_password")
            else:
                try:
                    wp_test = WordPressClient(
                        base_url=normalized_base_url,
                        username=username,
                        app_password=app_password,
                        verify_ssl=verify_ssl,
                    )
                    me = wp_test.get_current_user()
                    st.success(f"OK. Authenticated as: id={me.get('id')} name={me.get('name')} slug={me.get('slug')}")
                except Exception as e:
                    st.error(str(e))

    with colB:
        if st.button("Залить в WordPress (создать Draft)", type="primary"):
            if not (normalized_base_url and username and app_password):
                st.error("Заполни base_url, username, app_password")
                return

            wp = WordPressClient(
                base_url=normalized_base_url,
                username=username,
                app_password=app_password,
                verify_ssl=verify_ssl,
            )

        results = []
        for idx, post in enumerate(posts, start=1):
            with st.status(f"Uploading post {idx}/{len(posts)}: {post.get('title','(no title)')}", expanded=False) as status:
                try:
                    pin_paths = None
                    try:
                        if attach_pins:
                            pin_paths = pins_map_by_post_idx.get(int(idx))
                    except Exception:
                        pin_paths = None

                    out = upload_one_post(wp, post, base_dir=base_dir, pin_paths=pin_paths)
                    created_post = out["post"]
                    results.append(
                        {
                            "title": post.get("title"),
                            "id": created_post.get("id"),
                            "status": created_post.get("status"),
                            "link": created_post.get("link"),
                        }
                    )
                    status.update(label=f"OK: {post.get('title')}", state="complete")
                except Exception as e:
                    results.append(
                        {
                            "title": post.get("title"),
                            "id": None,
                            "status": "error",
                            "link": None,
                            "error": str(e),
                        }
                    )
                    status.update(label=f"ERROR: {post.get('title')}", state="error")

        st.subheader("Результаты")
        st.json(results)


if __name__ == "__main__":
    main()
