# Lightweight launcher for Streamlit UI
# Usage: streamlit run app_webp.py
from convert_to_webp import run_streamlit_app

if __name__ == "__main__":
    # Run only when this file is executed directly (e.g., `streamlit run app_webp.py`)
    run_streamlit_app()
# Do not auto-run on import to avoid duplicate UI rendering
