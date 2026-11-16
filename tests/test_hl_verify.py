"""Tests for hl_verify (2026-11-16): the frame check must pull a late clip back onto the run-up and trim a too-early one.
Run: python tests/test_hl_verify.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hl_verify as V            # noqa: E402

R, S, T, A, P, O = "RUNUP", "SHOT", "TRACK", "AFTER", "REPLAY", "OTHER"


class Rules(unittest.TestCase):
    def test_good_clip(self):
        j = V.judge([R, R, R, S, T, T, A, A], "four", 1.0)
        self.assertEqual(j["action"], "ok")

    def test_starts_in_ball_flight_is_late(self):          # the 2026-11-15 NGPL clips: ball already flying at frame 1
        j = V.judge([T, T, T, A, A, A, A, A], "four", 1.0)
        self.assertEqual((j["action"], j["shift"]), ("earlier", 7.0))   # 3 s of tracking seen + 4 s run-up

    def test_starts_on_the_shot_is_late(self):
        self.assertEqual(V.judge([S, T, T, A, A, A, A, A], "six", 1.5)["action"], "earlier")

    def test_after_without_runup_is_late(self):
        j = V.judge([A, A, A, A, A, A, A, A], "wicket", 1.0)
        self.assertEqual((j["action"], j["shift"]), ("earlier", 6.0))

    def test_previous_ball_aftermath_first_means_too_early(self):
        j = V.judge([A, A, R, R, S, T, A, A], "four", 1.0)
        self.assertEqual(j["action"], "later")
        self.assertAlmostEqual(j["shift"], 1.0)

    def test_replay_first_with_ball_later_is_ambiguous(self):
        j = V.judge([P, P, P, P, R, S, T, A], "six", 1.0)
        self.assertEqual(j["action"], "ambiguous")

    def test_long_runup_trimmed(self):
        j = V.judge([R] * 6 + [S, T], "four", 2.0)         # 12 s of run-up
        self.assertEqual(j["action"], "later")
        self.assertAlmostEqual(j["shift"], 12.0 - V.KEEP_LEAD_S)

    def test_ends_before_the_shot(self):
        j = V.judge([R] * 8, "four", 1.0)
        self.assertEqual((j["action"], j["shift"]), ("extend", 4.0))

    def test_dot_ball_needs_no_shot(self):
        self.assertEqual(V.judge([R, R, R, A, A, A, A, A], "dot", 1.0)["action"], "ok")

    def test_next_run_up_at_the_end_is_trimmed(self):
        j = V.judge([R, R, S, T, A, R, R, R], "four", 1.5)
        self.assertEqual(j["action"], "trim_end")
        self.assertAlmostEqual(j["shift"], 5 * 1.5 - 0.3)

    def test_replay_inside_trims_the_end(self):
        j = V.judge([R, R, S, T, A, A, P, P], "six", 1.5)
        self.assertEqual(j["action"], "trim_end")

    def test_unreadable_is_unknown_not_a_loop(self):
        self.assertEqual(V.judge([O] * 8, "four", 1.0)["action"], "unknown")

    def test_long_clip_without_a_tracking_cut_is_ok(self):   # bowled / lbw: the camera never follows a ball
        self.assertEqual(V.judge([R] * 6 + [A] * 18, "wicket", 0.5)["action"], "ok")


class CvLabels(unittest.TestCase):
    def test_labels_follow_the_camera(self):
        self.assertEqual(V.cv_labels([1, 1, 1, 0, 0, 1, 1, 1]), [R, R, R, T, T, A, A, A])
        self.assertEqual(V.cv_labels([0, 0, 0, 1, 1, 1, 1]), [T, T, T, A, A, A, A])     # starts inside the tracking = late

    def test_one_odd_frame_is_not_a_cut(self):
        self.assertEqual(V.cv_labels([1, 1, 0, 1, 1, 1]), [R] * 6)
        self.assertEqual(V.cv_labels([1, 1, 1, 1, 0, 1]), [R] * 6)


def world(T0, runup=5.0, flight=4.0, after=8.0, prev_after=True):
    """one ball: RUNUP [T0-runup, T0), SHOT [T0, T0+.8), TRACK flight, AFTER; before the run-up the previous ball's aftermath"""
    def lab(t):
        if t < T0 - runup:
            return A if prev_after else O
        if t < T0:
            return R
        if t < T0 + 0.8:
            return S
        if t < T0 + 0.8 + flight:
            return T
        if t < T0 + 0.8 + flight + after:
            return A
        return R                                           # next ball's run-up
    return lab


def make_label_fn(lab):
    def fn(a, b):
        dt = (b - a) / V.N_FRAMES
        return [lab(a + i * dt) for i in range(V.N_FRAMES)], dt
    return fn


class Loop(unittest.TestCase):
    def run_case(self, start, end, prev_end=0.0, nxt=1e9):
        seg = {"event": "four", "start": start, "end": end, "t": 100.0}
        V.verify_seg(seg, prev_end, nxt, 1e6, make_label_fn(world(100.0)), log=lambda *a: None)
        return seg

    def check_good(self, seg):
        self.assertTrue(seg["verify"]["ok"], seg["verify"])
        self.assertLessEqual(seg["start"], 100.0 - 1.0)       # starts on the run-up, before the ball
        self.assertGreaterEqual(seg["start"], 100.0 - V.MAX_LEAD_S - 1.0)
        self.assertGreaterEqual(seg["end"], 100.0 + 5.0)      # shot + flight shown

    def test_late_start_is_pulled_back(self):
        self.check_good(self.run_case(101.0, 109.0))

    def test_very_late_start(self):
        self.check_good(self.run_case(106.0, 114.0))

    def test_start_in_previous_ball_is_pushed_forward(self):
        self.check_good(self.run_case(84.0, 98.0))

    def test_too_short_end_is_extended(self):
        self.check_good(self.run_case(95.0, 100.5))

    def test_ok_clip_is_untouched(self):
        seg = self.run_case(96.0, 108.0)
        self.assertTrue(seg["verify"]["ok"])
        self.assertEqual((seg["start"], seg["end"]), (96.0, 108.0))

    def test_never_goes_back_into_the_previous_clip(self):
        seg = self.run_case(101.0, 109.0, prev_end=98.0)
        self.assertGreaterEqual(seg["start"], 98.2 - 1e-6)

    def test_gives_up_with_the_best_try_and_marks_unsure(self):
        seg = {"event": "four", "start": 101.0, "end": 109.0}
        V.verify_seg(seg, 100.9, 1e9, 1e6, make_label_fn(world(100.0)), log=lambda *a: None)
        self.assertFalse(seg["verify"]["ok"])

    def test_works_for_every_start_offset(self):
        for off in range(-12, 14):
            seg = self.run_case(100.0 + off, 100.0 + off + 8.0)
            self.assertTrue(seg["verify"]["ok"], (off, seg["verify"]))


PREVIEW = "/home/claude/hl/hl_p.mp4"      # the finished 523 s NGPL reel: clips 9 s and 123 s start with the ball already flying


@unittest.skipUnless(os.path.exists(PREVIEW), "needs the sample preview")
class RealFrames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.look = V.Looker(PREVIEW, 523.0, log=lambda *a: None)
        cls.mode = cls.look.calibrate()

    def test_model_is_learned_on_this_ground(self):
        self.assertEqual(self.mode, "learned", self.look.model.info)

    def test_late_clips_are_seen_as_late(self):
        for a, b in ((9, 17), (123, 131), (25, 33)):
            lab, dt = self.look.labels(a, b)
            self.assertEqual(lab[0], "TRACK", (a, "".join(x[0] for x in lab)))

    def test_good_clips_start_on_the_pitch_camera(self):
        for a, b in ((17, 25), (0, 9), (261, 272)):
            lab, dt = self.look.labels(a, b)
            self.assertEqual(lab[0], "RUNUP", (a, "".join(x[0] for x in lab)))

    def test_late_clip_is_flagged_by_judge(self):
        lab, dt = self.look.labels(123, 131)
        self.assertEqual(V.judge(lab, "four", dt)["action"], "earlier")


if __name__ == "__main__":
    unittest.main()
