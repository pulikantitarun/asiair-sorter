"""Fast, dependency-light image quality measurements for ASIAIR FITS lights.

The analyser intentionally works on a sampled view of the image.  It is not a
replacement for PixInsight SubframeSelector, but it is fast enough to rank a
large capture library before stacking and does not modify source files.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional

import numpy as np


_DTYPES = {8: ">u1", 16: ">i2", 32: ">i4", 64: ">i8", -32: ">f4", -64: ">f8"}


@dataclass
class QualityResult:
    path: str
    fwhm: float = 0.0
    eccentricity: float = 0.0
    star_count: int = 0
    background: float = 0.0
    noise: float = 0.0
    median_snr: float = 0.0
    elongation: float = 1.0
    score: float = 0.0
    clouds_likely: bool = False
    trailing_likely: bool = False
    poor_frame: bool = False
    sample_step: int = 1
    analysed_at: str = ""
    error: str = ""

    def to_dict(self):
        return asdict(self)


def _parse_value(raw: str):
    raw = raw.strip()
    if raw.startswith("'"):
        return raw[1:].split("'", 1)[0].strip()
    token = raw.split("/", 1)[0].strip()
    if token in ("T", "F"):
        return token == "T"
    try:
        return float(token.replace("D", "E")) if any(c in token.upper() for c in ".ED") else int(token)
    except ValueError:
        return token


def fits_layout(path: Path):
    """Return (header, data_offset, numpy_shape, dtype) for a primary FITS HDU."""
    header = {}
    offset = 0
    found_end = False
    with Path(path).open("rb") as fh:
        while offset < 2880 * 512:
            block = fh.read(2880)
            if len(block) != 2880:
                raise ValueError("Truncated FITS header")
            offset += 2880
            for pos in range(0, 2880, 80):
                card = block[pos:pos + 80].decode("ascii", errors="replace")
                key = card[:8].strip().upper()
                if key == "END":
                    found_end = True
                    break
                if key and card[8:10] == "= ":
                    header[key] = _parse_value(card[10:])
            if found_end:
                break
    if not found_end:
        raise ValueError("FITS END card not found")
    bitpix = int(header.get("BITPIX", 0))
    naxis = int(header.get("NAXIS", 0))
    if bitpix not in _DTYPES or naxis < 2:
        raise ValueError("Unsupported FITS pixel layout")
    axes = [int(header.get(f"NAXIS{i}", 0)) for i in range(1, naxis + 1)]
    if any(v <= 0 for v in axes):
        raise ValueError("Invalid FITS dimensions")
    return header, offset, tuple(reversed(axes)), np.dtype(_DTYPES[bitpix])


def read_fits_sample(path: Path, max_side: int = 1800):
    header, offset, shape, dtype = fits_layout(path)
    pixels = np.memmap(path, mode="r", dtype=dtype, offset=offset, shape=shape)
    while pixels.ndim > 2:
        pixels = pixels[0]
    step = max(1, int(math.ceil(max(pixels.shape) / max_side)))
    sample = np.asarray(pixels[::step, ::step], dtype=np.float32)
    bscale = float(header.get("BSCALE", 1.0) or 1.0)
    bzero = float(header.get("BZERO", 0.0) or 0.0)
    if bscale != 1.0 or bzero:
        sample = sample * bscale + bzero
    return sample, step, header


def _star_metrics(image: np.ndarray, background: float, noise: float, max_stars: int = 600):
    threshold = background + max(5.0 * noise, 1e-6)
    core = image[1:-1, 1:-1]
    peaks = np.isfinite(core) & (core > threshold)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if not (dx == 0 and dy == 0):
                peaks &= core > image[1 + dy:image.shape[0] - 1 + dy,
                                      1 + dx:image.shape[1] - 1 + dx]
    ys, xs = np.nonzero(peaks)
    ys += 1; xs += 1
    total_count = int(len(xs))
    if not total_count:
        return total_count, [], [], []
    brightness = image[ys, xs]
    keep = np.argsort(brightness)[-max_stars:]
    fwhm, ecc, snr = [], [], []
    radius = 5
    for y, x in zip(ys[keep], xs[keep]):
        if y < radius or x < radius or y + radius >= image.shape[0] or x + radius >= image.shape[1]:
            continue
        patch = np.asarray(image[y-radius:y+radius+1, x-radius:x+radius+1], dtype=np.float64)
        weights = np.clip(patch - background, 0, None)
        total = float(weights.sum())
        if total <= max(noise * 20, 1e-6):
            continue
        yy, xx = np.mgrid[-radius:radius+1, -radius:radius+1]
        cx = float((weights * xx).sum() / total); cy = float((weights * yy).sum() / total)
        dx = xx - cx; dy = yy - cy
        cxx = float((weights * dx * dx).sum() / total)
        cyy = float((weights * dy * dy).sum() / total)
        cxy = float((weights * dx * dy).sum() / total)
        trace = cxx + cyy
        disc = max((cxx - cyy) ** 2 + 4 * cxy * cxy, 0.0) ** 0.5
        major = max((trace + disc) / 2, 1e-6)
        minor = max((trace - disc) / 2, 1e-6)
        if major > radius * radius * 0.8:
            continue
        fwhm.append(2.3548 * math.sqrt((major + minor) / 2))
        ecc.append(math.sqrt(max(0.0, 1.0 - minor / major)))
        snr.append((float(patch.max()) - background) / max(noise, 1e-6))
    return total_count, fwhm, ecc, snr


def analyse_fits(path: Path, max_side: int = 1800) -> QualityResult:
    result = QualityResult(path=str(Path(path).resolve()), analysed_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        image, step, _header = read_fits_sample(path, max_side=max_side)
        finite = image[np.isfinite(image)]
        if finite.size < 100:
            raise ValueError("Not enough finite pixels")
        background = float(np.median(finite))
        noise = float(1.4826 * np.median(np.abs(finite - background)))
        count, fwhms, eccs, snrs = _star_metrics(image, background, noise)
        result.sample_step = step
        result.background = background
        result.noise = noise
        result.star_count = count
        result.fwhm = round(float(np.median(fwhms)) * step, 3) if fwhms else 0.0
        result.eccentricity = round(float(np.median(eccs)), 4) if eccs else 0.0
        result.median_snr = round(float(np.median(snrs)), 2) if snrs else 0.0
        result.elongation = round(1.0 / math.sqrt(max(1.0 - result.eccentricity ** 2, 1e-6)), 3)
        # Absolute flags are deliberately conservative. Session-relative cloud
        # and focus-drift flags are refined by the catalogue after multiple
        # frames are available.
        result.clouds_likely = count < 12 or result.median_snr < 6
        result.trailing_likely = result.eccentricity > 0.65 or result.elongation > 1.32
        score = 100.0
        if result.fwhm:
            score -= min(max(result.fwhm - 2.5, 0) * 7.0, 35.0)
        score -= min(max(result.eccentricity - 0.35, 0) * 80.0, 30.0)
        score -= 25.0 if result.clouds_likely else 0.0
        score -= 15.0 if result.trailing_likely else 0.0
        score += min(max(result.median_snr - 10, 0) / 10, 5.0)
        result.score = round(max(0.0, min(100.0, score)), 1)
        result.poor_frame = result.score < 50.0
    except Exception as exc:
        result.error = str(exc)
        result.poor_frame = True
    return result


class QualityBatch:
    def __init__(self):
        import threading
        self._pause = threading.Event()
        self._cancel = threading.Event()

    def pause(self): self._pause.set()
    def resume(self): self._pause.clear()
    def cancel(self): self._cancel.set()

    def run(self, paths, callback: Optional[Callable] = None):
        results = []
        started = time.monotonic()
        paths = list(paths)
        for index, path in enumerate(paths, 1):
            if self._cancel.is_set(): break
            while self._pause.is_set() and not self._cancel.wait(0.2): pass
            if self._cancel.is_set(): break
            result = analyse_fits(path)
            results.append(result)
            if callback:
                elapsed = time.monotonic() - started
                eta = (elapsed / index) * (len(paths) - index) if index else 0
                callback(index, len(paths), path, result, eta)
        return results


def create_thumbnail(source: Path, destination: Path, size=(480, 320)) -> Path:
    """Create a display thumbnail from FITS or a conventional final image."""
    from PIL import Image
    source = Path(source); destination = Path(destination)
    if source.suffix.lower() in (".fit", ".fits", ".fts"):
        image, _step, _header = read_fits_sample(source, max_side=max(size) * 3)
        finite = image[np.isfinite(image)]
        if not finite.size: raise ValueError("No finite image pixels")
        low, high = np.percentile(finite, (1, 99.7))
        scaled = np.clip((image - low) / max(high - low, 1e-9), 0, 1)
        scaled = np.sqrt(scaled)
        pil = Image.fromarray((scaled * 255).astype(np.uint8), mode="L")
    else:
        pil = Image.open(source).convert("RGB")
    pil.thumbnail(size)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pil.save(destination, "PNG")
    return destination
