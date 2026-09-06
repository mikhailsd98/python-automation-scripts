# -*- coding: utf-8 -*-
"""Unified Streamlit launcher for 4-stage bulk generation.

Goal: provide ONE UI to access the existing working functionality without inventing new logic.

IMPORTANT NOTE
- Tabs 1-2 are rendered by calling the existing functions from `app_bulk_fast_images_streamlit.py`.
- Tabs 3-4 are embedded by executing the existing scripts in-place (so you keep the same UI/logic).
  To avoid Streamlit config conflicts, we temporarily monkeypatch `st.set_page_config` while embedding.

This keeps behavior максимально близким к исходным скриптам.

Run:
  streamlit run app_unified_bulk_pipeline_streamlit.py
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import streamlit as st

import app_bulk_fast_images_streamlit as bulk
import stage3_csvbatch_core as stage3
import stage4_lora_core as stage4
import stage4_gemini_core as stage4_gemini


@contextmanager
def _gemini_vpn_requirement_temporarily(required: bool):
    """Temporarily set Gemini API helpers to require or not require VPN."""

    prev = os.environ.get("GEMINI_REQUIRE_VPN")
    os.environ["GEMINI_REQUIRE_VPN"] = "1" if required else "0"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("GEMINI_REQUIRE_VPN", None)
        else:
            os.environ["GEMINI_REQUIRE_VPN"] = prev


_PIPELINE_VPN_LOCK = threading.Lock()
_PIPELINE_VPN_LAST_OK_TS = 0.0


def _pipeline_vpn_enabled() -> bool:
    """Whether the unified pipeline should keep VPN connected for generation."""

    return bool(st.session_state.get("unified_require_vpn", True))


def _configure_pipeline_vpn_env() -> None:
    """Apply VPN-related env vars used by existing Gemini helpers."""

    if _pipeline_vpn_enabled():
        os.environ["GEMINI_REQUIRE_VPN"] = "1"
    else:
        os.environ["GEMINI_REQUIRE_VPN"] = "0"

    os.environ["GEMINI_VPN_RETRIES"] = "1" if bool(st.session_state.get("unified_vpn_one_try", False)) else "2"
    os.environ.setdefault("GEMINI_VPN_BACKOFF", "1.5")
    hosts_file = str(st.session_state.get("unified_vpn_hosts_file") or "").strip()
    if hosts_file:
        os.environ["GEMINI_VPN_HOSTS_FILE"] = hosts_file


def _ensure_pipeline_vpn(stage_label: str, *, min_interval_s: float = 20.0) -> None:
    """Ensure SSTP VPN is connected before a pipeline generation step.

    Uses the existing, battle-tested logic from `generate_pinterest_texts.py`:
    - rotates through `good_hosts_for_images.txt` / env override;
    - validates Gemini API reachability;
    - refuses to continue without VPN when enabled.
    """

    if not _pipeline_vpn_enabled():
        return

    _configure_pipeline_vpn_env()

    global _PIPELINE_VPN_LAST_OK_TS
    now = time.time()
    with _PIPELINE_VPN_LOCK:
        # Avoid spamming rasdial/HTTP checks when several UI wrappers call us in
        # the same Streamlit run. Each stage still calls this before it starts.
        if _PIPELINE_VPN_LAST_OK_TS and (now - _PIPELINE_VPN_LAST_OK_TS) < float(min_interval_s):
            return

        try:
            with st.spinner(f"VPN: checking/connecting before {stage_label}..."):
                import generate_pinterest_texts as gptx

                gptx._ensure_vpn_for_gemini()
            _PIPELINE_VPN_LAST_OK_TS = time.time()
        except Exception as e:
            raise RuntimeError(f"VPN is required before {stage_label}, but connection failed: {e}") from e


def _install_pipeline_vpn_guards_once() -> None:
    """Guard manual Stage1/Stage2/Stage3 starts rendered inside this unified app."""

    if bool(getattr(bulk, "_unified_pipeline_vpn_guards_installed", False)):
        return

    orig_fast = bulk.run_bulk_fast_generation
    orig_tongyi = bulk.run_bulk_tongyi_generation
    orig_run_one_task = bulk._run_one_task
    orig_csvbatch_start = stage3.src._csvbatch_start

    def _looks_network_or_vpn_error(err: str | None) -> bool:
        low = str(err or "").lower()
        return any(
            marker in low
            for marker in (
                "err_internet_disconnected",
                "err_network_changed",
                "dns_probe_finished_no_internet",
                "no internet",
                "offline",
                "timed out",
                "timeout",
                "connection reset",
                "connection aborted",
                "connection closed",
                "target closed",
                "browser closed",
                "page closed",
                "vpn",
                "gemini request is blocked",
            )
        )

    def _task_error_result(task, err: Exception) -> dict[str, Any]:
        return {
            "ok": False,
            "article_idx": getattr(task, "article_idx", None),
            "article_title": getattr(task, "article_title", ""),
            "prompt_idx": getattr(task, "prompt_idx", None),
            "out_dir": getattr(task, "out_dir", ""),
            "saved": [],
            "error": str(err),
        }

    def _guarded_run_one_task(*args, **kwargs):
        task = args[0] if args else kwargs.get("task")
        try:
            _ensure_pipeline_vpn("Gemini browser task")
        except Exception as e:
            return _task_error_result(task, e)

        res = orig_run_one_task(*args, **kwargs)
        try:
            ok = bool((res or {}).get("ok"))
            err = str((res or {}).get("error") or "")
        except Exception:
            ok, err = False, ""

        if ok or not _looks_network_or_vpn_error(err):
            return res

        try:
            _ensure_pipeline_vpn("Gemini browser task retry", min_interval_s=0.0)
        except Exception as e:
            if isinstance(res, dict):
                res["error"] = f"{err}; VPN retry check failed: {e}"
            return res

        res2 = orig_run_one_task(*args, **kwargs)
        try:
            if isinstance(res2, dict) and res2.get("ok"):
                return res2
        except Exception:
            pass
        return res2 or res

    def _guarded_fast(*args, **kwargs):
        _ensure_pipeline_vpn("Stage1 FAST")
        return orig_fast(*args, **kwargs)

    def _guarded_tongyi(*args, **kwargs):
        _ensure_pipeline_vpn("Stage2 Tongyi")
        return orig_tongyi(*args, **kwargs)

    def _guarded_csvbatch_start(*args, **kwargs):
        _ensure_pipeline_vpn("Stage3 BATCH")
        return orig_csvbatch_start(*args, **kwargs)

    bulk.run_bulk_fast_generation = _guarded_fast  # type: ignore[assignment]
    bulk.run_bulk_tongyi_generation = _guarded_tongyi  # type: ignore[assignment]
    bulk._run_one_task = _guarded_run_one_task  # type: ignore[assignment]
    stage3.src._csvbatch_start = _guarded_csvbatch_start  # type: ignore[assignment]
    bulk._unified_pipeline_vpn_guards_installed = True  # type: ignore[attr-defined]


def _sync_stage4_queue_state() -> None:
    """Ensure Stage4 is queued when it makes sense.

    Problem this solves:
    - If Stage3 is started manually in Tab3 (not via RUN FULL PIPELINE),
      `unified_stage4_pending` was never set, so Tab4 autorun would never trigger.
    - Even with RUN FULL PIPELINE, if the Stage4 uploader hadn't been rendered yet
      in the current run, the `stage4_uploaded_files` value could be missing at the
      moment we decide whether to queue Stage4.

    Strategy:
    - If Stage4 is enabled AND there are uploaded files under the shared key
      `stage4_uploaded_files` AND we have a Stage3 run id, then queue Stage4.
    - Autorun is still triggered in Tab4 only once per Stage3 run id.
    """

    if not bool(st.session_state.get("unified_enable_stage4")):
        return

    files4 = st.session_state.get("stage4_uploaded_files")
    if not files4:
        return

    rid = _get_stage3_run_id()
    if not rid:
        return

    # Do not keep re-queuing forever if we already STARTED Stage4 for this Stage3 run.
    # (We track actual start acknowledgement from Stage4 UI.)
    if str(st.session_state.get("unified_stage4_started_for_stage3_run_id") or "") == str(rid):
        return

    # Queue Stage4 so that Tab4 can trigger stage4_autorun once Stage3 is finished.
    st.session_state["unified_stage4_pending"] = True


def _st_autorefresh(*, interval_ms: int, key: str) -> bool:
    """Best-effort autorefresh helper.

    As requested: NO JS fallback.

    Returns True if an autorefresh mechanism was successfully installed.
    """

    # 1) Native Streamlit (preferred)
    try:
        if hasattr(st, "autorefresh"):
            st.autorefresh(interval=int(interval_ms), key=str(key))
            return True
    except Exception:
        pass

    # 2) Optional external package
    try:
        from streamlit_autorefresh import st_autorefresh  # type: ignore

        st_autorefresh(interval=int(interval_ms), key=str(key))
        return True
    except Exception:
        return False


def _ui_rerun() -> None:
    """Compatibility wrapper for Streamlit rerun API."""

    try:
        if hasattr(st, "rerun"):
            st.rerun()
            return
    except Exception:
        pass

    try:
        if hasattr(st, "experimental_rerun"):
            st.experimental_rerun()
            return
    except Exception:
        pass


def _maybe_request_stage4_autorun() -> None:
    """Request Stage4 autorun as soon as Stage3 is finished.

    This is intentionally OUTSIDE the tab4 rendering block.

    Why:
    - Tab blocks may not execute reliably depending on Streamlit version / active tab.
    - We want a deterministic orchestrator: once Stage3 is done and Stage4 has uploads,
      set `stage4_autorun=True` and force one rerun.

    Safety:
    - Only one forced rerun per Stage3 run_id (tracked by unified_stage4_autorun_requested_for_stage3_run_id).
    - Actual "Stage4 started" is still acknowledged by Stage4 UI via
      unified_stage4_started_for_stage3_run_id.
    """

    if not bool(st.session_state.get("unified_enable_stage4")):
        return

    if bool(st.session_state.get("stage4_running", False)):
        return

    rid = _get_stage3_run_id()
    if not rid:
        return

    # Wait until Stage3 fully finished
    try:
        if _is_stage3_running():
            return
    except Exception:
        return

    files4 = st.session_state.get("stage4_uploaded_files")
    queued = bool(st.session_state.get("unified_stage4_pending"))
    # `stage4_uploaded_files` may briefly disappear when the Stage4 uploader
    # wasn't rendered on some reruns. If Stage4 is already queued, still request
    # autorun so Gemini Stage4 can be rendered and recover uploader state.
    if not (files4 or queued):
        return

    started_for = str(st.session_state.get("unified_stage4_started_for_stage3_run_id") or "")
    if str(rid) == started_for:
        return

    requested_for = str(st.session_state.get("unified_stage4_autorun_requested_for_stage3_run_id") or "")
    if str(rid) == requested_for:
        # We already forced a rerun for this Stage3 run_id.
        return

    # Pass run id to Stage4 UI and request autorun.
    st.session_state["unified_current_stage3_run_id"] = str(rid)
    st.session_state["stage4_autorun"] = True
    st.session_state["unified_stage4_pending"] = False
    st.session_state["unified_stage4_autorun_requested_for_stage3_run_id"] = str(rid)

    _ui_rerun()


def _autorefresh_if_needed() -> None:
    """Trigger periodic reruns while Stage3 is running, so Stage4 autorun can fire.

    Why:
    - Stage3 runs in a background thread.
    - Streamlit will not automatically rerun when that thread finishes.
    - We therefore refresh periodically *only while Stage3 is running* and only if
      Stage4 is actually relevant (enabled + uploads, or already queued).

    Safety:
    - Once Stage3 is finished, we stop autorefresh immediately to avoid interrupting
      Stage4 generation (spinners/progress) inside embedded apps.
    """

    # Determine whether Stage4 is relevant for this run.
    stage4_enabled = bool(st.session_state.get("unified_enable_stage4"))
    has_uploads = bool(st.session_state.get("stage4_uploaded_files"))
    queued = bool(st.session_state.get("unified_stage4_pending"))

    # IMPORTANT: do not auto-rerun while Stage4 is actively generating.
    # Otherwise Streamlit autorefresh can interrupt long-running Playwright work.
    if bool(st.session_state.get("stage4_running", False)):
        return

    if not (queued or (stage4_enabled and has_uploads)):
        return

    # Only refresh while Stage3 is actually running.
    try:
        if not _is_stage3_running():
            return
    except Exception:
        return

    # Use our best-effort autorefresh.
    ok = _st_autorefresh(interval_ms=2000, key="unified_pipeline_autorefresh")

    # If autorefresh APIs are unavailable, fall back to a simple polling rerun.
    # This keeps the app responsive enough to:
    # - update Stage3 state (avoid infinite "running" UI)
    # - trigger Stage4 autorun immediately after Stage3 completes
    if not ok:
        try:
            time.sleep(2.0)
            _ui_rerun()
        except Exception:
            pass

    if bool(st.session_state.get("unified_debug", False)):
        if ok:
            st.caption("Autorefresh: enabled")
        else:
            st.warning(
                "Autorefresh is NOT available (no st.autorefresh and no streamlit-autorefresh). "
                "Using polling fallback (sleep+rerun) while Stage3 runs."
            )


def _get_stage3_run_id() -> str | None:
    """Return the Stage3 run_id we should track in the unified pipeline.

    Notes:
    - Tab3 UI uses `csvbatch_run_id`/`csvbatch_last_run_id`.
    - In some flows Tab3 can overwrite/clear those keys during reruns.

    To make Stage4 autorun robust, we *persist* the last seen Stage3 run_id
    into `unified_stage3_run_id`.
    """

    # Prefer the currently running id from Tab3 (if present)
    rid_active = st.session_state.get("csvbatch_run_id")
    if rid_active:
        st.session_state["unified_stage3_run_id"] = str(rid_active)
        return str(rid_active)

    # Otherwise prefer our persisted id
    rid_persisted = st.session_state.get("unified_stage3_run_id")
    if rid_persisted:
        return str(rid_persisted)

    # Fallback: last run id (and persist it so it doesn't get lost later)
    rid_last = st.session_state.get("csvbatch_last_run_id")
    if rid_last:
        st.session_state["unified_stage3_run_id"] = str(rid_last)
        return str(rid_last)

    return None


def _is_stage3_running() -> bool:
    """Best-effort check whether Stage3 csvbatch is currently running.

    Important: don't rely on `st.session_state["csvbatch_running"]` alone.
    It is updated by the Stage3 UI (Tab3) on reruns, but Tab4 can be opened
    while Stage3 is running and then remain "stuck" without reruns.

    We therefore always try to read the shared in-memory state from Stage3 and
    also sync `st.session_state["csvbatch_running"]` to avoid stale True.
    """

    rid = _get_stage3_run_id()
    if not rid:
        # If there's no run id, consider it not running.
        st.session_state["csvbatch_running"] = False
        return False

    try:
        state = stage3.get_state(run_id=str(rid))
        if isinstance(state, dict) and state:
            # Defensive: sometimes `running` may be stale; use finished/stopped/done_runs as stronger signals.
            finished = bool(state.get("finished"))
            stopped = bool(state.get("stopped"))
            try:
                done_runs = int(state.get("done_runs") or 0)
                total_runs = int(state.get("total_runs") or 0)
            except Exception:
                done_runs, total_runs = 0, 0

            if finished or stopped or (total_runs > 0 and done_runs >= total_runs):
                st.session_state["csvbatch_running"] = False
                return False

            running = bool(state.get("running"))
            st.session_state["csvbatch_running"] = running
            if running:
                # Remember that this run_id was observed running (helps guard against brief state glitches)
                ts_now = float(time.time())
                st.session_state["unified_stage3_last_seen_running_ts"] = ts_now
                m1 = st.session_state.get("unified_stage3_last_seen_running_ts_by_run_id")
                if not isinstance(m1, dict):
                    m1 = {}
                m1[str(rid)] = ts_now
                st.session_state["unified_stage3_last_seen_running_ts_by_run_id"] = m1
            return running

        # No state found for this run_id.
        # Be conservative: if we recently observed this run as running, treat it as running
        # for a short grace period to avoid starting Stage4 too early.
        # Try per-run_id last-seen first, then fallback to global.
        last_seen = 0.0
        try:
            m1 = st.session_state.get("unified_stage3_last_seen_running_ts_by_run_id")
            if isinstance(m1, dict):
                last_seen = float(m1.get(str(rid)) or 0.0)
        except Exception:
            last_seen = 0.0
        if not last_seen:
            try:
                last_seen = float(st.session_state.get("unified_stage3_last_seen_running_ts") or 0.0)
            except Exception:
                last_seen = 0.0

        if last_seen and (time.time() - last_seen) < 45.0:
            st.session_state["csvbatch_running"] = True
            if bool(st.session_state.get("unified_debug", False)):
                st.caption(f"Stage3 running (grace): missing state for rid={rid} but last_seen={int(time.time()-last_seen)}s ago")
            return True

        st.session_state["csvbatch_running"] = False
        return False
    except Exception:
        # Be conservative on transient errors: if Stage3 was observed running recently
        # (or was just started), assume it's still running for a short grace period.
        # Prefer per-run_id timestamps, then fallback to global ones.
        last_seen = 0.0
        started_ts = 0.0
        try:
            m1 = st.session_state.get("unified_stage3_last_seen_running_ts_by_run_id")
            if isinstance(m1, dict):
                last_seen = float(m1.get(str(rid)) or 0.0)
        except Exception:
            last_seen = 0.0
        if not last_seen:
            try:
                last_seen = float(st.session_state.get("unified_stage3_last_seen_running_ts") or 0.0)
            except Exception:
                last_seen = 0.0

        try:
            m0 = st.session_state.get("unified_stage3_started_ts_by_run_id")
            if isinstance(m0, dict):
                started_ts = float(m0.get(str(rid)) or 0.0)
        except Exception:
            started_ts = 0.0
        if not started_ts:
            try:
                started_ts = float(st.session_state.get("unified_stage3_started_ts") or 0.0)
            except Exception:
                started_ts = 0.0

        now = float(time.time())
        if (last_seen and (now - last_seen) < 45.0) or (started_ts and (now - started_ts) < 45.0):
            st.session_state["csvbatch_running"] = True
            return True

        # Fall back to last-known value (may be stale, but better than crashing)
        return bool(st.session_state.get("csvbatch_running", False))


def _ensure_vpn_for_pipeline_stage(stage_label: str) -> None:
    """Compatibility helper: keep VPN connected before Gemini/Tongyi stages."""

    _ensure_pipeline_vpn(stage_label, min_interval_s=0.0)


def _reset_fast_state_for_new_payload() -> None:
    """Clear Fast-tab state that otherwise survives payload switches.

    Needed because Streamlit keeps widget values by key (e.g. bulk_prompt_*), which can cause
    prompts/images to appear from a previous payload even after selecting a new JSON.
    """

    # Clear prompt editor widget values
    for k in list(st.session_state.keys()):
        if str(k).startswith("bulk_prompt_"):
            st.session_state.pop(k, None)

    # Clear loaded payload/articles cache and gallery scan state
    st.session_state.pop("bulk_payload", None)
    st.session_state.pop("bulk_articles", None)

    st.session_state["bulk_errors"] = []
    st.session_state["bulkfast_last_disk_scan_run_dir"] = ""
    st.session_state["bulkfast_force_disk_rescan"] = True
    st.session_state["bulk_saved_items"] = []
    st.session_state["bulk_saved"] = {}


def _wrap_prompt_for_fast_img2img(prompt: str) -> str:
    """Best-effort wrapper so txt2img prompts keep the required 10:16 ratio.

    New Gemini image models understand the aspect ratio from text, so the white
    10x16 base image is no longer required.
    """

    p = (prompt or "").strip()
    if not p:
        return ""

    low = p.lower()
    pre = str(getattr(bulk, "NBP_PROMPT_PRE", "") or "").strip()
    post = str(getattr(bulk, "NBP_PROMPT_POST", "") or "").strip()

    # If already wrapped, just ensure POST exists.
    if (
        (pre and low.startswith(pre.lower()))
        or ("change the white 10:16 ratio image" in low)
        or ("10:16 vertical aspect ratio" in low)
    ):
        if post and (post.lower() not in low):
            return f"{p} {post}".strip()
        return p

    try:
        p2 = bulk._sanitize_prompt(p).strip()
    except Exception:
        p2 = p

    if pre and post:
        return f"{pre}{p2}{post}".strip()
    if pre:
        return f"{pre}{p2}".strip()
    return p2


def _run_stage1_pro_via_fast(*, payload: dict[str, Any], run_base_dir: str, start_profile_num: int | None = None) -> dict[str, Any]:
    """Generate PRO images via FAST (Gemini img2img) and rename them to N_pro_* format."""

    # Same logic as Pro tab / Stage2 selection
    has_pro_articles = ("pro_articles" in payload) and (payload.get("pro_articles") is not None)
    articles = (payload.get("pro_articles") or []) if has_pro_articles else (payload.get("articles") or [])

    try:
        pro_prompt_count = int(payload.get("pro_prompt_count") or 4)
    except Exception:
        pro_prompt_count = 4

    pro_tasks = bulk._build_pro_tasks_from_articles(
        list(articles or []),
        str(run_base_dir),
        max_prompts_per_article=int(pro_prompt_count),
    )

    tasks_snapshot: list[dict[str, Any]] = []
    for t in pro_tasks:
        pkey = f"bulkpro_prompt_{t.article_idx}_{t.prompt_idx}"
        pval = (st.session_state.get(pkey) or str(t.prompt) or "").strip()
        pval = bulk._sanitize_prompt(pval or "").strip()
        if pval:
            tasks_snapshot.append({"task": t, "prompt": pval})

    if not tasks_snapshot:
        return {"ok": True, "total": 0, "saved": 0, "errors": 0, "run_base_dir": str(run_base_dir)}

    # FAST settings (reuse Stage1 keys)
    url = str(st.session_state.get("bulkfast_url") or bulk.DEFAULT_URLS[0])
    model_choice = str(st.session_state.get("bulkfast_model") or "Быстрая")
    headless = bool(st.session_state.get("bulkfast_headless"))
    executable_path = (str(st.session_state.get("bulkfast_exe_path") or "").strip() or None)
    user_data_dir = str(st.session_state.get("bulkfast_user_data_dir") or os.path.abspath(".chrome_automation_profile"))
    parallelism = int(st.session_state.get("bulkfast_parallelism") or 4)
    if start_profile_num is None:
        start_profile_num = int(st.session_state.get("bulkfast_start_profile_num") or 1)
    timeout_s = int(st.session_state.get("bulkfast_timeout") or 90)

    max_workers = max(1, min(12, int(parallelism), len(tasks_snapshot)))
    profile_pool, tmp_profiles = bulk._prepare_profile_pool(
        user_data_dir,
        max_workers=max_workers,
        start_profile_num=int(start_profile_num),
    )

    import concurrent.futures

    progress = st.progress(0)
    status = st.empty()

    total = len(tasks_snapshot)
    done = 0
    saved_ok = 0
    errors: list[dict[str, Any]] = []

    # Ensure target list exists for Pro tab gallery
    st.session_state.setdefault("bulkpro_saved_paths", [])

    def _run_one(item: dict[str, Any]) -> tuple[dict[str, Any], Any, str]:
        t = item["task"]
        prompt_clean = str(item.get("prompt") or "").strip()
        prompt_wrapped = _wrap_prompt_for_fast_img2img(prompt_clean)
        # Use a high prompt_idx to avoid collisions with normal Stage1 files (1_*,2_*...)
        prompt_idx = 1000 + int(getattr(t, "task_idx", 0) or 0)

        bt = bulk.BulkTask(
            article_idx=int(getattr(t, "article_idx", 0) or 0),
            article_title=str(getattr(t, "article_title", "") or ""),
            prompt_idx=int(prompt_idx),
            prompt=str(prompt_wrapped),
            out_dir=str(getattr(t, "out_dir", run_base_dir) or run_base_dir),
        )

        res = bulk._run_one_task(
            bt,
            url=url,
            headless=bool(headless),
            executable_path=executable_path,
            model_choice=model_choice,
            timeout_s=int(timeout_s),
            profile_pool=profile_pool,
        )
        return res, t, prompt_clean

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = [ex.submit(_run_one, it) for it in tasks_snapshot]
            for fut in concurrent.futures.as_completed(futs):
                res, t, prompt_clean = fut.result()
                done += 1

                if res.get("ok"):
                    saved_list = list(res.get("saved") or [])
                    for sp in saved_list:
                        try:
                            final_p = bulk._move_image_to_pro_named_file(
                                src_path=str(sp),
                                out_dir=str(getattr(t, "out_dir", run_base_dir) or run_base_dir),
                                task_idx=int(getattr(t, "task_idx", 0) or 0),
                                prompt_raw=str(prompt_clean or ""),
                            )
                        except Exception:
                            final_p = str(sp)

                        st.session_state.bulkpro_saved_paths.append(str(final_p))
                        saved_ok += 1
                else:
                    errors.append(
                        {
                            "ok": False,
                            "task_idx": int(getattr(t, "task_idx", 0) or 0),
                            "article_idx": int(getattr(t, "article_idx", 0) or 0),
                            "prompt_idx": int(getattr(t, "prompt_idx", 0) or 0),
                            "out_dir": str(getattr(t, "out_dir", run_base_dir) or run_base_dir),
                            "error": str(res.get("error") or "Unknown"),
                        }
                    )

                progress.progress(int(done / max(1, total) * 100))
                try:
                    status.write(f"PRO-via-FAST: {done}/{total} done")
                except Exception:
                    pass
    finally:
        # dedupe
        try:
            st.session_state["bulkpro_saved_paths"] = list(dict.fromkeys(st.session_state.get("bulkpro_saved_paths") or []))
        except Exception:
            pass

        # Cleanup temp profiles (same as Stage1)
        try:
            for p in tmp_profiles or []:
                try:
                    shutil.rmtree(str(p), ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            pass

    # Keep errors visible in Pro tab too
    st.session_state.setdefault("bulkpro_errors", [])
    if errors:
        try:
            st.session_state["bulkpro_errors"].extend(errors)
        except Exception:
            pass

    return {"ok": True, "total": total, "saved": saved_ok, "errors": len(errors), "run_base_dir": str(run_base_dir)}


def _run_stage1_missing_recovery(
    *,
    payload: dict[str, Any],
    run_base_dir: str,
    start_profile_num: int,
) -> dict[str, Any]:
    """Run the same Fast + PRO/NBP recovery as the manual Stage1 button.

    This deliberately reads the result folder rather than trusting the in-memory
    gallery.  A file can exist but still be an invalid ``.bin`` download; the
    disk scanners only recognise usable image extensions, so that task stays in
    the recovery list.
    """

    run_dir = str(run_base_dir)

    fast_tasks = bulk._build_tasks(list(payload.get("articles") or []), run_dir)
    fast_on_disk = bulk._scan_fast_saved_items_from_disk(run_dir)
    fast_saved_keys: set[tuple[int, int]] = set()
    for item in fast_on_disk:
        try:
            path = Path(str(item.get("path") or ""))
            if bulk._looks_like_image_file(path):
                fast_saved_keys.add((int(item.get("article_idx") or 0), int(item.get("prompt_idx") or 0)))
        except Exception:
            continue
    fast_missing = [
        task
        for task in fast_tasks
        if (int(task.article_idx), int(task.prompt_idx)) not in fast_saved_keys
    ]

    pro_tasks = bulk._build_pro_tasks_from_payload(payload, run_dir)
    pro_on_disk = bulk._scan_pro_saved_items_from_disk(run_dir)
    pro_missing = [
        task for task in pro_tasks if int(task.task_idx) not in set(int(key) for key in pro_on_disk)
    ]

    common = {
        "run_base_dir": run_dir,
        "url": str(st.session_state.get("bulkfast_url") or bulk.DEFAULT_URLS[0]),
        "headless": bool(st.session_state.get("bulkfast_headless")),
        "executable_path": (str(st.session_state.get("bulkfast_exe_path") or "").strip() or None),
        "user_data_dir": str(
            st.session_state.get("bulkfast_user_data_dir") or os.path.abspath(".chrome_automation_profile")
        ),
        "parallelism": int(st.session_state.get("bulkfast_parallelism") or 4),
        "start_profile_num": int(start_profile_num),
        "timeout_s": int(st.session_state.get("bulkfast_timeout") or 90),
    }

    result: dict[str, Any] = {
        "ok": True,
        "run_base_dir": run_dir,
        "fast_missing": len(fast_missing),
        "pro_missing": len(pro_missing),
        "fast": None,
        "pro": None,
    }

    if fast_missing:
        result["fast"] = bulk.run_bulk_fast_generation_subset(
            tasks=fast_missing,
            model_choice=str(st.session_state.get("bulkfast_model") or "Быстрая"),
            **common,
        )

    if pro_missing:
        result["pro"] = bulk.run_bulk_fast_pro_generation_subset(
            tasks=pro_missing,
            model_choice="Nano Banana Pro",
            **common,
        )

    result["errors"] = int((result["fast"] or {}).get("errors") or 0) + int(
        (result["pro"] or {}).get("errors") or 0
    )
    try:
        st.session_state["bulkfast_force_disk_rescan"] = True
    except Exception:
        pass
    return result


def _run_full_pipeline(payload_files: list[str]) -> None:
    """Run stages 1→2→3→(4) sequentially.

    Assumes the user already configured settings in tabs (we read session_state keys).
    """

    _configure_pipeline_vpn_env()
    _ensure_vpn_for_pipeline_stage("pipeline start")

    # ---- Stage1 + Stage2 per payload ----
    always_new = bool(st.session_state.get("unified_always_new_run_dir", True))

    # Stage1 profile numbering policy across multiple payload JSON
    try:
        base_parallelism = int(st.session_state.get("bulkfast_parallelism") or 4)
    except Exception:
        base_parallelism = 4

    try:
        base_start_profile = int(st.session_state.get("bulkfast_start_profile_num") or 1)
    except Exception:
        base_start_profile = 1

    autoinc_profiles = bool(st.session_state.get("unified_stage1_autoinc_profiles", False))

    for idx, pf in enumerate(payload_files):
        # Keep VPN connected across payloads. Older versions disconnected here;
        # generation now requires VPN, so we only re-check/reconnect.
        if idx > 0:
            _ensure_vpn_for_pipeline_stage(f"next payload {idx + 1}")

        st.markdown(f"### Payload: `{pf}`")
        payload = bulk._load_payload(str(pf))
        base_root = str(payload.get("base_root") or "generate automation")

        # For a new pipeline run we typically want a fresh run folder.
        # Using _suggest_run_dir() can reuse an older folder (it restores last mapping / newest existing).
        run_dir = bulk._ensure_run_base_dir(base_root) if always_new else bulk._suggest_run_dir(base_root, payload_path=str(pf))
        bulk._remember_run_dir(str(pf), run_dir)
        # Keep Stage1 UI state in sync with the pipeline-run folder so that gallery/rescan works.
        try:
            st.session_state.bulk_run_base_dir = str(run_dir)
        except Exception:
            pass
        st.session_state["bulkfast_run_base_dir"] = str(run_dir)

        effective_parallelism = base_parallelism
        effective_start_profile = base_start_profile + (idx * base_parallelism) if autoinc_profiles else base_start_profile

        if autoinc_profiles:
            st.caption(
                f"Stage1 профили для этого JSON: start={effective_start_profile} (base={base_start_profile}, +{idx}*{base_parallelism})"
            )

        with st.status(f"Stage1 FAST: {pf}", expanded=True) as s1:
            _ensure_vpn_for_pipeline_stage(f"Stage1 FAST: {pf}")
            res1 = bulk.run_bulk_fast_generation(
                payload=payload,
                run_base_dir=str(run_dir),
                url=str(st.session_state.get("bulkfast_url") or bulk.DEFAULT_URLS[0]),
                model_choice=str(st.session_state.get("bulkfast_model") or "Быстрая"),
                headless=bool(st.session_state.get("bulkfast_headless")),
                executable_path=(str(st.session_state.get("bulkfast_exe_path") or "").strip() or None),
                user_data_dir=str(st.session_state.get("bulkfast_user_data_dir") or os.path.abspath(".chrome_automation_profile")),
                parallelism=int(effective_parallelism),
                start_profile_num=int(effective_start_profile),
                timeout_s=int(st.session_state.get("bulkfast_timeout") or 90),
            )
            s1.write(res1)

        stage12_mode = str(st.session_state.get("unified_stage12_mode") or "Classic: FAST → Tongyi (Pro)")

        if stage12_mode.startswith("Unified"):
            # New mode: generate PRO via FAST and do not run Tongyi.
            with st.status(f"Stage1 FAST (PRO via FAST): {pf}", expanded=True) as s1p:
                _ensure_vpn_for_pipeline_stage(f"Stage1 FAST PRO: {pf}")
                res1p = _run_stage1_pro_via_fast(
                    payload=payload,
                    run_base_dir=str(run_dir),
                    start_profile_num=int(effective_start_profile),
                )
                s1p.write(res1p)

            st.info("Stage2 (Tongyi) skipped because Stage 1-2 mode is Unified")
        else:
            # Classic mode: Stage2 via Tongyi.
            # Tongyi stage: IMPORTANT — use pro_articles (if present) exactly like in original Pro tab
            has_pro_articles = ("pro_articles" in payload) and (payload.get("pro_articles") is not None)
            articles = (payload.get("pro_articles") or []) if has_pro_articles else (payload.get("articles") or [])
            try:
                pro_prompt_count = int(payload.get("pro_prompt_count") or 4)
            except Exception:
                pro_prompt_count = 4

            pro_tasks = bulk._build_pro_tasks_from_articles(
                list(articles or []),
                str(run_dir),
                max_prompts_per_article=int(pro_prompt_count),
            )

            # tasks_snapshot mirrors UI: read edited prompts if present, else payload prompts
            tasks_snapshot: list[dict[str, Any]] = []
            for t in pro_tasks:
                pkey = f"bulkpro_prompt_{t.article_idx}_{t.prompt_idx}"
                pval = (st.session_state.get(pkey) or str(t.prompt) or "").strip()
                pval = bulk._sanitize_prompt(pval or "").strip()
                tasks_snapshot.append({"task": t, "prompt": pval})
            tasks_snapshot = [x for x in tasks_snapshot if x.get("prompt")]

            if bool(st.session_state.get("unified_skip_stage2", False)):
                st.info("Stage2 (Tongyi) skipped by 'Skip Stage2 (Tongyi) — для тестов' in sidebar")
            else:
                with st.status(f"Stage2 TONGYI: {pf}", expanded=True) as s2:
                    _ensure_vpn_for_pipeline_stage(f"Stage2 Tongyi: {pf}")
                    try:
                        res2 = bulk.run_bulk_tongyi_generation(
                            tasks=pro_tasks,
                            tasks_snapshot=tasks_snapshot,
                            run_base_dir=str(run_dir),
                            tongyi_space_url=str(st.session_state.get("bulkpro_tongyi_space_url") or bulk.TONGYI_DEFAULT_SPACE_URL),
                            tongyi_headless=bool(st.session_state.get("bulkpro_tongyi_headless", True)),
                            tongyi_timeout_s=int(st.session_state.get("bulkpro_tongyi_timeout_s") or 360),
                            tongyi_size_text=str(st.session_state.get("bulkpro_tongyi_size_text") or bulk.TONGYI_DEFAULT_SIZE_TEXT),
                            tongyi_start_from=int(st.session_state.get("bulkpro_tongyi_start_from") or 1),
                        )
                    except Exception as e:
                        s2.error(f"Tongyi crashed: {e}")
                        raise
                    else:
                        s2.write(res2)

        # Run the same one-click recovery as Tab1 before moving on to the
        # next payload.  This catches Fast and PRO/NBP prompts that did not
        # save a valid image during their initial Stage1/Stage2 pass.
        with st.status(f"Stage1 recovery (Fast + PRO/NBP): {pf}", expanded=True) as s1_recovery:
            _ensure_vpn_for_pipeline_stage(f"Stage1 recovery: {pf}")
            recovery = _run_stage1_missing_recovery(
                payload=payload,
                run_base_dir=str(run_dir),
                start_profile_num=int(effective_start_profile),
            )
            s1_recovery.write(recovery)
            if int(recovery.get("errors") or 0):
                s1_recovery.update(label="Stage1 recovery finished with errors", state="error")
            elif int(recovery.get("fast_missing") or 0) or int(recovery.get("pro_missing") or 0):
                s1_recovery.update(label="Stage1 recovery complete", state="complete")
            else:
                s1_recovery.update(label="Stage1 recovery: all images were already saved", state="complete")

    # ---- Stage3: run once ----
    _ensure_vpn_for_pipeline_stage("Stage3 BATCH")

    with st.status("Stage3 BATCH (Tab3)", expanded=True) as s3:
        jobs = stage3.build_jobs_from_state()

        if not jobs:
            s3.write("Stage3 skipped: no jobs (fill overlay_text in Tab3 table)")
        else:
            total_runs = sum(int(j["portions"]) for j in jobs)
            base_dir3 = stage3.src._get_run_base_dir("csv_batch_pin_parallel")
            # Use microseconds to avoid run_id collisions (can otherwise reuse stale STOP-file).
            run_id = stage3.src.datetime.now().strftime("csvbatch_%Y%m%d_%H%M%S_%f")
            st.session_state["csvbatch_run_id"] = run_id
            st.session_state["csvbatch_last_run_id"] = run_id
            # Record start time (used as a conservative guard if state reads glitch).
            try:
                st.session_state["unified_stage3_started_ts"] = float(time.time())
                # Also track per-run_id (helps conservative running detection without leaking between runs)
                m0 = st.session_state.get("unified_stage3_started_ts_by_run_id")
                if not isinstance(m0, dict):
                    m0 = {}
                m0[str(run_id)] = float(st.session_state["unified_stage3_started_ts"])
                st.session_state["unified_stage3_started_ts_by_run_id"] = m0
            except Exception:
                pass
            # Clear any stale Stage4 autorun flag from previous runs so it cannot start early.
            st.session_state["stage4_autorun"] = False
            # Unified pipeline should track this run_id even if Tab3 UI later overwrites csvbatch_* keys
            st.session_state["unified_stage3_run_id"] = run_id
            st.session_state["csvbatch_last_base_dir"] = base_dir3

            cfg = {
                "url": st.session_state.get("csvbatch_url") or "https://aistudio.google.com/prompts/new_chat?model=gemini-2.5-flash-image",
                "model_choice": st.session_state.get("csvbatch_model") or "Быстрая",
                "headless": bool(st.session_state.get("csvbatch_headless")),
                "user_data_dir": st.session_state.get("csvbatch_user_data_dir") or os.path.abspath(".chrome_automation_profile"),
                "profile_numbers": stage3.src._parse_profile_numbers(st.session_state.get("csvbatch_profile_numbers") or ""),
                "executable_path": st.session_state.get("csvbatch_executable_path") or r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                "launch_stagger_s": int(st.session_state.get("csvbatch_launch_stagger_s") or 1),
                "base_image_path": "",
                "base_dir": base_dir3,
                "num_windows": int(st.session_state.get("csvbatch_num_windows") or 5),
                "total_runs": int(total_runs),
                "page_default_timeout_ms": int(st.session_state.get("csvbatch_page_default_timeout_ms") or 30000),
                "input_ready_timeout_ms": int(st.session_state.get("csvbatch_input_ready_timeout_ms") or 60000),
                "attach_timeout_ms": int(st.session_state.get("csvbatch_attach_timeout_ms") or 5000),
                "offline_wait_timeout_s": int(st.session_state.get("csvbatch_offline_wait_timeout_s") or 90),
                "gen_timeout_s": int(st.session_state.get("csvbatch_gen_timeout_s") or 90),
                "gen_retry_timeout_s": int(st.session_state.get("csvbatch_gen_retry_timeout_s") or 45),
                "max_session_attempts": int(st.session_state.get("csvbatch_max_session_attempts") or 2),
            }
            stage3.start_batch_run(run_id=run_id, jobs=jobs, cfg=cfg)
            s3.write({"run_id": run_id, "out_dir": base_dir3, "jobs": len(jobs)})

            # Optional: block and wait for Stage3 to complete, then start Stage4 immediately.
            if bool(st.session_state.get("unified_blocking_wait_stage3", True)):
                prog = st.progress(0, text="Stage3 running... waiting for completion")
                t0 = time.time()
                last_done = 0
                last_total = 0
                timed_out = False
                while True:
                    try:
                        state = stage3.get_state(run_id=str(run_id)) or {}
                    except Exception:
                        state = {}

                    try:
                        done = int(state.get("done_runs") or 0)
                        total = int(state.get("total_runs") or 0)
                    except Exception:
                        done, total = 0, 0

                    finished = bool(state.get("finished"))
                    stopped = bool(state.get("stopped"))
                    running = bool(state.get("running"))

                    # Update progress
                    if total > 0:
                        pct = min(100, int(done * 100 / max(total, 1)))
                        prog.progress(pct, text=f"Stage3 running... {done}/{total}")
                    else:
                        # Unknown totals; show spinner-like progress
                        if done != last_done or total != last_total:
                            prog.progress(0, text=f"Stage3 running... done={done} total={total}")

                    last_done, last_total = done, total

                    if finished or stopped or (total > 0 and done >= total) or (not running and (finished or total > 0)):
                        break

                    # Safety timeout (user can rerun if needed)
                    if time.time() - t0 > float(st.session_state.get("unified_stage3_wait_timeout_s") or 60 * 60):
                        # IMPORTANT: if we timed out, Stage3 may still be running.
                        # Do NOT start Stage4 in this case.
                        timed_out = True
                        s3.warning("Stage3 wait timeout reached; Stage4 will NOT start yet. It will be queued until Stage3 finishes.")
                        break

                    time.sleep(1.0)

                prog.empty()

                # Mark stage3 as not running only if we actually observed completion.
                if not timed_out:
                    st.session_state["csvbatch_running"] = False

                # Now start Stage4 (if enabled + uploads present) ONLY if Stage3 completed.
                if (not timed_out) and bool(st.session_state.get("unified_enable_stage4")) and bool(st.session_state.get("stage4_uploaded_files")):
                    st.session_state["unified_current_stage3_run_id"] = str(run_id)
                    st.session_state["stage4_autorun"] = True

                    _stage4_mode = str(st.session_state.get("unified_stage4_mode") or "LoRA (app_streamlit.py)")
                    if _stage4_mode.startswith("Gemini UI"):
                        s3.info("Starting Stage4 (Gemini UI) immediately after Stage3...")
                        _ensure_vpn_for_pipeline_stage("Stage4 Gemini UI")
                        # Guard: avoid rendering Gemini UI twice in the same Streamlit run.
                        st.session_state["unified_stage4_gemini_rendered_run_counter"] = int(st.session_state.get("unified_run_counter") or 0)
                        stage4_gemini.render_stage4_gemini()
                        # Stop further rendering in this run to avoid duplicate widget keys.
                        st.stop()
                    else:
                        s3.info("Starting Stage4 (LoRA) immediately after Stage3...")
                        _ensure_vpn_for_pipeline_stage("Stage4 LoRA")
                        stage4.render_stage4()

    # ---- Stage4 optional ----
    # IMPORTANT: Stage3 runs in background. Do NOT start Stage4 while Stage3 is still running.
    if bool(st.session_state.get("unified_enable_stage4")):
        files4 = st.session_state.get("stage4_uploaded_files")
        if files4:
            # Queue Stage4 autorun and execute it only when Stage3 completes and user opens Tab4.
            st.session_state["unified_stage4_pending"] = True
            st.info("Stage4 queued: will auto-start in Tab4 as soon as Stage3 finishes.")
        else:
            st.warning("Stage4 enabled, but no files uploaded in Stage4 tab.")


def main() -> None:
    st.set_page_config(page_title="Unified Bulk Pipeline", layout="wide")
    st.title("Unified Bulk Pipeline (4 stages)")

    # Per-rerun counter (helps avoid rendering the same embedded app twice in one run).
    st.session_state["unified_run_counter"] = int(st.session_state.get("unified_run_counter") or 0) + 1
    _run_counter = int(st.session_state.get("unified_run_counter") or 0)

    # Track if we already rendered Stage4 Gemini UI in THIS Streamlit run.
    st.session_state.setdefault("unified_stage4_gemini_rendered_run_counter", 0)

    # ---- Defaults tuning (do not overwrite user edits) ----
    _gemini_app = "https://gemini.google.com/app"
    _aistudio_new_chat = "https://aistudio.google.com/prompts/new_chat?model=gemini-2.5-flash-image"

    # Stage1 (Fast): default to Gemini web UI (first option in DEFAULT_URLS)
    st.session_state.setdefault("bulkfast_url", _gemini_app)
    st.session_state.setdefault("bulkfast_parallelism", 4)
    # Requested default: 90 sec
    st.session_state.setdefault("bulkfast_timeout", 90)

    # Stage2 (Tongyi): default per-image timeout
    st.session_state.setdefault("bulkpro_tongyi_timeout_s", 120)

    # Stage3 (CSV batch): default to Gemini web UI
    st.session_state.setdefault("csvbatch_url", _gemini_app)
    # Requested default: 90 sec
    st.session_state.setdefault("csvbatch_gen_timeout_s", 90)
    st.session_state.setdefault("csvbatch_gen_retry_timeout_s", 45)
    st.session_state.setdefault("csvbatch_max_session_attempts", 2)
    # If internet/VPN drops during initial navigation, Stage3 keeps the window open
    # and retries for this long before failing the portion.
    st.session_state.setdefault("csvbatch_offline_wait_timeout_s", 180)

    # Sidebar defaults
    st.session_state.setdefault(
        "unified_stage12_mode",
        "Unified: FAST generates Pro too (skip Tongyi)",
    )
    st.session_state.setdefault("unified_stage1_autoinc_profiles", True)
    st.session_state.setdefault("unified_require_vpn", True)
    st.session_state.setdefault("unified_vpn_hosts_file", "good_hosts_for_images.txt")

    # Stage 4 (Gemini UI) defaults
    st.session_state.setdefault("gemui_max_images", 1)
    st.session_state.setdefault("gemui_timeout_s", 110)
    st.session_state.setdefault("gemui_enable_parallel", True)
    st.session_state.setdefault("gemui_parallelism", 4)
    # Pins/day default (applies to Stage4 export UI)
    st.session_state.setdefault("pins_per_day", 5)

    st.caption(
        "Вкладки 1-2: из app_bulk_fast_images_streamlit.py. "
        "Вкладки 3-4: встроены как есть (исполняются внутри текущего приложения)."
    )

    _configure_pipeline_vpn_env()
    _install_pipeline_vpn_guards_once()

    # Global (cross-tab) panel
    with st.sidebar:
        st.subheader("Pipeline")
        payload_candidates = sorted([p.name for p in Path('.').glob('tmp_rovodev_bulk_fast_payload_*.json')])

        # Default: pick the 2 bottom-most JSON (only on first app open)
        if "unified_payloads" not in st.session_state:
            st.session_state["unified_payloads"] = (
                payload_candidates[-2:] if len(payload_candidates) >= 2 else list(payload_candidates)
            )

        picked_payloads = st.multiselect(
            "Payload JSON (можно несколько)",
            options=payload_candidates,
            default=st.session_state.get("unified_payloads", []),
            key="unified_payloads",
        )

        st.checkbox(
            "Stage 4 optional",
            value=bool(st.session_state.get('unified_enable_stage4', False)),
            key="unified_enable_stage4",
            help="Если выключено — этап 4 не нужен.",
        )

        st.selectbox(
            "Stage 4 mode",
            options=[
                "LoRA (app_streamlit.py)",
                "Gemini UI (app_streamlit_gemini.py)",
            ],
            index=0
            if str(st.session_state.get("unified_stage4_mode") or "").startswith("LoRA")
            else 1,
            key="unified_stage4_mode",
            help="Какой вариант этапа 4 запускать после Stage3.",
        )

        # Global setting consumed by app_streamlit.py / videos_streamlit.py
        # NOTE: Stage4 (app_streamlit.py) also defines a checkbox with key="vpn_one_try".
        # In the unified app this sidebar is always present, so using the same key would crash
        # with StreamlitDuplicateElementKey when Stage4 tab is opened.
        #
        # We therefore use an internal, namespaced key and mirror its value into
        # st.session_state["vpn_one_try"] for backward compatibility with Stage4.
        if "vpn_one_try" in st.session_state and "unified_vpn_one_try" not in st.session_state:
            st.session_state["unified_vpn_one_try"] = bool(st.session_state.get("vpn_one_try", False))

        vpn_enabled_ui = st.checkbox(
            "VPN режим для RUN FULL PIPELINE",
            value=bool(st.session_state.get("unified_require_vpn", True)),
            key="unified_require_vpn",
            help=(
                "Если включено — Stage1/Stage2/Stage3/Stage4 будут проверять/подключать VPN перед генерацией. "
                "Если выключено — пайплайн не будет трогать VPN и выставит GEMINI_REQUIRE_VPN=0."
            ),
        )
        st.caption("VPN включен" if bool(vpn_enabled_ui) else "VPN выключен: RUN FULL PIPELINE пойдет без подключения/ротации VPN.")

        st.text_input(
            "VPN hosts file",
            value=str(st.session_state.get("unified_vpn_hosts_file") or "good_hosts_for_images.txt"),
            key="unified_vpn_hosts_file",
            help="Used by existing Gemini VPN rotation logic. Default: good_hosts_for_images.txt",
            disabled=not bool(vpn_enabled_ui),
        )

        vpn_one_try = st.checkbox(
            "VPN: одна попытка подключения (retries=1)",
            value=bool(st.session_state.get("unified_vpn_one_try", False)),
            key="unified_vpn_one_try",
            disabled=not bool(vpn_enabled_ui),
            help="Если включить — при переключении VPN на конкретный хост будет только 1 попытка вместо 2.",
        )
        st.session_state["vpn_one_try"] = bool(vpn_one_try)
        # Apply to Gemini-side VPN rotation too (generate_pinterest_texts.py)
        _configure_pipeline_vpn_env()
        st.checkbox(
            "Всегда создавать новый run dir (для RUN FULL PIPELINE)",
            value=bool(st.session_state.get("unified_always_new_run_dir", True)),
            key="unified_always_new_run_dir",
            help=(
                "Если включено — для каждого payload создается новая run-папка (с датой). "
                "Если выключить — будет использоваться логика восстановления (_suggest_run_dir): последняя/самая новая папка."
            ),
        )

        st.selectbox(
            "Stage 1-2 mode",
            options=[
                "Classic: FAST → Tongyi (Pro)",
                "Unified: FAST generates Pro too (skip Tongyi)",
            ],
            index=0
            if str(st.session_state.get("unified_stage12_mode") or "Classic").startswith("Classic")
            else 1,
            key="unified_stage12_mode",
            help=(
                "Classic — как сейчас: Stage1 (FAST) генерирует обычные, Stage2 (Tongyi) генерирует PRO. "
                "Unified — новый режим: PRO тоже генерятся на Stage1 (FAST), Tongyi не запускается."
            ),
        )

        st.checkbox(
            "Skip Stage2 (Tongyi) — для тестов",
            value=bool(st.session_state.get("unified_skip_stage2", False)),
            key="unified_skip_stage2",
            help="Если включено — этап 2 (Tongyi) не запускается. Удобно, чтобы не тратить GPU/время во время тестов Stage3.",
        )

        st.checkbox(
            "Stage1: авто-сдвиг профилей между JSON",
            value=bool(st.session_state.get("unified_stage1_autoinc_profiles", False)),
            key="unified_stage1_autoinc_profiles",
            help=(
                "Если выбрано несколько payload JSON, то для каждого следующего JSON стартовый номер профиля "
                "увеличивается на 'Параллельно окон'. Пример: start=1, parallelism=3 → JSON1: 1-3, JSON2: 4-6."
            ),
        )

        st.checkbox(
            "Auto-start Stage4 immediately after Stage3 (wait in UI)",
            value=bool(st.session_state.get("unified_blocking_wait_stage3", True)),
            key="unified_blocking_wait_stage3",
            help=(
                "Если включено — RUN FULL PIPELINE будет ждать завершения Stage3 в этом же запуске "
                "и затем сразу запустит Stage4. Это самый надёжный режим без ручных кликов/обновлений."
            ),
        )

        st.checkbox(
            "Debug pipeline",
            value=bool(st.session_state.get("unified_debug", False)),
            key="unified_debug",
            help="Показывает текущие флаги/статусы для диагностики автозапуска Stage4.",
        )

        if bool(st.session_state.get("unified_debug", False)):
            try:
                rid_dbg = _get_stage3_run_id()
                st.caption(f"stage3_run_id={rid_dbg}")
                if rid_dbg:
                    st.json(stage3.get_state(run_id=str(rid_dbg)))
            except Exception as e:
                st.caption(f"debug get_state failed: {e}")

            files4_dbg = st.session_state.get("stage4_uploaded_files")
            try:
                files4_n = len(files4_dbg) if files4_dbg is not None else 0
            except Exception:
                files4_n = -1

            st.caption(f"stage4_uploaded_files_present={bool(files4_dbg)} (n={files4_n})")
            st.caption(f"unified_stage4_pending={bool(st.session_state.get('unified_stage4_pending'))}")
            st.caption(f"stage4_autorun={bool(st.session_state.get('stage4_autorun'))}")
            st.caption(
                "stage4_started_for_run_id="
                + str(st.session_state.get("unified_stage4_started_for_stage3_run_id") or "")
            )
            if st.button("Debug: reset Stage4 marker", key="unified_debug_reset_stage4_marker"):
                st.session_state.pop("unified_stage4_started_for_stage3_run_id", None)
                st.session_state.pop("unified_stage4_autorun_requested_for_stage3_run_id", None)
                st.session_state.pop("unified_stage4_autorun_for_stage3_run_id", None)  # legacy
                st.session_state.pop("unified_current_stage3_run_id", None)
                try:
                    st.rerun()
                except Exception:
                    pass

        st.session_state.setdefault("unified_stage3_wait_timeout_s", 3600)
        st.number_input(
            "Stage3 wait timeout (sec)",
            min_value=60,
            max_value=6 * 3600,
            value=int(st.session_state.get("unified_stage3_wait_timeout_s") or 3600),
            step=60,
            key="unified_stage3_wait_timeout_s",
            help="Только для режима blocking wait. По умолчанию 3600 сек (1 час).",
        )

        run_all = st.button(
            "RUN FULL PIPELINE (1→2→3→4)",
            type="primary",
            disabled=not bool(picked_payloads),
            help="Запускает последовательно: fast → tongyi → batch → (stage4 опц.).",
        )

    if run_all:
        _run_full_pipeline(picked_payloads)

    # Keep Stage4 queue state in sync even when Stage3 was started manually in Tab3.
    _sync_stage4_queue_state()

    # Force periodic reruns while waiting for background Stage3 to finish (needed for Stage4 autorun)
    _autorefresh_if_needed()

    # If Stage3 is currently running, make sure a stale Stage4 autorun flag can't fire.
    try:
        if _is_stage3_running():
            st.session_state["stage4_autorun"] = False
            if bool(st.session_state.get("unified_debug", False)):
                st.caption("Guard: Stage3 running → stage4_autorun cleared")
    except Exception as e:
        if bool(st.session_state.get("unified_debug", False)):
            st.caption(f"Guard: _is_stage3_running failed: {e}")

    # Deterministic Stage4 autorun request once Stage3 finishes (does not depend on active tab)
    _maybe_request_stage4_autorun()

    # --- Auto-render Stage4 (Gemini UI) even if Tab4 is not active ---
    # Streamlit sometimes does not execute inactive tab blocks; for a deterministic pipeline
    # we render Gemini Stage4 here when needed.
    auto_rendered_stage4_gemini = False
    try:
        _stage4_mode0 = str(st.session_state.get("unified_stage4_mode") or "LoRA (app_streamlit.py)")
        if _stage4_mode0.startswith("Gemini UI"):
            rid0 = _get_stage3_run_id()
            files0 = st.session_state.get("stage4_uploaded_files")
            pending0 = bool(st.session_state.get("unified_stage4_pending"))
            requested0 = str(st.session_state.get("unified_stage4_autorun_requested_for_stage3_run_id") or "")
            started0 = str(st.session_state.get("unified_stage4_started_for_stage3_run_id") or "")
            should0 = (
                bool(rid0)
                and (str(rid0) != started0)
                and (bool(files0) or pending0 or (str(rid0) == requested0))
            )

            # Only render here if Stage3 is finished and Stage4 still needs to start.
            if should0 and (not _is_stage3_running()):
                # Request autorun and render right away (independent of active tab)
                st.session_state["unified_current_stage3_run_id"] = str(rid0)
                st.session_state["stage4_autorun"] = True

                st.info("Stage4 (Gemini UI) autorun: starting...")
                auto_rendered_stage4_gemini = True
                st.session_state["unified_stage4_gemini_rendered_run_counter"] = int(st.session_state.get("unified_run_counter") or 0)

                # Avoid Streamlit config conflicts when embedding apps that call st.set_page_config()
                from contextlib import contextmanager

                @contextmanager
                def _patched_set_page_config():
                    orig = getattr(st, "set_page_config", None)

                    def _noop(*args, **kwargs):
                        return None

                    try:
                        if orig is not None:
                            st.set_page_config = _noop  # type: ignore[assignment]
                        yield
                    finally:
                        try:
                            if orig is not None:
                                st.set_page_config = orig  # type: ignore[assignment]
                        except Exception:
                            pass

                with _patched_set_page_config():
                    _ensure_vpn_for_pipeline_stage("Stage4 Gemini UI autorun")
                    with _gemini_vpn_requirement_temporarily(_pipeline_vpn_enabled()):
                        stage4_gemini.render_stage4_gemini()
    except Exception as _e_autostage4:
        auto_rendered_stage4_gemini = False
        if bool(st.session_state.get("unified_debug", False)):
            st.warning(f"Stage4 autorun render failed: {_e_autostage4}")

    _stage4_mode = str(st.session_state.get("unified_stage4_mode") or "LoRA (app_streamlit.py)")
    _tab4_title = (
        "4) Optional: LoRA generation (from app_streamlit.py)"
        if _stage4_mode.startswith("LoRA")
        else "4) Optional: Gemini UI generation (from app_streamlit_gemini.py)"
    )

    tab1, tab2, tab3, tab4 = st.tabs(
        [
            "1) Bulk Fast (Gemini)",
            "2) Bulk Tongyi / Pro",
            "3) Batch pins with overlay (Tab3 from app_parallel_overlay_streamlit.py)",
            _tab4_title,
        ]
    )

    # ---- Stage 1-2: reuse existing renderers ----
    with tab1:
        try:
            import app_bulk_fast_images_streamlit as bulk

            # Быстрый выбор payload без ручного ввода пути (оригинальный UI Stage1 поддерживает "Пересоздать промпт")
            try:
                cand0 = list(Path('.').glob('tmp_rovodev_bulk_fast_payload_*.json'))
                cand0.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
                payload_candidates = [str(p) for p in cand0]
            except Exception:
                payload_candidates = []

            if payload_candidates:
                def _on_unified_stage1_payload_change() -> None:
                    try:
                        picked = str(st.session_state.get("unified_stage1_payload_select") or "").strip()
                        if not picked:
                            return
                        # sync underlying widget used by bulk._render_fast_tab()
                        st.session_state["bulkfast_payload_path"] = picked
                        _reset_fast_state_for_new_payload()
                    except Exception:
                        pass

                picked_one = st.selectbox(
                    "Payload для Stage1 (просмотр/пересоздание)",
                    options=payload_candidates,
                    index=0,
                    key="unified_stage1_payload_select",
                    on_change=_on_unified_stage1_payload_change,
                )
                # Force-sync the original Stage1 payload path widget state to this selection.
                # (on_change already does it, but keep this as a safe fallback)
                try:
                    cur = st.session_state.get("bulkfast_payload_path")
                    if cur != picked_one:
                        st.session_state["bulkfast_payload_path"] = picked_one
                        _reset_fast_state_for_new_payload()
                except Exception:
                    st.session_state["bulkfast_payload_path"] = picked_one
                    _reset_fast_state_for_new_payload()

                colx, coly = st.columns([1, 3])
                if colx.button("Применить run dir", key="unified_stage1_apply_rundir"):
                    try:
                        payload0 = bulk._load_payload(str(picked_one))
                        base_root0 = str(payload0.get("base_root") or "generate automation")
                        run_dir0 = bulk._suggest_run_dir(base_root0, payload_path=str(picked_one))
                        bulk._remember_run_dir(str(picked_one), str(run_dir0))

                        # mimic "Use selected run dir" behavior from original app
                        st.session_state.bulk_run_base_dir = str(run_dir0)
                        st.session_state["bulkfast_run_base_dir"] = str(run_dir0)
                        st.session_state["bulk_errors"] = []
                        st.session_state["bulkfast_last_disk_scan_run_dir"] = ""
                        st.session_state["bulk_saved_items"] = []
                        st.session_state["bulk_saved"] = {}
                    except Exception as e:
                        coly.error(f"Не удалось применить run dir: {e}")
                    else:
                        try:
                            st.rerun()
                        except Exception:
                            pass

            bulk._render_fast_tab()

            st.divider()
            st.subheader("Просмотр результатов Fast для нескольких payload")
            try:
                cand = list(Path('.').glob('tmp_rovodev_bulk_fast_payload_*.json'))
                cand.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
                payload_opts = [str(p) for p in cand]
            except Exception:
                payload_opts = []

            picked_view = st.multiselect(
                "Payload JSON (можно несколько)",
                options=payload_opts,
                default=[],
                key="unified_fast_view_payloads",
                help="Выберите payload(ы) и ниже увидите найденные картинки в соответствующей run-папке.",
            )

            if picked_view:
                for pp in picked_view:
                    st.markdown(f"### {pp}")
                    try:
                        payload0 = bulk._load_payload(pp)
                        base_root0 = str(payload0.get('base_root') or 'generate automation')
                        run_dir0 = bulk._suggest_run_dir(base_root0, payload_path=str(pp))
                        st.caption(f"run_dir: {run_dir0}")
                        scanned = bulk._scan_fast_saved_items_from_disk(str(run_dir0))
                    except Exception as e2:
                        st.error(f"Не удалось загрузить/просканировать: {e2}")
                        continue

                    paths = []
                    for it in scanned:
                        try:
                            pth = str((it or {}).get('path') or '')
                            if pth and os.path.exists(pth):
                                paths.append(pth)
                        except Exception:
                            continue

                    if not paths:
                        st.info("Не найдено картинок (проверь run_dir или что генерация завершилась)")
                        continue

                    cols = st.columns(4)
                    for i, pth in enumerate(paths):
                        with cols[i % 4]:
                            st.image(pth, caption=os.path.basename(pth), use_container_width=True)

        except Exception as e:
            st.error("Failed to render Stage 1 from app_bulk_fast_images_streamlit.py")
            st.exception(e)

    with tab2:
        try:
            import app_bulk_fast_images_streamlit as bulk

            bulk._render_pro_tab()
        except Exception as e:
            st.error("Failed to render Stage 2 from app_bulk_fast_images_streamlit.py")
            st.exception(e)

    with tab3:
        stage3.render_stage3_tab()

    with tab4:
        if not st.session_state.get("unified_enable_stage4"):
            st.info("Этап 4 выключен в sidebar. Включите, если нужно.")
        else:
            # Render the selected Stage4 UI.
            # We only control *autorun* so that Stage4 starts automatically after Stage3 finishes.

            # Stage4 defaults (do not overwrite user edits)
            st.session_state.setdefault("selected_loras", ["Flux Face Realism"])

            # NOTE: do NOT set/clear stage4_autorun here.
            # Stage4 autorun is orchestrated globally by `_maybe_request_stage4_autorun()`.

            # Avoid Streamlit config conflicts when embedding apps that call st.set_page_config()
            from contextlib import contextmanager

            @contextmanager
            def _patched_set_page_config():
                orig = getattr(st, "set_page_config", None)

                def _noop(*args, **kwargs):
                    return None

                try:
                    if orig is not None:
                        st.set_page_config = _noop  # type: ignore[assignment]
                    yield
                finally:
                    try:
                        if orig is not None:
                            st.set_page_config = orig  # type: ignore[assignment]
                    except Exception:
                        pass

            _stage4_mode = str(st.session_state.get("unified_stage4_mode") or "LoRA (app_streamlit.py)")

            with _patched_set_page_config():
                if _stage4_mode.startswith("Gemini UI"):
                    already = int(st.session_state.get("unified_stage4_gemini_rendered_run_counter") or 0) == int(st.session_state.get("unified_run_counter") or 0)
                    if already:
                        st.info("Stage4 (Gemini UI) is already rendered in this run.")
                    else:
                        # Mark as rendered for this run (prevents duplicate keys)
                        st.session_state["unified_stage4_gemini_rendered_run_counter"] = int(st.session_state.get("unified_run_counter") or 0)
                        _ensure_vpn_for_pipeline_stage("Stage4 Gemini UI")
                        with _gemini_vpn_requirement_temporarily(_pipeline_vpn_enabled()):
                            stage4_gemini.render_stage4_gemini()
                else:
                    _ensure_vpn_for_pipeline_stage("Stage4 LoRA")
                    stage4.render_stage4()


if __name__ == "__main__":
    main()
