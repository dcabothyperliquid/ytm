"""logo_probe regression: a source logo made of TWO parts (pink round emblem on a bright sky = almost no luma edge, plus a caption
below it) must be covered completely by our circle (Dilshan Cricketer Salempur / DCS logo bug, 2026-11-23: only the caption was found,
the pink emblem peeked out above our LML circle)."""
import importlib.util
import math
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
X0, Y0, CW, CH = 904, 0, 376, 200


def _load():
    spec = importlib.util.spec_from_file_location("ytm_runner_t", os.path.join(HERE, "..", "ytm_runner.py"))
    m = importlib.util.module_from_spec(spec)
    old = sys.argv
    sys.argv = ["x"]
    try:
        spec.loader.exec_module(m)
    finally:
        sys.argv = old
    return m


def _frame(parts):
    buf = bytearray(bytes((236, 246, 250)) * (CW * CH))              # bright sky
    for (kind, cx, cy, a, b, col) in parts:
        for y in range(CH):
            for x in range(CW):
                if (kind == "circle" and math.hypot(x - cx, y - cy) <= a) or (kind == "rect" and abs(x - cx) <= a and abs(y - cy) <= b):
                    i = (y * CW + x) * 3
                    buf[i:i + 3] = bytes(col)
    return bytes(buf)


class LogoProbeTwoPart(unittest.TestCase):
    def test_emblem_plus_caption_fully_covered(self):
        m = _load()
        # frame coords: emblem centre (1240, 46) r=26 (pink), caption (1243, 91) 52x20 (blue)  -> logo spans y 20..101, x 1214..1269
        parts = [("circle", 1240 - X0, 46 - Y0, 26, 0, (228, 40, 100)), ("rect", 1243 - X0, 91 - Y0, 26, 10, (30, 90, 230))]
        fr = _frame(parts)
        m.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout=fr * 5, returncode=0)
        spec = m.logo_probe("x")
        self.assertIsNotNone(spec)
        r = spec["w"] / 2.0
        cx, cy = spec["x"] + r, spec["y"] + r
        for px, py in ((1240, 20), (1214, 46), (1266, 46), (1217, 101), (1269, 101), (1217, 81), (1269, 81)):
            self.assertLessEqual(math.hypot(px - cx, py - cy), r, "logo point %s,%s not covered by circle %s" % (px, py, spec))

    def test_small_round_logo_stays_tight(self):
        m = _load()
        fr = _frame([("circle", 1240 - X0, 80 - Y0, 20, 0, (20, 110, 40))])
        m.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout=fr * 5, returncode=0)
        spec = m.logo_probe("x")
        self.assertIsNotNone(spec)
        self.assertLessEqual(spec["w"], 40 * 1.5)           # a 40 px ball is not covered by a circle >1.5x its size


if __name__ == "__main__":
    unittest.main()
