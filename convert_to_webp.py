#!/usr/bin/env python3
"""
Small utility to convert images to WebP.

Usage (CLI):
  python convert_to_webp.py --input path/to/file_or_dir --output path/to/outdir --quality 50 --lossless False --keep-metadata True
  python convert_to_webp.py --help

Usage (Streamlit):
  streamlit run convert_to_webp.py

Notes:
- Supports PNG, JPG/JPEG, GIF (first frame), BMP, TIFF, WEBP (re-encode), HEIC/HEIF (if pillow-heif installed).
- Preserves transparency.
- Can optionally keep EXIF metadata.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path
from typing import Iterable, Optional, Tuple
from datetime import datetime
import uuid

# Optional: enable HEIC/HEIF support if available
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
except Exception:
    pass

from PIL import Image, UnidentifiedImageError

SUPPORTED_EXTS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif", ".webp", ".jfif", ".pjpeg", ".pjp", ".heic", ".heif",
}


def ensure_outdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def derive_output_path(input_path: Path, outdir: Path, suffix: str = ".webp") -> Path:
    # If filename contains 'Gemini' (case-insensitive), randomize the output name for convenience
    name = input_path.stem
    if "gemini" in name.lower():
        name = uuid.uuid4().hex  # random 32-char hex
    # Flat output: write all files into outdir with .webp suffix
    return outdir / (name + suffix)




def convert_image_to_webp(
    input_path: Path,
    output_path: Path,
    quality: int = 80,
    lossless: bool = False,
    keep_metadata: bool = True,
    method: int = 6,
    icc_profile: bool = True,
) -> Tuple[bool, Optional[str]]:
    """Convert a single image to WebP.

    Returns (success, error_message_if_any).
    """
    try:
        with Image.open(input_path) as im:
            im.load()  # ensure loaded before conversion
            exif = im.info.get("exif") if keep_metadata else None
            icc = im.info.get("icc_profile") if (icc_profile and keep_metadata) else None

            # Convert mode if needed while preserving alpha
            if im.mode in ("RGBA", "LA"):
                pass  # preserves alpha
            elif im.mode == "P":
                im = im.convert("RGBA")  # paletted images may carry transparency
            elif im.mode in ("CMYK", "YCbCr"):
                im = im.convert("RGB")
            elif im.mode not in ("RGB", "RGBA", "L"):
                im = im.convert("RGB")

            save_kwargs = {
                "format": "WEBP",
                "lossless": lossless,
                "quality": 100 if lossless else int(max(0, min(100, quality))),
                "method": int(max(0, min(6, method))),  # 0..6, 6 = best compression
                "icc_profile": icc,
            }
            if exif:
                save_kwargs["exif"] = exif

            ensure_outdir(output_path.parent)
            im.save(output_path, **save_kwargs)

            # If something created a .web file anyway (unexpected), rename it to .webp.
            try:
                alt_web = output_path.with_suffix(".web")
                if alt_web.exists() and not output_path.exists():
                    alt_web.replace(output_path)
            except Exception:
                pass

            # Validate that the output is a real WebP (some environments can produce corrupted/partial files).
            try:
                head = Path(output_path).read_bytes()[:16]
                # WebP container is RIFF....WEBP
                if not (len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP"):
                    try:
                        Path(output_path).unlink(missing_ok=True)
                    except TypeError:
                        if Path(output_path).exists():
                            Path(output_path).unlink()
                    return False, f"Invalid WebP output (header mismatch): {output_path}"
            except Exception:
                # If we can't validate, don't fail the conversion.
                pass

        return True, None
    except UnidentifiedImageError:
        return False, f"Unrecognized image format: {input_path}"
    except Exception as e:
        return False, f"{input_path}: {e}"


def iter_input_images(path: Path) -> Iterable[Path]:
    if path.is_file():
        if path.suffix.lower() in SUPPORTED_EXTS:
            yield path
    elif path.is_dir():
        for p in path.rglob("*"):
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                yield p


def run_cli(args: argparse.Namespace) -> int:
    src = Path(args.input).resolve()
    # Create a session folder inside the requested output dir
    base_outdir = Path(args.output).resolve()
    session_name = datetime.now().strftime("webp_session_%Y-%m-%d_%H-%M-%S")
    outdir = base_outdir / session_name
    ensure_outdir(outdir)

    images = list(iter_input_images(src))
    if not images:
        print("No supported images found.")
        return 1

    total = len(images)
    ok = 0
    skipped = 0
    for p in images:
        out_path = derive_output_path(p, outdir)
        if out_path.exists() and not args.overwrite:
            skipped += 1
            if not args.quiet:
                print(f"Skip (exists): {out_path}")
            continue
        success, err = convert_image_to_webp(
            p,
            out_path,
            quality=args.quality,
            lossless=args.lossless,
            keep_metadata=not args.strip_metadata,
            method=args.method,
            icc_profile=not args.strip_metadata,
        )
        if success:
            ok += 1
            if not args.quiet:
                print(f"OK: {p.name} -> {out_path.name}")
        else:
            print(f"ERROR: {err}")

    print(f"Done. Converted: {ok}/{total}. Skipped: {skipped}.")
    return 0 if ok > 0 else 2


# ------------------ Streamlit UI ------------------

def run_streamlit_app(embed: bool = False, default_folder: Optional[str] = None, only_filled_default: bool = False, default_base_outdir: Optional[str] = None, default_session_name: Optional[str] = None, follow_latest_defaults: bool = True) -> None:
    try:
        import streamlit as st
    except Exception as e:
        print("Streamlit is not installed. Install with: pip install streamlit pillow")
        raise

    if not embed:
        st.set_page_config(page_title="Image to WebP Converter", page_icon="🖼️", layout="centered")
    st.title("🖼️ Image → WebP Converter")
    st.caption("Drop images or point to a folder. Converts with Pillow, preserves transparency, optional metadata.")

    with st.sidebar:
        st.header("Settings")
        lossless = st.toggle("Lossless", value=False, help="Use WebP lossless compression", key="opt_lossless")
        quality = 100 if lossless else st.slider("Quality", 0, 100, 80, help="Ignored if lossless is enabled", key="opt_quality")
        method = st.slider("Method (compression effort)", 0, 6, 6, help="Higher = better compression, slower", key="opt_method")
        keep_metadata = st.toggle("Keep metadata (EXIF/ICC)", value=True, key="opt_keep_metadata")
        overwrite = st.toggle("Overwrite existing", value=False, key="opt_overwrite")
        default_outdir_val = default_base_outdir if default_base_outdir else str((Path.cwd() / "webp_output").resolve())
        if follow_latest_defaults or ("opt_base_outdir" not in st.session_state):
            st.session_state.opt_base_outdir = default_outdir_val
        base_outdir_text = st.text_input("Base output directory", value=st.session_state.get("opt_base_outdir", default_outdir_val), key="opt_base_outdir")
        base_outdir = Path(base_outdir_text).expanduser().resolve()
        session_name_default = default_session_name if default_session_name else datetime.now().strftime("webp_session_%Y-%m-%d_%H-%M-%S")
        if follow_latest_defaults or ("opt_session_name" not in st.session_state):
            st.session_state.opt_session_name = session_name_default
        session_name = st.text_input("Session folder name", value=st.session_state.get("opt_session_name", session_name_default), help="A subfolder will be created under the base output directory", key="opt_session_name")
        outdir = base_outdir / session_name

    st.subheader("Upload files")
    uploaded = st.file_uploader("Drag-and-drop images", type=[
        "png", "jpg", "jpeg", "bmp", "tif", "tiff", "gif", "webp", "jfif", "pjpeg", "pjp", "heic", "heif"
    ], accept_multiple_files=True, key="uploader_files")

    st.markdown("— or —")

    st.subheader("Convert an existing folder")
    col1, col2 = st.columns([3,1])
    with col1:
        # Default to provided folder if embedding app sets it
        initial_folder = default_folder if default_folder else str(Path.cwd())
        # Optionally follow defaults pushed by embedding host each rerender
        if follow_latest_defaults or ("opt_folder_path" not in st.session_state):
            st.session_state.opt_folder_path = initial_folder
        folder_text = st.text_input("Folder path with images", value=st.session_state.get("opt_folder_path", initial_folder), key="opt_folder_path")
    with col2:
        scan_btn = st.button("Scan folder", key="btn_scan_folder")

    # Pending conversion list persisted across reruns
    if "webp_pending" not in st.session_state:
        st.session_state.webp_pending = []  # list of (src:str, dst:str)
    if "webp_outdir" not in st.session_state:
        st.session_state.webp_outdir = str(outdir)

    to_convert: list[Tuple[Path, Path]] = []

    # Persist outdir for subsequent actions even if UI reruns
    st.session_state.webp_outdir = str(outdir)

    # Handle uploads
    if uploaded:
        ensure_outdir(outdir)
        for f in uploaded:
            try:
                # Validate and open
                data = f.read()
                im = Image.open(io.BytesIO(data))
                im.load()
                # Save temporarily to convert using same function path-based
                tmp_in = outdir / f"__staging__{f.name}"
                ensure_outdir(tmp_in.parent)
                with open(tmp_in, "wb") as tmp:
                    tmp.write(data)
                out_path = derive_output_path(tmp_in, outdir)
                to_convert.append((tmp_in, out_path))
            except Exception as e:
                st.error(f"Failed to read {f.name}: {e}")

    # Handle folder scan
    if scan_btn and folder_text.strip():
        folder = Path(folder_text).expanduser().resolve()
        if folder.exists() and folder.is_dir():
            files = list(iter_input_images(folder))
            # If embedded with 'only_filled_default', prefilter to *_filled.*
            if only_filled_default:
                files = [p for p in files if p.stem.lower().endswith("_filled")]
            if not files:
                st.info("No supported images found in the folder.")
            else:
                for p in files:
                    out_path = derive_output_path(p, outdir)
                    to_convert.append((p, out_path))
                st.success(f"Found {len(files)} image(s) in folder.")
                # Save pending list in session_state to survive reruns
                st.session_state.webp_pending = [(str(p), str(derive_output_path(p, outdir))) for p in files]
        else:
            st.error("Folder path does not exist or is not a directory.")

    # If a rerender happened after scan, restore pending list
    if not to_convert and st.session_state.get("webp_pending"):
        try:
            to_convert = [(Path(s), Path(d)) for (s, d) in st.session_state.webp_pending]
            outdir = Path(st.session_state.get("webp_outdir", str(outdir)))
        except Exception:
            pass

    if to_convert:
        st.write(f"Ready to convert: {len(to_convert)} file(s)")
        if st.button("Convert now", type="primary", key="btn_convert_now"): 
            ensure_outdir(outdir)
            successes = 0
            skipped = 0
            st.write(f"Output folder: {outdir}")
            errors: list[str] = []
            progress = st.progress(0)
            status = st.empty()
            for idx, (src, dst) in enumerate(to_convert, start=1):
                try:
                    src = Path(src)
                    dst = Path(dst)
                except Exception:
                    pass
                if dst.exists() and not overwrite:
                    skipped += 1
                else:
                    ok, err = convert_image_to_webp(
                        src,
                        dst,
                        quality=quality,
                        lossless=lossless,
                        keep_metadata=keep_metadata,
                        method=method,
                        icc_profile=keep_metadata,
                    )
                    if ok:
                        successes += 1
                    else:
                        errors.append(err or f"Unknown error for {src}")
                progress.progress(int(idx / len(to_convert) * 100))
                status.text(f"{idx}/{len(to_convert)} processed…")

            # Clean up staging temporary files
            for src, _ in to_convert:
                if src.name.startswith("__staging__"):
                    try:
                        src.unlink(missing_ok=True)
                    except Exception:
                        pass

            st.success(f"Done. Converted: {successes}. Skipped: {skipped}. Errors: {len(errors)}.")
            if errors:
                with st.expander("Show errors"):
                    for e in errors:
                        st.write("- ", e)
    else:
        st.caption("Add files or scan a folder to start.")


# ------------------ Argparse ------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Convert images to WebP (CLI and Streamlit UI)")
    p.add_argument("--input", "-i", help="Input file or directory", required=False, default=None)
    p.add_argument("--output", "-o", help="Output directory", required=False, default="webp_output")
    p.add_argument("--quality", "-q", type=int, default=80, help="Quality 0-100 (ignored if --lossless)")
    p.add_argument("--lossless", action="store_true", help="Enable lossless WebP")
    p.add_argument("--method", type=int, default=6, help="Compression effort 0-6 (higher = better, slower)")
    p.add_argument("--strip-metadata", action="store_true", help="Strip EXIF/ICC metadata")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    p.add_argument("--quiet", action="store_true", help="Reduce CLI output")
    p.add_argument("--cli", action="store_true", help="Force CLI mode even if run via streamlit")
    return p


def _running_under_streamlit() -> bool:
    bname = os.path.basename(sys.argv[0]).lower()
    if "streamlit" in bname:
        return True
    # Heuristic via env vars commonly present when Streamlit runs the script
    streamlit_env_keys = (
        "STREAMLIT_SERVER_ENABLED",
        "STREAMLIT_SERVER_PORT",
        "STREAMLIT_STATIC_DIR",
        "STREAMLIT_RUN_CONTEXT",
    )
    return any(os.environ.get(k) for k in streamlit_env_keys)


def main(argv: Optional[Iterable[str]] = None) -> int:
    # Robust detection of Streamlit runtime using internal API first
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx  # type: ignore
        if get_script_run_ctx() is not None and "--cli" not in sys.argv:
            run_streamlit_app()
            return 0
    except Exception:
        pass

    # Fallback heuristic
    if _running_under_streamlit() and "--cli" not in sys.argv:
        run_streamlit_app()
        return 0

    # If run via plain `python convert_to_webp.py` with no args, be helpful
    if argv is None and len(sys.argv) == 1:
        # Try to hint about UI instead of erroring out
        print("No arguments supplied. For UI, run: streamlit run convert_to_webp.py")
        print("For CLI usage, run with --help")
        return 1

    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    # Require input in CLI mode
    if args.input is None:
        parser.error("--input is required in CLI mode. Or run the Streamlit UI: streamlit run convert_to_webp.py")

    return run_cli(args)


def _is_streamlit_runtime() -> bool:
    try:
        # Streamlit 1.x way to detect active runtime
        from streamlit.runtime.scriptrunner import get_script_run_ctx  # type: ignore
        return get_script_run_ctx() is not None
    except Exception:
        return _running_under_streamlit()


if __name__ == "__main__":
    raise SystemExit(main())
else:
    # When executed by `streamlit run convert_to_webp.py`, __name__ != "__main__".
    # Only auto-render if THIS file is the Streamlit entry script (avoid double-render on import).
    try:
        import streamlit  # noqa: F401
        if os.path.basename(sys.argv[0]) == os.path.basename(__file__):
            run_streamlit_app()
    except Exception:
        # If Streamlit isn't present or any detection fails, do nothing on import
        pass
