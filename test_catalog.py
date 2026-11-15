import os
import struct
import tempfile
import unittest
from pathlib import Path

from astro_catalog import (
    Catalog,
    phd2_quality_score,
    read_fits_metadata,
    read_xisf_metadata,
    sha256_file,
)


def fits_card(key, value):
    if isinstance(value, str):
        encoded = f"'{value}'"
    elif isinstance(value, bool):
        encoded = "T" if value else "F"
    else:
        encoded = str(value)
    return f"{key:<8}= {encoded:<70}"[:80]


def write_fits(path: Path, target="M 42", filter_name="Ha"):
    cards = [
        fits_card("SIMPLE", True),
        fits_card("BITPIX", 16),
        fits_card("NAXIS", 0),
        fits_card("OBJECT", target),
        fits_card("INSTRUME", "ASI533MM"),
        fits_card("FILTER", filter_name),
        fits_card("EXPTIME", 120.0),
        fits_card("GAIN", 100),
        fits_card("CCD-TEMP", -10.0),
        fits_card("XBINNING", 1),
        fits_card("DATE-OBS", "2026-11-15T20:01:02"),
        fits_card("IMAGETYP", "Light"),
        "END".ljust(80),
    ]
    blob = "".join(cards).encode("ascii")
    blob += b" " * ((2880 - len(blob) % 2880) % 2880)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)


def write_xisf(path: Path, target="M 42", filter_name="Ha"):
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<xisf version="1.0"><Image geometry="10:10:1" sampleFormat="UInt16">
<Property id="Observation:Object:Name" type="String" value="{target}"/>
<Property id="Instrument:Camera:Name" type="String" value="ASI533MM"/>
<Property id="Instrument:Filter:Name" type="String" value="{filter_name}"/>
<Property id="Image:Type" type="String" value="Light"/>
<Property id="ImageIntegration:NumberOfImages" type="Int32" value="30"/>
<Property id="ImageIntegration:TotalExposure" type="Float64" value="3600"/>
</Image></xisf>'''.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"XISF0100" + struct.pack("<I", len(xml)) + b"\0\0\0\0" + xml)


class MetadataTests(unittest.TestCase):
    def test_reads_fits_header_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "light.fit"
            write_fits(path)
            metadata = read_fits_metadata(path)
        self.assertEqual(metadata["target"], "M 42")
        self.assertEqual(metadata["camera"], "ASI533MM")
        self.assertEqual(metadata["filter"], "Ha")
        self.assertEqual(metadata["capture_date"], "2026-11-15")
        self.assertEqual(metadata["exposure"], 120.0)

    def test_reads_xisf_master_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "MasterLight.xisf"
            write_xisf(path)
            metadata = read_xisf_metadata(path)
        self.assertEqual(metadata["target"], "M 42")
        self.assertEqual(metadata["filter"], "Ha")
        self.assertTrue(metadata["header_ok"])


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = Catalog(self.root / "catalog.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_catalog_associates_master_and_requires_backup_for_cleanup(self):
        archive = self.root / "archive"
        backup = self.root / "backup"
        raw = archive / "Light_M 42_120s_Bin1_ASI533MM_Ha_gain100_20261115-200102.fit"
        raw_copy = backup / raw.name
        master = archive / "MasterLight_M 42_Ha.xisf"
        write_fits(raw)
        raw_copy.parent.mkdir(parents=True)
        raw_copy.write_bytes(raw.read_bytes())
        write_xisf(master)

        first = self.catalog.scan(archive, "NAS", "archive")
        self.assertEqual(first.masters, 1)
        self.assertEqual(self.catalog.cleanup_candidates(), [])

        second = self.catalog.scan(backup, "Google Drive", "cloud")
        self.assertGreaterEqual(second.duplicate_groups, 1)
        candidates = self.catalog.cleanup_candidates()
        self.assertEqual({row["path"] for row in candidates}, {str(raw), str(raw_copy)})
        self.assertEqual(candidates[0]["master_path"], str(master))

    def test_cleanup_refuses_without_exact_confirmation(self):
        with self.assertRaises(PermissionError):
            self.catalog.delete_raws([], "yes")

    def test_cleanup_preserves_final_raw_copy(self):
        archive = self.root / "archive"
        backup = self.root / "backup"
        raw = archive / "Light_M 42_120s_Bin1_ASI533MM_Ha_gain100_20261115-200102.fit"
        raw_copy = backup / raw.name
        master = archive / "MasterLight_M 42_Ha.xisf"
        write_fits(raw)
        raw_copy.parent.mkdir(parents=True)
        raw_copy.write_bytes(raw.read_bytes())
        write_xisf(master)
        self.catalog.scan(archive)
        self.catalog.scan(backup)

        result = self.catalog.delete_raws([raw, raw_copy], "DELETE")
        self.assertEqual(len(result["deleted"]), 1)
        self.assertEqual(len(result["refused"]), 1)
        self.assertEqual(sum(path.exists() for path in (raw, raw_copy)), 1)

    def test_master_is_content_verified(self):
        archive = self.root / "archive"
        master = archive / "MasterLight_M 42_Ha.xisf"
        write_xisf(master)
        self.catalog.scan(archive)
        row = self.catalog.masters()[0]
        self.assertEqual(row["checksum"], sha256_file(master))
        self.assertEqual(row["verified"], 1)

    def test_audit_copy_can_be_undone_when_checksum_matches(self):
        src = self.root / "source.fit"
        dst = self.root / "copy.fit"
        write_fits(src)
        dst.write_bytes(src.read_bytes())
        checksum = sha256_file(dst)
        self.catalog.record_audit("copy", src, dst, checksum, undoable=True)
        audit_id = self.catalog.audit_rows()[0]["id"]
        self.assertTrue(self.catalog.undo(audit_id))
        self.assertFalse(dst.exists())

    def test_target_goal_and_progress(self):
        archive = self.root / "archive"
        write_fits(archive / "Light_M 42_120s_Bin1_ASI533MM_Ha_gain100_20261115-200102.fit")
        self.catalog.scan(archive)
        self.catalog.set_target_goal("M 42", "Ha", 10)
        row = self.catalog.target_progress()[0]
        self.assertEqual(row["goal_seconds"], 36000)
        self.assertEqual(row["collected_seconds"], 120)

    def test_rescan_marks_removed_files_missing(self):
        archive = self.root / "archive"
        raw = archive / "Light_M 42_120s_Bin1_ASI533MM_Ha_gain100_20261115-200102.fit"
        write_fits(raw)
        self.catalog.scan(archive)
        self.assertEqual(self.catalog.dashboard()["files"], 1)
        raw.unlink()
        self.catalog.scan(archive)
        self.assertEqual(self.catalog.dashboard()["files"], 0)

    def test_quality_score_penalises_bad_guiding(self):
        good = phd2_quality_score(0.5, 1.2, 0, 1000, 0.01, 0.01)
        bad = phd2_quality_score(2.0, 6.0, 50, 1000, 0.8, 0.6)
        self.assertGreater(good, bad)
        self.assertGreaterEqual(good, 0)
        self.assertLessEqual(good, 100)


if __name__ == "__main__":
    unittest.main()
