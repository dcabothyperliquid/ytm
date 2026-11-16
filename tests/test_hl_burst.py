"""Regression (2026-11-16, NGPL final): the 523 s reel lost a wicket (10-2 -> 12-3) and the whole 18-run / 1-wicket stretch of the
last over (67-4 -> 85-5) because the scoreboard operator typed the score after 3-6 balls and the jump matched no rule.
Run: python tests/test_hl_burst.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hl_scoreboard as S        # noqa: E402


def _rows(spec, mx=15):
    rows = []
    for t, r, w, b in spec:
        rows += [(t, r, w, b, mx), (t + 4, r, w, b, mx)]
    return rows


def _events(spec):
    ev, _ = S.events_from_states(S.consistent_states(_rows(spec)))
    return ev


class Jumps(unittest.TestCase):
    def test_wicket_typed_after_three_balls(self):         # 10-2 (1.2) -> 12-3 (1.5)
        ev = _events([(0, 10, 2, 8), (60, 12, 3, 11)])
        self.assertEqual([e["kind"] for e in ev], ["wicket"])

    def test_last_over_wicket_with_big_runs(self):         # 67-4 (6.3) -> 85-5 (7.3): 18 runs + wicket in 6 balls
        ev = _events([(0, 67, 4, 39), (60, 85, 5, 45)])
        kinds = [e["kind"] for e in ev]
        self.assertIn("wicket", kinds)
        self.assertIn("burst", kinds)

    def test_runs_only_burst(self):
        ev = _events([(0, 20, 1, 12), (60, 38, 1, 17)])    # 18 runs in 5 balls, no wicket
        self.assertEqual([e["kind"] for e in ev], ["burst"])
        b = ev[0]
        self.assertLess(b["t"], b["t_state"])
        self.assertAlmostEqual(b["t_prev"], 4.0)           # the stretch starts at the previous reading (its last frame)

    def test_quiet_multi_ball_jump_is_not_a_burst(self):
        ev = _events([(0, 20, 1, 12), (60, 26, 1, 15)])    # 6 runs in 3 balls: ordinary
        self.assertEqual([e["kind"] for e in ev], ["dot"])

    def test_old_rules_unchanged(self):
        ev = _events([(0, 0, 0, 0), (30, 4, 0, 1), (60, 10, 0, 2), (90, 10, 1, 3), (120, 12, 1, 4)])
        self.assertEqual([e["kind"] for e in ev], ["four", "six", "wicket", "dot"])

    def test_misread_is_still_rejected(self):              # 89 -> 99 in zero balls is an OCR misread, not a state
        a = {"runs": 89, "wk": 3, "balls": 40}
        self.assertFalse(S._ok_step(a, {"runs": 99, "wk": 3, "balls": 40}))
        self.assertTrue(S._ok_step(a, {"runs": 107, "wk": 4, "balls": 46}))


if __name__ == "__main__":
    unittest.main()
