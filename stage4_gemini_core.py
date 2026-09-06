# -*- coding: utf-8 -*-
"""Stage 4 (Gemini UI) core wrapper.

This wrapper allows embedding `app_streamlit_gemini.py` inside another Streamlit app
(e.g. `app_unified_bulk_pipeline_streamlit.py`).

Important:
- `app_streamlit_gemini.py` currently executes its UI at import time.
- It also calls `st.set_page_config()`, which is only allowed once per Streamlit app.

So we execute it via `runpy.run_path()` while temporarily monkeypatching
`st.set_page_config` to a no-op.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
import runpy


@contextmanager
def _patched_set_page_config():
    import streamlit as st

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


def render_stage4_gemini() -> None:
    """Render Stage4 Gemini UI app inside current Streamlit run."""

    here = os.path.abspath(os.path.dirname(__file__))
    target = os.path.join(here, "app_streamlit_gemini.py")

    # Ensure the executed script sees itself as __main__ (so any `if __name__ == ...` blocks work)
    with _patched_set_page_config():
        runpy.run_path(target, run_name="__main__")
