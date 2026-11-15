"""Removable ASIAIR source discovery, verification helpers, and safe eject."""

from __future__ import annotations

import ctypes
import os
import platform
import subprocess
from pathlib import Path


def _looks_like_asiair(root: Path) -> bool:
    try:
        names = {p.name.lower() for p in root.iterdir()}
    except OSError:
        return False
    markers = {"autorun", "plan", "preview", "emmc images", "tf images", "udisk images"}
    if names & markers:
        return True
    try:
        for folder in list(root.iterdir())[:30]:
            if folder.is_dir() and folder.name.lower() in markers:
                return True
    except OSError:
        pass
    return False


def discover_asiair_sources() -> list[Path]:
    system = platform.system()
    roots = []
    if system == "Windows":
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for index in range(26):
            if mask & (1 << index):
                root = Path(f"{chr(65+index)}:/")
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(root))
                if drive_type in (2, 3) and _looks_like_asiair(root):
                    roots.append(root)
    else:
        candidates = [Path("/Volumes"), Path("/media"), Path("/run/media")]
        for base in candidates:
            if not base.is_dir(): continue
            try:
                for root, dirs, _files in os.walk(base):
                    depth = len(Path(root).relative_to(base).parts)
                    if depth > 2: dirs[:] = []; continue
                    path = Path(root)
                    if _looks_like_asiair(path): roots.append(path); dirs[:] = []
            except OSError:
                pass
    return sorted(set(p.resolve() for p in roots), key=str)


def eject_source(path: Path):
    """Request OS-level eject. Call only after verified copy and user opt-in."""
    path = Path(path).resolve(); system = platform.system()
    if system == "Windows":
        drive = path.drive
        if not drive:
            return False, "The selected source is not a drive."
        device = rf"\\.\{drive}"
        GENERIC_READ = 0x80000000; GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3; INVALID = ctypes.c_void_p(-1).value
        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            device, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
        if handle == INVALID:
            return False, "Windows could not lock the drive; close other programs using it."
        try:
            returned = ctypes.c_ulong()
            def ioctl(code):
                return bool(ctypes.windll.kernel32.DeviceIoControl(
                    handle, code, None, 0, None, 0, ctypes.byref(returned), None))
            # LOCK_VOLUME, DISMOUNT_VOLUME, STORAGE_EJECT_MEDIA
            if not ioctl(0x00090018): return False, "Drive is busy and could not be locked."
            if not ioctl(0x00090020): return False, "Drive could not be dismounted."
            if not ioctl(0x002D4808): return False, "Drive was dismounted but did not report eject support."
            return True, f"{drive} was safely ejected."
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    if system == "Darwin":
        result = subprocess.run(["diskutil", "eject", str(path)], capture_output=True, text=True)
    else:
        result = subprocess.run(["udisksctl", "unmount", "-p", str(path)], capture_output=True, text=True)
    return result.returncode == 0, (result.stdout or result.stderr).strip()
