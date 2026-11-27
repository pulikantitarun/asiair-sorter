import ast
import os
import re
import shutil
import tempfile
import unittest
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import numpy as np


def _load_phd2_logic():
    source_path = Path(__file__).with_name("asiair_sorter.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    assignments = {"SESSIONS_ROOT", "PHD2_FOLDER", "_DATE_RE", "PHD2_ERROR_LABELS"}
    functions = {"_number", "parse_phd2_log", "discover_phd2_logs",
                 "_phd2_log_date", "sync_phd2_logs",
                 "_guide_window_mask", "_guide_window_rms", "_guide_event_groups", "_next_guiding_check"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names & assignments: nodes.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "PHD2Session":
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in functions:
            nodes.append(node)
    namespace = dict(
        re=re, os=os, shutil=shutil, np=np, Path=Path, datetime=datetime,
        dataclass=dataclass, field=field, Optional=Optional, List=List,
        Counter=Counter, HAS_MPL=True,
    )
    exec(compile(ast.Module(nodes, type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace


PHD = _load_phd2_logic()
parse_phd2_log = PHD["parse_phd2_log"]
sync_phd2_logs = PHD["sync_phd2_logs"]
discover_phd2_logs = PHD["discover_phd2_logs"]


SAMPLE_LOG = """PHD2 version 2.6.13
Guiding Begins at 2026-10-02 20:00:00
Equipment Profile = ASIAIR Rig
Camera = ZWO ASI220MM Mini
Mount = ZWO AM5
Focal length = 240 mm
Image scale = 2.50 arc-sec/px
Exposure = 2000 ms
X guide algorithm = Hysteresis, Hysteresis = 0.100, Aggression = 0.700, Minimum move = 0.200
RA = 5.50 hr, Dec = -5.0 deg, Hour angle = -0.5 hr, Pier side = East, Alt = 55 deg, Az = 160 deg
Lock position = 500.0, 400.0, Star position = 500.0, 400.0, HFD = 3.2 px
Frame,Time,mount,dx,dy,RARawDistance,DECRawDistance,RAGuideDistance,DECGuideDistance,RADuration,RADirection,DECDuration,DECDirection,XStep,YStep,StarMass,SNR,HFD,ErrorCode
1,0.0,"Mount",0.4,-0.2,0.4,-0.2,0.2,0.0,100,E,0,,,,12000,25.0,3.2,0
INFO: DITHER by 2.0 pixels
2,2.0,"Mount",-0.2,0.3,-0.2,0.3,-0.1,0.1,50,W,75,N,,,11000,20.0,3.4,2
Guiding Ends at 2026-10-02 20:00:04
"""


class PHD2LogTests(unittest.TestCase):
    def test_coaching_prioritizes_frame_errors_over_axis_imbalance(self):
        run = PHD["PHD2Session"](times=np.arange(400.), ra_err=np.ones(400),
                                dec_err=np.full(400, .2), error_codes=np.r_[np.ones(12), np.zeros(388)])
        title, evidence, *_ = PHD["_next_guiding_check"]([run])
        self.assertIn("guide-star reliability", title)
        self.assertIn("3.0%", evidence)

    def test_coaching_excludes_logged_settling_from_axis_comparison(self):
        run = PHD["PHD2Session"](times=np.arange(400.), ra_err=np.r_[np.full(10, 20.), np.full(390, .3)],
                                dec_err=np.full(400, .3), error_codes=np.zeros(400), settle_spans=[(0, 10)])
        self.assertIn("Keep your settings", PHD["_next_guiding_check"]([run])[0])
        run.dec_err = np.full(400, .15)
        self.assertIn("Check RA first", PHD["_next_guiding_check"]([run])[0])

    def test_coaching_distinguishes_timeout_and_inadequate_baseline(self):
        run = PHD["PHD2Session"](times=np.arange(5.), ra_err=np.ones(5), dec_err=np.ones(5))
        self.assertIn("longer baseline", PHD["_next_guiding_check"]([run])[0])
        run.events.append("INFO: Settling failed: timed out")
        self.assertIn("after a dither", PHD["_next_guiding_check"]([run])[0])

    def test_window_rms_excludes_bad_and_outside_samples(self):
        count, rms = PHD["_guide_window_rms"](
            np.arange(5), np.array([1, 3, 99, np.nan, 100]),
            np.array([2, 4, 99, 5, 100]),
            np.array([True, True, False, True, True]), (0, 3))
        self.assertEqual(count, 2)
        np.testing.assert_allclose(rms, np.sqrt([5, 10, 15]))
        self.assertEqual(PHD["_guide_window_rms"](
            np.arange(1), np.zeros(1), np.zeros(1), np.ones(1, dtype=bool), (2, 4)), (0, None))

    def test_event_groups_keep_edges_and_expand_when_zoomed(self):
        groups = PHD["_guide_event_groups"]([0, .2, 1, 10, 11], (0, 10), bins=4)
        self.assertEqual([count for _, count in groups], [3, 1])
        zoom = PHD["_guide_event_groups"]([0, .2, 1, 10, 11], (0, 1), bins=10)
        self.assertEqual(zoom, [(0., 1), (.2, 1), (1., 1)])

    def test_records_only_paired_settling_intervals(self):
        sample = SAMPLE_LOG.replace("2,2.0", "INFO: SETTLING STATE CHANGE, Settling started\n2,2.0")
        sample = sample.replace("Guiding Ends", "INFO: SETTLING STATE CHANGE, Settling complete\nGuiding Ends")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "PHD2_GuideLog_2026-10-02_200000.txt"
            path.write_text(sample, encoding="utf-8")
            self.assertEqual(parse_phd2_log(path)[0].settle_spans, [(1, 2)])
            path.write_text(sample.replace("INFO: SETTLING STATE CHANGE, Settling complete\n", ""), encoding="utf-8")
            self.assertEqual(parse_phd2_log(path)[0].settle_spans, [])

    def test_parser_extracts_metadata_metrics_pulses_and_events(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "PHD2_GuideLog_2026-10-02_200000.txt"
            path.write_text(SAMPLE_LOG, encoding="utf-8")
            sessions = parse_phd2_log(str(path))
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session.profile, "ASIAIR Rig")
        self.assertEqual(session.camera, "ZWO ASI220MM Mini")
        self.assertEqual(session.mount, "ZWO AM5")
        self.assertEqual(session.exposure_s, 2.0)
        self.assertEqual(session.units, "arcsec")
        self.assertAlmostEqual(session.ra_err[0], 1.0)
        self.assertAlmostEqual(session.dec_err[0], -0.5)
        self.assertEqual(len(session.dithers), 1)
        self.assertEqual(session.lost_frames, 1)
        self.assertEqual(list(session.ra_pulse), [100.0, 50.0])
        self.assertIn("DITHER", session.events[0])

    def test_sync_copies_once_then_updates_changed_log(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / "PHD2"
            destination = root / "archive"
            source.mkdir(parents=True)
            destination.mkdir()
            log = source / "PHD2_GuideLog_2026-10-02_200000.txt"
            log.write_text(SAMPLE_LOG, encoding="utf-8")

            first = sync_phd2_logs(source.parent, destination)
            second = sync_phd2_logs(source.parent, destination)
            log.write_text(SAMPLE_LOG + "INFO: Guiding parameter change\n", encoding="utf-8")
            third = sync_phd2_logs(source.parent, destination)

            self.assertEqual(len(first["copied"]), 1)
            self.assertEqual(len(second["unchanged"]), 1)
            self.assertEqual(len(third["updated"]), 1)
            archived = destination / "Sessions" / "2026-10-02" / "PHD2_Logs" / log.name
            self.assertTrue(archived.is_file())
            self.assertEqual(len(discover_phd2_logs(destination, include_debug=False)), 1)


if __name__ == "__main__":
    unittest.main()
