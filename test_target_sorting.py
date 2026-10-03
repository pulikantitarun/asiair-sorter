import ast
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


def _load_sorting_logic():
    """Load pure sorter functions without importing GUI-only dependencies."""
    source_path = Path(__file__).with_name("asiair_sorter.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    assignments = {
        "SESSIONS_ROOT", "LIGHTS_FOLDER", "DARKS_FOLDER", "FLATS_FOLDER",
        "BIAS_FOLDER", "PHD2_FOLDER", "IMAGING_EXTENSIONS",
        "PREVIEW_EXTENSIONS", "PHD2_EXTENSIONS", "KNOWN_FILTERS", "_DATE_RE",
        "_COMPACT_DATE_RE", "_DMY_DATE_RE",
        "_LIGHT_TARGET_RE", "_LIGHT_CAMERA_RE", "_INVALID_FOLDER_CHARS_RE",
    }
    functions = {
        "_rel_parts", "_find_date", "_safe_folder_name",
        "_find_target_from_filename", "_find_target",
        "_find_camera_and_filter_from_filename", "_find_frame_type",
        "_find_filter", "classify_file", "build_dest_path",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names & assignments:
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in functions:
            nodes.append(node)
    namespace = {"re": re, "Path": Path, "datetime": datetime}
    exec(compile(ast.Module(nodes, type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace


_SORTER = _load_sorting_logic()
classify_file = _SORTER["classify_file"]
build_dest_path = _SORTER["build_dest_path"]


class TargetSortingTests(unittest.TestCase):
    def classify(self, relative_path):
        root = Path(tempfile.gettempdir()) / "asiair-source"
        return classify_file(root / relative_path, root)

    def test_target_is_read_from_standard_asiair_light_filename(self):
        info = self.classify(
            "Light/Light_M 42_120.0s_Bin1_585MC_gain200_20260401-194032_0001.fit"
        )
        self.assertEqual(info["target"], "M 42")

    def test_target_with_spaces_is_kept_together(self):
        info = self.classify(
            "Light/Light_NGC 2244 Satellite Cluster_180.0s_Bin1_585MC_gain200_0001.fit"
        )
        self.assertEqual(info["target"], "NGC 2244 Satellite Cluster")

    def test_each_target_gets_its_own_folder(self):
        info = self.classify(
            "Autorun/2026-04-02/Light/Light_IC 434_120.0s_Bin1_533MM_H_gain101_0001.fit"
        )
        destination = build_dest_path(info, Path("D:/Archive"), "frame.fit")
        self.assertEqual(
            destination,
            Path("D:/Archive/Sessions/2026-04-02/533MM/IC 434/Lights/H/frame.fit"),
        )

    def test_camera_and_filter_are_read_from_filename(self):
        info = self.classify(
            "Light/Light_NGC 6960_60.0s_Bin1_533MM_O_gain101_0001.fit"
        )
        self.assertEqual(info["camera"], "533MM")
        self.assertEqual(info["filter_name"], "O")

    def test_colour_camera_has_no_false_filter(self):
        info = self.classify(
            "Light/Light_M 42_120.0s_Bin1_585MC_gain200_0001.fit"
        )
        self.assertEqual(info["camera"], "585MC")
        self.assertIsNone(info["filter_name"])

    def test_compact_capture_date_is_used_from_filename(self):
        info = self.classify(
            "Light/Light_M 42_120.0s_Bin1_585MC_gain200_20260401-194032_0001.fit"
        )
        self.assertEqual(info["date"], "2026-04-01")

    def test_day_month_year_source_folder_is_normalised(self):
        info = self.classify(
            "02-04-2026/Light/Light_M 42_120.0s_Bin1_585MC_gain200_0001.fit"
        )
        self.assertEqual(info["date"], "2026-04-02")

    def test_filename_target_wins_over_source_folder_target(self):
        info = self.classify(
            "Autorun/Wrong Target/Light/Light_NGC 6960_60.0s_Bin1_533MM_O_gain101_0001.fit"
        )
        self.assertEqual(info["target"], "NGC 6960")

    def test_legacy_autorun_folder_is_used_as_fallback(self):
        info = self.classify("Autorun/M 31/Light/legacy_name.fit")
        self.assertEqual(info["target"], "M 31")

    def test_non_light_calibration_frames_do_not_get_target_folders(self):
        info = self.classify(
            "Dark/Dark_M 42_120.0s_Bin1_585MC_gain200_0001.fit"
        )
        self.assertIsNone(info["target"])


if __name__ == "__main__":
    unittest.main()
