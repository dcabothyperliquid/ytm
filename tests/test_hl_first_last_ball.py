"""Regression tests (2026-11-15, North Goa final): the reel opened on the 5th ball and stopped 2 balls before the win.
Run: python tests/test_hl_first_last_ball.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hl_cutter as C            # noqa: E402
import hl_scoreboard as S        # noqa: E402


def _rows(spec, mx=8):
    rows = []
    for t, r, w, b in spec:
        rows += [(t, r, w, b, mx), (t + 4, r, w, b, mx)]
    return rows


class QuietBalls(unittest.TestCase):
    def test_single_and_double_are_events(self):
        ev, _ = S.events_from_states(S.consistent_states(_rows([(0, 0, 0, 0), (30, 2, 0, 1), (60, 2, 0, 2), (90, 3, 0, 3), (120, 5, 0, 4)])))
        self.assertEqual([(e["kind"], e["runs"]) for e in ev], [("dot", 2), ("dot", 0), ("dot", 1), ("dot", 2)])

    def test_boundaries_unchanged(self):
        ev, _ = S.events_from_states(S.consistent_states(_rows([(0, 0, 0, 0), (30, 4, 0, 1), (60, 10, 0, 2), (90, 10, 1, 3)])))
        self.assertEqual([e["kind"] for e in ev], ["four", "six", "wicket"])


def _sb(events, t_final, toss_done):
    return {"toss_start": 5.0, "toss_done": toss_done, "t_final": t_final, "events": events, "innings": [[10.0, 400.0], [500.0, 1000.0]],
            "break": (400.0, 500.0), "mx": 8, "last_over_start": 900.0, "death": [], "kinds": {}}


def _ev(kind, t, inn, runs=0):
    return {"kind": kind, "t": t, "t_end": t, "t_hit": t, "t_state": t + 6, "inn": inn, "balls": 1, "mx": 8, "text": "", "db": 0.0, "runs": runs}


class Plan(unittest.TestCase):
    def setUp(self):
        self.events = [_ev("dot", 100.0, 0, 2)] + [_ev("four", 150.0 + 20 * i, 0, 4) for i in range(4)] + \
                      [_ev("four", 520.0, 1, 4), _ev("dot", 900.0, 1, 1), _ev("dot", 920.0, 1, 0), _ev("dot", 940.0, 1, 2)]   # 940 = winning ball (2 runs)

    def test_first_ball_and_winning_ball_in_reel(self):
        plan = C.plan_from_segments([], 1100.0, 900.0, None, sb=_sb(self.events, t_final=946.0, toss_done=False))
        segs = plan["segs"]
        self.assertTrue(any(s["start"] <= 100.0 <= s["end"] for s in segs), "first ball (2 runs, t=100) missing")
        self.assertTrue(any(s["start"] <= 940.0 <= s["end"] for s in segs), "winning ball (t=940) missing")
        self.assertGreaterEqual(segs[-1]["end"], 946.0 + C.FINISH_S - 1.0, "finish must run past the final scoreboard state")

    def test_no_toss_scene_means_no_20s_filler(self):
        plan = C.plan_from_segments([], 1100.0, 900.0, None, sb=_sb(self.events, t_final=946.0, toss_done=False))
        self.assertNotIn("toss", plan["info"]["counts"])
        self.assertGreaterEqual(plan["segs"][0]["start"], 100.0 - C.FIRST_LEAD - 7.0)

    def test_real_toss_still_shown(self):
        plan = C.plan_from_segments([], 1100.0, 900.0, None, sb=_sb(self.events, t_final=946.0, toss_done=True))
        self.assertEqual(plan["info"]["counts"].get("toss"), 1)


if __name__ == "__main__":
    unittest.main()
