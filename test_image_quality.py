import tempfile
import unittest
from pathlib import Path

import numpy as np

from image_quality import analyse_fits, read_fits_sample


def card(key, value):
    encoded = f"'{value}'" if isinstance(value, str) else str(value)
    return f"{key:<8}= {encoded:<70}"[:80]


def write_star_field(path: Path, elongated=False):
    height = width = 128
    image = np.full((height, width), 1000.0, dtype=np.float64)
    yy, xx = np.mgrid[:height, :width]
    for y, x in [(20,20),(30,80),(50,45),(70,100),(90,20),(105,65),(80,60),(45,105),(110,110),(18,55),(62,15),(95,90),(35,35),(75,35),(55,75)]:
        sx, sy = (3.5, 1.2) if elongated else (1.4, 1.4)
        image += 12000 * np.exp(-((xx-x)**2/(2*sx*sx) + (yy-y)**2/(2*sy*sy)))
    image += np.random.default_rng(4).normal(0, 8, image.shape)
    stored = np.clip(image, 0, 65535).astype(">u2")
    cards = [card("SIMPLE", "T"), card("BITPIX", 16), card("NAXIS", 2),
             card("NAXIS1", width), card("NAXIS2", height), card("BZERO", 32768),
             "END".ljust(80)]
    header = "".join(cards).encode("ascii")
    header += b" " * ((2880 - len(header) % 2880) % 2880)
    # FITS signed int16 plus BZERO represents unsigned values.
    raw = (stored.astype(np.uint16).astype(np.int32) - 32768).astype(">i2")
    path.write_bytes(header + raw.tobytes())


class ImageQualityTests(unittest.TestCase):
    def test_reads_fits_pixels(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "stars.fit"
            write_star_field(path)
            image, step, header = read_fits_sample(path)
            self.assertEqual(image.shape, (128, 128))
            self.assertEqual(step, 1)
            self.assertEqual(header["NAXIS1"], 128)

    def test_detects_stars_and_trailing(self):
        with tempfile.TemporaryDirectory() as td:
            good = Path(td) / "good.fit"; bad = Path(td) / "bad.fit"
            write_star_field(good, elongated=False); write_star_field(bad, elongated=True)
            good_q = analyse_fits(good); bad_q = analyse_fits(bad)
            self.assertGreaterEqual(good_q.star_count, 12)
            self.assertLess(good_q.eccentricity, bad_q.eccentricity)
            self.assertTrue(bad_q.trailing_likely)
            self.assertGreater(good_q.score, bad_q.score)


if __name__ == "__main__":
    unittest.main()
