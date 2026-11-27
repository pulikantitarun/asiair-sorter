#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║         ASIAIR Astrophotography Session Sorter  v3.2         ║
╚══════════════════════════════════════════════════════════════╝

Usage (from source):
    pip install customtkinter matplotlib
    python asiair_sorter.py

Building a standalone Windows exe:
    pip install pyinstaller
    pyinstaller --onefile --windowed --collect-all customtkinter ^
                --collect-all matplotlib --hidden-import numpy ^
                --icon asiair_sorter.ico --name ASIAIR_Sorter ^
                asiair_sorter.py
"""

# ══════════════════════════════════════════════════════════════════════════════
# Auto-install dependencies
# ══════════════════════════════════════════════════════════════════════════════
import sys, subprocess

def _pip(*pkgs):
    if getattr(sys, "frozen", False):
        raise RuntimeError(
            "A bundled dependency could not be loaded. Reinstall ASIAIR Sorter "
            "instead of attempting an in-app package installation."
        )
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q"] + list(pkgs))

try:
    import customtkinter as ctk
except ImportError:
    _pip("customtkinter"); import customtkinter as ctk

try:
    import numpy as np
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    import matplotlib.gridspec as gridspec
    from matplotlib.widgets import SpanSelector
    from matplotlib.patches import Circle
    HAS_MPL = True
except ImportError:
    try:
        _pip("matplotlib", "numpy")
        import numpy as np
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        import matplotlib.gridspec as gridspec
        from matplotlib.widgets import SpanSelector
        from matplotlib.patches import Circle
        HAS_MPL = True
    except Exception:
        HAS_MPL = False

# ── Standard library ──────────────────────────────────────────────────────────
import json, os, re, shutil, threading, tkinter as tk
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
from typing import List, Optional
from PIL import Image

from astro_catalog import (
    Catalog, ScanController, SourceWatcher, merged_metadata, phd2_quality_score, sha256_file,
)
from image_quality import QualityBatch, create_thumbnail
from device_automation import discover_asiair_sources, eject_source

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS — edit here to customise output folder names
# ══════════════════════════════════════════════════════════════════════════════
SESSIONS_ROOT   = "Sessions"
LIGHTS_FOLDER   = "Lights"
DARKS_FOLDER    = "Darks"
FLATS_FOLDER    = "Flats"
BIAS_FOLDER     = "Bias"
PHD2_FOLDER     = "PHD2_Logs"

IMAGING_EXTENSIONS = frozenset({".fits", ".fit", ".fts"})
PREVIEW_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff"})
PHD2_EXTENSIONS    = frozenset({".log", ".txt"})
KNOWN_FILTERS      = ["OIII", "SII", "Hb", "Ha", "Lum", "RGB", "H", "O", "S", "R", "G", "B", "L"]

CONFIG_PATH = Path.home() / ".asiair_sorter_config.json"
_DATE_RE    = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?=[-_])")
_DMY_DATE_RE = re.compile(r"(?<!\d)(\d{2})-(\d{2})-(20\d{2})(?!\d)")
_LIGHT_TARGET_RE = re.compile(
    r"^Light_(?P<target>.+?)_\d+(?:\.\d+)?s_Bin\d+(?:_|$)",
    re.IGNORECASE,
)
_LIGHT_CAMERA_RE = re.compile(
    r"^Light_.+?_\d+(?:\.\d+)?s_Bin\d+_(?P<middle>.+?)_gain[-+]?\d+",
    re.IGNORECASE,
)
_INVALID_FOLDER_CHARS_RE = re.compile(r'[<>:"/\\|?*]')
VERSION     = "3.2"

# ── UI palette ────────────────────────────────────────────────────────────────
BG_APP     = "#14191b"
BG_CARD    = "#1c2426"
BG_INPUT   = "#111719"
BG_LOG     = "#111719"
FG_TEXT    = "#edf0eb"
FG_MUTED   = "#a1aeaa"
BORDER     = "#344145"
ACCENT     = "#287564"
ACCENT_HL  = "#328672"
FG_SIGNAL  = "#a4dfbb"
BTN_MOVE   = "#2b3639"
BTN_CANCEL = "#6e2b2b"

_C = {                          # log text tag colours
    "hdr":     "#ce93d8",
    "info":    "#79c0ff",
    "ok":      "#56d364",
    "dryrun":  "#e3b341",
    "del":     "#f78166",
    "skip":    "#6e7681",
    "err":     "#ff7b72",
    "summary": "#58a6ff",
    "bold":    "#e6edf3",
    "mono_dim": "#6e7681",
}

MC = {                          # matplotlib chart colours
    "fig":   BG_APP,
    "ax":    BG_INPUT,
    "grid":  BORDER,
    "tick":  FG_MUTED,
    "text":  FG_MUTED,
    "ra":    FG_SIGNAL,
    "dec":   "#f0a187",
    "snr":   "#e3b341",
    "dither":"#30363d",
    "pause": BG_CARD,
}

# ══════════════════════════════════════════════════════════════════════════════
# File classification logic
# ══════════════════════════════════════════════════════════════════════════════

def _rel_parts(fp: Path, root: Path) -> list:
    try: rel = fp.relative_to(root)
    except ValueError: rel = fp
    return list(rel.parts)[:-1]

def _find_date(parts: list, filename: str) -> str:
    # The capture timestamp in an ASIAIR filename is more precise than a
    # containing folder and avoids assigning overnight files to today's date.
    compact = _COMPACT_DATE_RE.search(filename)
    if compact:
        return f"{compact.group(1)}-{compact.group(2)}-{compact.group(3)}"
    for seg in reversed(parts):
        m = _DATE_RE.search(seg)
        if m: return m.group(1)
        dmy = _DMY_DATE_RE.search(seg)
        if dmy: return f"{dmy.group(3)}-{dmy.group(2)}-{dmy.group(1)}"
    m = _DATE_RE.search(filename)
    return m.group(1) if m else datetime.now().strftime("%Y-%m-%d")

def _safe_folder_name(value: str):
    """Return a Windows-safe target folder name without changing its identity."""
    if value is None:
        return None
    value = str(value)
    value = _INVALID_FOLDER_CHARS_RE.sub("_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        return None
    # Avoid Windows device names such as CON, PRN, COM1, and LPT1.
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", value):
        value += "_"
    return value[:120].rstrip(" .") or None

def _find_target_from_filename(filename: str):
    """Extract the ASIAIR target from Light_<target>_<exposure>s_Bin... names."""
    match = _LIGHT_TARGET_RE.match(Path(filename).stem)
    if not match:
        return None
    return _safe_folder_name(match.group("target"))

def _find_target(parts: list, filename: str = ""):
    # ASIAIR writes the target into each light filename. Prefer that value so
    # mixed-target source folders cannot collapse into one destination Lights
    # folder. Older layouts still fall back to Autorun/<target>.
    from_filename = _find_target_from_filename(filename)
    if from_filename:
        return from_filename
    for i, p in enumerate(parts):
        if p.lower() == "autorun" and i + 1 < len(parts):
            c = parts[i + 1]
            if not _DATE_RE.match(c): return _safe_folder_name(c)
    return None

def _find_camera_and_filter_from_filename(filename: str):
    """Return camera and optional filter from ASIAIR light filenames."""
    match = _LIGHT_CAMERA_RE.match(Path(filename).stem)
    if not match: return None, None
    pieces = match.group("middle").split("_")
    filter_name = None
    if len(pieces) > 1:
        for known in KNOWN_FILTERS:
            if pieces[-1].lower() == known.lower():
                filter_name = known
                pieces.pop()
                break
    camera = _safe_folder_name("_".join(pieces))
    return camera, filter_name

def _find_frame_type(parts: list) -> str:
    _map = {"light":"Light","lights":"Light","dark":"Dark","darks":"Dark",
            "flat":"Flat","flats":"Flat","bias":"Bias","offset":"Bias"}
    for p in parts:
        ft = _map.get(p.lower())
        if ft: return ft
    return "Light"

def _find_filter(parts: list):
    for p in parts:
        for f in KNOWN_FILTERS:
            if p.lower() == f.lower(): return f
    return None

def classify_file(fp: Path, src_root: Path, metadata=None):
    metadata = metadata or {}
    parts    = _rel_parts(fp, src_root)
    filename = fp.name
    ext      = fp.suffix.lower()
    if ext in PREVIEW_EXTENSIONS:
        return {"action":"delete_preview","frame_type":None,
                "date":_find_date(parts,filename),"target":None,"filter_name":None}
    if ext in PHD2_EXTENSIONS:
        fn = filename.lower()
        in_phd = any(p.lower() in ("phd2","phd") for p in parts)
        if in_phd or "phd" in fn or "guidelog" in fn:
            return {"action":"copy_phd2","frame_type":"PHD2",
                    "date":_find_date(parts,filename),"target":None,"filter_name":None}
        return None
    if ext in IMAGING_EXTENSIONS:
        embedded_type = str(metadata.get("frame_type") or "").lower()
        ft = next((name for name in ("Light", "Dark", "Flat", "Bias")
                   if name.lower() in embedded_type), _find_frame_type(parts))
        camera, filename_filter = _find_camera_and_filter_from_filename(filename) if ft=="Light" else (None,None)
        return {"action":"copy_fits","frame_type":ft,
                "date":metadata.get("capture_date") or _find_date(parts,filename),
                "target":(_safe_folder_name(metadata.get("target")) or
                          _find_target(parts, filename)) if ft=="Light" else None,
                "camera":_safe_folder_name(metadata.get("camera")) or camera,
                "filter_name":(_safe_folder_name(metadata.get("filter")) or filename_filter or
                               (_find_filter(parts) if ft=="Light" else None)),
                "exposure":metadata.get("exposure"), "gain":metadata.get("gain"),
                "temperature":metadata.get("temperature"), "binning":metadata.get("binning")}
    return None

def build_dest_path(info: dict, dst_root: Path, filename: str) -> Path:
    base = dst_root / SESSIONS_ROOT / info["date"]
    if info["action"] == "copy_phd2":
        return base / PHD2_FOLDER / filename
    if info["frame_type"] == "Light":
        base = base / (info.get("camera") or "Unknown Camera")
        base = base / (info.get("target") or "Unknown Target")
    elif info.get("target"):
        base = base / info["target"]
    folder_map = {"Light":LIGHTS_FOLDER,"Dark":DARKS_FOLDER,
                  "Flat":FLATS_FOLDER,"Bias":BIAS_FOLDER}
    base = base / folder_map.get(info["frame_type"], info["frame_type"])
    if info["frame_type"] == "Light" and info["filter_name"]:
        base = base / info["filter_name"]
    return base / filename

def resolve_conflict(path: Path) -> Path:
    if not path.exists(): return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    n = 1
    while True:
        c = parent / f"{stem}_{n}{suffix}";
        if not c.exists(): return c
        n += 1

# ── Config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception: pass
    return {}

def save_config(cfg: dict):
    try: CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception: pass

# ── Session summary ───────────────────────────────────────────────────────────
def write_session_summary(session_dir: Path, date: str, target,
                           camera,
                           entries: list, action: str):
    by_type: dict = {}
    for info, src, dst in entries:
        k = (info["frame_type"] or "Other", info["filter_name"] or "")
        by_type.setdefault(k, []).append((src, dst))
    p = session_dir / "session_summary.txt"
    with p.open("w", encoding="utf-8") as f:
        f.write("ASIAIR Session Summary\n" + "="*44 + "\n")
        f.write(f"Generated : {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write(f"Date      : {date}\n")
        if camera: f.write(f"Camera    : {camera}\n")
        if target: f.write(f"Target    : {target}\n")
        f.write(f"Operation : {action}\n")
        for (ft, fn), files in sorted(by_type.items()):
            lbl = ft + (f" ({fn})" if fn else "")
            f.write(f"\n{lbl}: {len(files)} file(s)\n")
            for src, _ in files: f.write(f"  {Path(src).name}\n")

# ══════════════════════════════════════════════════════════════════════════════
# PHD2 Log Parser
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PHD2Session:
    source_path: str = ""
    start_time: str = ""
    end_time:   str = ""
    phd_version: str = ""
    profile:    str = ""
    camera:     str = ""
    mount:      str = ""
    aux_mount:  str = ""
    focal_mm:   Optional[float] = None
    px_scale:   Optional[float] = None     # arcsec/px
    exposure_s: Optional[float] = None
    units:      str = "arcsec"             # or "px"
    sky_position: str = ""
    lock_position: str = ""
    header: dict = field(default_factory=dict)
    algorithms: dict = field(default_factory=dict)
    events: list = field(default_factory=list)

    times:     list = field(default_factory=list)
    ra_err:    list = field(default_factory=list)
    dec_err:   list = field(default_factory=list)
    snr:       list = field(default_factory=list)
    star_mass: list = field(default_factory=list)
    hfd:       list = field(default_factory=list)
    avg_dist:  list = field(default_factory=list)
    ra_pulse:  list = field(default_factory=list)
    dec_pulse: list = field(default_factory=list)
    ra_dir:    list = field(default_factory=list)
    dec_dir:   list = field(default_factory=list)
    error_codes: list = field(default_factory=list)
    dithers:   list = field(default_factory=list)   # times of dither events (sec)
    settle_spans: list = field(default_factory=list)
    _settle_after_frame: Optional[int] = None

    def finalise(self):
        if not HAS_MPL: return
        self.times    = np.array(self.times,   dtype=float)
        self.ra_err   = np.array(self.ra_err,  dtype=float)
        self.dec_err  = np.array(self.dec_err, dtype=float)
        self.snr      = np.array(self.snr,     dtype=float) if self.snr     else np.array([])
        self.star_mass= np.array(self.star_mass,dtype=float) if self.star_mass else np.array([])
        self.hfd      = np.array(self.hfd,     dtype=float) if self.hfd     else np.array([])
        self.avg_dist = np.array(self.avg_dist,dtype=float) if self.avg_dist else np.array([])
        self.ra_pulse = np.array(self.ra_pulse,dtype=float) if self.ra_pulse else np.array([])
        self.dec_pulse= np.array(self.dec_pulse,dtype=float) if self.dec_pulse else np.array([])
        self.error_codes = np.array(self.error_codes,dtype=int) if self.error_codes else np.array([],dtype=int)
        self.dithers  = np.array(self.dithers, dtype=float) if self.dithers  else np.array([])

    @property
    def n_frames(self) -> int: return len(self.times)

    @property
    def duration_min(self) -> float:
        if len(self.times) < 2: return 0
        return float((self.times[-1] - self.times[0]) / 60)

    @property
    def rms_ra(self) -> float:
        return float(np.sqrt(np.mean(self.ra_err**2))) if len(self.ra_err) else 0

    @property
    def rms_dec(self) -> float:
        return float(np.sqrt(np.mean(self.dec_err**2))) if len(self.dec_err) else 0

    @property
    def rms_total(self) -> float:
        if not len(self.ra_err): return 0
        return float(np.sqrt(np.mean(self.ra_err**2 + self.dec_err**2)))

    @property
    def peak_err(self) -> float:
        if not len(self.ra_err): return 0
        return float(np.max(np.sqrt(self.ra_err**2 + self.dec_err**2)))

    @property
    def median_err(self) -> float:
        if not len(self.ra_err): return 0
        return float(np.median(np.sqrt(self.ra_err**2 + self.dec_err**2)))

    @property
    def p95_err(self) -> float:
        if not len(self.ra_err): return 0
        return float(np.percentile(np.sqrt(self.ra_err**2 + self.dec_err**2), 95))

    @property
    def cadence_s(self) -> float:
        return float(np.median(np.diff(self.times))) if len(self.times) > 1 else 0

    @property
    def lost_frames(self) -> int:
        return int(np.count_nonzero(self.error_codes)) if len(self.error_codes) else 0

    def drift_per_min(self, axis: str) -> float:
        values = self.ra_err if axis == "ra" else self.dec_err
        if len(values) < 2 or self.duration_min <= 0: return 0
        return float(np.polyfit((self.times-self.times[0])/60, values, 1)[0])


PHD2_ERROR_LABELS = {
    0: "No error", 1: "Saturated star", 2: "Low SNR",
    3: "Low star mass", 4: "HFD below minimum", 5: "HFD above maximum",
    6: "Star near frame edge", 7: "Star mass changed", 8: "Unexpected error",
}

def _number(text: str):
    m = re.search(r"[-+]?\d+(?:\.\d+)?", text or "")
    return float(m.group(0)) if m else None


def parse_phd2_log(path: str) -> List[PHD2Session]:
    """Return a list of PHD2Session objects (one per Guiding Begins block)."""
    sessions: List[PHD2Session] = []
    cur: Optional[PHD2Session]  = None
    in_data   = False
    col: dict = {}
    use_arcsec = True
    file_version = ""

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line: continue
            lo = line.lower()

            if "phd2 version" in lo:
                file_version = line

            # ── New guiding block ─────────────────────────────────────
            if "guiding begins" in lo or "guiding_begins" in lo:
                cur = PHD2Session(source_path=str(path), phd_version=file_version)
                tm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})", line)
                if tm: cur.start_time = f"{tm.group(1)} {tm.group(2)}"
                else:
                    m = _DATE_RE.search(line)
                    if m: cur.start_time = m.group(1)
                sessions.append(cur)
                in_data = False; col = {}
                continue

            if cur is None: continue

            # ── Header metadata ───────────────────────────────────────
            if "=" in line and not in_data:
                k, _, v = line.partition("=")
                k, v = k.strip().lower(), v.strip()
                cur.header[k] = v
                if "equipment profile" in k: cur.profile  = v
                elif k == "camera":          cur.camera   = v
                elif k == "mount":           cur.mount    = v
                elif "aux mount" in k:       cur.aux_mount= v
                elif "focal length" in k:
                    cur.focal_mm = _number(v)
                elif "image scale" in k or "pixel scale" in k:
                    cur.px_scale = _number(v)
                elif "exposure" in k:
                    val = _number(v)
                    if val is not None:
                        cur.exposure_s = val / 1000 if "ms" in v.lower() else val
                if "guide algorithm" in k or any(
                    token in k for token in ("aggression", "minimum move", "hysteresis",
                                              "max ra duration", "max dec duration",
                                              "dec guide mode", "backlash")
                ):
                    cur.algorithms[k] = v
                continue

            if not in_data and lo.startswith("ra ="):
                cur.sky_position = line
                continue
            if not in_data and lo.startswith("lock position"):
                cur.lock_position = line
                continue

            # ── Column header ─────────────────────────────────────────
            if line.startswith("Frame,"):
                in_data = True
                cols = [c.strip() for c in line.split(",")]
                col  = {name: i for i, name in enumerate(cols)}
                use_arcsec = "RARawError" in col and "DECRawError" in col
                cur.units  = "arcsec" if use_arcsec or cur.px_scale else "px"
                continue

            # ── End of guiding block ──────────────────────────────────
            if "guiding ends" in lo or "guiding_ends" in lo:
                in_data = False
                tm = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", line)
                if tm and cur: cur.end_time = tm.group(1)
                continue

            # ── Session events ────────────────────────────────────────
            if in_data and cur and not line.split(",",1)[0].strip().isdigit():
                cur.events.append(line)
                if "settling state change" in lo:
                    if "settling started" in lo:
                        cur._settle_after_frame = len(cur.times)
                    elif "settling complete" in lo or "settling failed" in lo:
                        first = cur._settle_after_frame
                        if first is not None and first < len(cur.times):
                            cur.settle_spans.append((first, len(cur.times)))
                        cur._settle_after_frame = None
            if in_data and cur and "dither" in lo:
                if cur.times:
                    cur.dithers.append(cur.times[-1])
                continue

            # ── Data row ──────────────────────────────────────────────
            if in_data and cur:
                parts = line.split(",")
                if not parts[0].strip().isdigit(): continue

                def _f(name, alt=None):
                    for n in ([name, alt] if alt else [name]):
                        if n and n in col:
                            idx = col[n]
                            if idx < len(parts):
                                try: return float(parts[idx])
                                except ValueError: pass
                    return None

                def _f_any(*names):
                    for name in names:
                        value = _f(name)
                        if value is not None: return value
                    return None

                def _s(name):
                    if name in col and col[name] < len(parts):
                        return parts[col[name]].strip().strip('"')
                    return ""

                t = _f("Time")
                if t is None: continue
                ra  = _f_any("RARawError", "RARawDistance", "dx")
                dec = _f_any("DECRawError", "DECRawDistance", "dy")
                if ra is None or dec is None: continue
                if not use_arcsec and cur.px_scale:
                    ra *= cur.px_scale
                    dec *= cur.px_scale

                cur.times.append(t)
                cur.ra_err.append(ra)
                cur.dec_err.append(dec)
                snr = _f("SNR")
                cur.snr.append(snr if snr is not None else float("nan"))
                mass = _f("StarMass")
                cur.star_mass.append(mass if mass is not None else float("nan"))
                hfd = _f("HFD")
                cur.hfd.append(hfd if hfd is not None else float("nan"))
                ad = _f("Avg Dist")
                cur.avg_dist.append(ad if ad is not None else float("nan"))
                cur.ra_pulse.append(_f("RADuration") or 0)
                cur.dec_pulse.append(_f("DECDuration") or 0)
                cur.ra_dir.append(_s("RADirection"))
                cur.dec_dir.append(_s("DECDirection"))
                cur.error_codes.append(int(_f("ErrorCode") or 0))

    result = []
    for s in sessions:
        if s.times:
            s.finalise()
            result.append(s)
    return result


def discover_phd2_logs(root: Path, include_debug=True) -> List[Path]:
    """Find PHD2 guide/debug logs below an ASIAIR source or archive folder."""
    if not root or not root.is_dir(): return []
    found = []
    for rd, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            lo = name.lower()
            if not lo.endswith((".txt", ".log")): continue
            is_guide = "guidelog" in lo or "guide_log" in lo
            is_debug = "debuglog" in lo or "debug_log" in lo
            in_phd = "phd2" in str(Path(rd)).lower()
            if is_guide or (include_debug and is_debug) or (in_phd and "guide" in lo):
                found.append(Path(rd) / name)
    return sorted(found, key=lambda p: (p.stat().st_mtime, str(p)), reverse=True)


def _phd2_log_date(path: Path) -> str:
    match = _DATE_RE.search(str(path))
    if match: return match.group(1)
    compact = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", path.name)
    if compact: return f"{compact.group(1)}-{compact.group(2)}-{compact.group(3)}"
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


def sync_phd2_logs(source: Path, destination: Path):
    """Copy new/changed PHD2 logs into the archive without making duplicates."""
    copied, updated, unchanged, errors = [], [], [], []
    for src in discover_phd2_logs(source, include_debug=True):
        date = _phd2_log_date(src)
        dst = destination / SESSIONS_ROOT / date / PHD2_FOLDER / src.name
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            exists = dst.exists()
            same = (exists and src.stat().st_size == dst.stat().st_size and
                    src.stat().st_mtime <= dst.stat().st_mtime + 0.001)
            if same:
                unchanged.append(dst)
                continue
            shutil.copy2(str(src), str(dst))
            (updated if exists else copied).append(dst)
        except OSError as exc:
            errors.append((src, str(exc)))
    return {"copied":copied, "updated":updated, "unchanged":unchanged, "errors":errors}

# ══════════════════════════════════════════════════════════════════════════════
# PHD2 Viewer UI
# ══════════════════════════════════════════════════════════════════════════════

def _rms_colour(rms: float) -> str:
    if rms == 0:   return FG_MUTED
    if rms < 0.5:  return "#56d364"   # green  — excellent
    if rms < 1.0:  return "#e3b341"   # amber  — good
    if rms < 2.0:  return "#f78166"   # orange — fair
    return "#ff7b72"                   # red    — poor

def _style_ax(ax, ylabel=""):
    ax.set_facecolor(MC["ax"])
    ax.tick_params(colors=MC["tick"], which="both", labelsize=8)
    for sp in ax.spines.values(): sp.set_color(MC["grid"])
    ax.grid(True, color=MC["grid"], linewidth=0.5, alpha=0.9)
    ax.set_ylabel(ylabel, color=MC["text"], fontsize=9)
    ax.yaxis.label.set_color(MC["text"])


class StatBox(ctk.CTkFrame):
    def __init__(self, parent, label: str, **kw):
        super().__init__(parent, fg_color=BG_INPUT, corner_radius=8, **kw)
        ctk.CTkLabel(self, text=label, font=ctk.CTkFont(size=10),
                     text_color=FG_MUTED).pack(pady=(8,0), padx=10)
        self._v = ctk.CTkLabel(self, text="—",
                                font=ctk.CTkFont(size=15, weight="bold"),
                                text_color=FG_MUTED)
        self._v.pack(pady=(1,8), padx=10)

    def set(self, val: str, colour: str = FG_TEXT):
        self._v.configure(text=val, text_color=colour)


def _guide_window_mask(times, ra, dec, good, bounds):
    return (good & np.isfinite(ra) & np.isfinite(dec)
            & (times >= bounds[0]) & (times <= bounds[1]))


def _guide_window_rms(times, ra, dec, good, bounds):
    valid = _guide_window_mask(times, ra, dec, good, bounds)
    count = int(np.count_nonzero(valid))
    if not count:
        return 0, None
    ra2, dec2 = np.mean(ra[valid] ** 2), np.mean(dec[valid] ** 2)
    return count, tuple(np.sqrt([ra2, dec2, ra2 + dec2]))


def _guide_event_groups(times, bounds, bins=64):
    visible = np.asarray(times, dtype=float)
    visible = visible[(visible >= bounds[0]) & (visible <= bounds[1])]
    span = bounds[1] - bounds[0]
    if not visible.size or span <= 0:
        return []
    indices = np.minimum((bins * (visible - bounds[0]) / span).astype(int), bins - 1)
    return [(float(points.mean()), len(points)) for i in np.unique(indices)
            for points in [visible[indices == i]]]


def _next_guiding_check(sessions):
    """Rank one low-risk check. Thresholds flag patterns, not mount faults."""
    errors = measured_frames = 0
    timeout_count = 0
    valid_time_s = 0.0
    pairs = []
    units = {s.units for s in sessions if s.n_frames}
    for s in sessions:
        if not s.n_frames:
            continue
        codes = np.asarray(s.error_codes)
        good = np.ones(s.n_frames, dtype=bool)
        if len(codes) == s.n_frames:
            good = codes == 0
            errors += int(np.count_nonzero(~good))
            measured_frames += s.n_frames
        for first, stop in s.settle_spans:
            good[first:stop] = False
        timeout_count += sum("settl" in line.lower() and
                             bool(re.search(r"timed\s*out|time[- ]?out", line, re.I))
                             for line in s.events)
        ra, dec = np.asarray(s.ra_err), np.asarray(s.dec_err)
        good &= np.isfinite(ra) & np.isfinite(dec)
        if np.any(good):
            pairs.append(np.column_stack((ra[good], dec[good])))
        if len(s.times) > 1:
            steps = np.diff(s.times)
            adjacent = good[:-1] & good[1:] & (steps > 0)
            if np.any(steps > 0):
                typical_step = np.median(steps[steps > 0])
                valid_time_s += float(np.sum(steps[adjacent & (steps <= 3 * typical_step)]))
    if measured_frames and errors >= 5 and errors / measured_frames >= .01:
        return (
            "Check guide-star reliability first",
            f"{errors:,} of {measured_frames:,} guide frames reported errors ({errors / measured_frames:.1%}).",
            "Before touching mount settings, inspect guide-camera focus and star SNR. Check the Events tab for "
            "the recorded reasons and compare the affected times with clouds or obstructions.",
            "Run a short comparison on the same target with the same guiding settings. Look for fewer frame errors "
            "and steadier SNR. Only change one thing between runs.",
        )
    if timeout_count:
        return (
            "Check what happens after a dither",
            f"The log recorded {timeout_count:,} settling timeout message(s).",
            "Watch a few dithers in a short test run. Use Graphs to see which axis takes longer to return, "
            "and check whether capture resumes before it has settled. Don't loosen the timeout as the first fix.",
            "Compare a few consecutive dithers under similar conditions. Check that guiding settles before "
            "the next exposure and inspect the first light after each dither for trailing.",
        )
    count = sum(len(pair) for pair in pairs)
    if count < 120 or valid_time_s < 300:
        return (
            "Record a longer baseline before tuning",
            f"Only {count:,} usable samples across {valid_time_s / 60:.1f} minutes of adjacent valid frames.",
            "Keep your current settings. Record at least five minutes of steady guiding on the next target, "
            "with no dithers during the baseline segment.",
            "Load the new guide log and compare Next Session with how round the stars look in your lights.",
        )
    if len(units) == 1:
        ra, dec = np.sqrt(np.mean(np.concatenate(pairs) ** 2, axis=0))
        unit = "″" if "arcsec" in units else " px"
        evidence = (f"RA {ra:.2f}{unit}, Dec {dec:.2f}{unit} RMS across {count:,} valid frames. "
                    f"{errors:,} recorded frame errors; logged settling excluded from RMS.")
        if max(ra, dec) >= 1.4 * min(ra, dec) and abs(ra - dec) >= .10:
            axis = "RA" if ra > dec else "Dec"
            check = ("measure unguided RA movement. Check for cable drag or wind separately" if axis == "RA" else
                     "measure Dec drift and, if supported by your mount, check for backlash")
            return (
                f"Check {axis} first — don't tune both axes",
                evidence,
                f"Run PHD2 Guiding Assistant to {check}. This log shows an axis imbalance, not its cause. "
                "Keep aggressiveness, exposure and other settings unchanged until you have a baseline.",
                f"Compare {axis} RMS in two similar steady windows and inspect star shape in the lights. "
                "Keep any change only if the comparison improves.",
            )
    else:
        evidence = f"{count:,} valid frames; axis comparison skipped because the log mixes units."
    return (
        "Keep your settings for the next run",
        evidence,
        "This log doesn't flag a clear first correction. Check a few full-resolution lights for trailing "
        "before changing guiding settings; a low RMS alone doesn't tell you how the images turned out.",
        "Keep the same guiding profile and save a comparable baseline. If the lights show trailing, compare "
        "their capture times with Graphs and Star Plot before changing one thing.",
    )


class PHD2ViewerFrame(ctk.CTkFrame):
    def __init__(self, parent, catalog=None):
        super().__init__(parent, fg_color="transparent")
        self.catalog = catalog or Catalog()
        self._sessions: List[PHD2Session] = []
        self._library_paths = {}
        self._syncing = False
        self._auto_sync_var = tk.BooleanVar(value=True)
        self._fig = self._canvas = self._toolbar = None
        self._ax_err = self._ax_snr = None
        self._guide_data = None
        self._range_select = None
        self._build_ui()
        self.after(250, self._initialise_library)
        self.after(60000, self._auto_sync_tick)

    # ── Build ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Automatic log library / sync controls
        picker = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=10)
        picker.pack(fill="x", pady=(0,8))
        row = ctk.CTkFrame(picker, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=12)
        row.columnconfigure(1, weight=1)

        ctk.CTkLabel(row, text="PHD2 Log Library:",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=FG_TEXT).grid(row=0, column=0, sticky="w", padx=(0,10))

        self._log_var = tk.StringVar(value="No archived guide logs found")
        self._log_combo = ctk.CTkComboBox(
            row, variable=self._log_var, values=["No archived guide logs found"],
            fg_color=BG_INPUT, border_color=BORDER, text_color=FG_TEXT,
            button_color=ACCENT, button_hover_color=ACCENT_HL,
            height=34, command=lambda _value: self._load())
        self._log_combo.grid(row=0, column=1, sticky="ew", padx=(0,8))

        ctk.CTkButton(row, text="Sync Logs", width=84, height=34,
                      command=self._sync_logs).grid(row=0, column=2, padx=(0,6))
        ctk.CTkButton(row, text="Refresh", width=72, height=34,
                      command=lambda: self.refresh_library()).grid(row=0, column=3, padx=(0,6))
        ctk.CTkButton(row, text="Browse…", width=72, height=34,
                      command=self._browse).grid(row=0, column=4, padx=(0,6))
        ctk.CTkButton(row, text="Load", width=64, height=34,
                      fg_color=ACCENT, hover_color=ACCENT_HL,
                      command=self._load).grid(row=0, column=5)

        self._sync_lbl = ctk.CTkLabel(
            picker, text="Logs are copied from the remembered ASIAIR source into the archive.",
            font=ctk.CTkFont(size=10), text_color=FG_MUTED, anchor="w")
        self._sync_lbl.pack(side="left", fill="x", expand=True, padx=(16,8), pady=(0,8))
        ctk.CTkCheckBox(picker, text="Auto-sync every 60s",
                        variable=self._auto_sync_var, width=150,
                        font=ctk.CTkFont(size=10), text_color=FG_MUTED,
                        border_color=BORDER).pack(side="right", padx=(0,16), pady=(0,8))

        # Stats bar
        stats = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=10)
        stats.pack(fill="x", pady=(0,8))
        si = ctk.CTkFrame(stats, fg_color="transparent")
        si.pack(fill="x", padx=16, pady=10)

        self._sb = {}
        labels = ["Quality","RA RMS","Dec RMS","Total RMS","P95 Error","Peak Error","Duration",
                  "Frames","Cadence","Dithers","Lost Frames","Avg SNR","Avg HFD"]
        for i, lbl in enumerate(labels):
            sb = StatBox(si, lbl)
            sb.grid(row=i//7, column=i%7, padx=4, pady=3, sticky="ew")
            si.columnconfigure(i%7, weight=1)
            self._sb[lbl] = sb

        # Equipment / session info
        self._info_lbl = ctk.CTkLabel(self, text="",
                                       font=ctk.CTkFont(size=11, slant="italic"),
                                       text_color=FG_MUTED, anchor="w")
        self._info_lbl.pack(fill="x", padx=6, pady=(0,4))

        # Graph, complete details, and raw event views
        detail_tabs = ctk.CTkTabview(self, fg_color=BG_CARD, command=self._guiding_tab_changed,
                                     segmented_button_fg_color=BG_INPUT,
                                     segmented_button_selected_color=ACCENT)
        detail_tabs.pack(fill="both", expand=True)
        self._guiding_tabs = detail_tabs
        detail_tabs.add("Graphs"); detail_tabs.add("Star Plot")
        detail_tabs.add("Next Session")
        detail_tabs.add("Details"); detail_tabs.add("Events")
        self._build_next_session(detail_tabs.tab("Next Session"))
        self._graph_card = ctk.CTkFrame(detail_tabs.tab("Graphs"), fg_color=BG_CARD,
                                        corner_radius=10)
        self._graph_card.pack(fill="both", expand=True)
        self._details_box = ctk.CTkTextbox(detail_tabs.tab("Details"), fg_color=BG_LOG,
                                            text_color=FG_TEXT, font=("Consolas",11))
        self._details_box.pack(fill="both", expand=True, padx=4, pady=4)
        self._events_box = ctk.CTkTextbox(detail_tabs.tab("Events"), fg_color=BG_LOG,
                                           text_color=FG_TEXT, font=("Consolas",10))
        self._events_box.pack(fill="both", expand=True, padx=4, pady=4)

        if HAS_MPL:
            self._init_figure()
            self._init_star_plot(detail_tabs.tab("Star Plot"))
        else:
            ctk.CTkLabel(detail_tabs.tab("Star Plot"), text="Star Plot requires Matplotlib.").pack(pady=20)
            ctk.CTkLabel(
                self._graph_card,
                text="Install matplotlib to enable PHD2 graphs:\n\n"
                     "  pip install matplotlib",
                font=ctk.CTkFont(size=13), text_color=FG_MUTED,
            ).place(relx=0.5, rely=0.5, anchor="center")

    def _build_next_session(self, tab):
        body = ctk.CTkScrollableFrame(tab, fg_color=BG_INPUT, corner_radius=8)
        body.pack(fill="both", expand=True, padx=6, pady=5)
        self._next_labels = []
        for index, heading in enumerate(("START HERE", "WHY THIS CHECK", "WHAT TO DO", "HOW TO TELL IF IT HELPED")):
            ctk.CTkLabel(body, text=heading, text_color=FG_SIGNAL, anchor="w",
                         font=ctk.CTkFont(size=11, weight="bold")).pack(fill="x", padx=14, pady=(9,0))
            label = ctk.CTkLabel(body, anchor="w", justify="left", wraplength=710,
                                 font=ctk.CTkFont(size=19 if index == 0 else 13,
                                                 weight="bold" if index == 0 else "normal"))
            label.pack(fill="x", padx=14, pady=(0,5))
            self._next_labels.append(label)
        self._next_labels[0].configure(text="Load a PHD2 guide log to get a recommendation.")
        for label in self._next_labels[1:]:
            label.configure(text="—")

    def _init_figure(self):
        controls = ctk.CTkFrame(self._graph_card, fg_color="transparent")
        controls.pack(fill="x", padx=7, pady=(3,0))
        for label, minutes in (("Full night", None), ("10 min", 10), ("2 min", 2)):
            ctk.CTkButton(controls, text=label, width=72, height=25,
                          command=lambda m=minutes: self._zoom_guiding(m)).pack(side="left", padx=(0,4))
        ctk.CTkLabel(controls, text="Drag the overview to select a range",
                     text_color=FG_MUTED, font=ctk.CTkFont(size=11)).pack(side="right")
        self._window_stats = ctk.CTkLabel(self._graph_card, text="Window RMS · Load a guide log",
                                           text_color=FG_SIGNAL, anchor="w", font=ctk.CTkFont(size=11))
        self._window_stats.pack(fill="x", padx=9)
        self._fig = Figure(dpi=96, layout="constrained")
        self._fig.patch.set_facecolor(MC["fig"])
        gs = gridspec.GridSpec(4, 1, figure=self._fig,
                               height_ratios=[3.2, 1.15, .65, .60], hspace=0.04)
        self._ax_err = self._fig.add_subplot(gs[0])
        self._ax_snr = self._fig.add_subplot(gs[1], sharex=self._ax_err)
        self._ax_events = self._fig.add_subplot(gs[2], sharex=self._ax_err)
        self._ax_overview = self._fig.add_subplot(gs[3])

        _style_ax(self._ax_err, ylabel="Guide Error (arcsec)")
        _style_ax(self._ax_snr, ylabel="Star SNR")
        self._ax_snr.set_xlabel("Time (minutes)", color=MC["text"], fontsize=9)
        self._ax_err.tick_params(labelbottom=False)

        for ax in (self._ax_err, self._ax_snr):
            ax.text(0.5, 0.5, "Load a PHD2 log to see guiding data",
                    transform=ax.transAxes, ha="center", va="center",
                    color=FG_MUTED, fontsize=11)

        self._canvas = FigureCanvasTkAgg(self._fig, master=self._graph_card)
        self._canvas.mpl_connect("button_press_event", self._guide_event_click)

        # Toolbar with basic dark styling
        tb_frame = tk.Frame(self._graph_card, bg=BG_INPUT, bd=0)
        tb_frame.pack(side="bottom", fill="x")
        self._toolbar = NavigationToolbar2Tk(self._canvas, tb_frame, pack_toolbar=False)
        self._toolbar.config(bg=BG_INPUT)
        for ch in self._toolbar.winfo_children():
            try: ch.config(bg=BG_INPUT, fg=FG_TEXT,
                           highlightbackground=BG_INPUT,
                           activebackground=BORDER, activeforeground=FG_TEXT)
            except Exception: pass
        self._toolbar.update()
        self._toolbar.pack(side="bottom", fill="x")

        self._canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=(6,0))
        self._canvas.draw()

    def _init_star_plot(self, tab):
        toolbar = ctk.CTkFrame(tab, fg_color="transparent")
        toolbar.pack(fill="x", padx=9)
        ctk.CTkLabel(toolbar, text="Guide star around the lock point",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        ctk.CTkButton(toolbar, text="Full night", width=82, height=25,
                      command=lambda: self._zoom_guiding(None)).pack(side="right")
        self._star_summary = ctk.CTkLabel(tab, text="Load a guide log to see the star plot.",
                                          anchor="w", text_color=FG_MUTED, font=ctk.CTkFont(size=11))
        self._star_summary.pack(fill="x", padx=9)
        self._star_fig = Figure(dpi=96, layout="constrained", facecolor=BG_APP)
        self._star_ax = self._star_fig.add_subplot()
        self._star_canvas = FigureCanvasTkAgg(self._star_fig, master=tab)
        self._star_canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=(4,6))
        self._draw_star_plot()

    def _guiding_tab_changed(self):
        if HAS_MPL and self._guiding_tabs.get() == "Star Plot":
            self._draw_star_plot()

    def _draw_star_plot(self):
        ax = self._star_ax
        ax.clear()
        _style_ax(ax, ylabel="")
        ax.set_aspect("equal", adjustable="box")
        message = "Load a guide log to see the star plot."
        if self._guide_data is not None:
            times, ra, dec, good, units, _ = self._guide_data
            bounds = tuple(sorted(self._ax_err.get_xlim()))
            selected = _guide_window_mask(times, ra, dec, good, bounds)
            if units == "mixed units":
                message = "Star plot unavailable: this log mixes arcseconds and pixels."
            elif not np.any(selected):
                message = "No valid guide positions in the selected time range."
            else:
                x, y = ra[selected], dec[selected]
                radius95 = float(np.percentile(np.hypot(x, y), 95))
                limit = max(float(np.max(np.abs(x))), float(np.max(np.abs(y))), .1) * 1.10
                ax.set_xlim(-limit, limit)
                ax.set_ylim(-limit, limit)
                ax.grid(False)
                ax.axhline(0, color=BORDER, lw=.85)
                ax.axvline(0, color=BORDER, lw=.85)
                for radius in (.25 * limit, .5 * limit, .75 * limit, limit):
                    ax.add_patch(Circle((0, 0), radius, fill=False, edgecolor=BORDER, lw=.8))
                if radius95 > 0:
                    ax.add_patch(Circle((0, 0), radius95, fill=False, edgecolor=FG_SIGNAL,
                                        lw=1, ls="--", alpha=.80))
                ax.scatter(x, y, s=6 if len(x) < 2500 else 3, c=FG_SIGNAL,
                           alpha=.45 if len(x) < 2500 else .22, linewidths=0)
                ax.scatter(x[-1], y[-1], marker="x", s=48, c=MC["dec"], linewidths=1.5,
                           label="Last valid sample", zorder=4)
                ax.legend(loc="upper right", fontsize=8, facecolor=BG_CARD,
                          edgecolor=BORDER, labelcolor=FG_TEXT)
                unit = "″" if units == "arcsec" else " px"
                ax.set_xlabel(f"RA offset ({units})", fontsize=9, color=FG_MUTED)
                ax.set_ylabel(f"Dec offset ({units})", fontsize=9, color=FG_MUTED)
                message = (f"{len(x):,} samples  /  {bounds[0]:.1f}–{bounds[1]:.1f} min"
                           f"  /  95% within {radius95:.2f}{unit}"
                           "  /  logged settling excluded")
                self._star_summary.configure(text=message)
                self._star_canvas.draw_idle()
                return
        ax.set_xticks([]); ax.set_yticks([])
        ax.text(.5, .5, message, ha="center", va="center", transform=ax.transAxes, color=FG_MUTED)
        self._star_summary.configure(text=message)
        self._star_canvas.draw_idle()

    # ── Actions ────────────────────────────────────────────────────────────

    def _browse(self):
        p = filedialog.askopenfilename(
            title="Select PHD2 Guide Log",
            filetypes=[("PHD2 logs", "*.txt *.log"), ("All files", "*.*")],
            initialdir=str(Path.home()),
        )
        if p:
            label = f"Manual · {Path(p).name}"
            self._library_paths[label] = Path(p)
            self._log_var.set(label)
            self._load()

    def refresh_library(self, auto_load=False):
        cfg = load_config()
        dest_value = cfg.get("dest", "").strip()
        destination = Path(dest_value) if dest_value else None
        paths = discover_phd2_logs(destination, include_debug=False) if destination and destination.is_dir() else []
        self._library_paths = {
            str(path.relative_to(destination)): path for path in paths
        } if paths else {}
        values = list(self._library_paths) or ["No archived guide logs found"]
        self._log_combo.configure(values=values)
        if self._log_var.get() not in self._library_paths:
            self._log_var.set(values[0])
        self._sync_lbl.configure(
            text=f"{len(paths)} archived guide log(s) · {destination}" if paths else
                 "No archived guide logs yet. Choose folders on Sort Files, then click Sync Logs.")
        if auto_load and paths:
            self._load()

    def _initialise_library(self):
        self.refresh_library(auto_load=True)
        self._sync_logs(silent=True)

    def _auto_sync_tick(self):
        if self._auto_sync_var.get():
            self._sync_logs(silent=True)
        self.after(60000, self._auto_sync_tick)

    def _sync_logs(self, silent=False):
        if self._syncing: return
        cfg = load_config()
        source_value = cfg.get("source", "").strip()
        dest_value = cfg.get("dest", "").strip()
        source = Path(source_value) if source_value else None
        destination = Path(dest_value) if dest_value else None
        if not source or not destination or not source.is_dir() or not destination.is_dir():
            if not silent:
                messagebox.showerror("Folders required",
                    "Choose the ASIAIR source and destination on the Sort Files tab first.")
            return
        self._syncing = True
        self._sync_lbl.configure(text="Scanning and copying PHD2 logs…")
        def work():
            result = sync_phd2_logs(source, destination)
            def done():
                self._syncing = False
                self.refresh_library(auto_load=True)
                self._sync_lbl.configure(text=(
                    f"Sync complete · {len(result['copied'])} new · "
                    f"{len(result['updated'])} updated · {len(result['unchanged'])} unchanged · "
                    f"{len(result['errors'])} errors"))
                if result["errors"] and not silent:
                    messagebox.showwarning("PHD2 sync completed with errors",
                        "\n".join(f"{p}: {e}" for p,e in result["errors"][:8]))
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _load(self):
        selected = self._log_var.get().strip()
        path = self._library_paths.get(selected, Path(selected))
        if not path or not Path(path).is_file():
            messagebox.showerror("File not found", f"Cannot open:\n{path}")
            return
        try:
            sessions = parse_phd2_log(path)
        except Exception as e:
            messagebox.showerror("Parse error", f"Failed to parse log:\n{e}")
            return
        if not sessions:
            messagebox.showwarning("No data", "No guiding data found in this file.")
            return
        self._sessions = sessions
        scores = [self.catalog.record_phd2_quality(Path(path), session, session_index=index)
                  for index, session in enumerate(sessions, 1)]
        self._update_stats(sessions)
        if scores:
            score = sum(scores) / len(scores)
            colour = "#56d364" if score >= 80 else "#e3b341" if score >= 60 else "#ff7b72"
            self._sb["Quality"].set(f"{score:.0f}/100", colour)
        if HAS_MPL:
            self._draw_graphs(sessions)
        for label, value in zip(self._next_labels, _next_guiding_check(sessions)):
            label.configure(text=value)

    # ── Stats ──────────────────────────────────────────────────────────────

    def _update_stats(self, sessions: List[PHD2Session]):
        all_ra  = np.concatenate([s.ra_err  for s in sessions])
        all_dec = np.concatenate([s.dec_err for s in sessions])
        rms_ra    = float(np.sqrt(np.mean(all_ra**2)))
        rms_dec   = float(np.sqrt(np.mean(all_dec**2)))
        rms_total = float(np.sqrt(np.mean(all_ra**2 + all_dec**2)))
        peak      = float(np.max(np.sqrt(all_ra**2 + all_dec**2)))
        p95       = float(np.percentile(np.sqrt(all_ra**2 + all_dec**2),95))
        total_dur = sum(s.duration_min for s in sessions)
        total_frm = sum(s.n_frames     for s in sessions)
        total_dithers = sum(len(s.dithers) for s in sessions)
        lost_frames = sum(s.lost_frames for s in sessions)
        cadence = float(np.median([s.cadence_s for s in sessions if s.cadence_s])) if sessions else 0
        all_snr = np.concatenate([s.snr for s in sessions if len(s.snr)]) if any(len(s.snr) for s in sessions) else np.array([])
        all_hfd = np.concatenate([s.hfd for s in sessions if len(s.hfd)]) if any(len(s.hfd) for s in sessions) else np.array([])
        avg_snr = float(np.nanmean(all_snr)) if len(all_snr) and not np.all(np.isnan(all_snr)) else None
        avg_hfd = float(np.nanmean(all_hfd)) if len(all_hfd) and not np.all(np.isnan(all_hfd)) else None
        u  = '"' if sessions[0].units == "arcsec" else " px"
        rc = _rms_colour if sessions[0].units == "arcsec" else (lambda _: FG_TEXT)

        self._sb["RA RMS"].set(   f"{rms_ra:.2f}{u}",    rc(rms_ra))
        self._sb["Dec RMS"].set(  f"{rms_dec:.2f}{u}",   rc(rms_dec))
        self._sb["Total RMS"].set(f"{rms_total:.2f}{u}", rc(rms_total))
        self._sb["P95 Error"].set(f"{p95:.2f}{u}", FG_TEXT)
        self._sb["Peak Error"].set(f"{peak:.2f}{u}",     FG_TEXT)
        self._sb["Duration"].set( f"{total_dur:.1f} min",FG_TEXT)
        self._sb["Frames"].set(   f"{total_frm:,}",      FG_TEXT)
        self._sb["Cadence"].set(  f"{cadence:.2f} s" if cadence else "—", FG_TEXT)
        self._sb["Dithers"].set(  f"{total_dithers}", FG_TEXT)
        self._sb["Lost Frames"].set(f"{lost_frames}", "#ff7b72" if lost_frames else "#56d364")
        self._sb["Avg SNR"].set(  f"{avg_snr:.1f}" if avg_snr is not None else "—", FG_TEXT)
        self._sb["Avg HFD"].set(  f"{avg_hfd:.2f} px" if avg_hfd is not None else "—", FG_TEXT)

        # Equipment info line
        s0 = sessions[0]
        parts = []
        if s0.start_time: parts.append(s0.start_time)
        if s0.profile:    parts.append(s0.profile)
        if s0.camera:     parts.append(s0.camera)
        if s0.mount:      parts.append(s0.mount)
        if s0.focal_mm:   parts.append(f"{s0.focal_mm:.0f} mm")
        if s0.px_scale:   parts.append(f"{s0.px_scale:.2f}\"/px")
        if s0.exposure_s: parts.append(f"{s0.exposure_s:g} s guide exposure")
        if len(sessions) > 1:
            parts.append(f"{len(sessions)} guiding sessions in log")
        self._info_lbl.configure(text="  ·  ".join(parts))
        self._update_details(sessions)

    def _update_details(self, sessions: List[PHD2Session]):
        lines = []
        event_lines = []
        for index, s in enumerate(sessions, 1):
            unit = 'arcsec' if s.units == "arcsec" else "px"
            errors = Counter(int(code) for code in s.error_codes if int(code))
            ra_corr = int(np.count_nonzero(s.ra_pulse)) if len(s.ra_pulse) else 0
            dec_corr = int(np.count_nonzero(s.dec_pulse)) if len(s.dec_pulse) else 0
            lines.extend([
                f"GUIDING SESSION {index}", "="*72,
                f"Start / end       : {s.start_time or 'Unknown'}  →  {s.end_time or 'Unknown'}",
                f"Duration / frames : {s.duration_min:.2f} min / {s.n_frames:,}",
                f"PHD2              : {s.phd_version or 'Not recorded'}",
                f"Profile           : {s.profile or 'Not recorded'}",
                f"Camera            : {s.camera or 'Not recorded'}",
                f"Mount / aux mount : {s.mount or 'Not recorded'} / {s.aux_mount or 'None'}",
                f"Focal / scale     : {s.focal_mm or 0:g} mm / {s.px_scale or 0:g} arcsec/px",
                f"Guide exposure    : {s.exposure_s:g} s" if s.exposure_s else "Guide exposure    : Not recorded",
                f"Sky position      : {s.sky_position or 'Not recorded'}",
                f"Lock position     : {s.lock_position or 'Not recorded'}", "",
                f"RA RMS            : {s.rms_ra:.3f} {unit}",
                f"Dec RMS           : {s.rms_dec:.3f} {unit}",
                f"Total RMS         : {s.rms_total:.3f} {unit}",
                f"Median / P95      : {s.median_err:.3f} / {s.p95_err:.3f} {unit}",
                f"Peak error        : {s.peak_err:.3f} {unit}",
                f"Quality score     : {phd2_quality_score(s.rms_total, s.peak_err, s.lost_frames, s.n_frames, s.drift_per_min('ra'), s.drift_per_min('dec')):.1f} / 100",
                f"RA / Dec drift    : {s.drift_per_min('ra'):+.4f} / {s.drift_per_min('dec'):+.4f} {unit}/min",
                f"Median cadence    : {s.cadence_s:.3f} s",
                f"RA / Dec pulses   : {ra_corr:,} / {dec_corr:,}",
                f"RA pulse avg/max  : {np.mean(s.ra_pulse):.1f} / {np.max(s.ra_pulse):.0f} ms" if len(s.ra_pulse) else "RA pulse avg/max  : —",
                f"Dec pulse avg/max : {np.mean(s.dec_pulse):.1f} / {np.max(s.dec_pulse):.0f} ms" if len(s.dec_pulse) else "Dec pulse avg/max : —",
                f"Dithers / errors  : {len(s.dithers)} / {s.lost_frames}",
            ])
            if errors:
                lines.append("Guide-star errors : " + ", ".join(
                    f"{PHD2_ERROR_LABELS.get(code, 'Code '+str(code))} ×{count}"
                    for code,count in sorted(errors.items())))
            for label, values, suffix in (("SNR",s.snr,""),("Star mass",s.star_mass,""),("HFD",s.hfd," px")):
                finite = values[np.isfinite(values)] if len(values) else []
                if len(finite):
                    lines.append(f"{label:<18}: avg {np.mean(finite):.2f} · min {np.min(finite):.2f} · max {np.max(finite):.2f}{suffix}")
            if s.algorithms:
                lines.extend(["", "GUIDING SETTINGS"])
                lines.extend(f"  {key}: {value}" for key,value in sorted(s.algorithms.items()))
            lines.append("")
            event_lines.append(f"SESSION {index} · {s.start_time or 'Unknown start'}")
            event_lines.extend(s.events or ["No event messages recorded."])
            event_lines.append("")

        self._details_box.configure(state="normal")
        self._details_box.delete("1.0","end")
        self._details_box.insert("end","\n".join(lines))
        self._details_box.configure(state="disabled")
        self._events_box.configure(state="normal")
        self._events_box.delete("1.0","end")
        self._events_box.insert("end","\n".join(event_lines))
        self._events_box.configure(state="disabled")

    # ── Graphs ─────────────────────────────────────────────────────────────

    def _draw_graphs(self, sessions: List[PHD2Session]):
        ax_err, ax_snr = self._ax_err, self._ax_snr
        self._guide_data = None
        if self._range_select:
            self._range_select.disconnect_events()
        for axis in (ax_err, ax_snr, self._ax_events, self._ax_overview):
            axis.clear()

        units = sessions[0].units if len({s.units for s in sessions}) == 1 else "mixed units"
        _style_ax(ax_err, ylabel="″" if units == "arcsec" else units)
        _style_ax(ax_snr, ylabel="")
        ax_err.tick_params(labelbottom=False)
        ax_snr.tick_params(labelbottom=False)
        _style_ax(self._ax_events, ylabel="")
        self._ax_events.set_yticks([0, 1], ["Dither", "Frame error"], fontsize=8)
        self._ax_events.set_ylim(-.5, 1.5)
        _style_ax(self._ax_overview, ylabel="")
        self._ax_overview.set_yticks([0], ["Full night"], fontsize=8)
        self._ax_overview.set_xlabel("Run timeline (minutes; breaks shown as 3 min)",
                                     color=MC["text"], fontsize=8)

        GAP_MIN = 3.0   # gap between sessions (minutes)
        t_offset = 0.0

        all_t    = []
        all_ra   = []
        all_dec  = []
        all_snr_t= []
        all_snr  = []
        dither_t = []   # dither event times (in plot minutes)
        lost_t = []
        good_frames = []

        for i, s in enumerate(sessions):
            if s.n_frames == 0: continue

            t_min = (s.times - s.times[0]) / 60 + t_offset

            # RA / Dec
            all_t.extend(t_min)
            all_ra.extend(s.ra_err)
            all_dec.extend(s.dec_err)
            good = np.ones(len(t_min), dtype=bool)
            if len(s.error_codes) == len(t_min):
                codes = np.asarray(s.error_codes)
                good = codes == 0
                lost_t.extend(t_min[codes != 0])
            for first, past_last in s.settle_spans:
                good[first:past_last] = False
                if past_last - first > 1:
                    self._ax_events.axvspan(t_min[first], t_min[past_last - 1],
                                           color=ACCENT, alpha=.28)
            good_frames.extend(good)

            # Dither markers
            for dt in s.dithers:
                rel = (dt - s.times[0]) / 60 + t_offset
                dither_t.append(rel)

            # SNR (may have fewer points if some frames dropped)
            if len(s.snr):
                snr_t = t_min if len(s.snr) == len(t_min) else np.linspace(t_min[0], t_min[-1], len(s.snr))
                all_snr_t.extend(snr_t)
                all_snr.extend(s.snr)

            t_end = float(t_min[-1])

            # Insert NaN break so lines don't connect across sessions
            if i < len(sessions) - 1:
                pause_end = t_end + GAP_MIN
                all_t.append(t_end + GAP_MIN/2);  all_ra.append(float("nan"))
                all_dec.append(float("nan"))
                good_frames.append(False)
                if all_snr:
                    all_snr_t.append(t_end + GAP_MIN/2); all_snr.append(float("nan"))
                # Shade the gap
                for ax in (ax_err, ax_snr, self._ax_events, self._ax_overview):
                    ax.axvspan(t_end, pause_end, color=MC["pause"], alpha=0.6,
                               label="_nolegend_")
                t_offset = pause_end
            else:
                t_offset = t_end + GAP_MIN

        all_t   = np.array(all_t,   dtype=float)
        all_ra  = np.array(all_ra,  dtype=float)
        all_dec = np.array(all_dec, dtype=float)
        ax_err.plot(all_t, all_ra,  color=MC["ra"],  lw=0.85, alpha=0.85,
                    label="RA")
        ax_err.plot(all_t, all_dec, color=MC["dec"], lw=0.85, alpha=0.85,
                    label="Dec")

        ax_err.axhline(0, color=BORDER, lw=0.9)
        ax_err.legend(facecolor=BG_CARD, edgecolor=BORDER,
                             fontsize=9, labelcolor=FG_TEXT, loc="upper right")

        # ── SNR plot ─────────────────────────────────────────────────
        if all_snr:
            ax_snr.plot(np.array(all_snr_t, dtype=float),
                        np.array(all_snr,   dtype=float),
                        color=MC["snr"], lw=0.85, alpha=0.85, label="Star SNR")
            ax_snr.legend(facecolor=BG_CARD, edgecolor=BORDER,
                           fontsize=9, labelcolor=FG_TEXT, loc="upper right")
        else:
            ax_snr.text(0.5, 0.5, "No SNR data in this log",
                        transform=ax_snr.transAxes, ha="center", va="center",
                        color=FG_MUTED, fontsize=10)

        self._ax_overview.plot(all_t, np.hypot(all_ra, all_dec), color=FG_SIGNAL, lw=.6)
        bounds = (float(all_t[0]), max(float(all_t[-1]), float(all_t[0]) + 1 / 60))
        self._ax_overview.set_xlim(*bounds)
        self._guide_data = all_t, all_ra, all_dec, np.asarray(good_frames), units, bounds
        self._guide_events = (dither_t, lost_t)
        self._event_marks = [
            self._ax_events.scatter([], [], marker="|", color=MC["snr"]),
            self._ax_events.scatter([], [], marker="x", color=MC["dec"]),
        ]
        self._guide_span = self._ax_overview.axvspan(*bounds, color=ACCENT, alpha=.26)
        self._range_select = SpanSelector(
            self._ax_overview, self._select_guiding, "horizontal", minspan=1 / 600,
            props={"facecolor": FG_SIGNAL, "alpha": .22}, useblit=False)
        ax_err.callbacks.connect("xlim_changed", self._guide_window_changed)
        self._select_guiding(*bounds)

    def _select_guiding(self, left, right):
        if self._guide_data and left < right:
            self._ax_err.set_xlim(left, right)
            self._canvas.draw_idle()

    def _zoom_guiding(self, minutes, center=None):
        if self._guide_data is None:
            return
        start, end = self._guide_data[-1]
        if minutes is None or end - start <= minutes:
            self._select_guiding(start, end)
            return
        if center is None:
            center = start + minutes / 2 if np.allclose(self._ax_err.get_xlim(), (start, end)) else np.mean(self._ax_err.get_xlim())
        left = min(max(start, center - minutes / 2), end - minutes)
        self._select_guiding(left, left + minutes)

    def _guide_event_click(self, event):
        if (self._guide_data is None or event.inaxes is not self._ax_events
                or event.xdata is None or event.ydata is None
                or event.button != 1 or self._toolbar.mode):
            return
        row = int(round(event.ydata))
        if row not in (0, 1):
            return
        bounds = self._ax_err.get_xlim()
        groups = _guide_event_groups(self._guide_events[row], bounds)
        if groups:
            nearest, _ = min(groups, key=lambda g: abs(g[0] - event.xdata))
            if abs(nearest - event.xdata) <= (bounds[1] - bounds[0]) / 42:
                self._zoom_guiding(2, center=nearest)

    def _guide_window_changed(self, axis):
        times, ra, dec, good, units, full = self._guide_data
        bounds = tuple(sorted(axis.get_xlim()))
        count, rms = _guide_window_rms(times, ra, dec, good, bounds)
        if units == "mixed units":
            label = "Window RMS unavailable: this log mixes arcseconds and pixels"
        elif rms is None:
            label = "Window RMS · No valid frames in this range"
        else:
            unit = "″" if units == "arcsec" else " px"
            label = (f"Window RMS  ·  RA {rms[0]:.2f}{unit}   Dec {rms[1]:.2f}{unit}   "
                     f"Total {rms[2]:.2f}{unit}   /   {count:,} frames; logged settling excluded")
        self._window_stats.configure(text=label)
        for row, (events, marks) in enumerate(zip(self._guide_events, self._event_marks)):
            groups = _guide_event_groups(events, bounds)
            marks.set_offsets(np.asarray([(t, row) for t, _ in groups]).reshape(-1, 2))
            marks.set_sizes([min(100, 18 + 7 * n) for _, n in groups])
        self._guide_span.remove()
        left, right = max(full[0], bounds[0]), min(full[1], bounds[1])
        self._guide_span = self._ax_overview.axvspan(left, max(left, right), color=ACCENT, alpha=.26)
        if self._guiding_tabs.get() == "Star Plot":
            self._draw_star_plot()


# ══════════════════════════════════════════════════════════════════════════════
# Sort Files UI
# ══════════════════════════════════════════════════════════════════════════════

class FolderCard(ctk.CTkFrame):
    def __init__(self, parent, label: str, var: tk.StringVar, *, index, detail, **kw):
        super().__init__(parent, fg_color=BG_CARD, corner_radius=8,
                         border_width=1, border_color=BORDER, **kw)
        lf = ctk.CTkFrame(self, fg_color="transparent")
        lf.pack(fill="x", padx=16, pady=(12,4))
        ctk.CTkLabel(lf, text=index, text_color=FG_SIGNAL, width=28,
                     font=ctk.CTkFont(size=11, weight="bold")).pack(side="left", padx=(0,7))
        ctk.CTkLabel(lf, text=label, font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=FG_TEXT).pack(side="left")
        ctk.CTkLabel(self, text=detail, text_color=FG_MUTED, anchor="w",
                     font=ctk.CTkFont(size=12)).pack(fill="x", padx=16)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6,14))
        row.columnconfigure(0, weight=1)
        ctk.CTkEntry(row, textvariable=var,
                     placeholder_text="Click Browse… to select a folder",
                     fg_color=BG_INPUT, border_color=BORDER,
                     text_color=FG_TEXT, placeholder_text_color=FG_MUTED,
                     height=36).grid(row=0, column=0, sticky="ew", padx=(0,8))
        ctk.CTkButton(row, text="Browse…", width=90, height=36,
                      command=lambda: self._pick(var)).grid(row=0, column=1)

    def _pick(self, var):
        cur = var.get().strip()
        d = filedialog.askdirectory(
            initialdir=cur if cur and Path(cur).is_dir() else str(Path.home()))
        if d: var.set(d)


class SortFrame(ctk.CTkFrame):
    def __init__(self, parent, on_complete=None, catalog=None):
        super().__init__(parent, fg_color="transparent")
        self._on_complete = on_complete
        self.catalog = catalog or Catalog()
        cfg = load_config()
        self.src_var  = tk.StringVar(value=cfg.get("source",""))
        self.dst_var  = tk.StringVar(value=cfg.get("dest",""))
        self.dry_var  = tk.BooleanVar(value=False)
        self.eject_var = tk.BooleanVar(value=False)
        self.prog_var = tk.DoubleVar(value=0.0)
        self._running = False
        self._run_lines = []
        self._preview_report_requested = False
        self._build_ui()
        self.after(800, self._detect_sources)

    def _build_ui(self):
        folders = ctk.CTkFrame(self, fg_color="transparent")
        folders.pack(fill="x", pady=(0,14))
        folders.columnconfigure((0, 1), weight=1, uniform="import")
        FolderCard(folders, "Capture source", self.src_var, index="01",
                   detail="ASIAIR SD card or capture folder").grid(row=0, column=0, sticky="ew", padx=(0,6))
        FolderCard(folders, "Archive destination", self.dst_var, index="02",
                   detail="PC or mounted storage folder").grid(row=0, column=1, sticky="ew", padx=(6,0))

        # Options + buttons
        ctrl = ctk.CTkFrame(self, fg_color="transparent")
        ctrl.pack(fill="x", pady=(0,10))
        ctk.CTkCheckBox(ctrl, text="Dry run only",
                        variable=self.dry_var,
                        font=ctk.CTkFont(size=12), text_color=FG_TEXT,
                        checkmark_color="#ffffff", border_color=BORDER,
                        ).pack(side="left", padx=(0,20))
        ctk.CTkCheckBox(ctrl, text="Eject after verified copy",
                        variable=self.eject_var, font=ctk.CTkFont(size=11),
                        text_color=FG_MUTED, border_color=BORDER).pack(side="left", padx=(0,12))
        self.cancel_btn = ctk.CTkButton(
            ctrl, text="Cancel", width=80, height=36,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=BTN_CANCEL, hover_color="#8b3a3a",
            state="disabled", command=self._cancel)
        self.cancel_btn.pack(side="right", padx=(6,0))
        self.move_btn = ctk.CTkButton(
            ctrl, text="Preview Report", width=130, height=36,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=BTN_MOVE, hover_color="#3d444d",
            border_color=BORDER, border_width=1,
            command=self._start_preview)
        self.move_btn.pack(side="right", padx=(6,0))
        self.copy_btn = ctk.CTkButton(
            ctrl, text="Copy and verify", width=140, height=36,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HL,
            command=lambda: self._start("copy"))
        self.copy_btn.pack(side="right")

        # Progress
        prog_card = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=10)
        prog_card.pack(fill="x", pady=(0,10))
        pi = ctk.CTkFrame(prog_card, fg_color="transparent")
        pi.pack(fill="x", padx=16, pady=10)
        self.prog_bar = ctk.CTkProgressBar(pi, height=5, corner_radius=3,
                                            fg_color=BORDER,
                                            progress_color=ACCENT)
        self.prog_bar.set(0); self.prog_bar.pack(fill="x")
        sr = ctk.CTkFrame(pi, fg_color="transparent")
        sr.pack(fill="x", pady=(6,0))
        self.status_lbl = ctk.CTkLabel(sr, text="Ready.",
                                        font=ctk.CTkFont(size=11),
                                        text_color=FG_MUTED, anchor="w")
        self.status_lbl.pack(side="left")
        self.pct_lbl = ctk.CTkLabel(sr, text="0%",
                                     font=ctk.CTkFont(size=11, weight="bold"),
                                     text_color=FG_TEXT, width=40, anchor="e")
        self.pct_lbl.pack(side="right")

        # Log
        log_card = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=10)
        log_card.pack(fill="both", expand=True)
        lh = ctk.CTkFrame(log_card, fg_color="transparent")
        lh.pack(fill="x", padx=16, pady=(10,6))
        ctk.CTkLabel(lh, text="Transfer activity",
                     font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=FG_TEXT).pack(side="left")
        ctk.CTkButton(lh, text="Clear", width=60, height=24,
                      font=ctk.CTkFont(size=11),
                      fg_color="transparent", border_color=BORDER, border_width=1,
                      text_color=FG_MUTED, hover_color="#21262d",
                      command=self._clear_log).pack(side="right")
        ctk.CTkFrame(log_card, fg_color="#21262d", height=1,
                     corner_radius=0).pack(fill="x")

        lf = ctk.CTkFrame(log_card, fg_color="transparent")
        lf.pack(fill="both", expand=True, padx=2, pady=2)
        lf.rowconfigure(0, weight=1); lf.columnconfigure(0, weight=1)

        self.log = tk.Text(lf, bg=BG_LOG, fg=FG_TEXT,
                            font=("Consolas",10), wrap="word",
                            relief="flat", bd=0, state="disabled",
                            padx=14, pady=10, cursor="arrow",
                            selectbackground="#264f78")
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ctk.CTkScrollbar(lf, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)
        for name, colour in _C.items():
            self.log.tag_configure(name, foreground=colour)
        self.log.tag_configure("bold", foreground=_C["bold"],
                               font=("Consolas",10,"bold"))

    # ── Helpers ───────────────────────────────────────────────────────────

    def _log(self, msg: str, tag: str="info"):
        self._run_lines.append(msg)
        def _u():
            self.log.configure(state="normal")
            self.log.insert("end", msg+"\n", tag)
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(0, _u)

    def _clear_log(self):
        self.log.configure(state="normal"); self.log.delete("1.0","end")
        self.log.configure(state="disabled")

    def _set_status(self, msg): self.after(0, lambda: self.status_lbl.configure(text=msg))

    def _set_progress(self, pct):
        def _u():
            self.prog_bar.set(pct/100)
            self.pct_lbl.configure(text=f"{pct:.0f}%")
        self.after(0, _u)

    def _set_busy(self, busy):
        def _u():
            if busy:
                self.copy_btn.configure(state="disabled", fg_color="#21262d")
                self.move_btn.configure(state="disabled")
                self.cancel_btn.configure(state="normal")
            else:
                self.copy_btn.configure(state="normal", fg_color=ACCENT)
                self.move_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
        self.after(0, _u)

    def _start_preview(self):
        self._preview_report_requested = True
        self._start("copy", dry_override=True)

    def _start(self, mode, dry_override=None):
        src = self.src_var.get().strip(); dst = self.dst_var.get().strip()
        if not src: messagebox.showerror("Missing source","Please select a source folder."); return
        if not dst: messagebox.showerror("Missing destination","Please select a destination folder."); return
        if not Path(src).is_dir(): messagebox.showerror("Invalid source", f"Not found:\n{src}"); return
        if Path(src) == Path(dst): messagebox.showerror("Same folder","Source and destination must differ."); return
        save_config({"source":src,"dest":dst})
        self._clear_log(); self._set_progress(0); self._run_lines = []
        self._running = True; self._set_busy(True)
        threading.Thread(target=self._worker,
                         args=(Path(src),Path(dst),mode,
                               self.dry_var.get() if dry_override is None else dry_override),
                         daemon=True).start()

    def _cancel(self):
        self._running = False
        self._log("\n  Cancelling — finishing current file…","skip")

    def _worker(self, src, dst, mode, dry_run):
        try: self._sort(src, dst, mode, dry_run)
        except Exception as e: self._log(f"\nFATAL ERROR: {e}","err")
        finally:
            self._running = False; self._set_busy(False)
            if dry_run and self._preview_report_requested:
                self._preview_report_requested = False
                report_dir = self.catalog.db_path.parent / "import_reports"
                report_dir.mkdir(parents=True, exist_ok=True)
                report = report_dir / f"import_preview_{datetime.now():%Y%m%d_%H%M%S}.txt"
                report.write_text("\n".join(self._run_lines), encoding="utf-8")
                self.after(0, lambda: messagebox.showinfo("Import preview ready", f"No files were changed.\n\nReport saved to:\n{report}"))
            if self._on_complete and not dry_run:
                self.after(0, self._on_complete)

    def _sort(self, src, dst, mode, dry_run):
        label  = "[DRY RUN]  " if dry_run else ""
        action = "copy"
        SEP    = "─"*62
        self._log(SEP,"hdr")
        self._log(f"{label}{action.upper()}  ·  {datetime.now():%Y-%m-%d  %H:%M:%S}","hdr")
        self._log(f"  Source  →  {src}","mono_dim")
        self._log(f"  Dest    →  {dst}\n","mono_dim")

        self._set_status("Scanning source folder…")
        files: List[Path] = []
        for rd, dirs, names in os.walk(src):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for n in names:
                if not n.startswith("."): files.append(Path(rd)/n)

        total = len(files)
        self._log(f"  Found {total} file(s).\n","info")
        if total == 0: self._set_status("No files found."); return

        counts = {"Light":0,"Dark":0,"Flat":0,"Bias":0,
                  "PHD2":0,"deleted":0,"skip":0,"err":0}
        session_log: dict = {}
        cancelled = False

        for i, fp in enumerate(files):
            if not self._running: cancelled = True; break
            self._set_progress((i/total)*100)
            self._set_status(f"{i+1}/{total}  ·  {fp.name}")
            metadata = merged_metadata(fp) if fp.suffix.lower() in IMAGING_EXTENSIONS else {}
            info = classify_file(fp, src, metadata)
            if info is None:
                self._log(f"  SKIP      {fp.name}","skip"); counts["skip"]+=1; continue

            if info["action"] == "delete_preview":
                self._log(f"  DELETE    {fp.name}  (preview — removed from SD)","del")
                if not dry_run:
                    try:
                        fp.unlink(); counts["deleted"]+=1
                        self.catalog.record_audit("delete_preview", fp, status="complete",
                                                  undoable=False, details="ASIAIR preview removed")
                    except OSError as e: self._log(f"    ✗  {e}","err"); counts["err"]+=1
                else: counts["deleted"]+=1
                continue

            dest_fp = resolve_conflict(build_dest_path(info, dst, fp.name))
            try: rel = dest_fp.relative_to(dst)
            except ValueError: rel = dest_fp
            ftag = f"[{info['filter_name']}]" if info["filter_name"] else ""
            colour = "dryrun" if dry_run else "ok"
            self._log(f"  {info['frame_type']:<8}  {ftag:<8}  {fp.name}", colour)
            self._log(f"             → {rel}","mono_dim")

            sk = (info["date"], info.get("camera") or "", info.get("target") or "")
            session_log.setdefault(sk,[]).append((info,str(fp),str(dest_fp)))

            success = True
            if not dry_run:
                try:
                    dest_fp.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(fp),str(dest_fp))
                    source_checksum = sha256_file(fp)
                    checksum = sha256_file(dest_fp)
                    if source_checksum != checksum:
                        raise OSError("Checksum verification failed; source was left untouched")
                    self.catalog.record_audit("copy", fp, dest_fp, checksum,
                                              undoable=True,
                                              details=json.dumps({"frame_type": info["frame_type"], "verified": True}))
                except OSError as e:
                    self._log(f"    ✗  {e}","err"); counts["err"]+=1; success=False

            if success and info["frame_type"] in counts:
                counts[info["frame_type"]] += 1

        if not dry_run and session_log:
            self._log("\n  Writing session summaries…","info")
            for (date, camera, target), entries in session_log.items():
                sdir = dst/SESSIONS_ROOT/date
                if camera: sdir = sdir/camera
                if target: sdir = sdir/target
                try:
                    sdir.mkdir(parents=True, exist_ok=True)
                    write_session_summary(sdir, date, target or None,
                                          camera or None, entries, action)
                    self.catalog.record_session_event(date, camera or None, target or None,
                                                      "capture_imported", f"{len(entries)} verified file(s)")
                    try: disp = sdir.relative_to(dst)
                    except ValueError: disp = sdir
                    self._log(f"  ✓  session_summary.txt  →  {disp}","ok")
                except OSError as e: self._log(f"  ✗  {e}","err")

        self._set_progress(100)
        total_frm = sum(counts[k] for k in ("Light","Dark","Flat","Bias","PHD2"))
        self._log(f"\n{SEP}","hdr")
        self._log(f"  {label}{'CANCELLED' if cancelled else 'COMPLETE'}  —  "
                  f"{total_frm} frame(s) {action}d","bold")
        for k,lbl in [("Light","Lights"),("Dark","Darks"),("Flat","Flats"),
                       ("Bias","Bias"),("PHD2","PHD2 logs")]:
            self._log(f"  {lbl:<12}:  {counts[k]}","summary")
        self._log(f"  Previews deleted from SD :  {counts['deleted']}","del")
        if counts["skip"]: self._log(f"  Skipped      :  {counts['skip']}","skip")
        if counts["err"]:  self._log(f"  Errors       :  {counts['err']}","err")
        self._log(SEP,"hdr")
        self._set_status(
            f"{'Done' if not cancelled else 'Cancelled'}  —  "
            f"{total_frm} frames {action}d  ·  "
            f"{counts['deleted']} preview(s) removed from SD")
        if (not dry_run and not cancelled and not counts["err"] and self.eject_var.get()):
            ok, message = eject_source(src)
            self._log(f"  {'EJECTED' if ok else 'EJECT FAILED'}  {message}", "ok" if ok else "err")
            self.after(0, lambda: messagebox.showinfo("ASIAIR source", message) if ok else
                       messagebox.showwarning("Could not eject source", message))
        elif not dry_run and not cancelled and not counts["err"]:
            self.after(0, lambda: messagebox.showinfo(
                "Verified import complete",
                f"{total_frm} file(s) were copied and verified.\n\nRaw FITS files remain on the source."))

    def _detect_sources(self):
        self.after(30000, self._detect_sources)
        if self._running: return
        def work():
            sources = discover_asiair_sources()
            if sources:
                def show():
                    current = self.src_var.get().strip()
                    if not current or not Path(current).is_dir():
                        self.src_var.set(str(sources[0]))
                    self.status_lbl.configure(text=f"Detected ASIAIR source: {sources[0]}")
                self.after(0, show)
        threading.Thread(target=work, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# Main Application
# ══════════════════════════════════════════════════════════════════════════════

def _human_bytes(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB": return f"{value:.1f} {unit}"
        value /= 1024


class LibraryFrame(ctk.CTkFrame):
    """Searchable local catalogue; cloud/NAS locations remain ordinary folders."""
    def __init__(self, parent, catalog):
        super().__init__(parent, fg_color="transparent")
        self.catalog = catalog
        self._watcher = None
        self._watch_var = tk.BooleanVar(value=False)
        self._status_var = tk.StringVar(value="Catalogue ready")
        self._search_var = tk.StringVar()
        self._session_var = tk.StringVar(value="No sessions indexed")
        self._session_values = {}
        self._scan_controller = None
        self._quality_batch = None
        self._thumb_image = None
        self._build_ui()
        self.after(150, self.refresh)

    def _build_ui(self):
        bar = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=10)
        bar.pack(fill="x", pady=(0,8))
        ctk.CTkLabel(bar, textvariable=self._status_var, text_color=FG_MUTED, anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left", padx=12, fill="x", expand=True, anchor="w")
        ctk.CTkButton(bar, text="Scan Folder…", width=100, command=self._choose_scan).pack(side="right", padx=(4,12), pady=8)
        ctk.CTkButton(bar, text="Scan Destination", width=120,
                      fg_color=ACCENT, hover_color=ACCENT_HL,
                      command=self.scan_configured).pack(side="right", padx=4, pady=8)
        ctk.CTkButton(bar, text="Refresh", width=70, command=self.refresh).pack(side="right", padx=4, pady=8)
        ctk.CTkButton(bar, text="Cancel", width=60, fg_color=BTN_CANCEL,
                      command=self._cancel_scan).pack(side="right", padx=4, pady=8)
        ctk.CTkButton(bar, text="Pause", width=60,
                      command=self._pause_scan).pack(side="right", padx=4, pady=8)
        ctk.CTkCheckBox(bar, text="Watch ASIAIR", variable=self._watch_var,
                        command=self._toggle_watcher, text_color=FG_MUTED,
                        border_color=BORDER).pack(side="right", padx=10)

        self.tabs = ctk.CTkTabview(self, fg_color=BG_CARD,
                                   segmented_button_fg_color=BG_INPUT,
                                   segmented_button_selected_color=ACCENT)
        self.tabs.pack(fill="both", expand=True)
        self._boxes = {}
        for name in ("Overview", "Sessions", "Session", "Quality", "Masters", "Targets", "Search",
                     "Cleanup", "Duplicates", "Storage", "Audit"):
            self.tabs.add(name)
            tab = self.tabs.tab(name)
            if name == "Overview":
                self._build_overview(tab)
                continue
            controls = ctk.CTkFrame(tab, fg_color="transparent")
            controls.pack(fill="x", padx=4, pady=(4,0))
            if name == "Targets":
                ctk.CTkButton(controls, text="Set Integration Goal…", width=150,
                              command=self._set_goal).pack(side="left")
                ctk.CTkButton(controls, text="Target Alias…", width=110,
                              command=self._set_alias).pack(side="left", padx=6)
            elif name == "Session":
                self._session_combo = ctk.CTkComboBox(
                    controls, variable=self._session_var, values=["No sessions indexed"], width=330,
                    fg_color=BG_INPUT, border_color=BORDER, button_color=ACCENT)
                self._session_combo.pack(side="left", padx=(0,6))
                for label, command in (("Open",self._open_session),("Notes",self._edit_session),
                                       ("CSV",lambda: self._export_session("csv")),
                                       ("PDF",lambda: self._export_session("pdf")),
                                       ("Weather CSV",self._import_weather)):
                    ctk.CTkButton(controls, text=label, width=75, command=command).pack(side="left", padx=2)
            elif name == "Quality":
                ctk.CTkButton(controls, text="Analyse Pending Lights", width=160,
                              command=self._analyse_quality).pack(side="left")
                ctk.CTkButton(controls, text="Pause / Resume", width=110,
                              command=self._pause_quality).pack(side="left", padx=6)
                ctk.CTkButton(controls, text="Cancel", width=70, fg_color=BTN_CANCEL,
                              command=self._cancel_quality).pack(side="left")
            elif name == "Search":
                ctk.CTkEntry(controls, textvariable=self._search_var,
                             placeholder_text="Target, camera, filter, date or path",
                             fg_color=BG_INPUT, border_color=BORDER).pack(side="left", fill="x", expand=True, padx=(0,6))
                ctk.CTkButton(controls, text="Search", width=70,
                              command=self._run_search).pack(side="left")
            elif name == "Cleanup":
                ctk.CTkButton(controls, text="Review and Delete Eligible Raw Lights…", width=245,
                              fg_color=BTN_CANCEL, hover_color="#8b3a3a",
                              command=self._cleanup).pack(side="left")
                ctk.CTkLabel(controls, text="Requires verified XISF master + byte-identical backup; keeps one raw copy.",
                             text_color=FG_MUTED, font=ctk.CTkFont(size=10)).pack(side="left", padx=10)
            elif name == "Storage":
                ctk.CTkButton(controls, text="Add PC / NAS / Cloud Folder…", width=190,
                              command=self._add_storage).pack(side="left")
                ctk.CTkButton(controls, text="Refresh Status", width=110,
                              command=self._refresh_storage).pack(side="left", padx=6)
            elif name == "Audit":
                ctk.CTkButton(controls, text="Undo Operation…", width=130,
                              command=self._undo).pack(side="left")
                ctk.CTkButton(controls, text="Backup Database…", width=130,
                              command=self._backup_database).pack(side="left", padx=6)
                ctk.CTkButton(controls, text="Check / Repair", width=110,
                              command=self._check_database).pack(side="left")
            else:
                ctk.CTkLabel(controls, text=" ").pack(side="left")
            if name == "Session":
                self._thumbnail_label = ctk.CTkLabel(tab, text="Select a session to show a light-frame thumbnail.",
                                                     height=170, text_color=FG_MUTED)
                self._thumbnail_label.pack(fill="x", padx=4, pady=4)
            box = ctk.CTkTextbox(tab, fg_color=BG_LOG, text_color=FG_TEXT,
                                 font=("Consolas", 10), wrap="none")
            box.pack(fill="both", expand=True, padx=4, pady=4)
            self._boxes[name] = box

    def _build_overview(self, tab):
        metrics = ctk.CTkFrame(tab, fg_color="transparent")
        metrics.pack(fill="x", padx=12, pady=(10,12))
        metrics.columnconfigure(tuple(range(4)), weight=1, uniform="metric")
        self._metrics = {}
        for i, (key, title) in enumerate((("files", "Indexed files"), ("sessions", "Sessions"),
                                           ("masters", "Verified masters"), ("bytes", "Archive size"))):
            card = ctk.CTkFrame(metrics, fg_color=BG_INPUT, corner_radius=7)
            card.grid(row=0, column=i, sticky="ew", padx=(0,8) if i < 3 else 0)
            value = ctk.CTkLabel(card, text="—", text_color=FG_SIGNAL, anchor="w",
                                 font=ctk.CTkFont(size=25, weight="bold"))
            value.pack(fill="x", padx=13, pady=(10,0))
            ctk.CTkLabel(card, text=title, anchor="w", text_color=FG_MUTED,
                         font=ctk.CTkFont(size=12)).pack(fill="x", padx=13, pady=(0,9))
            self._metrics[key] = value
        self._overview_note = ctk.CTkLabel(tab, text="", text_color=FG_MUTED, anchor="w",
                                            wraplength=650, justify="left")
        self._overview_note.pack(fill="x", padx=13, pady=(0,10))
        ctk.CTkLabel(tab, text="Recent guiding runs", font=ctk.CTkFont(size=16, weight="bold"),
                     anchor="w").pack(fill="x", padx=13)
        box = ctk.CTkTextbox(tab, fg_color=BG_LOG, text_color=FG_TEXT, font=("Consolas", 11), wrap="none")
        box.pack(fill="both", expand=True, padx=12, pady=(6,10))
        self._boxes["Overview"] = box

    def _write(self, name, text):
        box = self._boxes[name]
        box.configure(state="normal"); box.delete("1.0", "end")
        box.insert("end", text or "No records yet."); box.configure(state="disabled")

    def refresh(self):
        d = self.catalog.dashboard()
        roots = self.catalog.storage_roots()
        quality = self.catalog.phd2_quality_rows()
        for key, label in self._metrics.items():
            label.configure(text=_human_bytes(d[key]) if key == "bytes" else f"{d[key] or 0:,}")
        self._overview_note.configure(text=(
            f"{d['targets'] or 0} targets  /  {len(roots)} storage locations  /  "
            f"{d['duplicate_groups']:,} duplicate groups ({_human_bytes(d['duplicate_bytes'])})  /  "
            f"{d['cleanup_files']:,} eligible raw copies ({_human_bytes(d['cleanup_bytes'])})"
            if d["files"] else "No files indexed yet. Scan your archive destination or choose an existing folder."))
        self._write("Overview", "\n".join(
            f"{r.get('capture_date') or 'Unknown':<12}  {r['score']:>5.1f}/100  RMS {r['rms_total']:.3f}  {Path(r['path']).name}"
            for r in quality[:10]) or "No PHD2 runs indexed. Open PHD2 Guiding to sync or browse for a log.")

        self._write("Sessions", "\n".join(
            f"{r['capture_date'] or 'Unknown':<12}  {(r['camera'] or 'Unknown'):<18}  {(r['target'] or 'Unknown'):<24} "
            f"{r['files']:>5} files  {r['integration_seconds']/3600:>7.2f} h  {_human_bytes(r['bytes'])}"
            for r in self.catalog.sessions()))
        sessions = self.catalog.sessions()
        self._session_values = {
            f"{r['capture_date'] or 'Unknown'} · {r['camera'] or 'Unknown'} · {r['target'] or 'Unknown'}":
            (r["capture_date"], r["camera"], r["target"]) for r in sessions}
        values = list(self._session_values) or ["No sessions indexed"]
        self._session_combo.configure(values=values)
        if self._session_var.get() not in self._session_values:
            self._session_var.set(values[0])
        quality = self.catalog.quality_rows()
        self._write("Quality", "\n".join(
            f"{r['score']:>5.1f}/100  FWHM {r['fwhm']:>6.2f}px  Ecc {r['eccentricity']:>5.3f}  "
            f"Stars {r['star_count']:>5}  BG {r['background']:>9.1f}  "
            f"{'CLOUD ' if r['clouds_likely'] else ''}{'TRAIL ' if r['trailing_likely'] else ''}"
            f"{'FOCUS-DRIFT ' if r['focus_drift'] else ''}{'POOR' if r['poor_frame'] else ''}\n  {r['path']}"
            for r in sorted(quality, key=lambda row: (row['score'], row['path']), reverse=True)))
        self._write("Masters", "\n\n".join(
            f"{'VERIFIED' if r['verified'] else 'UNVERIFIED'}  {r['target'] or 'Unknown'} / {r['filter_name'] or 'No filter'}\n"
            f"  {r['sub_count'] or '?'} subs · {(r['integration_seconds'] or 0)/3600:.2f} h · SHA256 {(r['checksum'] or 'none')[:16]}…\n  {r['path']}"
            for r in self.catalog.masters()))
        self._write("Targets", "\n".join(
            f"{(r['target'] or 'Unknown'):<28} {(r['filter_name'] or 'All'):<8}  "
            f"{r['collected_seconds']/3600:>7.2f} h / {r['goal_seconds']/3600:>7.2f} h  "
            f"{(100*r['collected_seconds']/r['goal_seconds'] if r['goal_seconds'] else 0):>6.1f}%  {r['frames']} frames"
            for r in self.catalog.target_progress()))
        candidates = self.catalog.cleanup_candidates()
        self._write("Cleanup", "\n\n".join(
            f"{_human_bytes(r['size']):>10}  {r['target'] or 'Unknown'} / {r['filter_name'] or 'No filter'}\n"
            f"  RAW    {r['path']}\n  MASTER {r['master_path']}"
            for r in candidates))
        self._write("Duplicates", "\n\n".join(
            f"{r['copies']} copies · {_human_bytes(r['size'])} each · SHA256 {r['sha256'][:16]}…\n{r['paths']}"
            for r in self.catalog.duplicate_groups()))
        self._write("Storage", "\n\n".join(
            f"{('ONLINE ' if r['online'] else 'OFFLINE') if r['online'] is not None else 'CACHED '}  {r['label']} [{r['role']}]\n"
            f"  Free {_human_bytes(r.get('free_bytes'))} / {_human_bytes(r.get('total_bytes'))}\n  {r['path']}"
            for r in roots))
        self._write("Audit", "\n".join(
            f"#{r['id']:<5} {r['timestamp']}  {r['operation']:<15} {r['status']:<9} "
            f"{'UNDO' if r['undoable'] and not r['undone'] else '':<5}  {r['src'] or ''}  →  {r['dst'] or ''}"
            for r in self.catalog.audit_rows()))
        self._run_search()

    def _run_search(self):
        rows = self.catalog.search_files(self._search_var.get())
        self._write("Search", "\n".join(
            f"{r['capture_date'] or 'Unknown':<12} {(r['frame_type'] or r['kind'] or ''):<9} "
            f"{(r['target'] or ''):<24} {(r['filter_name'] or ''):<7} {_human_bytes(r['size']):>10}  {r['path']}"
            for r in rows))

    def _choose_scan(self):
        path = filedialog.askdirectory(title="Choose astronomy folder to catalogue")
        if path: self._scan(Path(path), Path(path).name, "archive")

    def scan_configured(self):
        value = load_config().get("dest", "").strip()
        if not value or not Path(value).is_dir():
            messagebox.showerror("Destination required", "Choose a valid destination on Sort Files first.")
            return
        self._scan(Path(value), "Primary archive", "archive")

    def _scan(self, path, label, role):
        self._status_var.set(f"Scanning {path}…")
        self._scan_controller = ScanController(hash_rate_mbps=50)
        def work():
            started = __import__('time').monotonic()
            try:
                result = self.catalog.scan(path, label, role,
                    controller=self._scan_controller,
                    progress=lambda i,n,p: self.after(0, lambda i=i,n=n,started=started:
                        self._status_var.set(f"Indexing {i}/{n} · ETA {((__import__('time').monotonic()-started)/max(i,1)*(n-i)):.0f}s")))
                text = (f"Scan complete · {result.indexed} new · {result.updated} updated · "
                        f"{result.unchanged} unchanged · {result.masters} masters · {result.errors} errors")
            except Exception as exc: text = f"Scan failed: {exc}"
            self._scan_controller = None
            self.after(0, lambda: (self._status_var.set(text), self.refresh()))
        threading.Thread(target=work, daemon=True).start()

    def _pause_scan(self):
        if not self._scan_controller: return
        if self._scan_controller.pause_event.is_set():
            self._scan_controller.resume(); self._status_var.set("Scan resumed")
        else:
            self._scan_controller.pause(); self._status_var.set("Scan paused")

    def _cancel_scan(self):
        if self._scan_controller:
            self._scan_controller.cancel(); self._status_var.set("Cancelling scan…")

    def _set_goal(self):
        target = simpledialog.askstring("Target goal", "Target name:", parent=self)
        if not target: return
        filt = simpledialog.askstring("Target goal", "Filter (leave blank for none):", parent=self) or ""
        hours = simpledialog.askfloat("Target goal", "Integration goal in hours:", minvalue=0, parent=self)
        if hours is not None: self.catalog.set_target_goal(target, filt, hours); self.refresh()

    def _set_alias(self):
        alias = simpledialog.askstring("Target alias", "Alias currently found in files (for example NGC 1976):", parent=self)
        if not alias: return
        canonical = simpledialog.askstring("Target alias", "Canonical target name (for example M 42):", parent=self)
        if not canonical: return
        try: self.catalog.set_target_alias(alias, canonical)
        except ValueError as exc: messagebox.showerror("Alias", str(exc)); return
        self.refresh()

    def _selected_session(self):
        value = self._session_values.get(self._session_var.get())
        if not value: messagebox.showinfo("Session", "Choose an indexed session first.")
        return value

    def _open_session(self):
        selected = self._selected_session()
        if not selected: return
        detail = self.catalog.session_detail(*selected)
        files, meta = detail["files"], detail["meta"]
        quality = [row for row in files if row.get("score") is not None]
        lines = [
            f"SESSION  {detail['capture_date'] or 'Unknown'} · {detail['camera'] or 'Unknown'} · {detail['target'] or 'Unknown'}",
            "="*96,
            f"Rating: {'★'*meta.get('rating',0)}{'☆'*(5-meta.get('rating',0))}    Tags: {meta.get('tags','')}",
            f"Files: {len(files)}    Masters: {len(detail['masters'])}    PHD2: {len(detail['phd2'])}    Weather samples: {len(detail['weather'])}",
        ]
        if quality:
            lines += [f"Quality average: {sum(r['score'] for r in quality)/len(quality):.1f}/100    "
                      f"Poor: {sum(bool(r.get('poor_frame')) for r in quality)}    "
                      f"Cloud warnings: {sum(bool(r.get('clouds_likely')) for r in quality)}    "
                      f"Trails: {sum(bool(r.get('trailing_likely')) for r in quality)}"]
        if detail["phd2"]:
            lines += [f"Guiding: {sum(r['score'] for r in detail['phd2'])/len(detail['phd2']):.1f}/100 average quality"]
        if detail["weather"]:
            temps = [r['temperature'] for r in detail['weather'] if r['temperature'] is not None]
            clouds = [r['cloud_percent'] for r in detail['weather'] if r['cloud_percent'] is not None]
            lines += [f"Weather: temperature {sum(temps)/len(temps):.1f} C" if temps else "Weather: temperature unavailable",
                      f"         cloud {sum(clouds)/len(clouds):.0f}% average" if clouds else "         cloud unavailable"]
        lines += ["", "NOTES", meta.get("notes","") or "No notes.", "", "TIMELINE"]
        lines += [f"{r['timestamp']}  {r['event_type']}  {r['details'] or ''}" for r in detail["events"]] or ["No timeline events yet."]
        lines += ["", "FILES"]
        for row in files:
            q = f" Q {row['score']:.1f}" if row.get("score") is not None else ""
            flags = (" CLOUD" if row.get("clouds_likely") else "") + (" TRAIL" if row.get("trailing_likely") else "")
            lines.append(f"{(row.get('frame_type') or row.get('kind') or ''):<9} {(row.get('filter_name') or ''):<7}{q:<9}{flags:<14} {row['path']}")
        lines += ["", "MASTERS"] + [m["path"] for m in detail["masters"]]
        self._write("Session", "\n".join(lines))
        light = next((Path(r["path"]) for r in files if Path(r["path"]).suffix.lower() in IMAGING_EXTENSIONS), None)
        if light and light.is_file():
            cache = self.catalog.db_path.parent / "thumbnails" / f"{abs(hash(str(light)))}.png"
            def work():
                try:
                    create_thumbnail(light, cache)
                    def show():
                        with Image.open(cache) as opened:
                            pil = opened.copy()
                        self._thumb_image = ctk.CTkImage(light_image=pil, dark_image=pil, size=pil.size)
                        self._thumbnail_label.configure(image=self._thumb_image, text="")
                    self.after(0, show)
                except Exception as exc:
                    self.after(0, lambda e=str(exc): self._thumbnail_label.configure(text=f"Thumbnail unavailable: {e}", image=None))
            threading.Thread(target=work, daemon=True).start()

    def _edit_session(self):
        selected = self._selected_session()
        if not selected: return
        current = self.catalog.session_detail(*selected)["meta"]
        rating = simpledialog.askinteger("Session rating", "Rating from 0 to 5:", initialvalue=current.get("rating",0), minvalue=0, maxvalue=5, parent=self)
        if rating is None: return
        tags = simpledialog.askstring("Session tags", "Comma-separated tags:", initialvalue=current.get("tags",""), parent=self)
        if tags is None: return
        notes = simpledialog.askstring("Session notes", "Observing and processing notes:", initialvalue=current.get("notes",""), parent=self)
        if notes is None: return
        self.catalog.set_session_meta(*selected, rating, notes, tags)
        self.catalog.record_session_event(*selected, "notes_updated", f"Rating {rating}/5; tags {tags}")
        self._open_session()

    def _export_session(self, kind):
        selected = self._selected_session()
        if not selected: return
        suffix = ".csv" if kind == "csv" else ".pdf"
        path = filedialog.asksaveasfilename(defaultextension=suffix,
            filetypes=[(kind.upper(), f"*{suffix}")], initialfile=f"session_{selected[0] or 'unknown'}{suffix}")
        if not path: return
        if kind == "csv": self.catalog.export_session_csv(*selected, Path(path))
        else: self.catalog.export_session_pdf(*selected, Path(path))
        messagebox.showinfo("Session report", f"Saved to:\n{path}")

    def _import_weather(self):
        path = filedialog.askopenfilename(title="Import weather CSV", filetypes=[("CSV", "*.csv")])
        if not path: return
        try: count = self.catalog.import_weather_csv(Path(path))
        except Exception as exc: messagebox.showerror("Weather import", str(exc)); return
        messagebox.showinfo("Weather import", f"Imported {count} weather samples.")
        self._open_session()

    def _analyse_quality(self):
        if self._quality_batch: return
        pending = self.catalog.pending_quality_paths()
        if not pending:
            messagebox.showinfo("Image quality", "All indexed light frames are already analysed."); return
        self._quality_batch = QualityBatch()
        self._status_var.set(f"Analysing {len(pending)} light frames…")
        def progress(i,n,path,result,eta):
            self.after(0, lambda: self._status_var.set(
                f"Image quality {i}/{n} · {result.score:.0f}/100 · ETA {eta:.0f}s · {path.name}"))
        def work():
            try: self.catalog.analyse_pending_quality(self._quality_batch, progress)
            except Exception as exc: self.after(0, lambda e=str(exc): messagebox.showerror("Image quality", e))
            finally:
                self._quality_batch = None
                self.after(0, lambda: (self._status_var.set("Image-quality analysis complete"), self.refresh()))
        threading.Thread(target=work, daemon=True).start()

    def _pause_quality(self):
        if not self._quality_batch: return
        if self._quality_batch._pause.is_set():
            self._quality_batch.resume(); self._status_var.set("Image analysis resumed")
        else:
            self._quality_batch.pause(); self._status_var.set("Image analysis paused")

    def _cancel_quality(self):
        if self._quality_batch:
            self._quality_batch.cancel(); self._status_var.set("Cancelling image analysis…")

    def _cleanup(self):
        rows = self.catalog.cleanup_candidates()
        if not rows:
            messagebox.showinfo("Nothing eligible", "No raw light has both a verified master and a byte-identical backup.")
            return
        size = sum(r["size"] for r in rows)
        if not messagebox.askyesno("Review raw-light cleanup",
            f"{len(rows)} raw lights use {_human_bytes(size)}.\n\nThe app will keep at least one raw copy per checksum group. Continue?"):
            return
        word = simpledialog.askstring("Final confirmation", "Type DELETE to remove eligible copies:", parent=self)
        try: result = self.catalog.delete_raws([Path(r["path"]) for r in rows], word or "")
        except PermissionError as exc: messagebox.showerror("Cleanup cancelled", str(exc)); return
        messagebox.showinfo("Cleanup complete", f"Deleted {len(result['deleted'])}; preserved/refused {len(result['refused'])}; errors {len(result['errors'])}.")
        self.refresh()

    def _add_storage(self):
        path = filedialog.askdirectory(title="Choose mounted PC, NAS, Google Drive or OneCloud folder")
        if not path: return
        label = simpledialog.askstring("Storage label", "Name this location:", initialvalue=Path(path).name, parent=self)
        role = simpledialog.askstring("Storage role", "Role (archive, backup, cloud, sd-card):", initialvalue="backup", parent=self)
        if label: self.catalog.add_storage_root(Path(path), label, role or "backup"); self.refresh()

    def _refresh_storage(self):
        roots = self.catalog.storage_roots()
        if not roots: return
        self._status_var.set("Refreshing storage locations in background…")
        def work():
            for index, root in enumerate(roots, 1):
                self.after(0, lambda i=index,n=len(roots),name=root['label']:
                           self._status_var.set(f"Storage {i}/{n}: {name}"))
                self.catalog.refresh_storage_root(root["id"])
            self.after(0, lambda: (self._status_var.set("Storage refresh complete"), self.refresh()))
        threading.Thread(target=work, daemon=True).start()

    def _backup_database(self):
        path = filedialog.asksaveasfilename(defaultextension=".sqlite3",
            filetypes=[("SQLite database", "*.sqlite3")],
            initialfile=f"asiair_catalog_backup_{datetime.now():%Y%m%d}.sqlite3")
        if not path: return
        try: self.catalog.backup_database(Path(path))
        except Exception as exc: messagebox.showerror("Database backup", str(exc)); return
        messagebox.showinfo("Database backup", f"Verified SQLite backup saved to:\n{path}")

    def _check_database(self):
        health = self.catalog.database_health(False)
        if health["ok"]:
            messagebox.showinfo("Database health", "Integrity check passed: OK")
            return
        if not messagebox.askyesno("Database health", f"Integrity check returned: {health['before']}\n\nBack up and attempt REINDEX/VACUUM repair?"):
            return
        backup = self.catalog.db_path.with_name(f"catalog_before_repair_{datetime.now():%Y%m%d_%H%M%S}.sqlite3")
        self.catalog.backup_database(backup)
        result = self.catalog.database_health(True)
        messagebox.showinfo("Database repair", f"Backup: {backup}\nResult: {result['after']}")

    def _undo(self):
        audit_id = simpledialog.askinteger("Undo operation", "Audit operation ID to undo:", minvalue=1, parent=self)
        if audit_id is None: return
        if self.catalog.undo(audit_id): messagebox.showinfo("Undo complete", f"Operation #{audit_id} was undone.")
        else: messagebox.showerror("Cannot undo", "The operation is not undoable, was already undone, or its file changed.")
        self.refresh()

    def _toggle_watcher(self):
        if self._watcher: self._watcher.stop(); self._watcher = None
        if not self._watch_var.get(): self._status_var.set("ASIAIR watcher stopped"); return
        value = load_config().get("source", "").strip()
        if not value or not Path(value).is_dir():
            self._watch_var.set(False); messagebox.showerror("Source required", "Choose a valid ASIAIR source first."); return
        source = Path(value)
        def changed(path):
            self.after(0, lambda: self._status_var.set("New ASIAIR files detected; updating index…"))
            try: self.catalog.scan(path, "ASIAIR SD card", "sd-card", hash_duplicates=False)
            finally: self.after(0, self.refresh)
        self._watcher = SourceWatcher(source, changed); self._watcher.start()
        self._status_var.set(f"Watching {source}")

    def shutdown(self):
        if self._watcher: self._watcher.stop()
        if self._scan_controller: self._scan_controller.cancel()
        if self._quality_batch: self._quality_batch.cancel()

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")
ctk.ThemeManager.theme["CTkButton"].update(
    fg_color=BTN_MOVE, hover_color=BORDER, text_color=FG_TEXT, corner_radius=6)
ctk.ThemeManager.theme["CTkCheckBox"].update(fg_color=ACCENT, hover_color=ACCENT_HL)
ctk.ThemeManager.theme["CTkSegmentedButton"].update(
    selected_hover_color=ACCENT_HL, unselected_color=BG_INPUT,
    unselected_hover_color=BTN_MOVE, text_color=FG_TEXT)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.catalog = Catalog()
        self.title("ASIAIR Session Sorter")
        self.geometry("1240x800")
        self.minsize(1060, 650)
        self.configure(fg_color=BG_APP)
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self):
        rail = ctk.CTkFrame(self, fg_color=BG_INPUT, width=156, corner_radius=0)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        ctk.CTkFrame(self, width=1, fg_color=BORDER, corner_radius=0).pack(side="left", fill="y")
        mark = tk.Canvas(rail, width=32, height=32, bg=BG_INPUT, highlightthickness=0)
        mark.pack(anchor="w", padx=17, pady=(22,4))
        mark.create_oval(5, 5, 27, 27, outline=FG_SIGNAL)
        mark.create_line(16, 0, 16, 11, fill=FG_SIGNAL)
        mark.create_line(16, 21, 16, 32, fill=FG_SIGNAL)
        mark.create_line(0, 16, 11, 16, fill=FG_SIGNAL)
        mark.create_line(21, 16, 32, 16, fill=FG_SIGNAL)
        mark.create_oval(14, 14, 18, 18, outline=FG_SIGNAL, fill=FG_SIGNAL)
        ctk.CTkLabel(rail, text="Session Sorter", text_color=FG_TEXT,
                     font=ctk.CTkFont(size=17, weight="bold")).pack(anchor="w", padx=17)
        ctk.CTkLabel(rail, text="ASIAIR  /  " + VERSION, text_color=FG_MUTED,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=17, pady=(0,22))

        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(side="left", fill="both", expand=True, padx=20, pady=(16,16))
        self._heading = ctk.CTkLabel(main, text="", text_color=FG_TEXT,
                                     font=ctk.CTkFont(size=28, weight="bold"), anchor="w")
        self._heading.pack(fill="x")
        self._description = ctk.CTkLabel(main, text="", text_color=FG_MUTED, anchor="w",
                                         font=ctk.CTkFont(size=13))
        self._description.pack(fill="x", pady=(0,11))
        pages = ctk.CTkFrame(main, fg_color="transparent")
        pages.pack(fill="both", expand=True)
        pages.grid_rowconfigure(0, weight=1)
        pages.grid_columnconfigure(0, weight=1)

        self.library = LibraryFrame(pages, self.catalog)
        viewer = PHD2ViewerFrame(pages, self.catalog)
        def sorted_complete():
            viewer.refresh_library(auto_load=True)
            self.library.scan_configured()
        sorter = SortFrame(pages, on_complete=sorted_complete, catalog=self.catalog)
        self._pages = {
            "Import": (sorter, "Import captures", "Preview the folder plan, then verify copies before ejecting."),
            "Library": (self.library, "Astrophotography library", "Browse sessions, assess lights, and track your archive."),
            "PHD2 Guiding": (viewer, "PHD2 guiding", "Review guide logs, correction pulses, and dithers."),
        }
        self._nav = {}
        for name, (page, _, _) in self._pages.items():
            page.grid(row=0, column=0, sticky="nsew")
            button = ctk.CTkButton(rail, text=name, anchor="w", height=39, border_spacing=10,
                                   command=lambda n=name: self._show_page(n))
            button.pack(fill="x", padx=9, pady=3)
            self._nav[name] = button
        self._show_page("Import")

    def _show_page(self, name):
        page, heading, description = self._pages[name]
        page.tkraise()
        self._heading.configure(text=heading)
        self._description.configure(text=description)
        for key, button in self._nav.items():
            button.configure(fg_color=BTN_MOVE if key == name else "transparent",
                             hover_color=BORDER if key == name else BTN_MOVE,
                             text_color=FG_SIGNAL if key == name else FG_MUTED)

    def _close(self):
        self.library.shutdown()
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    App().mainloop()
