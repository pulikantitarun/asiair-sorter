#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║         ASIAIR Astrophotography Session Sorter  v2.3         ║
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
    HAS_MPL = True
except ImportError:
    try:
        _pip("matplotlib", "numpy")
        import numpy as np
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        import matplotlib.gridspec as gridspec
        HAS_MPL = True
    except Exception:
        HAS_MPL = False

# ── Standard library ──────────────────────────────────────────────────────────
import json, os, re, shutil, threading, tkinter as tk
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import List, Optional

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
VERSION     = "2.3"

# ── UI palette ────────────────────────────────────────────────────────────────
BG_APP     = "#0d1117"
BG_CARD    = "#1c2333"
BG_INPUT   = "#161b22"
BG_LOG     = "#0d1117"
FG_TEXT    = "#e6edf3"
FG_MUTED   = "#8b949e"
BORDER     = "#30363d"
ACCENT     = "#1f6feb"
ACCENT_HL  = "#388bfd"
BTN_MOVE   = "#2d333b"
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
    "fig":   "#0d1117",
    "ax":    "#161b22",
    "grid":  "#21262d",
    "tick":  "#6e7681",
    "text":  "#8b949e",
    "ra":    "#58a6ff",
    "dec":   "#f78166",
    "snr":   "#e3b341",
    "dither":"#30363d",
    "pause": "#1c2333",
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

def classify_file(fp: Path, src_root: Path):
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
        ft = _find_frame_type(parts)
        camera, filename_filter = _find_camera_and_filter_from_filename(filename) if ft=="Light" else (None,None)
        return {"action":"copy_fits","frame_type":ft,
                "date":_find_date(parts,filename),
                "target":_find_target(parts, filename) if ft=="Light" else None,
                "camera":camera,
                "filter_name":filename_filter or (_find_filter(parts) if ft=="Light" else None)}
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


class PHD2ViewerFrame(ctk.CTkFrame):
    def __init__(self, parent):
        super().__init__(parent, fg_color="transparent")
        self._sessions: List[PHD2Session] = []
        self._library_paths = {}
        self._syncing = False
        self._auto_sync_var = tk.BooleanVar(value=True)
        self._fig = self._canvas = self._toolbar = None
        self._ax_err = self._ax_snr = None
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
        labels = ["RA RMS","Dec RMS","Total RMS","P95 Error","Peak Error","Duration",
                  "Frames","Cadence","Dithers","Lost Frames","Avg SNR","Avg HFD"]
        for i, lbl in enumerate(labels):
            sb = StatBox(si, lbl)
            sb.grid(row=i//6, column=i%6, padx=4, pady=3, sticky="ew")
            si.columnconfigure(i%6, weight=1)
            self._sb[lbl] = sb

        # Equipment / session info
        self._info_lbl = ctk.CTkLabel(self, text="",
                                       font=ctk.CTkFont(size=11, slant="italic"),
                                       text_color=FG_MUTED, anchor="w")
        self._info_lbl.pack(fill="x", padx=6, pady=(0,4))

        # Graph, complete details, and raw event views
        detail_tabs = ctk.CTkTabview(self, fg_color=BG_CARD,
                                     segmented_button_fg_color=BG_INPUT,
                                     segmented_button_selected_color=ACCENT)
        detail_tabs.pack(fill="both", expand=True)
        detail_tabs.add("Graphs"); detail_tabs.add("Details"); detail_tabs.add("Events")
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
        else:
            ctk.CTkLabel(
                self._graph_card,
                text="Install matplotlib to enable PHD2 graphs:\n\n"
                     "  pip install matplotlib",
                font=ctk.CTkFont(size=13), text_color=FG_MUTED,
            ).place(relx=0.5, rely=0.5, anchor="center")

    def _init_figure(self):
        self._fig = Figure(dpi=96)
        self._fig.patch.set_facecolor(MC["fig"])
        gs = gridspec.GridSpec(2, 1, figure=self._fig,
                               height_ratios=[2.8, 1], hspace=0.06)
        self._ax_err = self._fig.add_subplot(gs[0])
        self._ax_snr = self._fig.add_subplot(gs[1], sharex=self._ax_err)

        _style_ax(self._ax_err, ylabel="Guide Error (arcsec)")
        _style_ax(self._ax_snr, ylabel="Star SNR")
        self._ax_snr.set_xlabel("Time (minutes)", color=MC["text"], fontsize=9)
        self._ax_err.tick_params(labelbottom=False)

        for ax in (self._ax_err, self._ax_snr):
            ax.text(0.5, 0.5, "Load a PHD2 log to see guiding data",
                    transform=ax.transAxes, ha="center", va="center",
                    color=FG_MUTED, fontsize=11)

        self._canvas = FigureCanvasTkAgg(self._fig, master=self._graph_card)

        # Toolbar with basic dark styling
        tb_frame = tk.Frame(self._graph_card, bg="#161b22", bd=0)
        tb_frame.pack(side="bottom", fill="x")
        self._toolbar = NavigationToolbar2Tk(self._canvas, tb_frame, pack_toolbar=False)
        self._toolbar.config(bg="#161b22")
        for ch in self._toolbar.winfo_children():
            try: ch.config(bg="#161b22", fg=FG_TEXT,
                           highlightbackground="#161b22",
                           activebackground=BORDER, activeforeground=FG_TEXT)
            except Exception: pass
        self._toolbar.update()
        self._toolbar.pack(side="bottom", fill="x")

        self._canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=(6,0))
        self._canvas.draw()

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
        self._update_stats(sessions)
        if HAS_MPL:
            self._draw_graphs(sessions)

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
        ax_err.cla(); ax_snr.cla()

        units = sessions[0].units
        _style_ax(ax_err, ylabel=f"Guide Error ({units})")
        _style_ax(ax_snr, ylabel="Star SNR")
        ax_snr.set_xlabel("Time (minutes)", color=MC["text"], fontsize=9)
        ax_err.tick_params(labelbottom=False)

        GAP_MIN = 3.0   # gap between sessions (minutes)
        t_offset = 0.0

        all_t    = []
        all_ra   = []
        all_dec  = []
        all_snr_t= []
        all_snr  = []
        dither_t = []   # dither event times (in plot minutes)

        for i, s in enumerate(sessions):
            if s.n_frames == 0: continue

            t_min = (s.times - s.times[0]) / 60 + t_offset

            # RA / Dec
            all_t.extend(t_min)
            all_ra.extend(s.ra_err)
            all_dec.extend(s.dec_err)

            # Dither markers
            for dt in s.dithers:
                rel = (dt - s.times[0]) / 60 + t_offset
                dither_t.append(rel)

            # SNR (may have fewer points if some frames dropped)
            if len(s.snr):
                snr_t = np.linspace(t_min[0], t_min[-1], len(s.snr))
                all_snr_t.extend(snr_t)
                all_snr.extend(s.snr)

            t_end = float(t_min[-1])

            # Insert NaN break so lines don't connect across sessions
            if i < len(sessions) - 1:
                pause_end = t_end + GAP_MIN
                all_t.append(t_end + GAP_MIN/2);  all_ra.append(float("nan"))
                all_dec.append(float("nan"))
                if all_snr:
                    all_snr_t.append(t_end + GAP_MIN/2); all_snr.append(float("nan"))
                # Shade the gap
                for ax in (ax_err, ax_snr):
                    ax.axvspan(t_end, pause_end, color=MC["pause"], alpha=0.6,
                               label="_nolegend_")
                t_offset = pause_end
            else:
                t_offset = t_end + GAP_MIN

        all_t   = np.array(all_t,   dtype=float)
        all_ra  = np.array(all_ra,  dtype=float)
        all_dec = np.array(all_dec, dtype=float)
        valid   = ~np.isnan(all_ra)

        # Compute aggregate RMS from valid frames only
        rms_ra  = float(np.sqrt(np.mean(all_ra[valid]**2)))
        rms_dec = float(np.sqrt(np.mean(all_dec[valid]**2)))
        u = '"' if units == "arcsec" else " px"

        # ── Error plot ───────────────────────────────────────────────
        ax_err.plot(all_t, all_ra,  color=MC["ra"],  lw=0.85, alpha=0.85,
                    label=f"RA  RMS={rms_ra:.2f}{u}")
        ax_err.plot(all_t, all_dec, color=MC["dec"], lw=0.85, alpha=0.85,
                    label=f"Dec RMS={rms_dec:.2f}{u}")

        # ±RMS reference bands
        for rms, colour in [(rms_ra, MC["ra"]), (-rms_ra, MC["ra"]),
                             (rms_dec, MC["dec"]), (-rms_dec, MC["dec"])]:
            ax_err.axhline(rms, color=colour, lw=0.8, ls="--", alpha=0.40)
        ax_err.axhline(0, color=BORDER, lw=0.9)

        # Dither markers
        for dt in dither_t:
            ax_err.axvline(dt, color=MC["dither"], lw=1.2, ls=":", alpha=0.7)
        if dither_t:
            # Dummy line for legend entry
            ax_err.axvline(dither_t[0], color=MC["dither"], lw=1.2, ls=":",
                           alpha=0.7, label=f"Dither ×{len(dither_t)}")

        leg = ax_err.legend(facecolor="#1c2333", edgecolor=BORDER,
                             fontsize=9, labelcolor=FG_TEXT, loc="upper right")

        # ── SNR plot ─────────────────────────────────────────────────
        if all_snr:
            ax_snr.plot(np.array(all_snr_t, dtype=float),
                        np.array(all_snr,   dtype=float),
                        color=MC["snr"], lw=0.85, alpha=0.85, label="Star SNR")
            ax_snr.legend(facecolor="#1c2333", edgecolor=BORDER,
                           fontsize=9, labelcolor=FG_TEXT, loc="upper right")
        else:
            ax_snr.text(0.5, 0.5, "No SNR data in this log",
                        transform=ax_snr.transAxes, ha="center", va="center",
                        color=FG_MUTED, fontsize=10)

        self._fig.tight_layout(pad=1.0)
        self._canvas.draw()


# ══════════════════════════════════════════════════════════════════════════════
# Sort Files UI
# ══════════════════════════════════════════════════════════════════════════════

class FolderCard(ctk.CTkFrame):
    def __init__(self, parent, label: str, var: tk.StringVar, **kw):
        super().__init__(parent, fg_color=BG_CARD, corner_radius=10, **kw)
        lf = ctk.CTkFrame(self, fg_color="transparent")
        lf.pack(fill="x", padx=16, pady=(12,4))
        ctk.CTkLabel(lf, text=label, font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=FG_TEXT).pack(side="left")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0,12))
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
    def __init__(self, parent, on_complete=None):
        super().__init__(parent, fg_color="transparent")
        self._on_complete = on_complete
        cfg = load_config()
        self.src_var  = tk.StringVar(value=cfg.get("source",""))
        self.dst_var  = tk.StringVar(value=cfg.get("dest",""))
        self.dry_var  = tk.BooleanVar(value=False)
        self.prog_var = tk.DoubleVar(value=0.0)
        self._running = False
        self._build_ui()

    def _build_ui(self):
        FolderCard(self, "⬤  SD Card / Asiair Source Folder",
                   self.src_var).pack(fill="x", pady=(0,8))
        FolderCard(self, "⬤  Destination Storage Folder",
                   self.dst_var).pack(fill="x", pady=(0,10))

        # Options + buttons
        ctrl = ctk.CTkFrame(self, fg_color="transparent")
        ctrl.pack(fill="x", pady=(0,10))
        ctk.CTkCheckBox(ctrl, text="Dry Run  —  preview only, no files touched",
                        variable=self.dry_var,
                        font=ctk.CTkFont(size=12), text_color=FG_TEXT,
                        checkmark_color="#ffffff", border_color=BORDER,
                        ).pack(side="left", padx=(0,20))
        self.cancel_btn = ctk.CTkButton(
            ctrl, text="■  Cancel", width=110, height=36,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=BTN_CANCEL, hover_color="#8b3a3a",
            state="disabled", command=self._cancel)
        self.cancel_btn.pack(side="right", padx=(6,0))
        self.move_btn = ctk.CTkButton(
            ctrl, text="✂  Move Files", width=130, height=36,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=BTN_MOVE, hover_color="#3d444d",
            border_color=BORDER, border_width=1,
            command=lambda: self._start("move"))
        self.move_btn.pack(side="right", padx=(6,0))
        self.copy_btn = ctk.CTkButton(
            ctrl, text="▶  Copy Files", width=130, height=36,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HL,
            command=lambda: self._start("copy"))
        self.copy_btn.pack(side="right")

        # Progress
        prog_card = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=10)
        prog_card.pack(fill="x", pady=(0,10))
        pi = ctk.CTkFrame(prog_card, fg_color="transparent")
        pi.pack(fill="x", padx=16, pady=10)
        self.prog_bar = ctk.CTkProgressBar(pi, height=14, corner_radius=7,
                                            fg_color="#21262d",
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
        ctk.CTkLabel(lh, text="Output Log",
                     font=ctk.CTkFont(size=12, weight="bold"),
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

    def _start(self, mode):
        src = self.src_var.get().strip(); dst = self.dst_var.get().strip()
        if not src: messagebox.showerror("Missing source","Please select a source folder."); return
        if not dst: messagebox.showerror("Missing destination","Please select a destination folder."); return
        if not Path(src).is_dir(): messagebox.showerror("Invalid source", f"Not found:\n{src}"); return
        if Path(src) == Path(dst): messagebox.showerror("Same folder","Source and destination must differ."); return
        save_config({"source":src,"dest":dst})
        self._clear_log(); self._set_progress(0)
        self._running = True; self._set_busy(True)
        threading.Thread(target=self._worker,
                         args=(Path(src),Path(dst),mode,self.dry_var.get()),
                         daemon=True).start()

    def _cancel(self):
        self._running = False
        self._log("\n  Cancelling — finishing current file…","skip")

    def _worker(self, src, dst, mode, dry_run):
        try: self._sort(src, dst, mode, dry_run)
        except Exception as e: self._log(f"\nFATAL ERROR: {e}","err")
        finally:
            self._running = False; self._set_busy(False)
            if self._on_complete and not dry_run:
                self.after(0, self._on_complete)

    def _sort(self, src, dst, mode, dry_run):
        label  = "[DRY RUN]  " if dry_run else ""
        action = "move" if mode=="move" else "copy"
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
            info = classify_file(fp, src)
            if info is None:
                self._log(f"  SKIP      {fp.name}","skip"); counts["skip"]+=1; continue

            if info["action"] == "delete_preview":
                self._log(f"  DELETE    {fp.name}  (preview — removed from SD)","del")
                if not dry_run:
                    try: fp.unlink(); counts["deleted"]+=1
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
                    (shutil.move if mode=="move" else shutil.copy2)(str(fp),str(dest_fp))
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


# ══════════════════════════════════════════════════════════════════════════════
# Main Application
# ══════════════════════════════════════════════════════════════════════════════

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("ASIAIR Session Sorter")
        self.geometry("800x700")
        self.minsize(700, 580)
        self.configure(fg_color=BG_APP)
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0, height=58)
        hdr.pack(fill="x"); hdr.pack_propagate(False)
        inner = ctk.CTkFrame(hdr, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(inner, text="✦  ASIAIR Session Sorter",
                     font=ctk.CTkFont(size=17, weight="bold"),
                     text_color=FG_TEXT).pack(side="left", padx=(0,10))
        ctk.CTkLabel(inner, text=f"v{VERSION}",
                     font=ctk.CTkFont(size=11),
                     text_color=FG_MUTED).pack(side="left", pady=(4,0))
        ctk.CTkFrame(self, fg_color=ACCENT, height=2,
                     corner_radius=0).pack(fill="x")

        # ── Tab view ──────────────────────────────────────────────────
        tabs = ctk.CTkTabview(self, fg_color=BG_APP,
                               segmented_button_fg_color=BG_CARD,
                               segmented_button_selected_color=ACCENT,
                               segmented_button_selected_hover_color=ACCENT_HL,
                               segmented_button_unselected_color=BG_CARD,
                               segmented_button_unselected_hover_color=BTN_MOVE,
                               text_color=FG_TEXT,
                               text_color_disabled=FG_MUTED)
        tabs.pack(fill="both", expand=True, padx=14, pady=(8,12))
        tabs.add("Sort Files")
        tabs.add("PHD2 Viewer")

        viewer = PHD2ViewerFrame(tabs.tab("PHD2 Viewer"))
        SortFrame(tabs.tab("Sort Files"),
                  on_complete=lambda: viewer.refresh_library(auto_load=True)).pack(
                      fill="both", expand=True)
        viewer.pack(fill="both", expand=True)


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    App().mainloop()
