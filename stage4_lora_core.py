# -*- coding: utf-8 -*-
"""Stage 4 core wrapper.

We keep the original working UI/logic from `app_streamlit.py`.
After refactor, `app_streamlit.py` runs its UI only inside `main()`.

This wrapper lets the unified app render Stage4 by calling `render_stage4()`.
"""

from __future__ import annotations

import os


def render_stage4() -> None:
    """Render Stage4 (LoRA) inside current Streamlit run.

    Notes:
    - `app_streamlit.py` calls `st.set_page_config()`. In an embedded scenario (unified app)
      this can crash with "set_page_config can only be called once".
    - We therefore temporarily patch `st.set_page_config` to a no-op.
    """

    # If Stage4 is rendered inside a bigger Streamlit app, keep VPN setting consistent.
    # Stage4 UI uses key="vpn_one_try" in its own checkbox.
    try:
        import streamlit as st

        if "unified_require_vpn" in st.session_state:
            os.environ["GEMINI_REQUIRE_VPN"] = "1" if bool(st.session_state.get("unified_require_vpn", True)) else "0"
        if "unified_vpn_one_try" in st.session_state:
            st.session_state["vpn_one_try"] = bool(st.session_state.get("unified_vpn_one_try", False))
    except Exception:
        # If Streamlit isn't available or session isn't initialized, just render as-is.
        pass

    # Import lazily to avoid side effects (env vars, heavy imports) when Stage4 is disabled
    # or when another Stage4 mode is used in the unified pipeline.
    import app_streamlit as src

    # Avoid Streamlit config conflicts when embedding apps that call st.set_page_config()
    try:
        import streamlit as st
        from contextlib import contextmanager

        if "unified_require_vpn" in st.session_state:
            os.environ["GEMINI_REQUIRE_VPN"] = "1" if bool(st.session_state.get("unified_require_vpn", True)) else "0"

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
            src.main()
    except Exception:
        # Fallback: if patching failed, try normal run.
        src.main()
