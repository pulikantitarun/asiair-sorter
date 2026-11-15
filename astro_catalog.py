"""Local catalogue and safety engine for ASIAIR Session Sorter.

The module deliberately uses only Python's standard library so the catalogue
works in the standalone application without adding a large astronomy package.
It reads the FITS cards needed by the sorter and the XML header embedded in
XISF files; image pixel data is never loaded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional


CATALOG_EXTENSIONS = frozenset({".fit", ".fits", ".fts", ".xisf", ".log", ".txt"})
FITS_EXTENSIONS = frozenset({".fit", ".fits", ".fts"})
MASTER_EXTENSIONS = frozenset({".xisf"})
PHD2_EXTENSIONS = frozenset({".log", ".txt"})
_INVALID_FOLDER_CHARS_RE = re.compile(r'[<>:"/\\|?*]')
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)(?!\d)")
_LIGHT_RE = re.compile(
    r"^Light_(?P<target>.+?)_(?P<exposure>\d+(?:\.\d+)?)s_Bin(?P<bin>\d+)_(?P<middle>.+?)_gain(?P<gain>[-+]?\d+)",
    re.IGNORECASE,
)
KNOWN_FILTERS = ("OIII", "SII", "Hb", "Ha", "Lum", "RGB", "H", "O", "S", "R", "G", "B", "L")


def default_data_dir() -> Path:
    configured = os.environ.get("ASIAIR_SORTER_DATA_DIR")
    return Path(configured) if configured else Path.home() / ".asiair_sorter"


def _safe_text(value) -> Optional[str]:
    if value is None:
        return None
    text = _INVALID_FOLDER_CHARS_RE.sub("_", str(value))
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:160] or None


def _normal(value) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _parse_fits_value(raw: str):
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("'"):
        result = []
        i = 1
        while i < len(raw):
            if raw[i] == "'":
                if i + 1 < len(raw) and raw[i + 1] == "'":
                    result.append("'")
                    i += 2
                    continue
                break
            result.append(raw[i])
            i += 1
        return "".join(result).strip()
    token = raw.split("/", 1)[0].strip()
    if token in ("T", "F"):
        return token == "T"
    try:
        if any(ch in token.upper() for ch in (".", "E", "D")):
            return float(token.replace("D", "E"))
        return int(token)
    except ValueError:
        return token


def read_fits_header(path: Path, max_blocks: int = 256) -> dict:
    """Read primary FITS header cards without loading image data."""
    header = {}
    with Path(path).open("rb") as fh:
        for _ in range(max_blocks):
            block = fh.read(2880)
            if not block:
                break
            if len(block) % 80:
                block += b" " * (80 - (len(block) % 80))
            for offset in range(0, len(block), 80):
                card = block[offset:offset + 80].decode("ascii", errors="replace")
                key = card[:8].strip()
                if key == "END":
                    return header
                if not key or key in ("COMMENT", "HISTORY") or card[8:10] != "= ":
                    continue
                header[key.upper()] = _parse_fits_value(card[10:])
    return header


def _first(header: dict, *keys):
    for key in keys:
        value = header.get(key)
        if value not in (None, ""):
            return value
    return None


def _iso_date(value) -> Optional[str]:
    if not value:
        return None
    match = _DATE_RE.search(str(value))
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else None


def read_fits_metadata(path: Path) -> dict:
    header = read_fits_header(path)
    return {
        "target": _safe_text(_first(header, "OBJECT", "OBJNAME", "TARGET")),
        "camera": _safe_text(_first(header, "INSTRUME", "CAMERA", "CAMMODEL")),
        "filter": _safe_text(_first(header, "FILTER", "FILTNAME")),
        "exposure": _number(_first(header, "EXPTIME", "EXPOSURE")),
        "gain": _number(_first(header, "GAIN", "EGAIN")),
        "temperature": _number(_first(header, "CCD-TEMP", "SET-TEMP", "SENSOR-T")),
        "binning": _safe_text(_first(header, "XBINNING", "BINNING", "CCDXBIN")),
        "capture_date": _iso_date(_first(header, "DATE-OBS", "DATE-BEG", "DATE")),
        "frame_type": _safe_text(_first(header, "IMAGETYP", "FRAME", "OBSTYPE")),
        "header_ok": bool(header),
        "header": header,
    }


def _number(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_xisf_metadata(path: Path) -> dict:
    """Read the XML header in an XISF 1.0 file."""
    with Path(path).open("rb") as fh:
        if fh.read(8) != b"XISF0100":
            raise ValueError("Not an XISF 1.0 file")
        header_len_raw = fh.read(4)
        if len(header_len_raw) != 4:
            raise ValueError("Truncated XISF header")
        header_len = struct.unpack("<I", header_len_raw)[0]
        fh.read(4)  # reserved
        if header_len <= 0 or header_len > 64 * 1024 * 1024:
            raise ValueError("Invalid XISF XML header length")
        xml = fh.read(header_len)
    root = ET.fromstring(xml.decode("utf-8", errors="replace"))
    props = {}
    image_attrs = {}
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "Image" and not image_attrs:
            image_attrs = dict(elem.attrib)
        elif tag == "Property":
            prop_id = elem.attrib.get("id", "")
            value = elem.attrib.get("value")
            if value is None and elem.text:
                value = elem.text.strip()
            if prop_id:
                props[prop_id] = value

    def prop(*names):
        normalized = {_normal(k): v for k, v in props.items()}
        for name in names:
            value = normalized.get(_normal(name))
            if value not in (None, ""):
                return value
        return None

    target = prop("Observation:Object:Name", "OBJECT", "TARGET")
    camera = prop("Instrument:Camera:Name", "INSTRUME", "CAMERA")
    filter_name = prop("Instrument:Filter:Name", "FILTER")
    exposure = prop("Instrument:ExposureTime", "EXPTIME", "EXPOSURE")
    date = prop("Observation:Time:Start", "DATE-OBS", "DATE")
    image_type = prop("Image:Type", "IMAGETYP", "FRAME")
    return {
        "target": _safe_text(target),
        "camera": _safe_text(camera),
        "filter": _safe_text(filter_name),
        "exposure": _number(exposure),
        "capture_date": _iso_date(date),
        "frame_type": _safe_text(image_type),
        "geometry": image_attrs.get("geometry"),
        "sample_format": image_attrs.get("sampleFormat"),
        "header_ok": True,
        "properties": props,
    }


def infer_filename_metadata(path: Path) -> dict:
    stem = path.stem
    result = {"target": None, "camera": None, "filter": None, "exposure": None,
              "gain": None, "binning": None, "capture_date": _iso_date(stem),
              "frame_type": None}
    match = _LIGHT_RE.match(stem)
    if match:
        middle = match.group("middle").split("_")
        filter_name = None
        if len(middle) > 1 and middle[-1].lower() in {f.lower() for f in KNOWN_FILTERS}:
            filter_name = middle.pop()
        result.update(
            target=_safe_text(match.group("target")),
            camera=_safe_text("_".join(middle)),
            filter=_safe_text(filter_name),
            exposure=_number(match.group("exposure")),
            gain=_number(match.group("gain")),
            binning=match.group("bin"),
            frame_type="Light",
        )
    else:
        lower = stem.lower()
        for frame in ("Light", "Dark", "Flat", "Bias"):
            if lower.startswith(frame.lower() + "_") or frame.lower() in {p.lower() for p in path.parts}:
                result["frame_type"] = frame
                break
    return result


def merged_metadata(path: Path) -> dict:
    inferred = infer_filename_metadata(path)
    try:
        embedded = read_fits_metadata(path) if path.suffix.lower() in FITS_EXTENSIONS else read_xisf_metadata(path)
    except (OSError, ValueError, ET.ParseError, struct.error):
        embedded = {"header_ok": False}
    result = dict(inferred)
    for key, value in embedded.items():
        if value not in (None, "") or key in ("header_ok", "header", "properties"):
            result[key] = value
    return result


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def phd2_quality_score(rms_total: float, peak: float, lost_frames: int,
                       frame_count: int, drift_ra: float = 0.0,
                       drift_dec: float = 0.0) -> float:
    """Return a transparent 0-100 guiding score; 100 is best."""
    score = 100.0
    score -= min(max(rms_total, 0.0) * 22.0, 45.0)
    score -= min(max(peak - 1.0, 0.0) * 4.0, 20.0)
    lost_ratio = (lost_frames / frame_count) if frame_count else 0.0
    score -= min(lost_ratio * 300.0, 20.0)
    score -= min((abs(drift_ra) + abs(drift_dec)) * 3.0, 15.0)
    return round(max(0.0, min(100.0, score)), 1)


@dataclass
class ScanResult:
    indexed: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0
    duplicate_groups: int = 0
    masters: int = 0


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS storage_roots(
  id INTEGER PRIMARY KEY, label TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL DEFAULT 'archive', last_seen TEXT, total_bytes INTEGER,
  free_bytes INTEGER
);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, storage_root_id INTEGER,
  size INTEGER NOT NULL, mtime REAL NOT NULL, sha256 TEXT, extension TEXT,
  kind TEXT, frame_type TEXT, capture_date TEXT, camera TEXT, target TEXT,
  filter_name TEXT, exposure REAL, gain REAL, temperature REAL, binning TEXT,
  header_ok INTEGER NOT NULL DEFAULT 0, master_id INTEGER, backup_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active', indexed_at TEXT NOT NULL,
  FOREIGN KEY(storage_root_id) REFERENCES storage_roots(id),
  FOREIGN KEY(master_id) REFERENCES masters(id)
);
CREATE INDEX IF NOT EXISTS idx_files_session ON files(capture_date,camera,target);
CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256,size);
CREATE INDEX IF NOT EXISTS idx_files_target ON files(target,filter_name);
CREATE TABLE IF NOT EXISTS masters(
  id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, file_id INTEGER,
  target TEXT, camera TEXT, filter_name TEXT, integration_seconds REAL,
  sub_count INTEGER, checksum TEXT, verified INTEGER NOT NULL DEFAULT 0,
  verified_at TEXT, FOREIGN KEY(file_id) REFERENCES files(id)
);
CREATE TABLE IF NOT EXISTS target_goals(
  target TEXT NOT NULL, filter_name TEXT NOT NULL DEFAULT '', goal_seconds REAL NOT NULL,
  PRIMARY KEY(target,filter_name)
);
CREATE TABLE IF NOT EXISTS phd2_quality(
  path TEXT PRIMARY KEY, capture_date TEXT, target TEXT, rms_ra REAL, rms_dec REAL,
  rms_total REAL, peak REAL, lost_frames INTEGER, frame_count INTEGER,
  drift_ra REAL, drift_dec REAL, score REAL, details_json TEXT, indexed_at TEXT
);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, operation TEXT NOT NULL,
  src TEXT, dst TEXT, checksum TEXT, status TEXT NOT NULL, undoable INTEGER NOT NULL DEFAULT 0,
  undone INTEGER NOT NULL DEFAULT 0, details TEXT
);
"""


class Catalog:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else default_data_dir() / "catalog.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _root_id(self, conn, root: Path, label: str, role: str) -> int:
        root = Path(root).resolve()
        total = free = None
        try:
            usage = shutil.disk_usage(root)
            total, free = usage.total, usage.free
        except OSError:
            pass
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO storage_roots(label,path,role,last_seen,total_bytes,free_bytes)
               VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
               label=excluded.label, role=excluded.role, last_seen=excluded.last_seen,
               total_bytes=excluded.total_bytes, free_bytes=excluded.free_bytes""",
            (label, str(root), role, now, total, free),
        )
        return conn.execute("SELECT id FROM storage_roots WHERE path=?", (str(root),)).fetchone()[0]

    def add_storage_root(self, path: Path, label: str, role: str = "archive"):
        with self.connect() as conn:
            return self._root_id(conn, path, label, role)

    def storage_roots(self) -> list[dict]:
        rows = []
        with self.connect() as conn:
            for row in conn.execute("SELECT * FROM storage_roots ORDER BY label"):
                item = dict(row)
                try:
                    usage = shutil.disk_usage(item["path"])
                    item.update(total_bytes=usage.total, free_bytes=usage.free, online=True)
                except OSError:
                    item["online"] = False
                rows.append(item)
        return rows

    def scan(self, root: Path, label: Optional[str] = None, role: str = "archive",
             progress: Optional[Callable[[int, int, Path], None]] = None,
             hash_duplicates: bool = True) -> ScanResult:
        root = Path(root).resolve()
        if not root.is_dir():
            raise FileNotFoundError(root)
        candidates = []
        for rd, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in names:
                path = Path(rd) / name
                if path.suffix.lower() in CATALOG_EXTENSIONS:
                    candidates.append(path)
        result = ScanResult()
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            root_id = self._root_id(conn, root, label or root.name or str(root), role)
            seen_paths = {str(path) for path in candidates}
            for index, path in enumerate(candidates, 1):
                if progress:
                    progress(index, len(candidates), path)
                try:
                    stat = path.stat()
                    old = conn.execute("SELECT size,mtime,status FROM files WHERE path=?", (str(path),)).fetchone()
                    if (old and old["status"] == "active" and old["size"] == stat.st_size
                            and abs(old["mtime"] - stat.st_mtime) < 0.001):
                        result.unchanged += 1
                        continue
                    meta = merged_metadata(path) if path.suffix.lower() in FITS_EXTENSIONS | MASTER_EXTENSIONS else {}
                    kind = "master" if self._is_master(path, meta) else (
                        "frame" if path.suffix.lower() in FITS_EXTENSIONS else "phd2")
                    conn.execute(
                        """INSERT INTO files(path,storage_root_id,size,mtime,extension,kind,frame_type,
                           capture_date,camera,target,filter_name,exposure,gain,temperature,binning,
                           header_ok,status,indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(path) DO UPDATE SET storage_root_id=excluded.storage_root_id,
                           size=excluded.size,mtime=excluded.mtime,extension=excluded.extension,
                           kind=excluded.kind,frame_type=excluded.frame_type,capture_date=excluded.capture_date,
                           camera=excluded.camera,target=excluded.target,filter_name=excluded.filter_name,
                           exposure=excluded.exposure,gain=excluded.gain,temperature=excluded.temperature,
                           binning=excluded.binning,header_ok=excluded.header_ok,status='active',
                           indexed_at=excluded.indexed_at,sha256=NULL""",
                        (str(path), root_id, stat.st_size, stat.st_mtime, path.suffix.lower(), kind,
                         meta.get("frame_type"), meta.get("capture_date"), meta.get("camera"),
                         meta.get("target"), meta.get("filter"), meta.get("exposure"), meta.get("gain"),
                         meta.get("temperature"), meta.get("binning"), int(bool(meta.get("header_ok"))),
                         "active", now),
                    )
                    file_id = conn.execute("SELECT id FROM files WHERE path=?", (str(path),)).fetchone()[0]
                    if kind == "master":
                        # A master is considered verified only when both its XISF
                        # header and its content checksum were read successfully.
                        checksum = sha256_file(path)
                        conn.execute("UPDATE files SET sha256=? WHERE id=?", (checksum, file_id))
                        sub_count, integration = self._master_totals(path, meta)
                        conn.execute(
                            """INSERT INTO masters(path,file_id,target,camera,filter_name,integration_seconds,
                               sub_count,checksum,verified,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(path) DO UPDATE SET file_id=excluded.file_id,target=excluded.target,
                               camera=excluded.camera,filter_name=excluded.filter_name,
                               integration_seconds=excluded.integration_seconds,sub_count=excluded.sub_count,
                               checksum=excluded.checksum,verified=excluded.verified,
                               verified_at=excluded.verified_at""",
                            (str(path), file_id, meta.get("target"), meta.get("camera"), meta.get("filter"),
                             integration, sub_count, checksum, int(bool(meta.get("header_ok"))), now),
                        )
                        result.masters += 1
                    result.updated += int(bool(old))
                    result.indexed += int(not bool(old))
                except (OSError, sqlite3.Error, ValueError):
                    result.errors += 1
            # Do not silently keep files that disappeared from this root in
            # totals or cleanup decisions. Their history remains searchable in
            # the database through the status field and audit trail.
            for known in conn.execute(
                "SELECT id,path FROM files WHERE storage_root_id=? AND status='active'", (root_id,)
            ).fetchall():
                if known["path"] not in seen_paths:
                    conn.execute("UPDATE files SET status='missing' WHERE id=?", (known["id"],))
            if hash_duplicates:
                result.duplicate_groups = self._refresh_duplicates(conn)
            self._associate_masters(conn)
        return result

    @staticmethod
    def _is_master(path: Path, meta: dict) -> bool:
        if path.suffix.lower() not in MASTER_EXTENSIONS:
            return False
        text = (path.stem + " " + str(meta.get("frame_type") or "")).lower()
        return "master" in text and ("light" in text or "integration" in text)

    @staticmethod
    def _master_totals(path: Path, meta: dict):
        props = meta.get("properties") or {}
        normalized = {_normal(k): v for k, v in props.items()}
        sub_count = None
        integration = None
        for key, value in normalized.items():
            if any(token in key for token in ("numberofimages", "imagecount", "subcount", "stackcount")):
                try: sub_count = int(float(value))
                except (TypeError, ValueError): pass
            if any(token in key for token in ("integrationtime", "totalexposure")):
                integration = _number(value)
        if integration is None and sub_count and meta.get("exposure"):
            integration = sub_count * meta["exposure"]
        match = re.search(r"(?:^|[_ -])(\d+)x(\d+(?:\.\d+)?)s(?:[_ -]|$)", path.stem, re.I)
        if match:
            sub_count = sub_count or int(match.group(1))
            integration = integration or int(match.group(1)) * float(match.group(2))
        return sub_count, integration

    def _refresh_duplicates(self, conn) -> int:
        rows = conn.execute(
            "SELECT size,COUNT(*) n FROM files WHERE status='active' GROUP BY size HAVING n>1"
        ).fetchall()
        for group in rows:
            files = conn.execute("SELECT id,path,sha256 FROM files WHERE size=? AND status='active'", (group["size"],)).fetchall()
            for item in files:
                if not item["sha256"]:
                    try:
                        digest = sha256_file(Path(item["path"]))
                        conn.execute("UPDATE files SET sha256=? WHERE id=?", (digest, item["id"]))
                    except OSError:
                        pass
        conn.execute("UPDATE files SET backup_count=0")
        duplicate_rows = conn.execute(
            """SELECT sha256,COUNT(DISTINCT storage_root_id) roots,COUNT(*) copies
               FROM files WHERE sha256 IS NOT NULL AND status='active'
               GROUP BY sha256 HAVING copies>1"""
        ).fetchall()
        for group in duplicate_rows:
            backup_count = max(int(group["roots"]) - 1, int(group["copies"]) - 1)
            conn.execute("UPDATE files SET backup_count=? WHERE sha256=?", (backup_count, group["sha256"]))
        return len(duplicate_rows)

    def _associate_masters(self, conn):
        masters = conn.execute(
            """SELECT m.* FROM masters m JOIN files f ON f.id=m.file_id
               WHERE m.verified=1 AND f.status='active'"""
        ).fetchall()
        lights = conn.execute(
            "SELECT id,target,camera,filter_name FROM files WHERE kind='frame' AND lower(frame_type) LIKE '%light%'"
        ).fetchall()
        for light in lights:
            best = None
            for master in masters:
                if _normal(light["target"]) and _normal(light["target"]) != _normal(master["target"]):
                    continue
                if _normal(light["camera"]) and _normal(master["camera"]) and _normal(light["camera"]) != _normal(master["camera"]):
                    continue
                if _normal(light["filter_name"]) and _normal(master["filter_name"]) and _normal(light["filter_name"]) != _normal(master["filter_name"]):
                    continue
                best = master["id"]
                break
            conn.execute("UPDATE files SET master_id=? WHERE id=?", (best, light["id"]))

    def dashboard(self) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) files,COALESCE(SUM(size),0) bytes,
                   COUNT(DISTINCT capture_date||'|'||COALESCE(camera,'')||'|'||COALESCE(target,'')) sessions,
                   COUNT(DISTINCT target) targets,
                   SUM(CASE WHEN kind='master' THEN 1 ELSE 0 END) masters
                   FROM files WHERE status='active'"""
            ).fetchone()
            duplicates = conn.execute(
                """SELECT COALESCE(SUM((copies-1)*size),0) reclaimable,COUNT(*) groups FROM
                   (SELECT sha256,size,COUNT(*) copies FROM files WHERE sha256 IS NOT NULL AND status='active'
                    GROUP BY sha256,size HAVING copies>1)"""
            ).fetchone()
            cleanup = conn.execute(
                """SELECT COUNT(*) n,COALESCE(SUM(size),0) bytes FROM files
                   WHERE status='active' AND kind='frame' AND lower(frame_type) LIKE '%light%'
                   AND master_id IS NOT NULL AND backup_count>=1"""
            ).fetchone()
            result = dict(row)
            result.update(duplicate_groups=duplicates["groups"], duplicate_bytes=duplicates["reclaimable"],
                          cleanup_files=cleanup["n"], cleanup_bytes=cleanup["bytes"])
            return result

    def sessions(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT capture_date,camera,target,COUNT(*) files,COALESCE(SUM(size),0) bytes,
                   COALESCE(SUM(CASE WHEN lower(frame_type) LIKE '%light%' THEN exposure ELSE 0 END),0) integration_seconds,
                   COUNT(DISTINCT filter_name) filters
                   FROM files WHERE status='active' AND kind='frame'
                   GROUP BY capture_date,camera,target ORDER BY capture_date DESC,target"""
            ).fetchall()
            return [dict(row) for row in rows]

    def masters(self) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM masters ORDER BY target,filter_name,path")]

    def duplicate_groups(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT sha256,size,COUNT(*) copies,GROUP_CONCAT(path,'\n') paths
                   FROM files WHERE sha256 IS NOT NULL AND status='active'
                   GROUP BY sha256,size HAVING copies>1 ORDER BY size*(copies-1) DESC"""
            ).fetchall()
            return [dict(row) for row in rows]

    def search_files(self, query: str = "", limit: int = 500) -> list[dict]:
        """Search the local index without opening or loading image data."""
        term = f"%{query.strip()}%"
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT path,size,kind,frame_type,capture_date,camera,target,filter_name,
                          exposure,gain,temperature,binning,header_ok,backup_count,status
                   FROM files WHERE status='active' AND
                     (?='' OR path LIKE ? OR target LIKE ? OR camera LIKE ? OR
                      filter_name LIKE ? OR capture_date LIKE ? OR frame_type LIKE ?)
                   ORDER BY capture_date DESC,target,path LIMIT ?""",
                (query.strip(), term, term, term, term, term, term, max(1, int(limit))),
            ).fetchall()
            return [dict(row) for row in rows]

    def cleanup_candidates(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT f.*,m.path master_path,m.verified FROM files f JOIN masters m ON m.id=f.master_id
                   WHERE f.status='active' AND f.kind='frame' AND lower(f.frame_type) LIKE '%light%'
                   AND f.backup_count>=1 AND m.verified=1 ORDER BY f.target,f.filter_name,f.capture_date"""
            ).fetchall()
            return [dict(row) for row in rows]

    def target_progress(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT f.target,COALESCE(f.filter_name,'') filter_name,
                   COALESCE(SUM(f.exposure),0) collected_seconds,COUNT(*) frames,
                   COALESCE(g.goal_seconds,0) goal_seconds
                   FROM files f LEFT JOIN target_goals g ON g.target=f.target
                     AND g.filter_name=COALESCE(f.filter_name,'')
                   WHERE f.status='active' AND f.kind='frame' AND lower(f.frame_type) LIKE '%light%'
                   GROUP BY f.target,COALESCE(f.filter_name,'') ORDER BY f.target,filter_name"""
            ).fetchall()
            return [dict(row) for row in rows]

    def set_target_goal(self, target: str, filter_name: str, hours: float):
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO target_goals(target,filter_name,goal_seconds) VALUES(?,?,?)
                   ON CONFLICT(target,filter_name) DO UPDATE SET goal_seconds=excluded.goal_seconds""",
                (target.strip(), filter_name.strip(), max(0.0, float(hours)) * 3600),
            )

    def record_phd2_quality(self, path: Path, session, target: Optional[str] = None,
                            session_index: int = 1):
        score = phd2_quality_score(session.rms_total, session.peak_err, session.lost_frames,
                                   session.n_frames, session.drift_per_min("ra"),
                                   session.drift_per_min("dec"))
        date = _iso_date(session.start_time) or _iso_date(path.name)
        details = {"duration_min": session.duration_min, "dithers": len(session.dithers),
                   "cadence_s": session.cadence_s, "units": session.units}
        record_key = f"{path}::session={max(1, int(session_index))}"
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO phd2_quality(path,capture_date,target,rms_ra,rms_dec,rms_total,peak,
                   lost_frames,frame_count,drift_ra,drift_dec,score,details_json,indexed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                   capture_date=excluded.capture_date,target=excluded.target,rms_ra=excluded.rms_ra,
                   rms_dec=excluded.rms_dec,rms_total=excluded.rms_total,peak=excluded.peak,
                   lost_frames=excluded.lost_frames,frame_count=excluded.frame_count,
                   drift_ra=excluded.drift_ra,drift_dec=excluded.drift_dec,score=excluded.score,
                   details_json=excluded.details_json,indexed_at=excluded.indexed_at""",
                (record_key, date, target, session.rms_ra, session.rms_dec, session.rms_total,
                 session.peak_err, session.lost_frames, session.n_frames,
                 session.drift_per_min("ra"), session.drift_per_min("dec"), score,
                 json.dumps(details), datetime.now().isoformat(timespec="seconds")),
            )
        return score

    def phd2_quality_rows(self) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM phd2_quality ORDER BY capture_date DESC,path")]

    def record_audit(self, operation: str, src=None, dst=None, checksum=None,
                     status="complete", undoable=False, details=None):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit(timestamp,operation,src,dst,checksum,status,undoable,details) VALUES(?,?,?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), operation, str(src) if src else None,
                 str(dst) if dst else None, checksum, status, int(bool(undoable)), details),
            )

    def audit_rows(self, limit: int = 500) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]

    def undo(self, audit_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM audit WHERE id=?", (audit_id,)).fetchone()
            if not row or not row["undoable"] or row["undone"]:
                return False
            src, dst = Path(row["src"] or ""), Path(row["dst"] or "")
            if row["operation"] == "move" and dst.is_file() and not src.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
            elif row["operation"] == "copy" and dst.is_file():
                if row["checksum"] and sha256_file(dst) != row["checksum"]:
                    return False
                dst.unlink()
            else:
                return False
            conn.execute("UPDATE audit SET undone=1,status='undone' WHERE id=?", (audit_id,))
            return True

    def delete_raws(self, paths: Iterable[Path], confirmation: str) -> dict:
        if confirmation != "DELETE":
            raise PermissionError("Cleanup requires the exact confirmation word DELETE")
        eligible = {row["path"]: row for row in self.cleanup_candidates()}
        deleted, refused, errors = [], [], []
        with self.connect() as conn:
            for raw in paths:
                path = str(Path(raw).resolve())
                if path not in eligible:
                    refused.append(path)
                    continue
                try:
                    row = conn.execute(
                        "SELECT sha256 FROM files WHERE path=? AND status='active'", (path,)
                    ).fetchone()
                    # Preserve at least one active raw light for every checksum
                    # group, even if the user selects every displayed candidate.
                    # The PixInsight master is an integration product, not a
                    # byte-for-byte backup of its source subframes.
                    if not row or not row["sha256"]:
                        refused.append(path)
                        continue
                    remaining = conn.execute(
                        """SELECT COUNT(*) FROM files WHERE sha256=? AND status='active'
                           AND kind='frame' AND lower(frame_type) LIKE '%light%'""",
                        (row["sha256"],),
                    ).fetchone()[0]
                    if remaining <= 1:
                        refused.append(path)
                        continue
                    size = Path(path).stat().st_size
                    Path(path).unlink()
                    conn.execute("UPDATE files SET status='deleted' WHERE path=?", (path,))
                    conn.execute(
                        "INSERT INTO audit(timestamp,operation,src,status,undoable,details) VALUES(?,?,?,?,0,?)",
                        (datetime.now().isoformat(timespec="seconds"), "delete_raw", path, "complete",
                         json.dumps({"size": size, "master": eligible[path]["master_path"]})),
                    )
                    deleted.append(path)
                except OSError as exc:
                    errors.append((path, str(exc)))
        return {"deleted": deleted, "refused": refused, "errors": errors}


class SourceWatcher:
    """Polling watcher that is safe for removable drives and network shares."""
    def __init__(self, source: Path, callback: Callable[[Path], None], interval: float = 15.0):
        self.source = Path(source)
        self.callback = callback
        self.interval = max(2.0, float(interval))
        self._stop = threading.Event()
        self._thread = None
        self._last_signature = None

    def _signature(self):
        if not self.source.is_dir():
            return None
        try:
            count = 0
            newest = 0.0
            for rd, _, names in os.walk(self.source):
                for name in names:
                    path = Path(rd) / name
                    if path.suffix.lower() in CATALOG_EXTENSIONS:
                        count += 1
                        try: newest = max(newest, path.stat().st_mtime)
                        except OSError: pass
            return count, newest
        except OSError:
            return None

    def _run(self):
        while not self._stop.wait(self.interval):
            signature = self._signature()
            if signature and signature != self._last_signature:
                previous = self._last_signature
                self._last_signature = signature
                if previous is not None:
                    self.callback(self.source)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._last_signature = self._signature()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

