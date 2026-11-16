"""2026-11-16: every ball clip ends after the commentator's last word about THAT ball, and keeps its run-up start (no sentence snap back).
Run: PYTHONPATH=. python tests/test_hl_snap_ball.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hl_cutter as C            # noqa: E402


def plan_of(*segs):
    return {"segs": [dict(s) for s in segs], "total": 0.0, "info": {}}


SENT = [{"start": 96.0, "end": 100.5, "text": "bowler daudta hai"}, {"start": 103.0, "end": 109.5, "text": "aur yeh gaya chhakka bahut door"}]


class SnapBall(unittest.TestCase):
    def test_four_end_waits_for_the_sentence(self):
        p = C.snap_plan(plan_of({"start": 99.0, "end": 106.0, "event": "four", "vchecked": True}), SENT, 1000.0, snap_all=True)
        self.assertAlmostEqual(p["segs"][0]["end"], 109.5 + C.SNAP_TAIL)

    def test_start_keeps_the_run_up_even_inside_a_sentence(self):
        p = C.snap_plan(plan_of({"start": 99.0, "end": 106.0, "event": "four", "vchecked": True}), SENT, 1000.0, snap_all=True)
        self.assertEqual(p["segs"][0]["start"], 99.0)

    def test_without_snap_all_plain_clips_stay_fixed(self):
        p = C.snap_plan(plan_of({"start": 99.0, "end": 106.0, "event": "four"}), SENT, 1000.0)
        self.assertEqual(p["segs"][0]["end"], 106.0)

    def test_never_runs_into_a_removed_replay(self):
        p = C.snap_plan(plan_of({"start": 99.0, "end": 106.0, "event": "six", "vchecked": True, "hi": 107.0}), SENT, 1000.0, snap_all=True)
        self.assertLessEqual(p["segs"][0]["end"], 107.0)


if __name__ == "__main__":
    unittest.main()
