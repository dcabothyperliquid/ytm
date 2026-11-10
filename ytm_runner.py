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
import math
import os
import re
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
KICK_DOWN_SECS = int(os.environ.get("YTM_KICK_DOWN", "60"))   # ingest pause so YouTube sees a fresh stream start (auto-start)
STALL_SECS = 30           # ffmpeg output time not advancing this long -> source is stuck -> recover/failover
PROBE_RETRIES = 3         # consecutive failed probes (5s apart) before giving up on a source
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

    def recent_ids(self, cid, limit=15):
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



# ── auto caption (title/description of OUR live; needs YT_CLIENT_ID/SECRET/REFRESH_TOKEN) ──
_SPORTS = [("kho kho", "Kho Kho"), ("khokho", "Kho Kho"), ("kabaddi", "Kabaddi"), ("cricket", "Cricket"),
           ("football", "Football"), ("soccer", "Football"), ("volleyball", "Volleyball"), ("hockey", "Hockey"),
           ("basketball", "Basketball"), ("badminton", "Badminton"), ("tennis", "Tennis"),
           ("kushti", "Wrestling"), ("wrestling", "Wrestling"), ("dangal", "Wrestling")]


def detect_sport(*texts):
    t = " ".join(texts).lower()
    for k, name in _SPORTS:
        if k in t:
            return name
    return ""


def _clean_seg(t):
    t = re.sub(r"[^\w\s\-\.,'&()/:]", " ", t or "")          # drop emoji / symbols
    return " ".join(t.split())


def caption_from(src, channel=""):
    """Source title -> (title, description) for OUR live. Sport is detected from the source title/channel, so
    cricket -> cricket, kabaddi -> kabaddi, football -> football. Source channel branding/emoji/dates are dropped.
    'Quarter Final-A vs B || Bakhra Cup Azamgarh' -> 'Quarter Final - A vs B || Bakhra Cup Azamgarh Live'."""
    sport = detect_sport(src, channel)
    fix = lambda t: " ".join(w.title() if (w.isupper() and len(w) > 3 and w.isalpha()) else w for w in t.split())
    if "||" in (src or ""):
        parts = [_clean_seg(p) for p in src.split("||") if _clean_seg(p)]
    else:
        ch = re.sub(r"\W+", "", (channel or "").lower())
        parts = []
        for p in (src or "").split("|"):
            p = _clean_seg(p)
            pn = re.sub(r"\W+", "", p.lower())
            if not p or re.search(r"[^\x00-\x7f]", p) or re.fullmatch(r"[\d\W]+", p) or re.fullmatch(r"live", pn):
                continue                                           # empty / Hindi / date-only / bare "live"
            if (ch and (pn == ch or pn in ch)) or re.fullmatch(r"(?:%s)?(?:haryana|live|official)+" % sport.lower().replace(" ", ""), pn):
                continue                                           # source channel's own branding
            parts.append(p)
        parts = parts[:2]
    main, tour = (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")
    main, tour = fix(main), fix(tour)
    stage, teams = "", main
    m = re.match(r"^([^-]{2,40}?)\s*-\s*(.+)$", main)
    if m:
        stage, teams = m.group(1).strip(), m.group(2).strip()
    title = ("%s - %s" % (stage, teams) if stage else teams)
    sp = sport or "Sports"
    if tour:
        tl = tour if "live" in tour.lower() else tour + " Live"
        cands = [title + " || " + tl, title + " || " + tour]
    else:
        cands = [title if "live" in title.lower() else title + " | Live " + sp, title]
    title = next((c for c in cands if len(c) <= 100), cands[-1])
    desc = [(tour + " - " + stage) if (tour and stage) else (tour or stage or "Live " + sp), teams,
            "Live %s match coverage." % sp.lower(), ""]
    tag = "".join(re.sub(r"[^0-9A-Za-z]", "", w).capitalize() for w in tour.split()[:2])
    desc.append((("#%s " % tag) if tag else "") + "#Live" + sp.replace(" ", ""))
    if len(title) > 100:
        title = title[:100].rsplit(" ", 1)[0]
    return title.strip(), "\n".join(desc)


def _oauth_token():
    cid, sec, rt = (os.environ.get(k, "").strip() for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))
    if not (cid and sec and rt):
        return None
    data = urllib.parse.urlencode({"client_id": cid, "client_secret": sec, "refresh_token": rt,
                                   "grant_type": "refresh_token"}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=data), timeout=20) as r:
        return json.loads(r.read())["access_token"]


DIAG = {}


def _ensure_broadcast(call, src_title, channel):
    """No ACTIVE broadcast on our channel: bind/create one (public, auto-start) so the incoming stream goes live.
    Returns 'wait'; the next caption tick finds the active broadcast and sets the caption."""
    try:
        ls = call("GET", "liveStreams", {"part": "snippet,status", "mine": "true", "maxResults": 5}).get("items", [])
        lb = call("GET", "liveBroadcasts", {"part": "snippet,status,contentDetails", "mine": "true", "maxResults": 10}).get("items", [])
        DIAG["yt"] = {"streams": [(i["snippet"].get("title"), i["status"].get("streamStatus"),
                                   (i["status"].get("healthStatus") or {}).get("status")) for i in ls],
                      "broadcasts": [(i["snippet"].get("title", "")[:40], i["status"].get("lifeCycleStatus")) for i in lb]}
        st = next((i for i in ls if i["status"].get("streamStatus") == "active"), None)
        if not st:
            return "wait"                                   # nothing arriving yet
        bc = next((i for i in lb if i["status"].get("lifeCycleStatus") in ("created", "ready", "testing", "liveStarting")), None)
        if bc is None:
            if time.time() - DIAG.get("bc_at", 0) < 180:
                return "wait"                               # created one a moment ago, give YouTube time
            title, desc = caption_from(src_title, channel)
            now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
            bc = call("POST", "liveBroadcasts", {"part": "snippet,status,contentDetails"}, {
                "snippet": {"title": title, "description": desc, "scheduledStartTime": now},
                "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
                "contentDetails": {"enableAutoStart": True, "enableAutoStop": False, "enableDvr": True,
                                   "monitorStream": {"enableMonitorStream": False}}})
            DIAG["bc_at"] = time.time()
            log("broadcast created: " + title)
            DIAG["yt"]["created"] = title
        if (bc.get("contentDetails") or {}).get("boundStreamId") != st["id"]:
            call("POST", "liveBroadcasts/bind", {"id": bc["id"], "part": "id,contentDetails", "streamId": st["id"]}, {})
        def _err(e):
            try:
                return "%s %s" % (e, e.read().decode("utf8", "ignore")[:300])
            except Exception:
                return str(e)[:300]
        try:
            call("POST", "liveBroadcasts/transition", {"broadcastStatus": "live", "id": bc["id"], "part": "status"}, {})
        except Exception as e:
            DIAG["yt"]["transition"] = _err(e)          # full reason (invalidTransition etc.), not just "403"
            log("transition live failed: " + DIAG["yt"]["transition"])
            try:                                        # fallback: ready -> testing -> live (once per broadcast: 50 units each)
                if bc["id"] in DIAG.setdefault("fb_done", set()):
                    return "wait"
                DIAG["fb_done"].add(bc["id"])
                call("POST", "liveBroadcasts/transition", {"broadcastStatus": "testing", "id": bc["id"], "part": "status"}, {})
                call("POST", "liveBroadcasts/transition", {"broadcastStatus": "live", "id": bc["id"], "part": "status"}, {})
                DIAG["yt"]["transition"] += " | fallback via testing OK"
            except Exception as e2:
                DIAG["yt"]["transition2"] = _err(e2)
    except Exception as e:
        DIAG["yt"] = "ensure error: %s" % str(e)[:200]
    return "wait"


def _prepare(call, src_title, channel):
    """Make sure a bound broadcast exists BEFORE ffmpeg starts sending: its enableAutoStart then fires on the fresh ingest.
    (Bound while the stream is already flowing, auto-start never fires and a manual transition gives invalidTransition.)
    Returns 'fresh' (stream idle, auto-start will fire), 'busy' (stream already active: may need a kick) or 'nostream'."""
    ls = call("GET", "liveStreams", {"part": "snippet,status", "mine": "true", "maxResults": 5}).get("items", [])
    if not ls:
        return "nostream"
    st = next((i for i in ls if i["status"].get("streamStatus") == "active"), ls[0])
    lb = call("GET", "liveBroadcasts", {"part": "snippet,status,contentDetails", "mine": "true", "maxResults": 10}).get("items", [])
    bc = next((i for i in lb if i["status"].get("lifeCycleStatus") in ("created", "ready", "testing", "liveStarting", "live")), None)
    changed = False
    if bc is None:
        title, desc = caption_from(src_title, channel)
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        bc = call("POST", "liveBroadcasts", {"part": "snippet,status,contentDetails"}, {
            "snippet": {"title": title, "description": desc, "scheduledStartTime": now},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
            "contentDetails": {"enableAutoStart": True, "enableAutoStop": False, "enableDvr": True,
                               "monitorStream": {"enableMonitorStream": False}}})
        DIAG["bc_at"] = time.time()
        log("broadcast pre-created: " + title)
        changed = True
    if (bc.get("contentDetails") or {}).get("boundStreamId") != st["id"]:
        call("POST", "liveBroadcasts/bind", {"id": bc["id"], "part": "id,contentDetails", "streamId": st["id"]}, {})
        changed = True
    return "busy" if (changed and st["status"].get("streamStatus") == "active") else "fresh"


def prepare_broadcast(src_title, channel=""):
    tok = _oauth_token()
    if not tok:
        return "skip"
    H = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}

    def call(method, path, params, body=None):
        url = _API + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers=H, method=method)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    return _prepare(call, src_title, channel)


def set_caption(src_title, channel=""):
    """Returns 'done', 'wait' (our broadcast not active yet) or 'skip' (no OAuth secrets)."""
    tok = _oauth_token()
    if not tok:
        return "skip"
    H = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}

    def call(method, path, params, body=None):
        url = _API + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers=H, method=method)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")

    items = call("GET", "liveBroadcasts", {"part": "snippet,status", "broadcastStatus": "active", "maxResults": 5}).get("items", [])
    if not items:
        return _ensure_broadcast(call, src_title, channel)
    it = items[0]
    title, desc = caption_from(src_title, channel)
    sn = {"title": title, "description": desc, "scheduledStartTime": it["snippet"]["scheduledStartTime"]}
    call("PUT", "liveBroadcasts", {"part": "snippet"}, {"id": it["id"], "snippet": sn})
    log("caption set: " + title)
    return "done"


def end_broadcast():
    """Relay stopped: move our ACTIVE broadcast to 'complete' so the channel stops showing LIVE.
    (Broadcasts are created with enableAutoStop=False, so killing ffmpeg alone leaves them live.)"""
    tok = _oauth_token()
    if not tok:
        return "skip"
    H = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}

    def call(method, path, params):
        url = _API + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=b"{}" if method == "POST" else None, headers=H, method=method)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")

    n = 0
    try:
        items = call("GET", "liveBroadcasts", {"part": "id,status", "broadcastStatus": "active", "maxResults": 10}).get("items", [])
        for it in items:
            try:
                call("POST", "liveBroadcasts/transition", {"broadcastStatus": "complete", "id": it["id"], "part": "status"})
                n += 1
            except Exception as e:
                log("end broadcast %s: %s" % (it["id"], str(e)[:100]))
    except Exception as e:
        log("end_broadcast error: %s" % str(e)[:120])
        return "error"
    log("broadcast(s) completed: %d" % n)
    return "done"


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


def _tag_geom():
    cx, cy = (os.environ.get("YTM_TAG_POS", "646:588").split(":") + ["588"])[:2]
    bw, bh = (os.environ.get("YTM_TAG_BOX", "250:64").split(":") + ["64"])[:2]
    return int(cx), int(cy), int(bw), int(bh)


def mark_present(hls):
    """Is a static watermark sitting where our LML strip goes? Grabs 4 frames (2 s apart) of that spot from the source:
    a watermark = high contrast (text) that stays the same between frames. Plain ground / moving players / camera pans
    do not qualify. True/False, or None when the probe failed (caller then keeps the strip = old behaviour).
    Tunables: YTM_MARK_STD (min contrast, default 12), YTM_MARK_DIFF (max frame-to-frame change, default 4)."""
    try:
        cx, cy, bw, bh = _tag_geom()
        x, y = cx - bw // 2, cy - bh // 2
        vf = ("fps=0.5,scale=1280:720:flags=bilinear:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
              "crop=%d:%d:%d:%d,format=gray" % (bw, bh, x, y))
        p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", hls, "-an", "-vf", vf,
                            "-frames:v", "4", "-f", "rawvideo", "-"], capture_output=True, timeout=45)
        n = bw * bh
        frames = [p.stdout[i * n:(i + 1) * n] for i in range(len(p.stdout) // n)]
        if len(frames) < 3:
            log("mark probe: only %d frame(s) -> keeping strip" % len(frames))
            return None
        stds = []
        for f in frames:
            m = sum(f) / n
            stds.append((sum((v - m) ** 2 for v in f) / n) ** 0.5)
        diffs = [sum(abs(a - b) for a, b in zip(frames[i], frames[i + 1])) / n for i in range(len(frames) - 1)]
        std, diff = sum(stds) / len(stds), max(diffs)
        ok = std >= float(os.environ.get("YTM_MARK_STD", "12")) and diff <= float(os.environ.get("YTM_MARK_DIFF", "4"))
        log("mark probe: contrast=%.1f change=%.1f -> %s" % (std, diff, "watermark -> LML strip" if ok else "no watermark -> no strip"))
        return ok
    except Exception as e:
        log("mark probe failed (%s) -> keeping strip" % str(e)[:80])
        return None


LOGO_PROBE_VER = 2          # bump when sizing logic changes: cached per-match specs from older logic are re-probed


def logo_probe(hls):
    """Find the SOURCE channel's static logo in the top-right of the frame and decide what OUR logo should look like.
    Grabs 5 frames (1.5 s apart) of the top-right region (1280x720 frame coords), keeps pixels that are sharp-edged AND
    unchanged between frames (= a static overlay, not moving players), groups them into blobs and takes the biggest.
    A locked camera also has static background edges, so it tries strict levels first (only very sharp / dense edges =
    a logo) and relaxes step by step; a blob that touches the left/bottom border of the region is background, not a logo.
    -> dict(shape='circle'|'rect', w=px, x=px, y=px)  or None (nothing reliable -> caller uses the default circle).
    Wide blob (aspect >= 1.5) -> rectangular logo, otherwise circular. Size follows the blob, clamped.
    Tunables: YTM_LOGO_STATIC (max change between frames, 12)."""
    X0, Y0, CW, CH, CELL = 904, 0, 376, 200, 8
    gw, gh = CW // CELL, CH // CELL
    try:
        vf = ("fps=0.67,scale=1280:720:flags=bilinear:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
              "crop=%d:%d:%d:%d,format=gray" % (CW, CH, X0, Y0))
        p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", hls, "-an", "-vf", vf,
                            "-frames:v", "5", "-f", "rawvideo", "-"], capture_output=True, timeout=45)
        n = CW * CH
        fr = [p.stdout[i * n:(i + 1) * n] for i in range(len(p.stdout) // n)]
        if len(fr) < 4:
            log("logo probe: only %d frame(s) -> default logo" % len(fr))
            return None
        k = len(fr)
        mean = [sum(f[i] for f in fr) / k for i in range(n)]
        stat_t = float(os.environ.get("YTM_LOGO_STATIC", "12"))
        es = [0.0] * n                                   # edge strength, only where the pixel is static
        for y in range(CH - 1):
            row = y * CW
            for x in range(CW - 1):
                i = row + x
                e = abs(mean[i + 1] - mean[i]) + abs(mean[i + CW] - mean[i])
                if e >= 24 and max(abs(f[i] - mean[i]) for f in fr) <= stat_t:
                    es[i] = e

        def blob(edge_t, min_cnt):
            cnt = [0] * (gw * gh)
            for i in range(n):
                if es[i] >= edge_t:
                    cnt[((i // CW) // CELL) * gw + min((i % CW) // CELL, gw - 1)] += 1
            act = {c for c, v in enumerate(cnt) if v >= min_cnt}
            best, seen = None, set()
            for c0 in act:                               # 8-connected blobs of active cells
                if c0 in seen:
                    continue
                comp, stack = [], [c0]
                seen.add(c0)
                while stack:
                    c = stack.pop()
                    comp.append(c)
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            nx, ny = c % gw + dx, c // gw + dy
                            nc = ny * gw + nx
                            if 0 <= nx < gw and 0 <= ny < gh and nc in act and nc not in seen:
                                seen.add(nc)
                                stack.append(nc)
                tot = sum(cnt[c] for c in comp)
                if len(comp) >= 6 and (best is None or tot > best[0]):
                    best = (tot, comp)
            return best

        pick = None
        for edge_t, min_cnt in ((90, 12), (60, 12), (40, 10), (24, 8)):
            best = blob(edge_t, min_cnt)
            if not best:
                continue
            xs, ys = [c % gw for c in best[1]], [c // gw for c in best[1]]
            bw, bh = (max(xs) - min(xs) + 1) * CELL, (max(ys) - min(ys) + 1) * CELL
            if min(xs) == 0 or max(ys) == gh - 1 or bw > 340 or bh > 190 or bw < 32 or bh < 24:
                continue                                 # touches the region border / absurd size = background, try next level
            pick = (best, xs, ys, bw, bh)
            break
        if not pick:
            log("logo probe: no reliable static logo -> default logo")
            return None
        best, xs, ys, bw, bh = pick
        bx, by = X0 + min(xs) * CELL, Y0 + min(ys) * CELL
        fill = len(best[1]) / float((max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1))
        bx, by, bw, bh = bx - CELL, max(0, by - CELL), bw + 2 * CELL, bh + 2 * CELL     # +1 cell margin: glyph-style logos are only partly caught
        cx, cy = bx + bw / 2.0, by + bh / 2.0
        if os.environ.get("YTM_LOGO_RECT", "off").lower() == "on" and bw / float(bh) >= 1.25:
            shape, w = "rect", int(min(280, max(150, bw, bh * 2.5)))     # optional, off by default (was too big)
            h = int(w / 2.5)
        else:
            shape = "circle"                              # always our round logo, sized so the circle covers the source box
            if bw / float(bh) >= 1.25 or fill >= 0.92:
                d = math.hypot(bw, bh)                    # wide / square source: circle through the box corners covers it all
            else:
                d = max(bw, bh) * 1.08 + 4                # round-ish source: small safety margin (no tiny edge peeking)
            w = int(min(190, max(90, d)))
            h = w
        x = int(min(1280 - w - 8, max(8, cx - w / 2.0)))
        y = int(min(720 - h - 8, max(8, cy - h / 2.0)))
        spec = {"shape": shape, "w": w, "x": x, "y": y}
        log("logo probe: source logo %dx%d @%d,%d fill=%.2f -> our %s logo %dpx @%d,%d" % (bw, bh, bx, by, fill, shape, w, x, y))
        return spec
    except Exception as e:
        log("logo probe failed (%s) -> default logo" % str(e)[:80])
        return None


def ffmpeg_cmd(hls, rtmp, strip=True, logo_spec=None):
    base = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5", "-fflags", "+genpts", "-i", hls]
    logo = os.environ.get("YTM_LOGO", "logo.png")
    if logo_spec and logo_spec.get("shape") == "rect" and os.path.isfile("logo_rect.png"):
        logo = "logo_rect.png"
    if not os.path.isfile(logo):
        return base + ["-c", "copy", "-f", "flv", rtmp]          # no logo file -> old behaviour (stream copy)
    # logo mode: re-encode. Frame is forced to 1280x720, source-channel logo corners are smeared (delogo),
    # then YOUR logo goes top-right on top. YTM_DELOGO="x:y:w:h;x:y:w:h" overrides the boxes, YTM_DELOGO=off disables.
    # YTM_LOGO_POS = overlay expr (default top-right), YTM_LOGO_W = logo width px (default 150).
    boxes = os.environ.get("YTM_DELOGO", "").strip()
    if not boxes:
        boxes = "off"   # no smear box by default: it showed as an ugly blur patch beside our logo. Override e.g. YTM_DELOGO="1090:10:180:190" if a source logo peeks out.
    pos = os.environ.get("YTM_LOGO_POS", "W-w-18:16")
    lw = os.environ.get("YTM_LOGO_W", "120")
    chain = "[0:v]fps=30,scale=1280:720:flags=lanczos:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if boxes.lower() != "off":
        for bx in boxes.split(";"):
            if bx.count(":") == 3:
                chain += ",delogo=x=%s:y=%s:w=%s:h=%s" % tuple(bx.split(":"))
    # centre-bottom "ACL" mark (permission-holder OK'd removal): covered by a solid branded strip with our tag (no blur smear).
    # YTM_TAG="" / "off" -> strip off and the mark is delogo-smeared instead. YTM_TAG_POS="cx:cy", YTM_TAG_BOX="w:h" (1280x720 frame).
    tag = os.environ.get("YTM_TAG", "LML").strip() if strip else "off"
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    cx, cy = (os.environ.get("YTM_TAG_POS", "646:588").split(":") + ["588"])[:2]
    bw, bh = (os.environ.get("YTM_TAG_BOX", "250:64").split(":") + ["64"])[:2]
    tagimg = os.environ.get("YTM_TAG_IMG", "lml_tag.png").strip()
    use_img = bool(strip and tagimg and tagimg.lower() != "off" and os.path.isfile(tagimg)
                   and all(re.fullmatch(r"\d{1,4}", v) for v in (cx, cy, bw, bh)))
    if use_img:                                                   # branded tag PNG (opaque, hides the source watermark)
        chain += "[b0];[2:v]scale=%s:%s,format=rgba[tg];[b0][tg]overlay=%d:%d" % (bw, bh, int(cx) - int(bw) // 2, int(cy) - int(bh) // 2)
    elif tag and tag.lower() != "off" and os.path.isfile(font) and re.fullmatch(r"[A-Za-z0-9 @._-]{1,24}", tag) \
            and all(re.fullmatch(r"\d{1,4}", v) for v in (cx, cy, bw, bh)):
        chain += (",drawbox=x=%s-%s/2:y=%s-%s/2:w=%s:h=%s:color=0x0b3d91:t=fill"
                  ",drawtext=fontfile=%s:text='%s':fontcolor=white:fontsize=28:x=%s-text_w/2:y=%s-text_h/2:"
                  "shadowcolor=black@0.6:shadowx=2:shadowy=2" % (cx, bw, cy, bh, bw, bh, font, tag, cx, cy))
    elif strip and boxes.lower() != "off":
        chain += ",delogo=x=521:y=556:w=250:h=64"
    if logo_spec:                                                 # auto-sized to the detected source logo
        lw, pos = str(int(logo_spec["w"])), "%d:%d" % (int(logo_spec["x"]), int(logo_spec["y"]))
    fc = chain + "[bg];[1:v]scale=%s:-1[lg];[bg][lg]overlay=%s,split=2[v][s];[s]fps=1/20,scale=640:-1[snap]" % (lw, pos)
    return (base + ["-i", logo] + (["-i", tagimg] if use_img else []) + ["-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", os.environ.get("YTM_PRESET", "veryfast"), "-profile:v", "high", "-crf", "20", "-maxrate", "6000k", "-bufsize", "12000k",
            "-g", "60", "-pix_fmt", "yuv420p", "-c:a", "copy", "-f", "flv", rtmp,
            "-map", "[snap]", "-q:v", "5", "-f", "image2", "-update", "1", "-y", "/tmp/ytm_frame.jpg"])


class Relay:
    def __init__(self, state, rtmp, _probe=probe, _popen=subprocess.Popen):
        self.st, self.rtmp, self._probe, self._popen = state, rtmp, _probe, _popen
        self.proc, self.restarts = None, 0

    def _spawn(self, hls):
        self._elog = open("/tmp/ffmpeg.log", "wb")
        self._last_t, self._last_t_at = None, time.time()
        strip = True
        if os.environ.get("YTM_MARK_DETECT", "off").lower() != "off":
            r = mark_present(hls)
            strip = True if r is None else r
            self.st["mark_probe"] = {"watermark": r, "strip": strip, "at": int(time.time())}
        spec = None
        if os.environ.get("YTM_LOGO_AUTO", "on").lower() != "off":
            vid = (self.st.get("relay") or {}).get("video_id") or getattr(self, "_cur_vid", None)
            lp = self.st.get("logo_probe") or {}
            if vid and lp.get("video_id") == vid and lp.get("ver") == LOGO_PROBE_VER and "spec" in lp:     # same match, restart/chain -> reuse, no extra delay
                spec = lp["spec"]
            else:
                spec = logo_probe(hls)
                self.st["logo_probe"] = {"video_id": vid, "spec": spec, "ver": LOGO_PROBE_VER, "at": int(time.time())}
        return self._popen(ffmpeg_cmd(hls, self.rtmp, strip, spec), stderr=self._elog)

    def _tail(self):
        """last ffmpeg output (stream key masked) -> state, so it is visible without Actions logs"""
        try:
            with open("/tmp/ffmpeg.log", "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 600))
                t = f.read().decode("utf8", "replace").replace("\r", "\n")
            self.st["ffmpeg_tail"] = re.sub(r"rtmps?://\S+", "rtmp://***", t)[-350:]
        except Exception:
            pass
        now = time.time()
        if now - getattr(self, "_snap_at", 0) > 120 and os.path.isfile("/tmp/ytm_frame.jpg"):
            self._snap_at = now                    # what viewers really see -> ytm_frame.jpg on the data branch
            try:
                sha = None
                try:
                    sha = gh("GET", "contents/ytm_frame.jpg?ref=%s" % BRANCH).get("sha")
                except urllib.error.HTTPError:
                    pass
                body = {"message": "frame", "branch": BRANCH,
                        "content": base64.b64encode(open("/tmp/ytm_frame.jpg", "rb").read()).decode()}
                if sha:
                    body["sha"] = sha
                gh("PUT", "contents/ytm_frame.jpg", body)
            except Exception as e:
                log("frame upload: %s" % str(e)[:80])

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
        try:
            if outcome in ("stopped", "ended"):      # not for "interrupted" (job chain resumes) / failover
                end_broadcast()
        except Exception as e:
            log("end_broadcast: %s" % str(e)[:100])
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
        prep = None
        try:
            prep = prepare_broadcast(c["title"], c["channel"])
        except Exception as e:
            log("prepare broadcast failed: %s" % str(e)[:120])
        self._cur_vid = c["video_id"]
        self.proc = self._spawn(hls)
        self.st["relay"] = {"video_id": c["video_id"], "title": c["title"], "channel": c["channel"],
                            "url": c["url"], "started": int(time.time()), "not_live_since": None}
        if prep == "busy":
            self.st["relay"]["kick_early"] = True

    def _record_fail(self, c, note):
        self.st.setdefault("history", []).insert(0, {
            "video_id": c["video_id"], "title": c["title"], "channel": c["channel"], "started": None,
            "ended": int(time.time()), "outcome": "failed", "note": note[:120]})

    @staticmethod
    def _tok(title):
        segs = [x for x in (_clean_seg(p) for p in re.split(r"\|+", title or "")) if x and not re.search(r"[^\x00-\x7f]", x)]
        seg = segs[0] if segs else (title or "")
        return set(re.findall(r"[a-z0-9]+", seg.lower())) - {"live", "match", "vs", "v", "the", "of"}

    def _pick_alt(self, cur, now):
        """Best replacement source: same match (similar first title segment) first, then others in channel-priority order."""
        ct = self._tok(cur.get("title"))
        cands = [c for c in getattr(self, "live_last", []) if c["video_id"] != cur.get("video_id") and not self._blocked(c["video_id"], now)]
        def sim(c):
            t = self._tok(c["title"])
            return len(ct & t) / float(len(ct | t) or 1)
        same = sorted([c for c in cands if sim(c) >= 0.6], key=lambda c: -sim(c))
        return same + [c for c in cands if c not in same]

    def _failover(self, outcome, note):
        """Source broke/ended: close it and IMMEDIATELY start the next best live (same match elsewhere, else next in priority)."""
        r = dict(self.st.get("relay") or {})
        now = time.time()
        self._finish(outcome, note)
        for c in ([] if getattr(self, "manual", False) else self._pick_alt(r, now)):
            self.start(c)
            if self.st.get("relay"):
                self.st["relay"]["failover_from"] = r.get("video_id")
                log("failover -> " + c["title"][:70])
                return True
        return False

    def _stalled(self):
        """ffmpeg alive but its output time stopped advancing (source frozen): kill it so recovery kicks in."""
        try:
            with open("/tmp/ffmpeg.log", "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 800))
                m = re.findall(r"time=(\d+):(\d+):([\d.]+)", f.read().decode("utf8", "replace"))
        except Exception:
            return False
        t = (int(m[-1][0]) * 3600 + int(m[-1][1]) * 60 + float(m[-1][2])) if m else None
        if t != getattr(self, "_last_t", None):
            self._last_t, self._last_t_at = t, time.time()
            return False
        return time.time() - getattr(self, "_last_t_at", time.time()) > STALL_SECS

    def poll(self):
        """Call every few seconds: detect ffmpeg exit / stall, recover the same source fast, else fail over."""
        r = self.st.get("relay")
        if r and self.proc:
            self._tail()
            if DIAG.get("yt"):
                self.st["yt_diag"] = DIAG["yt"]
            if self.proc.poll() is None and self._stalled():
                log("ffmpeg stalled > %ds -> kill" % STALL_SECS)
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except Exception:
                    self.proc.kill()
                r["stalled"] = r.get("stalled", 0) + 1
        if not r:
            return
        now = time.time()
        if self.proc is not None:
            if self.proc.poll() is None:
                return
            log("ffmpeg exit %s" % self.proc.poll())
            self.proc = None
            r["retry_at"], r["probe_fail"] = 0, 0
        elif now < r.get("retry_at", 0):
            return                                           # waiting for the next quick probe retry
        try:
            is_live, hls = self._probe(r["url"])
        except Exception as e:
            is_live, hls = None, None
            log("probe error: %s" % str(e)[:100])
        if is_live and hls and self.restarts < MAX_RESTARTS:
            self.restarts += 1
            log("source still live -> restart ffmpeg (%d)" % self.restarts)
            self.proc = self._spawn(hls)
        elif is_live is False:
            if not self._failover("ended", "source ended"):
                pass
        elif r.get("probe_fail", 0) + 1 < PROBE_RETRIES and is_live is None:
            r["probe_fail"] = r.get("probe_fail", 0) + 1
            r["retry_at"] = now + 5
        else:
            self._failover("failed", "ffmpeg/probe failed")

    def caption_tick(self, now, _set=None):
        """Retry every ~25s (max 12x) until our broadcast is active, then set title/description once."""
        r = self.st.get("relay")
        if not r or r.get("caption_done") or now - r.get("caption_try", 0) < (25 if r.get("caption_n", 0) < 12 else 600):
            return
        r["caption_try"] = now
        r["caption_n"] = r.get("caption_n", 0) + 1
        try:
            res = (_set or set_caption)(r.get("title", ""), r.get("channel", ""))
        except Exception as e:
            log("caption error: %s" % str(e)[:120])
            res = "wait"
        if res in ("done", "skip"):                     # never give up while broadcast is not live (retry slower after 12)
            r["caption_done"] = True
        elif not r.get("kicked") and r["caption_n"] >= (2 if r.get("kick_early") else 4):
            yt = DIAG.get("yt")
            bcs = yt.get("broadcasts", []) if isinstance(yt, dict) else []
            if any(b[1] in ("ready", "testing") for b in bcs):   # still not live although a broadcast is waiting
                self._kick_ingest(r)

    def _kick_ingest(self, r):
        """Stuck on 'ready' (stream was flowing when the broadcast got bound): pause ingest so YouTube marks the stream idle,
        then the normal restart path reconnects = a fresh stream start, which fires the bound broadcast's auto-start."""
        r["kicked"] = True
        log("broadcast stuck on ready -> pausing ingest %ds to re-trigger auto-start" % KICK_DOWN_SECS)
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except Exception:
                self.proc.kill()
        self.proc = None
        r["retry_at"], r["probe_fail"] = time.time() + KICK_DOWN_SECS, 0

    def stop(self, outcome="stopped", note="manual"):
        if self.st.get("relay"):
            self._finish(outcome, note)

    # -- decisions on each detection
    def _blocked(self, vid, now):
        h = self.st.get("history", [])
        fails = [x for x in h if x["video_id"] == vid and x["outcome"] == "failed"]
        if len(fails) >= MAX_ATTEMPTS or (fails and now - fails[0]["ended"] < RETRY_COOLDOWN):
            return True
        for x in h[:10]:                                  # newest entry for this video decides:
            if x["video_id"] == vid and x["outcome"] in ("ended", "stopped", "interrupted"):
                return x["outcome"] != "interrupted"      # job-chain interrupt = resume, even if an older manual stop exists
        return False

    def step(self, live, cfg, now):
        self.live_last = live
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
        if getattr(self, "manual", False):                # manual mode: only the dashboard "Mirror ab" starts a relay ...
            h = (self.st.get("history") or [None])[0]     # ... except resuming a match cut by the 6h job chain
            if h and h.get("note") == "job chained" and now - (h.get("ended") or 0) < 300:
                c = next((x for x in live if x["video_id"] == h.get("video_id")), None)
                if c:
                    self.start(c)
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
# ── self-chaining (GitHub cron is best-effort and can silently skip runs) ─
WORKFLOW_FILE = "yt-mirror.yml"


def ensure_next_queued():
    """Keep one run waiting behind this one (concurrency group 'yt-mirror'), so the chain never
    depends on the */15 cron firing. workflow_dispatch with GITHUB_TOKEN is allowed to trigger runs."""
    try:
        for stt in ("queued", "pending", "waiting"):
            r = gh("GET", "actions/workflows/%s/runs?status=%s&per_page=5" % (WORKFLOW_FILE, stt))
            if r.get("workflow_runs"):
                return False                                   # something already waiting
        gh("POST", "actions/workflows/%s/dispatches" % WORKFLOW_FILE, {"ref": "main"})
        log("next run queued (self-chain)")
        return True
    except Exception as e:
        log("self-chain failed: %s" % e)
        return False


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


def run(_sleep=time.sleep, _read=None, _write=None, _relay_factory=None, _detector_factory=None, max_loops=None, _chain=None):
    _chain = bool(os.environ.get("GITHUB_ACTIONS")) if _chain is None else _chain
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
    last_cfg = last_detect = last_write = last_chain = 0
    last_sig, live, errors, cfg, loops = None, [], [], {}, 0
    while time.time() < deadline and not _stop["flag"]:
        now = time.time()
        if _chain and now - last_chain >= 1800:               # keep a successor queued (cron may skip)
            last_chain = now
            ensure_next_queued()
        relay.poll()
        if hasattr(relay, "caption_tick"):
            relay.caption_tick(now)
        if now - last_cfg >= CFG_EVERY:                       # config/commands: cheap, every ~15s
            cfg = _read(CFG_PATH)[0] or {}
            last_cfg = now
            if not (cfg.get("enabled") and cfg.get("channels") and in_window(cfg)):
                log("disabled / outside window / no channels -> stop")
                relay.stop("stopped", "mirror off")
                break
            st["poll_secs"] = int(cfg.get("poll_secs", 60))
            relay.manual = (cfg.get("mode") == "manual")
            st["mode"] = "manual" if relay.manual else "auto"
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
    _cmd = (sys.argv[1:] or ["run"])[0]
    if _cmd == "endlive":                       # one-off: complete any ACTIVE broadcast on our channel
        print("endlive:", end_broadcast())
        sys.exit(0)
    sys.exit(gate() if _cmd == "gate" else run())
