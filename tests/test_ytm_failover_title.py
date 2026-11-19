"""Regression (2026-11-19): relay 1 failed over from 'KPC TROPHY 2026 || INAMPURI' to another match (Sagar Bhau Tangde Chashak),
but YouTube kept the OLD match title/thumbnail: outcome 'failed' did not end the broadcast, the next match was streamed into it,
and set_caption skipped it because that broadcast id was already captioned.
Run: python tests/test_ytm_failover_title.py"""
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ytm_runner as R  # noqa: E402


class FakeYT:
    """Tiny liveBroadcasts fake: one ACTIVE broadcast bound to our stream."""
    def __init__(self, title):
        self.bc = {"id": "B1", "snippet": {"title": title, "scheduledStartTime": "2026-11-19T10:00:00Z"},
                   "status": {"lifeCycleStatus": "live"}, "contentDetails": {"boundStreamId": "S1"}}
        self.puts = []

    def __call__(self, req, timeout=20):
        url = req.full_url
        body = json.loads(req.data) if req.data else {}
        if "liveStreams" in url:
            out = {"items": [{"id": "S1", "snippet": {}, "status": {"streamStatus": "active"},
                              "cdn": {"ingestionInfo": {"streamName": "KEY"}}}]}
        elif "liveBroadcasts" in url and req.get_method() == "PUT":
            self.puts.append(body["snippet"]["title"])
            self.bc["snippet"]["title"] = body["snippet"]["title"]
            out = {}
        elif "liveBroadcasts" in url:
            out = {"items": [self.bc]}
        else:
            out = {}
        return io.BytesIO(json.dumps(out).encode())


class FailoverTitle(unittest.TestCase):
    def setUp(self):
        R.DIAG.clear()
        self.env = mock.patch.dict(os.environ, {"YT_STREAM_KEY": "KEY", "YTM_STYLE": "off"})
        self.env.start()
        self.tok = mock.patch.object(R, "_oauth_token", return_value="t")
        self.tok.start()
        self.thumb = mock.patch.object(R, "_set_thumb", return_value=True)
        self.thumb.start()

    def tearDown(self):
        for p in (self.env, self.tok, self.thumb):
            p.stop()

    def test_reused_broadcast_gets_new_match_title(self):
        yt = FakeYT("")
        with mock.patch("urllib.request.urlopen", yt):
            R.set_caption("KPC TROPHY 2026 || INAMPURI", "@a")
            first = yt.bc["snippet"]["title"]
            R.set_caption("Sagar Bhau Tangde Chashak 2026 || All India Open", "@b")   # failover into the same broadcast
            second = yt.bc["snippet"]["title"]
        self.assertNotEqual(first, second)
        self.assertEqual(len(yt.puts), 2)

    def test_same_title_is_put_only_once(self):
        yt = FakeYT("")
        with mock.patch("urllib.request.urlopen", yt):
            for _ in range(4):
                R.set_caption("KPC TROPHY 2026 || INAMPURI", "@a")
        self.assertEqual(len(yt.puts), 1)                  # 50 quota units: never per tick

    def test_different_match_failover_ends_old_broadcast(self):
        r = R.Relay({"relay": {"video_id": "A", "title": "KPC TROPHY 2026 | FINAL | INAMPURI"}}, "rtmp://x/KEY")
        ended = []
        r._end_yt = lambda: ended.append(1) or "done"
        r.start = lambda c: r.st.update(relay={"video_id": c["video_id"], "title": c["title"]})
        r.live_last = [{"video_id": "B", "title": "Sagar Bhau Tangde Chashak 2026 | Open", "channel": "x"}]
        self.assertTrue(r._failover("failed", "ffmpeg/probe failed"))
        self.assertEqual(len(ended), 1)

    def test_same_match_failover_keeps_broadcast(self):
        r = R.Relay({"relay": {"video_id": "A", "title": "KPC TROPHY 2026 | FINAL | INAMPURI"}}, "rtmp://x/KEY")
        ended = []
        r._end_yt = lambda: ended.append(1) or "done"
        r.start = lambda c: r.st.update(relay={"video_id": c["video_id"], "title": c["title"]})
        r.live_last = [{"video_id": "B", "title": "KPC TROPHY 2026 | FINAL | INAMPURI", "channel": "y"}]
        self.assertTrue(r._failover("failed", "ffmpeg/probe failed"))
        self.assertEqual(ended, [])                        # same match, other camera: viewers must not see a cut


if __name__ == "__main__":
    unittest.main()
