"""Regression (2026-11-22): (a) a dashboard 'start' command was acked before the live list was filled (fresh run) and silently lost;
(b) failed probes blocked a video forever; (c) a flagged runner kept failing with 'Video unavailable' until someone restarted it.
Run: python tests/test_ytm_commands_selfheal.py"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ytm_runner as R


class FakeRelay:
    def __init__(self):
        self.started, self.stopped = [], 0

    def start(self, c):
        self.started.append(c["video_id"])

    def stop(self, *a):
        self.stopped += 1


def cmd(t, vid=None, age=5):
    return {"type": t, "video_id": vid, "id": int((time.time() - age) * 1000)}


class ApplyCommands(unittest.TestCase):
    def test_start_waits_for_live_list(self):
        r, st, c = FakeRelay(), {}, cmd("start", "AAA")
        R.apply_commands({"commands": [c]}, st, r, [], time.time())       # live list still empty
        self.assertEqual(r.started, []); self.assertNotIn("acked", st)    # kept pending, not lost
        R.apply_commands({"commands": [c]}, st, r, [{"video_id": "AAA"}], time.time())
        self.assertEqual(r.started, ["AAA"]); self.assertEqual(st["acked"], c["id"])

    def test_start_dropped_after_wait(self):
        r, st, c = FakeRelay(), {}, cmd("start", "AAA", age=R.CMD_WAIT + 10)
        R.apply_commands({"commands": [c]}, st, r, [], time.time())
        self.assertEqual(r.started, []); self.assertEqual(st["acked"], c["id"])

    def test_later_command_does_not_wait_for_old_pending(self):
        r, st = FakeRelay(), {}
        a, b = cmd("start", "AAA", age=20), cmd("stop", age=5)
        R.apply_commands({"commands": [a, b]}, st, r, [], time.time())
        self.assertEqual(r.stopped, 1); self.assertEqual(st["acked"], b["id"])

    def test_start_ignored_while_relaying(self):
        r, st, c = FakeRelay(), {"relay": {"video_id": "X"}}, cmd("start", "AAA")
        R.apply_commands({"commands": [c]}, st, r, [{"video_id": "AAA"}], time.time())
        self.assertEqual(r.started, []); self.assertEqual(st["acked"], c["id"])


class Blocked(unittest.TestCase):
    def test_old_failures_expire(self):
        now = time.time()
        h = [{"video_id": "V", "outcome": "failed", "ended": now - 4000} for _ in range(5)]
        self.assertFalse(R.Relay({"history": h}, "rtmp://x/y")._blocked("V", now))
        h = [{"video_id": "V", "outcome": "failed", "ended": now - 300} for _ in range(3)]
        self.assertTrue(R.Relay({"history": h}, "rtmp://x/y")._blocked("V", now))


class Flagged(unittest.TestCase):
    def fails(self, vids, age=600):
        now = time.time()
        return [{"video_id": v, "outcome": "failed", "ended": now - age, "note": "probe: ERROR: Video unavailable. This content isn't available."} for v in vids]

    def test_flagged_on_two_videos(self):
        now = time.time()
        st = {"history": self.fails(["A", "A", "B", "B"]), "run_started": now - 900}
        self.assertTrue(R.runner_flagged(st, now))

    def test_not_flagged_single_video_or_relaying_or_recent(self):
        now = time.time()
        self.assertFalse(R.runner_flagged({"history": self.fails(["A"] * 5), "run_started": now - 900}, now))
        self.assertFalse(R.runner_flagged({"history": self.fails(["A", "A", "B", "B"]), "run_started": now - 900, "relay": {"video_id": "A"}}, now))
        self.assertFalse(R.runner_flagged({"history": self.fails(["A", "A", "B", "B"]), "run_started": now - 60}, now))
        self.assertFalse(R.runner_flagged({"history": self.fails(["A", "A", "B", "B"]), "run_started": now - 900, "selfheal_at": now - 100}, now))

    def test_regex_only_runner_errors(self):
        self.assertTrue(R.FLAG_RE.search("probe: ERROR: Video unavailable. This content isn't available."))
        self.assertTrue(R.FLAG_RE.search("Sign in to confirm you're not a bot"))
        self.assertFalse(R.FLAG_RE.search("source not back in 900s"))
        self.assertFalse(R.FLAG_RE.search("both streams ok"))


if __name__ == "__main__":
    unittest.main()
