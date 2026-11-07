"""
YouTube Live Mirror — ALL-IN-ONE worker for GitHub Actions (stdlib only, no repo imports).
Render / Turso are not involved: config + state live as JSON files on the `ytm-data`
branch of this repo (written by the dashboard proxy / by this worker).

  python ytm_runner.py gate   # cheap check: should the heavy job run? (prints go=true/false)
  python ytm_runner.py run    # watch channels + relay (loops ~5h50m, next run chains via cron)

Env: GITHUB_TOKEN, GITHUB_REPOSITORY (set by Actions), YOUTUBE_API_KEY, YT_STREAM_KEY,
     optional YT_RTMP_BASE, cookies.txt (from YT_COOKIES secret).
"""
import base64
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

REPO = os.environ.get("GITHUB_REPOSITORY", "dcabothyperliquid/ytm")
BRANCH = "ytm-data"
CFG_PATH, ST_PATH = "ytm_config.json", "ytm_state.json"
MAX_RUN_MIN = int(os.environ.get("MAX_RUN_MIN", "350"))
ENDED_GRACE = 90          # s the source may be missing from the live list before we stop
RETRY_COOLDOWN = 120
MAX_ATTEMPTS = 3
MAX_RESTARTS = 8
HEARTBEAT = 90            # state is re-written at least this often so the dashboard sees the worker as alive
CFG_EVERY = 15
COOKIES_FILE = "cookies.txt"
_API = "https://www.googleapis.com/youtube/v3/"
_RSS = "https://www.youtube.com/feeds/videos.xml?channel_id="
_UA = {"User-Agent": "Mozilla/5.0 (compatible; ytm/1.0)"}
IST = 5.5 * 3600
_stop = {"flag": False}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ── GitHub file store (data branch) ──────────────────────────────────────
def gh(method, path, body=None):
    req = urllib.request.Request(
        "https://api.github.com/repos/%s/%s" % (REPO, path),
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Authorization": "Bearer " + os.environ.get("GITHUB_TOKEN", ""),
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "ytm-runner", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def read_json(path):
    """-> (obj or None, sha or None)"""
    try:
        d = gh("GET", "contents/%s?ref=%s" % (path, BRANCH))
        return json.loads(base64.b64decode(d["content"]).decode()), d["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise


def write_json(path, obj, sha=None, msg="ytm update"):
    body = {"message": msg, "branch": BRANCH,
            "content": base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()}
    if sha:
        body["sha"] = sha
    try:
        return gh("PUT", "contents/" + path, body)["content"]["sha"]
    except urllib.error.HTTPError as e:
        if e.code in (409, 422):                     # stale sha: refetch once
            _, cur = read_json(path)
            body["sha"] = cur
            return gh("PUT", "contents/" + path, body)["content"]["sha"]
        raise


# ── helpers ──────────────────────────────────────────────────────────────
def in_window(cfg, now=None):
    ws, we = int(cfg.get("window_start", 0)), int(cfg.get("window_end", 24))
    if ws == 0 and we == 24:
        return True
    h = time.gmtime((now or time.time()) + IST).tm_hour
    return ws <= h < we if ws < we else (h >= ws or h < we)


def day_start(now=None):
    t = time.gmtime(now or time.time())
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, 0)) - time.timezone


def minutes_today(state, now=None):
    now = now or time.time()
    s0, tot = day_start(now), 0.0
    for h in state.get("history", []):
        if not h.get("started"):          # failed probe: never streamed, must not count toward the daily cap
            continue
        a, b = max(h.get("started") or 0, s0), h.get("ended") or 0
        if b > a:
            tot += (b - a) / 60.0
    r = state.get("relay")
    if r and r.get("started"):
        tot += (now - max(r["started"], s0)) / 60.0
    return int(tot)


def http(url, timeout=15):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return r.read()


# ── YouTube detection ────────────────────────────────────────────────────
class Detector:
    def __init__(self, state):
        self.st = state

    def _api(self, path, params):
        key = os.environ.get("YOUTUBE_API_KEY", "").strip()
        if not key:
            raise RuntimeError("YOUTUBE_API_KEY GitHub secret set nahi hai")
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if self.st.get("units_day") != day:
            self.st["units_day"], self.st["units_today"] = day, 0
        self.st["units_today"] = self.st.get("units_today", 0) + 1
        return json.loads(http(_API + path + "?" + urllib.parse.urlencode(dict(params, key=key))))

    def channel_id(self, c):
        if c.get("kind") == "id":
            return c["value"]
        res = self.st.setdefault("resolved", {})
        k = c["value"].lower()
        if k not in res:
            items = self._api("channels", {"part": "id", "forHandle": c["value"]}).get("items") or []
            if not items:
                raise RuntimeError("handle nahi mila: " + c["value"])
            res[k] = items[0]["id"]
        return res[k]

    def recent_ids(self, cid, limit=6):
        try:
            root = ET.fromstring(http(_RSS + cid))
            ids = [e.text for e in root.iter("{http://www.youtube.com/xml/schemas/2015}videoId") if e.text]
            if ids:
                return ids[:limit]
        except Exception as e:
            log("rss fail %s: %s" % (cid, e))
        d = self._api("playlistItems", {"part": "contentDetails", "maxResults": limit, "playlistId": "UU" + cid[2:]})
        return [i["contentDetails"]["videoId"] for i in d.get("items", [])]

    def live(self, cfg):
        kws = [k.strip().lower() for k in (cfg.get("keywords") or "").split(",") if k.strip()]
        per, errs = [], []
        for c in cfg.get("channels", []):
            try:
                cid = self.channel_id(c)
                per.append((c, cid, self.recent_ids(cid)))
            except Exception as e:
                errs.append("%s: %s" % (c.get("name") or c["value"], str(e)[:80]))
        ids = list(dict.fromkeys(v for _, _, l in per for v in l))
        info = {}
        for i in range(0, len(ids), 50):
            for it in self._api("videos", {"part": "snippet,liveStreamingDetails", "id": ",".join(ids[i:i + 50])}).get("items", []):
                info[it["id"]] = it
        out = []
        for c, cid, l in per:
            for v in l:
                it = info.get(v)
                if not it or it["snippet"].get("liveBroadcastContent") != "live":
                    continue
                t = it["snippet"].get("title", "")
                if kws and not any(k in t.lower() for k in kws):
                    continue
                out.append({"video_id": v, "title": t[:120], "channel": c.get("name") or c["value"],
                            "url": "https://www.youtube.com/watch?v=" + v})
        return out, errs


# ── relay ────────────────────────────────────────────────────────────────
def probe(url):
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "--js-runtimes", "node", "--remote-components", "ejs:github"] + (["--cookies", COOKIES_FILE] if os.path.exists(COOKIES_FILE) else []) + ["-J", url]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "yt-dlp failed").strip().splitlines()[-1][:160])
    info = json.loads(p.stdout)
    best = None
    for f in info.get("formats") or []:
        if not str(f.get("protocol", "")).startswith("m3u8") or f.get("vcodec") in (None, "none") or f.get("acodec") in (None, "none"):
            continue
        h = f.get("height") or 0
        if h <= 720 and (best is None or h > (best.get("height") or 0)):
            best = f
    if best is None and info.get("url"):
        best = {"url": info["url"]}
    return bool(info.get("is_live")), (best or {}).get("url")


def ffmpeg_cmd(hls, rtmp):
    return ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5", "-fflags", "+genpts", "-i", hls, "-c", "copy", "-f", "flv", rtmp]


class Relay:
    def __init__(self, state, rtmp, _probe=probe, _popen=subprocess.Popen):
        self.st, self.rtmp, self._probe, self._popen = state, rtmp, _probe, _popen
        self.proc, self.restarts = None, 0

    # -- lifecycle
    def _finish(self, outcome, note=""):
        r = self.st.get("relay") or {}
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except Exception:
                self.proc.kill()
        self.proc = None
        self.st.setdefault("history", []).insert(0, {
            "video_id": r.get("video_id"), "title": r.get("title"), "channel": r.get("channel"),
            "started": r.get("started"), "ended": int(time.time()), "outcome": outcome, "note": note})
        del self.st["history"][30:]
        self.st["relay"] = None

    def start(self, c):
        try:
            is_live, hls = self._probe(c["url"])
        except Exception as e:
            self._record_fail(c, "probe: %s" % e)
            return
        if not is_live or not hls:
            return                       # not actually live (yet); try again next cycle
        log("relay start: " + c["title"][:80])
        self.restarts = 0
        self.proc = self._popen(ffmpeg_cmd(hls, self.rtmp))
        self.st["relay"] = {"video_id": c["video_id"], "title": c["title"], "channel": c["channel"],
                            "url": c["url"], "started": int(time.time()), "not_live_since": None}

    def _record_fail(self, c, note):
        self.st.setdefault("history", []).insert(0, {
            "video_id": c["video_id"], "title": c["title"], "channel": c["channel"], "started": None,
            "ended": int(time.time()), "outcome": "failed", "note": note[:120]})

    def poll(self):
        """Call every few seconds: handle ffmpeg exiting."""
        r = self.st.get("relay")
        if not r or not self.proc or self.proc.poll() is None:
            return
        code = self.proc.poll()
        log("ffmpeg exit %s" % code)
        self.proc = None
        try:
            is_live, hls = self._probe(r["url"])
        except Exception as e:
            is_live, hls = None, None
            note = "probe: %s" % e
        if is_live and hls and self.restarts < MAX_RESTARTS:
            self.restarts += 1
            log("source still live -> restart ffmpeg (%d)" % self.restarts)
            self.proc = self._popen(ffmpeg_cmd(hls, self.rtmp))
        elif is_live is False:
            self._finish("ended", "source ended")
        else:
            self._finish("failed", "ffmpeg/probe failed")

    def stop(self, outcome="stopped", note="manual"):
        if self.st.get("relay"):
            self._finish(outcome, note)

    # -- decisions on each detection
    def _blocked(self, vid, now):
        h = self.st.get("history", [])
        fails = [x for x in h if x["video_id"] == vid and x["outcome"] == "failed"]
        if len(fails) >= MAX_ATTEMPTS or (fails and now - fails[0]["ended"] < RETRY_COOLDOWN):
            return True
        return any(x["video_id"] == vid and x["outcome"] in ("ended", "stopped") for x in h[:10])

    def step(self, live, cfg, now):
        r = self.st.get("relay")
        cap = int(cfg.get("daily_cap_h", 12)) * 60
        if r:
            if any(x["video_id"] == r["video_id"] for x in live):
                r["not_live_since"] = None
            else:
                r["not_live_since"] = r.get("not_live_since") or int(now)
                if now - r["not_live_since"] > ENDED_GRACE:
                    self._finish("ended", "not in live list")
                    r = None
            if r and minutes_today(self.st, now) >= cap:
                self._finish("stopped", "daily cap")
            if r:
                return
        if minutes_today(self.st, now) >= cap:
            return
        for c in live:
            if not self._blocked(c["video_id"], now):
                self.start(c)
                if self.st.get("relay"):
                    return


def apply_commands(cfg, st, relay, live, now):
    acked = st.get("acked", 0)
    for cmd in sorted(cfg.get("commands", []), key=lambda c: c["id"]):
        if cmd["id"] <= acked:
            continue
        st["acked"] = cmd["id"]
        if cmd["type"] == "stop":
            relay.stop("stopped", "manual stop")
        elif cmd["type"] == "start" and not st.get("relay"):
            c = next((x for x in live if x["video_id"] == cmd.get("video_id")), None)
            if c:
                relay.start(c)


# ── entry points ─────────────────────────────────────────────────────────
def gate():
    out = "false"
    try:
        cfg, _ = read_json(CFG_PATH)
        st, _ = read_json(ST_PATH)
        if cfg and cfg.get("enabled") and cfg.get("channels") and in_window(cfg) \
                and minutes_today(st or {}) < int(cfg.get("daily_cap_h", 12)) * 60:
            out = "true"
    except Exception as e:
        log("gate error: %s" % e)
    log("gate go=%s" % out)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("go=%s\n" % out)
    return 0


def run(_sleep=time.sleep, _read=None, _write=None, _relay_factory=None, _detector_factory=None, max_loops=None):
    _read, _write = _read or read_json, _write or write_json
    key = os.environ.get("YT_STREAM_KEY", "").strip()
    if not key:
        log("YT_STREAM_KEY secret missing")
        return 1
    signal.signal(signal.SIGTERM, lambda *a: _stop.update(flag=True))
    st, st_sha = _read(ST_PATH)
    st = st or {}
    st["run_started"] = int(time.time())
    # an interrupted previous run may have left a relay record: close it, new run resumes from live list
    if st.get("relay"):
        st["history"] = st.get("history", [])
        st["history"].insert(0, {**{k: st["relay"].get(k) for k in ("video_id", "title", "channel", "started")},
                                 "ended": int(time.time()), "outcome": "interrupted", "note": "job chained"})
        st["relay"] = None
    relay = (_relay_factory or Relay)(st, os.environ.get("YT_RTMP_BASE", "rtmp://a.rtmp.youtube.com/live2").rstrip("/") + "/" + key)
    det = (_detector_factory or Detector)(st)
    deadline = time.time() + MAX_RUN_MIN * 60
    last_cfg = last_detect = last_write = 0
    last_sig, live, errors, cfg, loops = None, [], [], {}, 0
    while time.time() < deadline and not _stop["flag"]:
        now = time.time()
        relay.poll()
        if now - last_cfg >= CFG_EVERY:                       # config/commands: cheap, every ~15s
            cfg = _read(CFG_PATH)[0] or {}
            last_cfg = now
            if not (cfg.get("enabled") and cfg.get("channels") and in_window(cfg)):
                log("disabled / outside window / no channels -> stop")
                relay.stop("stopped", "mirror off")
                break
            st["poll_secs"] = int(cfg.get("poll_secs", 60))
            apply_commands(cfg, st, relay, live, now)
        if now - last_detect >= cfg_poll(st):                 # YouTube detection: every poll_secs
            last_detect = now
            try:
                live, errors = det.live(cfg)
                st["last_check"] = int(now)
                relay.step(live, cfg, now)
            except Exception as e:
                errors = ["%s: %s" % (type(e).__name__, str(e)[:120])]
            if minutes_today(st, now) >= int(cfg.get("daily_cap_h", 12)) * 60 and not st.get("relay"):
                log("daily cap reached -> stop")
                break
        st.update(live=live, errors=errors, heartbeat=int(now),
                  status="relaying" if st.get("relay") else "watching")
        sig = json.dumps({k: v for k, v in st.items() if k not in ("heartbeat", "last_check", "units_today")}, sort_keys=True)
        if sig != last_sig or now - last_write >= HEARTBEAT:
            try:
                st_sha = _write(ST_PATH, st, st_sha, "ytm state")
                last_sig, last_write = sig, now
            except Exception as e:
                log("state write failed: %s" % e)
        loops += 1
        if max_loops and loops >= max_loops:
            break
        _sleep(5)
    if st.get("relay"):
        relay.stop("interrupted", "job ended, next run resumes" if not _stop["flag"] else "cancelled")
    st.update(status="idle", live=[], heartbeat=0)
    try:
        _write(ST_PATH, st, st_sha, "ytm state final")
    except Exception as e:
        log("final write failed: %s" % e)
    return 0


def cfg_poll(st):
    return max(30, min(300, int(st.get("poll_secs", 60))))


if __name__ == "__main__":
    sys.exit(gate() if (sys.argv[1:] or ["run"])[0] == "gate" else run())
