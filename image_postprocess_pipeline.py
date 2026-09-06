"""Shared post-processing pipeline for generated images.

Used by Streamlit apps:
- Watermark removal via Photoshop automation (Gemini watermark)
- Normalize pins to fixed size (crop to fit)
- Convert to WebP

The goal is to reuse the same logic across apps (unified + bulk).
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".psd"}


@dataclass(frozen=True)
class PhotoshopWatermarkOptions:
    size_or_scale: int = 13
    out_format: str = "PNG"  # PNG|JPEG|AUTO (as supported by photoshop_crop_bottom_right.py)
    mode: str = "scale"  # scale|fixed
    margin_left: int = 25
    margin_bottom: int = 25


@dataclass(frozen=True)
class NormalizeOptions:
    target_w: int = 640
    target_h: int = 1024
    try_match_size: bool = True


@dataclass(frozen=True)
class WebpOptions:
    quality: int = 80
    lossless: bool = False
    keep_metadata: bool = True
    method: int = 6
    overwrite: bool = False


def iter_existing_image_paths(paths: Iterable[str | Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        try:
            pp = Path(p).expanduser()
        except Exception:
            continue
        if pp.exists() and pp.is_file() and pp.suffix.lower() in SUPPORTED_IMAGE_EXTS:
            out.append(pp.resolve())
    # de-dup preserve order
    return list(dict.fromkeys(out))


def remove_watermark_photoshop_batch(
    image_paths: Iterable[str | Path],
    *,
    opts: PhotoshopWatermarkOptions | None = None,
    python_exe: str | None = None,
    script_path: str | Path = "photoshop_crop_bottom_right.py",
    use_direct_import: bool = True,
    wait_timeout_s: float = 120.0,
    wait_poll_s: float = 0.5,
    max_rounds: int = 3,
    chunk_size: int = 25,
) -> tuple[list[Path], str | None]:
    """Run Photoshop watermark removal script for many images.

    Returns (filled_paths, error).

    The script writes next to each source file as: <stem>_filled.(png|jpg)
    depending on `out_format`.
    """
    opts = opts or PhotoshopWatermarkOptions()
    srcs = iter_existing_image_paths(image_paths)
    if not srcs:
        return [], "No input images"

    def _invoke_photoshop(batch_srcs: list[Path]) -> str | None:
        """Invoke Photoshop automation for a batch of sources. Returns error string (or None).

        Reliability-first strategy:
        - Run each source image in its own short-lived helper process with a hard timeout.
        - If Photoshop/COM gets stuck (it happens intermittently), we kill the helper and (optionally)
          restart Photoshop, then continue.

        This avoids the "silent stop" situation where Photoshop stays open but does not proceed.
        """
        direct_err: str | None = None

        def _kill_photoshop_best_effort() -> None:
            if os.name != "nt":
                return
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "taskkill /IM Photoshop.exe /F"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except Exception:
                pass

        if use_direct_import:
            try:
                py = python_exe or sys.executable
                for src in batch_srcs:
                    # One image per helper call (hard timeout => guaranteed forward progress)
                    snippet = (
                        "import time; "
                        "from photoshop_crop_bottom_right import run_photoshop_crop; "
                        f"p={repr(str(src))}; "
                        f"size={float(opts.size_or_scale)}; "
                        f"fmt={repr(str(opts.out_format).upper())}; "
                        f"mode={repr(str(opts.mode))}; "
                        f"ml={int(opts.margin_left)}; "
                        f"mb={int(opts.margin_bottom)}; "
                        "run_photoshop_crop(p, size_or_scale=size, out_suffix='_filled', out_format=fmt, mode=mode, margin_left=ml, margin_bottom=mb, ps=None)"
                    )
                    try:
                        proc = subprocess.run(
                            [py, "-c", snippet],
                            capture_output=True,
                            text=True,
                            timeout=float(wait_timeout_s) + 30.0,
                        )
                        if proc.returncode != 0:
                            # Don't abort the whole batch; record and continue.
                            direct_err = (direct_err + "\n" if direct_err else "") + (proc.stderr or proc.stdout or f"Exit code {proc.returncode}")
                    except subprocess.TimeoutExpired:
                        # Hard recovery: kill Photoshop and continue with next files.
                        _kill_photoshop_best_effort()
                        direct_err = (direct_err + "\n" if direct_err else "") + f"Timeout on {src} (Photoshop restarted)"
                    except Exception as e:
                        direct_err = (direct_err + "\n" if direct_err else "") + str(e)
            except Exception as e:
                direct_err = str(e)

        if (not use_direct_import) or direct_err:
            py = python_exe or sys.executable
            cmd = [
                py,
                str(Path(script_path)),
                *[str(p) for p in batch_srcs],
                str(int(opts.size_or_scale)),
                str(opts.out_format).upper(),
                str(opts.mode),
                str(int(opts.margin_left)),
                str(int(opts.margin_bottom)),
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=float(wait_timeout_s) + 60.0)
            except Exception as e:
                return (direct_err + "\n" if direct_err else "") + str(e)
            if proc.returncode != 0:
                return (direct_err + "\n" if direct_err else "") + f"Photoshop error (code {proc.returncode}): {proc.stderr or proc.stdout}"

        return direct_err

    # Wait for outputs to be written (Photoshop can finish file I/O slightly after COM returns)
    def _expected_out_path(src: Path) -> Path | None:
        fmt = str(opts.out_format).upper()
        if fmt == "AUTO":
            # Must match photoshop_crop_bottom_right.py behavior:
            # - .jpg/.jpeg -> JPEG
            # - everything else (png/webp/tiff/bmp/psd/unknown) -> PNG
            ext = src.suffix.lower()
            if ext in {".jpg", ".jpeg"}:
                fmt = "JPEG"
            else:
                fmt = "PNG"
        if fmt == "PNG":
            return src.with_name(src.stem + "_filled.png")
        if fmt == "JPEG":
            return src.with_name(src.stem + "_filled.jpg")
        return None

    # Fallback candidates pattern (if AUTO/format mismatch or jpeg saved as .jpeg)
    def _scan_candidates(src: Path) -> list[Path]:
        return [
            p
            for p in src.parent.glob(f"{src.stem}_filled*")
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg"}
        ]

    def _wait_for_batch_outputs(batch_srcs: list[Path], timeout_s: float) -> tuple[list[Path], list[Path]]:
        """Return (filled_paths, still_missing_srcs) for a single batch."""
        expected_map: dict[Path, Path | None] = {s: _expected_out_path(s) for s in batch_srcs}
        missing_srcs: set[Path] = set(batch_srcs)
        found: list[Path] = []

        deadline = datetime.now().timestamp() + float(timeout_s)
        while missing_srcs and datetime.now().timestamp() < deadline:
            newly_found: list[tuple[Path, Path]] = []
            for src in list(missing_srcs):
                exp = expected_map.get(src)
                if exp and exp.exists():
                    newly_found.append((src, exp))
                    continue
                for cand in _scan_candidates(src):
                    if cand.exists():
                        newly_found.append((src, cand))
                        break

            for src, outp in newly_found:
                try:
                    if outp.exists():
                        found.append(outp.resolve())
                    if src in missing_srcs:
                        missing_srcs.discard(src)
                except Exception:
                    continue

            if missing_srcs:
                import time
                time.sleep(float(wait_poll_s))

        # de-dup preserve order
        found = list(dict.fromkeys([p for p in found if p.exists()]))
        return found, list(missing_srcs)

    # Run in multiple rounds, retrying only those sources whose *_filled did not appear.
    remaining = list(srcs)
    filled: list[Path] = []
    errors: list[str] = []

    for rnd in range(max(1, int(max_rounds))):
        if not remaining:
            break

        bs = max(1, int(chunk_size))
        batches = [remaining[i : i + bs] for i in range(0, len(remaining), bs)]
        next_remaining: list[Path] = []

        for batch in batches:
            err = _invoke_photoshop(batch)
            if err:
                errors.append(err)

            got, miss_srcs = _wait_for_batch_outputs(batch, float(wait_timeout_s))
            filled.extend(got)
            next_remaining.extend(miss_srcs)

        # Prepare for next round
        remaining = list(dict.fromkeys(next_remaining))

    # De-dup preserve order
    filled = list(dict.fromkeys([p for p in filled if p.exists()]))

    if remaining:
        # Build missing outputs list for error message
        missing_outs: list[Path] = []
        for src in remaining:
            exp = _expected_out_path(src)
            if exp is not None:
                missing_outs.append(exp)
            else:
                # fall back to generic pattern
                missing_outs.extend(_scan_candidates(src))

        miss_txt = "\n".join(str(p) for p in sorted(set(missing_outs)))
        err_txt = "\n".join([e for e in errors if e])
        msg = f"Some *_filled files were not created after retries (rounds={max_rounds}, chunk={chunk_size})."
        if miss_txt:
            msg += f"\nMissing (expected) outputs:\n{miss_txt}"
        if err_txt:
            msg += f"\nPhotoshop invocation errors (may be intermittent):\n{err_txt}"
        return filled, msg

    if errors:
        # Non-fatal: we produced all outputs but had intermittent invocation errors.
        return filled, "\n".join([e for e in errors if e])

    return filled, None


def crop_to_fit(img, tw: int, th: int):
    """Resize and crop to exactly (tw, th) preserving aspect ratio.

    Mirrors the logic used in Tab 4 of app_unified_streamlit.py.
    """
    from PIL import Image  # local import (optional dependency at runtime)

    w, h = img.size
    if w <= 0 or h <= 0:
        return img

    scale = max(tw / w, th / h)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    resized = img.resize((new_w, new_h), Image.LANCZOS)

    left = max(0, (new_w - tw) // 2)
    top = max(0, (new_h - th) // 2)
    right = left + tw
    bottom = top + th
    return resized.crop((left, top, right, bottom))


def _save_match_size(img, out_path: Path, orig_size_bytes: int) -> None:
    """Best-effort size matching for JPEG/WebP; mirrors unified Tab4 approach."""
    import io

    ext = out_path.suffix.lower()

    # Only make sense for JPEG/WebP
    if ext not in (".jpg", ".jpeg", ".webp"):
        # Save as-is for other formats
        if ext == ".png":
            img.save(out_path, format="PNG", optimize=True)
        else:
            img.save(out_path)
        return

    target = max(1, int(orig_size_bytes or 0))
    if target <= 0:
        # Fallback default quality
        if ext in (".jpg", ".jpeg"):
            img.save(out_path, format="JPEG", quality=90, subsampling="4:2:0", optimize=True)
        else:
            img.save(out_path, format="WEBP", quality=80, method=6)
        return

    # Binary search over quality (match app_unified_streamlit Tab4)
    lo, hi = 30, 95
    best: tuple[int, bytes, int] | None = None
    for _ in range(8):
        q = (lo + hi) // 2
        buf = io.BytesIO()
        if ext in (".jpg", ".jpeg"):
            params = dict(format="JPEG", quality=q, subsampling="4:2:0", optimize=True)
        else:
            params = dict(format="WEBP", quality=q, method=6)
        try:
            img.save(buf, **params)
            size = buf.tell()
        except Exception:
            size = 10**12
        best = (q, buf.getvalue(), size)

        if abs(size - target) <= max(1, int(target * 0.10)):
            break
        if size > target:
            hi = q - 1
        else:
            lo = q + 1

    if best is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(best[1])
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path)


def normalize_images_to_dir(
    image_paths: Iterable[str | Path],
    *,
    out_dir: str | Path,
    opts: NormalizeOptions | None = None,
    keep_ext: bool = True,
) -> tuple[list[Path], list[str]]:
    """Normalize (crop-to-fit) images into a directory.

    Returns (out_paths, errors).
    """
    opts = opts or NormalizeOptions()
    from PIL import Image

    outdir = Path(out_dir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    srcs = iter_existing_image_paths(image_paths)
    out_paths: list[Path] = []
    errors: list[str] = []

    for p in srcs:
        try:
            with Image.open(p) as im:
                im = im.convert("RGB") if im.mode not in ("RGB", "RGBA") else im
                out = crop_to_fit(im, int(opts.target_w), int(opts.target_h))

                out_name = p.stem + f"_{int(opts.target_w)}x{int(opts.target_h)}" + (p.suffix if keep_ext else ".png")
                out_path = outdir / out_name

                if opts.try_match_size:
                    try:
                        orig_size = p.stat().st_size
                    except Exception:
                        try:
                            orig_size = len(p.read_bytes())
                        except Exception:
                            orig_size = 0
                    _save_match_size(out, out_path, orig_size)
                else:
                    ext = out_path.suffix.lower()
                    if ext in (".jpg", ".jpeg"):
                        out.save(out_path, format="JPEG", quality=90, subsampling="4:2:0", optimize=True)
                    elif ext == ".png":
                        out.save(out_path, format="PNG", optimize=True)
                    elif ext == ".webp":
                        out.save(out_path, format="WEBP", quality=80, method=6)
                    else:
                        out.save(out_path)

                out_paths.append(out_path)
        except Exception as e:
            errors.append(f"{p}: {e}")

    return out_paths, errors


def convert_images_to_webp_dir(
    image_paths: Iterable[str | Path],
    *,
    out_dir: str | Path,
    opts: WebpOptions | None = None,
) -> tuple[list[Path], list[str]]:
    """Convert images to WebP into a directory (flat output).

    Important:
    - Some inputs can have double extensions like "file.png.png" (e.g. manually renamed).
      Using Path.stem would produce "file.png" and the output becomes "file.png.webp".
      That can confuse downstream tools (including WordPress) and also easily exceed Windows
      filename limits.

    This function derives a safe output basename:
    - strips repeated image extensions from the end
    - truncates to a conservative length
    - ensures uniqueness in the output dir
    """
    opts = opts or WebpOptions()
    from convert_to_webp import convert_image_to_webp

    outdir = Path(out_dir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    srcs = iter_existing_image_paths(image_paths)
    out_paths: list[Path] = []
    errors: list[str] = []

    def _safe_webp_basename(src: Path) -> str:
        name = src.name
        low = name.lower()
        # strip trailing repeated extensions (handles .png.png, .jpg.jpeg.png, etc.)
        exts = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
        changed = True
        while changed:
            changed = False
            for e in exts:
                if low.endswith(e):
                    name = name[: -len(e)]
                    low = name.lower()
                    changed = True
                    break
        base = name.strip().rstrip(".")
        if not base:
            base = src.stem or "image"

        # keep filenames VERY short to avoid server-side upload issues.
        # Some WordPress stacks effectively truncate filenames to 8.3 when handling uploads,
        # which can turn ".webp" into ".web" (and break MIME validation).
        MAX_BASE = 40
        if len(base) > MAX_BASE:
            import hashlib as _hashlib
            h = _hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:8]
            # Keep only a small readable prefix + hash.
            base = (base[:30].rstrip("._- ") + f"_h{h}")
            if len(base) > MAX_BASE:
                base = base[:MAX_BASE].rstrip("._- ")
        if not base:
            base = "image"
        return base

    def _unique_dst(base: str) -> Path:
        cand = outdir / f"{base}.webp"
        if not cand.exists() or opts.overwrite:
            return cand
        for i in range(1, 1000):
            c2 = outdir / f"{base}_{i:02d}.webp"
            if not c2.exists() or opts.overwrite:
                return c2
        # fallback
        return outdir / f"{base}_{datetime.now().strftime('%H%M%S')}.webp"

    for src in srcs:
        base = _safe_webp_basename(src)
        dst = _unique_dst(base)
        if dst.exists() and not opts.overwrite:
            out_paths.append(dst)
            continue
        ok, err = convert_image_to_webp(
            src,
            dst,
            quality=int(opts.quality),
            lossless=bool(opts.lossless),
            keep_metadata=bool(opts.keep_metadata),
            method=int(opts.method),
            icc_profile=bool(opts.keep_metadata),
        )
        if ok:
            out_paths.append(dst)
        else:
            errors.append(err or f"Failed: {src}")

    return out_paths, errors


@dataclass(frozen=True)
class PipelineResult:
    filled: list[Path]
    normalized: list[Path]
    webp: list[Path]
    errors: list[str]


def run_full_pipeline(
    image_paths: Iterable[str | Path],
    *,
    base_out_dir: str | Path,
    ps_opts: PhotoshopWatermarkOptions | None = None,
    norm_opts: NormalizeOptions | None = None,
    webp_opts: WebpOptions | None = None,
    enable_normalize: bool = True,
) -> PipelineResult:
    """Watermark removal -> (optional) normalize -> webp.

    Directory layout:
      base_out_dir/
        normalized_{W}x{H}/  (normalized images)
        webp_{W}x{H}/        (webp from normalized)

    Watermark removal writes *_filled next to original images (as per Photoshop script).
    """
    errors: list[str] = []

    filled, err = remove_watermark_photoshop_batch(image_paths, opts=ps_opts)
    if err:
        errors.append(err)

    # If watermark removal produced nothing, we likely fell back to originals previously.
    # Keep pipeline running, but make it explicit in errors so it's visible in UI.
    if not filled:
        if bool(enable_normalize):
            errors.append("Watermark removal produced 0 '*_filled' files; normalization will use original images.")
        else:
            errors.append("Watermark removal produced 0 '*_filled' files; WebP conversion will use original images (normalize disabled).")

    norm_opts = norm_opts or NormalizeOptions()

    normalized: list[Path] = []
    norm_errs: list[str] = []

    if bool(enable_normalize):
        norm_dir = Path(base_out_dir).expanduser().resolve() / f"normalized_{norm_opts.target_w}x{norm_opts.target_h}"
        normalized, norm_errs = normalize_images_to_dir(filled or image_paths, out_dir=norm_dir, opts=norm_opts)
        errors.extend(norm_errs)
        webp_inputs = normalized
        webp_dir = Path(base_out_dir).expanduser().resolve() / f"webp_{norm_opts.target_w}x{norm_opts.target_h}"
    else:
        # Skip normalize entirely: convert WebP directly from *_filled (or originals if watermark step produced none).
        webp_inputs = filled or iter_existing_image_paths(image_paths)
        webp_dir = Path(base_out_dir).expanduser().resolve() / "webp_no_normalize"

    webp, webp_errs = convert_images_to_webp_dir(webp_inputs, out_dir=webp_dir, opts=webp_opts)
    errors.extend(webp_errs)

    return PipelineResult(filled=filled, normalized=normalized, webp=webp, errors=errors)
