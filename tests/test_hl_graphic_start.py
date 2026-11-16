"""2026-11-16 (Naem): highlight clips showed only the ball flying (four / six going), no run-up, because the clip start came from a
camera cut-away detector + per-match calibration (right on one match, wrong on the next). New rule, no guessing:
  start = SIX / FOUR / WICKET graphic onset - fixed lead (four / six 13 s, wicket 16 s)  -> the bowler's run-up is always in
  end   = graphic onset + 1.5 s, then moved to the end of that ball's commentary sentence
Run: python tests/test_hl_graphic_start.py"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hl_cutter as C            # noqa: E402
import hl_scoreboard as S        # noqa: E402


def _sb(events, last_over_start=1500.0):
    return {"toss_start": 5.0, "toss_done": False, "t_final": 1900.0, "events": events, "innings": [[10.0, 700.0], [800.0, 1900.0]],
            "break": (700.0, 800.0), "mx": 8, "last_over_start": last_over_start, "death": [], "kinds": {}}


def _gfx(kind, tb, inn=0, db=0.0, banner=True):
    """an event exactly as hl_scoreboard.align_events leaves it: t = graphic onset - BACK_BY_KIND"""
    t = tb - S.BACK_BY_KIND[kind]
    e = {"kind": kind, "t": t, "t_end": t, "t_hit": t, "t_state": tb + 6, "inn": inn, "balls": 1, "mx": 8, "text": "", "db": db, "runs": 4}
    if banner:
        e["t_banner"] = tb
    return e


def _seg_at(plan, t):
    return next(s for s in plan["segs"] if s["start"] <= t <= s["end"])


class GraphicWindow(unittest.TestCase):
    def plan(self, evs):
        return C.plan_from_segments([], 2000.0, 900.0, None, sb=_sb(evs))

    def test_four_six_wicket_start_at_fixed_lead_before_graphic(self):
        evs = [_gfx("four", 200.0), _gfx("six", 300.0), _gfx("wicket", 400.0)]
        plan = self.plan(evs)
        for kind, tb, lead in (("four", 200.0, 13.0), ("six", 300.0, 13.0), ("wicket", 400.0, 16.0)):
            s = _seg_at(plan, tb)
            self.assertLessEqual(s["start"], tb - lead + 0.01, kind)       # never later than the fixed lead (a featured six gets +1 s more)
            self.assertGreaterEqual(s["start"], tb - lead - 1.01, kind)
            self.assertGreaterEqual(s["end"], tb + C.GFX_TAIL - 0.01, kind)

    def test_run_up_is_in_for_every_kind(self):                    # the shot is >= 3 s after the clip start, always
        for kind in ("four", "six", "wicket"):
            tb = 500.0
            plan = self.plan([_gfx(kind, tb)])
            shot = tb - S.BACK_BY_KIND[kind]
            self.assertGreaterEqual(shot - _seg_at(plan, tb)["start"], 3.0, kind)

    def test_event_without_graphic_uses_the_same_window(self):     # graphic missed by OCR: t is the lag-based estimate
        a = self.plan([_gfx("six", 300.0, banner=True)])
        b = self.plan([_gfx("six", 300.0, banner=False)])
        self.assertEqual(_seg_at(a, 300.0)["start"], _seg_at(b, 300.0)["start"])

    def test_graphic_clips_are_marked_for_sentence_snap(self):
        plan = self.plan([_gfx("four", 200.0), _gfx("six", 300.0), _gfx("wicket", 400.0)])
        for tb in (200.0, 300.0, 400.0):
            s = _seg_at(plan, tb)
            self.assertTrue(s["feat"] and s["gfx"])

    def test_wide_and_noball_unchanged(self):
        e = {"kind": "wide", "t": 250.0, "t_end": 250.0, "t_hit": 250.0, "t_state": 256.0, "inn": 0, "balls": 1, "mx": 8, "text": "", "db": 0.0, "runs": 1}
        s = _seg_at(self.plan([e]), 250.0)
        self.assertAlmostEqual(s["start"], 250.0 - 9.0, places=1)
        self.assertAlmostEqual(s["end"], 250.0 + 1.5, places=1)


class NoCutAwayDetector(unittest.TestCase):
    def test_functions_gone(self):
        for name in ("refine_shots", "shot_time", "shot_cut_time", "hard_cuts"):
            self.assertFalse(hasattr(C, name), name)

    def test_build_plan_does_not_calibrate(self):
        src = inspect.getsource(C.build_plan)
        self.assertNotIn("refine_shots", src)
        self.assertNotIn("calibration", src)


class SentenceSnap(unittest.TestCase):
    def plan(self, start, end, gfx=True):
        return {"segs": [{"start": start, "end": end, "event": "four", "parts": 1, "replay": False, "feat": True, "gfx": gfx}],
                "total": end - start, "info": {}}

    def test_end_runs_to_the_end_of_the_commentary_sentence(self):
        commentary = [{"start": 190.0, "end": 208.0, "text": "oh that is struck beautifully over cover for four"}]
        out = C.snap_plan(self.plan(187.0, 201.5), commentary, 2000.0)
        self.assertAlmostEqual(out["segs"][0]["end"], 208.0 + C.SNAP_TAIL, places=1)

    def test_run_up_is_not_trimmed_by_a_sentence_tail(self):
        commentary = [{"start": 170.0, "end": 188.0, "text": "end of the previous ball talk"}]
        out = C.snap_plan(self.plan(187.0, 200.0), commentary, 2000.0)
        self.assertLessEqual(out["segs"][0]["start"], 187.0)

    def test_start_may_move_earlier_to_a_sentence_start(self):
        commentary = [{"start": 184.5, "end": 195.0, "text": "bowler runs in and bowls"}]
        out = C.snap_plan(self.plan(187.0, 200.0), commentary, 2000.0)
        self.assertLess(out["segs"][0]["start"], 187.0)


class LengthGuard(unittest.TestCase):
    def test_boundary_heavy_match_is_trimmed_wickets_kept(self):
        evs = [_gfx("four", 100.0 + 25.0 * i, 0, db=float(i % 7)) for i in range(26)]
        evs += [_gfx("wicket", 120.0 + 120.0 * i, 0) for i in range(5)]
        evs += [_gfx("four", 820.0 + 27.0 * i, 1, db=float(i % 5)) for i in range(26)]
        plan = C.plan_from_segments([], 2000.0, 900.0, None, sb=_sb(evs))
        self.assertGreater(plan["dropped"]["four"], 0)
        for i in range(5):
            self.assertTrue(any(s["start"] <= 120.0 + 120.0 * i <= s["end"] for s in plan["segs"]), "wicket %d dropped" % i)


if __name__ == "__main__":
    unittest.main()
