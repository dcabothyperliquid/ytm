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
ST2_PATH = "ytm_state2.json"   # relay 2 (second parallel relay on the same channel, manual only)
ST3_PATH = "ytm_state3.json"   # relay 3 (third parallel relay, same rules as relay 2)
SLOT = os.environ.get("YTM_SLOT", "1")                       # "2"/"3" = manual relay job: own stream key, own state, never touches relay 1's broadcast
FRAME_FILE = "ytm_frame.jpg" if SLOT == "1" else "ytm_frame%s.jpg" % SLOT
STREAM2_TITLE = "YTM Relay 2"
MANUAL_SLOTS = ("2", "3")


def _sfx(slot=None):
    """env-var suffix of a slot: '' for relay 1, '_2' / '_3' for the manual relays"""
    slot = slot or SLOT
    return "" if slot == "1" else "_" + slot


def _st_path(slot):
    return {"1": ST_PATH, "2": ST2_PATH, "3": ST3_PATH}[slot]


def _wf_file(slot):
    return "yt-mirror%s.yml" % ("" if slot == "1" else slot)
MAX_RUN_MIN = int(os.environ.get("MAX_RUN_MIN", "350"))
ENDED_GRACE = 90          # s the source may be missing from the live list before we stop
RETRY_COOLDOWN = 120
MAX_ATTEMPTS = 3
FAIL_WINDOW = 1800         # s: only failures this recent count toward MAX_ATTEMPTS (old ones used to block a video forever)
MAX_RESTARTS = 8
RETRY_WINDOW = int(os.environ.get("YTM_RETRY_WINDOW", "900"))   # s a dropped source is retried before we give up and take another match
RUN_OK_SECS = 120         # ffmpeg that ran this long counts as healthy (drop timer resets)
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

    def streams_live_ids(self, cid, ttl=240):
        """Live video ids from the channel's /streams tab via yt-dlp (no API quota). YouTube sometimes leaves a
        running live out of both RSS and the uploads playlist; this catches it. Candidates only: live() still
        verifies them through videos.list. Cached per channel for `ttl` s (yt-dlp is slow)."""
        cache = self.__dict__.setdefault("_sc", {})
        now = time.time()
        if cid in cache and now - cache[cid][0] < ttl:
            return cache[cid][1]
        ids = []
        try:
            r = subprocess.run(["yt-dlp", "--flat-playlist", "-J", "--playlist-end", "6",
                                "https://www.youtube.com/channel/%s/streams" % cid],
                               capture_output=True, text=True, timeout=60)
            ids = [e["id"] for e in json.loads(r.stdout or "{}").get("entries", []) if e.get("live_status") == "is_live" and e.get("id")]
        except Exception as e:
            log("streams tab fail %s: %s" % (cid, str(e)[:80]))
        cache[cid] = (now, ids)
        return ids

    def is_live(self, vid):
        """True/False from videos.list for ONE id (1 unit); None if unknown (API error) -> caller must not stop."""
        try:
            it = (self._api("videos", {"part": "snippet", "id": vid}).get("items") or [None])[0]
        except Exception as e:
            log("verify fail %s: %s" % (vid, str(e)[:80]))
            return None
        return bool(it) and it["snippet"].get("liveBroadcastContent") == "live"

    def live(self, cfg):
        kws = [k.strip().lower() for k in (cfg.get("keywords") or "").split(",") if k.strip()]
        per, errs = [], []
        for c in cfg.get("channels", []):
            try:
                cid = self.channel_id(c)
                l = self.recent_ids(cid)
                l = l + [v for v in self.streams_live_ids(cid) if v not in l]
                per.append((c, cid, l))
            except Exception as e:
                errs.append("%s: %s" % (c.get("name") or c["value"], str(e)[:80]))
        rel = self.st.get("relay") or {}                  # always re-check the video we are relaying, even if feeds dropped it
        if rel.get("video_id") and per and not any(rel["video_id"] in l for _, _, l in per):
            tgt = next((x for x in per if (x[0].get("name") or x[0]["value"]) == rel.get("channel")), per[0])
            tgt[2].append(rel["video_id"])
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
                try:
                    import ytm_style
                    ytm_style.SRC_DESC[t[:120]] = (it["snippet"].get("description") or "")[:1500]
                except Exception:
                    pass
                if kws and not any(k in t.lower() for k in kws):
                    continue
                out.append({"video_id": v, "title": t[:120], "channel": c.get("name") or c["value"],
                            "handle": c["value"], "channel_id": cid,
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


def _caption_plain(src, channel=""):
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


def caption_from(src, channel=""):
    """(title, description). Pro style title (emoji + stage + tournament + teams + brand) from ytm_style;
    YTM_STYLE=off or any error -> the old plain title."""
    title, desc = _caption_plain(src, channel)
    if os.environ.get("YTM_STYLE", "on").lower() != "off":
        try:
            import ytm_style
            title = ytm_style.live_title(src, channel) or title
            desc = ytm_style.live_desc(src, channel) or desc
        except Exception as e:
            log("style title failed (%s) -> plain title" % str(e)[:80])
    return title, desc


def _set_thumb(tok, vid, src_title, channel):
    """Banner (stream frame + stage + tournament + teams + LIVE ON strip) -> YouTube thumbnail. 50 quota units.
    Needs a phone-verified channel; any failure is only logged (never stops the relay). Returns True when set."""
    if os.environ.get("YTM_STYLE", "on").lower() == "off" or os.environ.get("YTM_BANNER", "on").lower() == "off":
        return True
    try:
        import ytm_style
        frame = "/tmp/ytm_frame.jpg"
        out = "/tmp/ytm_banner%s.jpg" % SLOT
        ytm_style.make_banner(frame if os.path.isfile(frame) else None, src_title, out, channel)
        req = urllib.request.Request("https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId=%s&uploadType=media" % vid,
                                     data=open(out, "rb").read(), method="POST",
                                     headers={"Authorization": "Bearer " + tok, "Content-Type": "image/jpeg"})
        urllib.request.urlopen(req, timeout=40).read()
        log("thumbnail set for " + vid)
        return True
    except urllib.error.HTTPError as e:
        log("thumbnail failed: %s (custom thumbnails need a phone-verified channel)" % _errtxt(e)[:160])
    except Exception as e:
        log("thumbnail failed: %s" % str(e)[:120])
    return False


def _errtxt(e):
    """short, readable reason incl. the YouTube error body (reason/message), for state + logs"""
    try:
        body = e.read().decode("utf8", "ignore") if hasattr(e, "read") else ""
        j = json.loads(body) if body.strip().startswith("{") else {}
        er = (j.get("error") or {}) if isinstance(j, dict) else {}
        why = ",".join(x.get("reason", "") for x in (er.get("errors") or [])[:2]) or er.get("status", "")
        return ("%s %s %s" % (getattr(e, "code", ""), why, (er.get("message") or body)[:160])).strip()
    except Exception:
        return str(e)[:200]


AUTH = {"dead": False}
AUTH_MSG = ("YouTube login expire ho gaya (invalid_grant) - GitHub secret YT_REFRESH_TOKEN naya banao. "
            "Tab tak broadcast title/end-live kaam nahi karega aur naya match start nahi hoga (purane broadcast me mix na ho).")


def _oauth_token():
    cid, sec, rt = (os.environ.get(k, "").strip() for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))
    if not (cid and sec and rt):
        return None
    data = urllib.parse.urlencode({"client_id": cid, "client_secret": sec, "refresh_token": rt,
                                   "grant_type": "refresh_token"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=data), timeout=20) as r:
            tok = json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf8", "ignore")
        except Exception:
            body = ""
        if e.code in (400, 401) and "invalid_grant" in body:
            if not AUTH["dead"]:
                log("YouTube OAuth: invalid_grant (refresh token expired/revoked)")
            AUTH["dead"] = True                      # dashboard shows AUTH_MSG; new matches are held back (see Relay._clear_stale)
        raise
    AUTH["dead"] = False
    return tok


DIAG = {}


def _slot_key():
    return os.environ.get("YT_STREAM_KEY" + _sfx(), "").strip()


def _my_stream(items):
    """The liveStream that carries THIS slot's key, so two relays on one channel never touch each other's broadcast.
    None = not identifiable (relay 1 then falls back to the old single-stream behaviour; relay 2 refuses to guess)."""
    key = _slot_key()
    if not key:
        return None
    for i in items:
        if ((i.get("cdn") or {}).get("ingestionInfo") or {}).get("streamName") == key:
            return i
    return None


def _my_stream_id(call):
    if DIAG.get("sid"):
        return DIAG["sid"]
    try:
        ls = call("GET", "liveStreams", {"part": "snippet,status,cdn", "mine": "true", "maxResults": 50}).get("items", [])
    except Exception as e:
        log("my stream lookup: %s" % str(e)[:100])
        return None
    m = _my_stream(ls)
    if m:
        DIAG["sid"] = m["id"]
    return DIAG.get("sid")


def _mine_only(items, sid):
    """keep only broadcasts bound to our stream (or not bound yet)"""
    if not sid:
        return items
    return [i for i in items if (i.get("contentDetails") or {}).get("boundStreamId") in (None, sid)]


def _ensure_broadcast(call, src_title, channel):
    """No ACTIVE broadcast on our channel: bind/create one (public, auto-start) so the incoming stream goes live.
    Returns 'wait'; the next caption tick finds the active broadcast and sets the caption."""
    try:
        ls = call("GET", "liveStreams", {"part": "snippet,status,cdn", "mine": "true", "maxResults": 50}).get("items", [])
        lb = call("GET", "liveBroadcasts", {"part": "snippet,status,contentDetails", "mine": "true", "maxResults": 25}).get("items", [])
        mine = _my_stream(ls)
        if SLOT != "1" and not mine:
            return "wait"                                   # never guess on relay 2: it could hijack relay 1's broadcast
        if mine:
            DIAG["sid"] = mine["id"]
            lb = _mine_only(lb, mine["id"])
        DIAG["yt"] = {"streams": [(i["snippet"].get("title"), i["status"].get("streamStatus"),
                                   (i["status"].get("healthStatus") or {}).get("status")) for i in ls],
                      "broadcasts": [(i["snippet"].get("title", "")[:40], i["status"].get("lifeCycleStatus")) for i in lb]}
        if mine:
            st = mine if mine["status"].get("streamStatus") == "active" else None
        else:
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
        trc = DIAG.setdefault("tr_cnt", {})
        if time.time() - DIAG.get("tr_at", 0) < 120 or trc.get(bc["id"], 0) >= 4:
            return "wait"                                   # transition = 50 units: once per 2 min, max 4 per broadcast (kick is free)
        DIAG["tr_at"] = time.time()
        trc[bc["id"]] = trc.get(bc["id"], 0) + 1
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
    ls = call("GET", "liveStreams", {"part": "snippet,status,cdn", "mine": "true", "maxResults": 50}).get("items", [])
    if not ls:
        return "nostream"
    mine = _my_stream(ls)
    if SLOT != "1" and not mine:
        return "nostream"                                    # never guess on relay 2
    st = mine or next((i for i in ls if i["status"].get("streamStatus") == "active"), ls[0])
    if mine:
        DIAG["sid"] = mine["id"]
    lb = call("GET", "liveBroadcasts", {"part": "snippet,status,contentDetails", "mine": "true", "maxResults": 25}).get("items", [])
    lb = _mine_only(lb, mine["id"] if mine else None)
    ended = DIAG.get("ended_ids", set())             # a broadcast we just completed may still read 'live' for a few seconds: never reuse it
    bc = next((i for i in lb if i["id"] not in ended
               and i["status"].get("lifeCycleStatus") in ("created", "ready", "testing", "liveStarting", "live")), None)
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

    sid = _my_stream_id(call)
    if SLOT != "1" and not sid:
        return "wait"
    items = call("GET", "liveBroadcasts", {"part": "snippet,status,contentDetails", "broadcastStatus": "active", "maxResults": 10}).get("items", [])
    if sid:
        items = [i for i in items if (i.get("contentDetails") or {}).get("boundStreamId") == sid]
    ended = DIAG.setdefault("ended_ids", set())
    items = [i for i in items if i["id"] not in ended
             and (i.get("status") or {}).get("lifeCycleStatus") not in ("complete", "revoked")]   # dying broadcast of the previous relay
    if not items:
        return _ensure_broadcast(call, src_title, channel)
    it = items[0]
    title, desc = caption_from(src_title, channel)
    capt = DIAG.setdefault("capt_ids", {})
    if isinstance(capt, set):                               # (older runs kept a set of ids)
        capt = DIAG["capt_ids"] = {i: None for i in capt}
    if capt.get(it["id"]) != title:                         # PUT costs 50 quota units: once per (broadcast, title), never per tick
        # FIX: failover to ANOTHER match can reuse the still-live broadcast. It was captioned once (old match) and then skipped
        # forever, so the new match streamed under the old title/thumbnail. Now a changed source title re-captions it.
        sn = {"title": title, "description": desc, "scheduledStartTime": it["snippet"]["scheduledStartTime"]}
        call("PUT", "liveBroadcasts", {"part": "snippet"}, {"id": it["id"], "snippet": sn})
        if capt.get(it["id"]) is not None:
            DIAG.setdefault("thumb_try", {}).pop(it["id"], None)   # new match -> new thumbnail too
        capt[it["id"]] = title
        log("caption set: " + title)
    th = DIAG.setdefault("thumb_try", {})
    if th.get(it["id"], 0) < 3 and th.get(it["id"], 0) >= 0:      # up to 3 tries; -1 = done
        has_frame = os.path.isfile("/tmp/ytm_frame.jpg")
        if has_frame or th.get(it["id"], 0) >= 2:                 # wait (max 2 ticks) for a real frame, then go with the plain gradient
            th[it["id"]] = -1 if _set_thumb(tok, it["id"], src_title, channel) else th.get(it["id"], 0) + 1
        else:
            th[it["id"]] = th.get(it["id"], 0) + 1
    if (it.get("status") or {}).get("lifeCycleStatus") != "live":
        return "wait"                                       # titled, but not LIVE yet: keep watching (watchdog re-kicks)
    return "done"


def check_live():
    """True = our bound broadcast is LIVE on YouTube, False = it is not (ready/created/complete/none), None = cannot tell (no OAuth, API error).
    1 quota unit; used by the live-watch so a relay never looks LIVE in the dashboard while YouTube shows nothing."""
    tok = _oauth_token()
    if not tok:
        return None
    H = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}

    def call(method, path, params):
        req = urllib.request.Request(_API + path + "?" + urllib.parse.urlencode(params), headers=H, method=method)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    try:
        sid = _my_stream_id(call)
        if SLOT != "1" and not sid:
            return None
        items = call("GET", "liveBroadcasts", {"part": "id,status,contentDetails", "mine": "true", "maxResults": 10}).get("items", [])
    except Exception as e:
        log("check_live: %s" % str(e)[:100])
        return None
    if sid:
        items = [i for i in items if (i.get("contentDetails") or {}).get("boundStreamId") == sid]
    ended = DIAG.get("ended_ids", set())
    st = [(i.get("status") or {}).get("lifeCycleStatus") for i in items if i["id"] not in ended]
    return "live" in st


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

    n = bad = 0
    try:
        sid = _my_stream_id(call)
        if SLOT != "1" and not sid:
            return "skip"                                    # cannot tell which broadcast is ours: do not end relay 1's
        items = call("GET", "liveBroadcasts", {"part": "id,status,contentDetails", "broadcastStatus": "active", "maxResults": 10}).get("items", [])
        if sid:
            items = [i for i in items if (i.get("contentDetails") or {}).get("boundStreamId") == sid]
        for it in items:
            try:
                call("POST", "liveBroadcasts/transition", {"broadcastStatus": "complete", "id": it["id"], "part": "status"})
                DIAG.setdefault("ended_ids", set()).add(it["id"])
                n += 1
            except Exception as e:
                bad += 1
                log("end broadcast %s: %s" % (it["id"], str(e)[:100]))
    except Exception as e:
        log("end_broadcast error: %s" % str(e)[:120])
        return "error"
    log("broadcast(s) completed: %d%s" % (n, (" (FAILED: %d)" % bad) if bad else ""))
    return "error" if bad else "done"


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
    """Spot the centre-bottom probe looks at = exactly where our badge goes (YTM_WM_BOX x:y:w:h, 1280x720 frame)."""
    try:
        wx, wy, ww, wh = [int(v) for v in os.environ.get("YTM_WM_BOX", "471:577:330:38").split(":")]
        return wx + ww // 2, wy + wh // 2, ww, wh
    except Exception:
        return 636, 596, 330, 38


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


WORDMARK_ASPECT = 0.5414       # lml_wordmark.png height / width
LOGO_PROBE_VER = 8          # bump when sizing logic changes: cached per-match specs from older logic are re-probed


def logo_probe(hls):
    """Find the SOURCE channel's static logo in the top-right of the frame and decide what OUR logo should look like.
    Grabs 5 frames (1.5 s apart) of the top-right region (1280x720 frame coords), keeps pixels that are sharp-edged AND
    unchanged between frames (= a static overlay, not moving players), groups them into blobs and takes the biggest.
    A locked camera also has static background edges, so it tries strict levels first (only very sharp / dense edges =
    a logo) and relaxes step by step; a blob that touches the left/bottom border of the region is background, not a logo.
    -> dict(shape='circle', w=px, x=px, y=px)  or None (nothing reliable -> caller uses the default circle).
    Our logo is ALWAYS the circular one; its size follows the blob so the circle fully covers it (clamped to YTM_LOGO_MAX).
    Tunables: YTM_LOGO_STATIC (max change between frames, 12), YTM_LOGO_MAX (max circle px, 260)."""
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
            if X0 + (min(xs) + max(xs) + 1) * CELL / 2.0 < float(os.environ.get("YTM_LOGO_MINX", "1010")):
                continue                                 # blob centre too far left of the corner = static background (stands/banner), not a corner logo
            pick = (best, xs, ys, bw, bh, edge_t)
            break
        if not pick:
            log("logo probe: no reliable static logo -> default logo")
            return None
        best, xs, ys, bw, bh, edge_t = pick
        # TIGHT cover: use the real static edge pixels of the blob (not the 8px cell box) -> centre of their bbox, radius = farthest
        # pixel from that centre. Circle diameter = 2*radius + small pad, so a small source logo (e.g. a tennis ball) is covered
        # by a circle of about the same size, not 1.3x-2x bigger. YTM_LOGO_PAD (px each side, 4), YTM_LOGO_SCALE (1.04: our
        # PNG ring has ~4% transparent margin), YTM_LOGO_MIN (40), YTM_LOGO_MAX (260).
        comp = set(best[1])
        px = []
        for i in range(n):
            if es[i] >= edge_t and (((i // CW) // CELL) * gw + min((i % CW) // CELL, gw - 1)) in comp:
                px.append((i % CW, i // CW))
        x0p, x1p = min(p_[0] for p_ in px), max(p_[0] for p_ in px)
        y0p, y1p = min(p_[1] for p_ in px), max(p_[1] for p_ in px)
        # refine: weak edges of the same logo (low-contrast side of a ball etc.) were missed by the strict level -> also take every
        # static edge pixel (>=24) within 10 px of the box found so far; bounded growth, so far-away background can't pull it out.
        gx0, gx1, gy0, gy1 = max(0, x0p - 10), min(CW - 1, x1p + 10), max(0, y0p - 10), min(CH - 1, y1p + 10)
        px = [(xx, yy) for yy in range(gy0, gy1 + 1) for xx in range(gx0, gx1 + 1) if es[yy * CW + xx] > 0] or px
        x0p, x1p = min(p_[0] for p_ in px), max(p_[0] for p_ in px)
        y0p, y1p = min(p_[1] for p_ in px), max(p_[1] for p_ in px)
        cx, cy = X0 + (x0p + x1p) / 2.0, Y0 + (y0p + y1p) / 2.0
        dist = sorted(math.hypot(p_[0] - (x0p + x1p) / 2.0, p_[1] - (y0p + y1p) / 2.0) for p_ in px)
        rad = dist[min(len(dist) - 1, int(len(dist) * 0.97))]     # 97th percentile: a few stray static pixels can't inflate the circle
        bx, by, bw, bh = X0 + x0p, Y0 + y0p, x1p - x0p + 1, y1p - y0p + 1
        fill = len(comp) / float((max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1))
        shape = "circle"                                  # always our round logo
        pad = float(os.environ.get("YTM_LOGO_PAD", "4"))
        d = 2.0 * rad + 2.0 * pad
        w = int(min(float(os.environ.get("YTM_LOGO_MAX", "260")), max(float(os.environ.get("YTM_LOGO_MIN", "40")), d * float(os.environ.get("YTM_LOGO_SCALE", "1.04")))))
        h = w
        bleed = int(w * 0.12)                             # a circle fully inside the frame can't cover a badge hugging the corner -> allow a small bleed past the edge
        x = int(min(1280 - w + bleed, max(-bleed, cx - w / 2.0)))
        y = int(min(720 - h + bleed, max(-bleed, cy - h / 2.0)))
        spec = {"shape": shape, "w": w, "x": x, "y": y}
        log("logo probe: source logo %dx%d @%d,%d fill=%.2f -> our %s logo %dpx @%d,%d" % (bw, bh, bx, by, fill, shape, w, x, y))
        return spec
    except Exception as e:
        log("logo probe failed (%s) -> default logo" % str(e)[:80])
        return None


FB_CFG = {"r1": True, "r2": True, "r3": True}      # dashboard switches (cfg["fb"]); default on, so the secret alone still works


def fb_key_set():
    return bool(os.environ.get("FB_STREAM_KEY" + _sfx(), "").strip())


YT_CFG = {"r1": True, "r2": True, "r3": True}      # dashboard switches (cfg["yt"]): YouTube output per relay, default on


def fb_cfg_apply(cfg):
    """read the dashboard's per-platform switches (cfg.fb / cfg.yt) into the module state"""
    f = (cfg or {}).get("fb") or {}
    for k in ("r1", "r2", "r3"):
        FB_CFG[k] = bool(f.get(k, True))
    y = (cfg or {}).get("yt") or {}
    for k in ("r1", "r2", "r3"):
        YT_CFG[k] = bool(y.get(k, True))


def outputs():
    """-> (yt, fb_url). At least one output is always on: if the dashboard switched both off, YouTube stays (Stop is the way to end a relay)."""
    fb = fb_rtmp()
    return (bool(YT_CFG["r" + SLOT]) or not fb), fb


def fb_rtmp():
    """Facebook Page output (opt-in). Returns rtmps URL or None.
    Relay 1 uses secret FB_STREAM_KEY, relay 2 FB_STREAM_KEY_2, relay 3 FB_STREAM_KEY_3 (a live key can only carry one stream at a time,
    so relay 2 needs its own FB key/live). Unset = no Facebook for that relay. FB_ENABLE=off kills both."""
    key = os.environ.get("FB_STREAM_KEY" + _sfx(), "").strip()
    if not key or os.environ.get("FB_ENABLE", "on").strip().lower() == "off" or not FB_CFG["r" + SLOT]:
        return None
    base = os.environ.get("FB_RTMP_BASE", "rtmps://live-api-s.facebook.com:443/rtmp").rstrip("/")
    return base + "/" + key


def _tee(rtmp, fb):
    """One encode -> YouTube + Facebook. YouTube failure ends ffmpeg (recovery logic restarts it);
    a Facebook failure is ignored so it can never take the YouTube relay down."""
    return "[f=flv]%s|[f=flv:onfail=ignore]%s" % (rtmp, fb)


WM_PROBE_VER = 1
WM_ASPECT = 76 / 660.0       # lml_badge.png is 660x76: the badge keeps this aspect when it is auto-sized


def wm_probe(hls):
    """Find the SOURCE's static watermark at the centre-bottom of the frame and decide where / how big OUR badge goes.
    Same idea as logo_probe: 5 frames (1.5 s apart) of the centre-bottom strip (1280x720 frame coords), keep pixels that are
    sharp-edged AND unchanged between frames (= a static overlay, not moving players), group them into blobs, take the biggest
    that sits near the horizontal centre. Blobs that touch the strip border, are absurdly big/small or sit off-centre are
    background / scoreboard, not a watermark. The badge keeps its aspect, is at least YTM_WM_MIN wide (default 330 = the old
    fixed size), at most YTM_WM_MAX (540), centred on the blob and kept above the scoreboard.
    -> dict(x, y, w, h) in 1280x720 coords, or None (nothing reliable -> caller keeps the fixed default box)."""
    X0, Y0, CW, CH, CELL = 360, 536, 560, 104, 8
    gw, gh = CW // CELL, CH // CELL
    try:
        vf = ("fps=0.67,scale=1280:720:flags=bilinear:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
              "crop=%d:%d:%d:%d,format=gray" % (CW, CH, X0, Y0))
        p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", hls, "-an", "-vf", vf,
                            "-frames:v", "5", "-f", "rawvideo", "-"], capture_output=True, timeout=45)
        n = CW * CH
        fr = [p.stdout[i * n:(i + 1) * n] for i in range(len(p.stdout) // n)]
        if len(fr) < 4:
            log("wm probe: only %d frame(s) -> default badge" % len(fr))
            return None
        k = len(fr)
        mean = [sum(f[i] for f in fr) / k for i in range(n)]
        stat_t = float(os.environ.get("YTM_WM_STATIC", "12"))
        es = [0.0] * n
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
            for c0 in act:
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
            cx = X0 + (min(xs) + max(xs) + 1) * CELL / 2.0
            cy = Y0 + (min(ys) + max(ys) + 1) * CELL / 2.0
            if min(xs) == 0 or max(xs) == gw - 1 or max(ys) == gh - 1 or bw > 520 or bw < 56 or bh > 56 or bh < 12 or bw < 2.2 * bh:
                continue                                 # touches the strip border / absurd size = background or scoreboard
            if abs(cx - 640) > float(os.environ.get("YTM_WM_CENTER_TOL", "110")) or not (556 <= cy <= 628):
                continue                                 # not a centred watermark
            pick = (bw, bh, cx, cy, set(best[1]))
            break
        if not pick:
            log("wm probe: no reliable static watermark -> default badge")
            return None
        bw, bh, cx, cy, comp = pick
        near = set()                                     # refine the cell-sized box to the real pixel extent (cells are 8 px coarse)
        for c in comp:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    near.add((c // gw + dy) * gw + (c % gw + dx))
        pxs = pys = None
        for i in range(n):
            if es[i] and (((i // CW) // CELL) * gw + min((i % CW) // CELL, gw - 1)) in near:
                x_, y_ = i % CW, i // CW
                pxs = (x_, x_) if pxs is None else (min(pxs[0], x_), max(pxs[1], x_))
                pys = (y_, y_) if pys is None else (min(pys[0], y_), max(pys[1], y_))
        if pxs:
            bw, bh = pxs[1] - pxs[0] + 1, pys[1] - pys[0] + 1
            cx, cy = X0 + (pxs[0] + pxs[1] + 1) / 2.0, Y0 + (pys[0] + pys[1] + 1) / 2.0
        need_w = bw + 20
        need_h = bh + 10
        w = max(need_w, need_h / WM_ASPECT) * float(os.environ.get("YTM_WM_SCALE", "1.0"))
        w = min(float(os.environ.get("YTM_WM_MAX", "540")), max(float(os.environ.get("YTM_WM_MIN", "330")), w))
        w = int(w) & ~1
        h = int(round(w * WM_ASPECT)) & ~1
        x = int(min(1280 - w, max(0, cx - w / 2.0)))
        y = int(min(650 - h, max(540, cy - h / 2.0)))
        spec = {"x": x, "y": y, "w": w, "h": h}
        log("wm probe: source watermark %dx%d @%d,%d -> our badge %dx%d @%d,%d" % (bw, bh, int(cx - bw / 2.0), int(cy - bh / 2.0), w, h, x, y))
        return spec
    except Exception as e:
        log("wm probe failed (%s) -> default badge" % str(e)[:80])
        return None


def ffmpeg_cmd(hls, rtmp, strip=True, logo_spec=None, permitted=False, wm_spec=None):
    yt, fb = outputs()
    base = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5", "-fflags", "+genpts", "-i", hls]
    logo = os.environ.get("YTM_LOGO", "lml_logo.png")      # round LML logo, navy-filled inside the ring (lml_logo.png). Alternatives: YTM_LOGO=logo.png (transparent wordmark + navy LIVE patch) or lml_circle.png
    if not os.path.isfile(logo):
        if fb and yt:
            return base + ["-map", "0:v?", "-map", "0:a?", "-c", "copy", "-tag:a", "10", "-f", "tee", _tee(rtmp, fb)]
        return base + ["-c", "copy", "-f", "flv", fb if fb and not yt else rtmp]          # no logo file -> old behaviour (stream copy)
    # logo mode: re-encode. Frame is forced to 1280x720, source-channel logo corners are smeared (delogo),
    # then YOUR logo goes top-right on top. YTM_DELOGO="x:y:w:h;x:y:w:h" overrides the boxes, YTM_DELOGO=off disables.
    # YTM_LOGO_POS = overlay expr (default top-right), YTM_LOGO_W = logo width px (default 150).
    boxes = os.environ.get("YTM_DELOGO", "").strip()
    if not boxes:
        boxes = "off"   # NO blur: top-right is covered by our big corner circle instead of delogo. (old box 1126:1:152:146) source top-right badge (Ten 10) removed with delogo, then our TRANSPARENT logo goes on top. YTM_DELOGO=off disables.
    # fallback when the probe finds no static logo (animated / flipping source logos change every frame):
    # big circle tucked into the top-right corner (slight bleed past the edge) so a corner badge stays hidden.
    # permitted source: our logo sits centred on the source's round top-right logo (centre 1186,89),
    # whose pixels are removed first with a tight circle mask (no rectangle smear). Any other source: plain logo, nothing removed.
    pos = os.environ.get("YTM_LOGO_POS", "1101:39" if permitted else "W-w-4:22")
    lw = os.environ.get("YTM_LOGO_W", "170" if permitted else "150")
    chain = "[0:v]fps=30,scale=1280:720:flags=lanczos:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if boxes.lower() != "off":
        for bx in boxes.split(";"):
            if bx.count(":") == 3:
                chain += ",delogo=x=%s:y=%s:w=%s:h=%s" % tuple(bx.split(":"))
    if permitted:
        mk = os.environ.get("YTM_RM_MASK", "mask_ten10_tr.png").strip()
        if mk.lower() != "off" and os.path.isfile(mk):
            chain += ",removelogo=filename=%s" % mk                # tight circle mask on the top-right round logo
        # centre-bottom watermark: overwritten with real grass copied from just below it (feathered edges, no blur).
        # YTM_CLEAN="x:y:w:h:srcy;..." (1280x720 frame), =off disables.
        cl = os.environ.get("YTM_CLEAN", "off").strip()
        for i, bx in enumerate([] if cl.lower() == "off" else cl.split(";")):
            if re.fullmatch(r"\d{1,4}:\d{1,4}:\d{1,4}:\d{1,4}:\d{1,4}", bx.strip()):
                cx_, cy_, cw_, ch_, sy_ = bx.strip().split(":")
                chain += (",split[pa%d][pb%d];[pb%d]crop=%s:%s:%s:%s,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                          "a='255*min(1,min(X,%d-X)/8)*min(1,min(Y,%d-Y)/8)'[pp%d];[pa%d][pp%d]overlay=%s:%s:format=auto"
                          % (i, i, i, cw_, ch_, cx_, sy_, int(cw_) - 1, int(ch_) - 1, i, i, i, cx_, cy_))
        # bottom ticker bar carries the source's name/contact in scrolling text -> replaced by our own ticker (YTM_TICKER text, =off keeps the source bar)
        tk = os.environ.get("YTM_TICKER", "LML SPORTS  |  LOCAL MATCH LIVE  |  LIVE CRICKET & MORE SPORTS  |  SUBSCRIBE LML SPORTS").strip()
        if tk.lower() != "off":
            chain += ",drawbox=x=0:y=689:w=iw:h=31:color=black:t=fill"
            _f = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if os.path.isfile(_f) and re.fullmatch(r"[A-Za-z0-9 @._&|-]{1,120}", tk):
                chain += (",drawtext=fontfile=%s:text='%s':fontcolor=0xffd400:fontsize=22:y=h-30:x=w-mod(t*110\\,w+text_w)" % (_f, tk))
    # Source-channel marks (permission-holder OK'd removal) are removed from the SOURCE picture before our logo goes on.
    # YTM_REMOVE="x:y:w:h;..." overrides the delogo boxes (1280x720 frame), =off disables.
    # Ten 10 centre-bottom watermark (icon + text + icon) -> delogo; on plain grass it leaves no visible patch.
    rm = os.environ.get("YTM_REMOVE", "").strip() or "off"
    if rm.lower() != "off":
        for bx in rm.split(";"):
            if re.fullmatch(r"\d{1,4}:\d{1,4}:\d{1,4}:\d{1,4}", bx.strip()):
                chain += ",delogo=x=%s:y=%s:w=%s:h=%s" % tuple(bx.strip().split(":"))
    bo = os.environ.get("YTM_BLACKOUT", "0:691:153:25" if permitted else "off").strip()          # source logo on the left end of the black ticker bar -> solid black (YTM_BLACKOUT=off disables)
    if bo.lower() != "off":
        for bx in bo.split(";"):
            if re.fullmatch(r"\d{1,4}:\d{1,4}:\d{1,4}:\d{1,4}", bx.strip()):
                chain += ",drawbox=x=%s:y=%s:w=%s:h=%s:color=black:t=fill" % tuple(bx.strip().split(":"))
    # centre-bottom "ACL" mark (permission-holder OK'd removal): covered by a solid branded strip with our tag (no blur smear).
    # YTM_TAG="" / "off" -> strip off and the mark is delogo-smeared instead. YTM_TAG_POS="cx:cy", YTM_TAG_BOX="w:h" (1280x720 frame).
    tag = os.environ.get("YTM_TAG", "off").strip() if strip else "off"      # bottom-centre brand tag OFF by default (YTM_TAG=LML brings the text strip back)
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    cx, cy = (os.environ.get("YTM_TAG_POS", ("636:596" if permitted else "646:588")).split(":") + ["588"])[:2]
    bw, bh = (os.environ.get("YTM_TAG_BOX", ("330:38" if permitted else "250:64")).split(":") + ["64"])[:2]
    tagimg = os.environ.get("YTM_TAG_IMG", "off").strip()                # bottom-centre tag image OFF by default (YTM_TAG_IMG=lml_tag.png brings it back)
    use_img = bool(strip and tagimg and tagimg.lower() != "off" and os.path.isfile(tagimg)
                   and all(re.fullmatch(r"\d{1,4}", v) for v in (cx, cy, bw, bh)))
    if use_img:                                                   # branded tag PNG (opaque, hides the source watermark)
        chain += "[b0];[2:v]scale=%s:%s,format=rgba[tg];[b0][tg]overlay=%d:%d" % (bw, bh, int(cx) - int(bw) // 2, int(cy) - int(bh) // 2)
    elif tag and tag.lower() != "off" and not os.path.isfile(font) and re.fullmatch(r"\d{1,4}", cx) and re.fullmatch(r"\d{1,4}", cy) \
            and re.fullmatch(r"\d{1,4}", bw) and re.fullmatch(r"\d{1,4}", bh):
        chain += ",drawbox=x=%s-%s/2:y=%s-%s/2:w=%s:h=%s:color=0x0b3d91:t=fill" % (cx, bw, cy, bh, bw, bh)   # no font on the runner: solid strip only
    elif tag and tag.lower() != "off" and os.path.isfile(font) and re.fullmatch(r"[A-Za-z0-9 @._-]{1,24}", tag) \
            and all(re.fullmatch(r"\d{1,4}", v) for v in (cx, cy, bw, bh)):
        chain += (",drawbox=x=%s-%s/2:y=%s-%s/2:w=%s:h=%s:color=0x0b3d91:t=fill"
                  ",drawtext=fontfile=%s:text='%s':fontcolor=white:fontsize=28:x=%s-text_w/2:y=%s-text_h/2:"
                  "shadowcolor=black@0.6:shadowx=2:shadowy=2" % (cx, bw, cy, bh, bw, bh, font, tag, cx, cy))
    # other channels' corner marks (permission-holder OK'd removal): small opaque branded patches. Default boxes = the usual
    # bottom-left channel logo, a right-edge watermark just above the scoreboard and the bottom-right corner.
    # YTM_PATCHES="x:y:w:h;x:y:w:h" (1280x720 frame) overrides, YTM_PATCHES=off disables.
    pt = os.environ.get("YTM_PATCHES", "off").strip() or "off"   # patches OFF by default: other channels' marks are REMOVED with delogo (YTM_REMOVE) instead
    if pt.lower() != "off" and os.path.isfile(font):
        for bx in pt.split(";"):
            if re.fullmatch(r"\d{1,4}:\d{1,4}:\d{1,4}:\d{1,4}", bx.strip()):
                px, py, pw, ph = bx.strip().split(":")
                chain += (",drawbox=x=%s:y=%s:w=%s:h=%s:color=0x0b3d91:t=fill"
                          ",drawtext=fontfile=%s:text='LML':fontcolor=white:fontsize=%d:x=%s+(%s-text_w)/2:y=%s+(%s-text_h)/2"
                          % (px, py, pw, ph, font, max(9, min(int(ph) - 10, int(pw) // 4)), px, pw, py, ph))
    wm = None
    badge = None
    try:                                                          # subtle badge: 0.0 (invisible) .. 1.0 (solid); default 1.0 = solid, nothing shows through
        wm_alpha = "%.2f" % max(0.0, min(1.0, float(os.environ.get("YTM_WM_ALPHA", "1.0"))))
    except ValueError:
        wm_alpha = "1.00"
    if strip and os.environ.get("YTM_WM", "off").strip().lower() != "off":
        # OUR LML SPORTS badge, centre-bottom, on every relay (own branding; no source mark is removed, only covered by the pill).
        # centre-bottom spot (x471-801,y577-615) carries OUR logo + text. YTM_WM=off hides it.
        # YTM_WM_BOX="x:y:w:h" (1280x720 frame), YTM_WM_TEXT, YTM_WM=off disables.
        bx_ = os.environ.get("YTM_WM_BOX", "471:577:330:38").strip()
        wt = os.environ.get("YTM_WM_TEXT", "LOCAL MATCH LIVE").strip()
        if re.fullmatch(r"\d{1,4}:\d{1,4}:\d{1,4}:\d{1,4}", bx_):
            wx, wy, ww, wh = [int(v) for v in bx_.split(":")]
            if wm_spec:                                           # auto-fitted to the source watermark (wm_probe)
                try:
                    wx, wy, ww, wh = int(wm_spec["x"]), int(wm_spec["y"]), int(wm_spec["w"]), int(wm_spec["h"])
                except Exception:
                    pass
            bimg = os.environ.get("YTM_WM_IMG", "lml_badge.png").strip()      # pro-style LML SPORTS badge (make_lml_badge.py); =off -> old black bar + text
            if bimg and bimg.lower() != "off" and os.path.isfile(bimg):
                badge = (bimg, wx, wy, ww, wh)
            else:
                chain += ",drawbox=x=%d:y=%d:w=%d:h=%d:color=black@0.99:t=fill" % (wx, wy, ww, wh)
                lh = wh - 6
                lwid = int(lh * 708 / 419.0)
                gx = wx + 12
                if os.path.isfile(font) and re.fullmatch(r"[A-Za-z0-9 @._&|-]{1,40}", wt):
                    chain += (",drawtext=fontfile=%s:text='%s':fontcolor=white:fontsize=%d:x=%d:y=%d+(%d-text_h)/2"
                              % (font, wt, max(12, wh // 2), gx + lwid + 14, wy, wh))
                wm = (lh, gx, wy + 3)
    if logo_spec:                                                 # auto-sized to the detected source logo
        lw, pos = str(int(logo_spec["w"])), "%d:%d" % (int(logo_spec["x"]), int(logo_spec["y"]))
    # wordmark test (YTM_LOGO=lml_wordmark.png, trimmed LML panther wordmark, 700x~420 transparent): the circle size w from the probe is the
    # circle's width, a wide wordmark needs more width to cover the same spot -> width = w * YTM_WORDMARK_K (1.5), centred where the circle was.
    if os.path.basename(logo) == "lml_wordmark.png":
        try:
            w0 = int(float(lw))
            wn = int(w0 * float(os.environ.get("YTM_WORDMARK_K", "1.5")))
            hn = int(wn * WORDMARK_ASPECT)
            if logo_spec:
                dy = int(float(os.environ.get("YTM_WORDMARK_DY", "-22")))      # px, negative = up (LML letters over the source ball, SPORTS over its caption)
                dx = int(float(os.environ.get("YTM_WORDMARK_DX", "0")))
                pos = "%d:%d" % (int(logo_spec["x"]) + w0 // 2 - wn // 2 + dx, max(0, int(logo_spec["y"]) + w0 // 2 - hn // 2 + dy))
            lw = str(wn)
        except ValueError:
            pass
    # navy patch right under the wordmark logo: fills the strip where a source's sub-badge/"LIVE" text pokes out below our transparent logo.
    # YTM_LOGO_PATCH=off disables; YTM_LOGO_PATCH_COLOR = hex colour (default navy 0x101b38).
    if os.environ.get("YTM_LOGO_PATCH", "on").strip().lower() != "off" and os.path.basename(logo) == "logo.png":
        try:
            lwn = int(float(lw))
            lhn = int(lwn * 419 / 708.0)                      # wordmark aspect (logo.png is 708x419)
            lxn, lyn = (int(logo_spec["x"]), int(logo_spec["y"])) if logo_spec else (1280 - lwn - 4, 22)
            pw, ph = int(lwn * 0.80), max(22, int(lwn * 0.30))
            px_, py_ = lxn + (lwn - pw) // 2, lyn + lhn - int(lwn * 0.05)
            px_, py_ = max(0, min(1280 - pw, px_)), max(0, min(720 - ph, py_))
            pc = os.environ.get("YTM_LOGO_PATCH_COLOR", "0x101b38").strip()
            if re.fullmatch(r"0x[0-9a-fA-F]{6}", pc):
                chain += ",drawbox=x=%d:y=%d:w=%d:h=%d:color=%s:t=fill" % (px_, py_, pw, ph, pc)
                _pf = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                if os.path.isfile(_pf):
                    chain += (",drawtext=fontfile=%s:text='LIVE':fontcolor=0xe2182c:fontsize=%d:x=%d+(%d-text_w)/2:y=%d+(%d-text_h)/2"
                              % (_pf, max(12, int(ph * 0.55)), px_, pw, py_, ph))
        except Exception:
            pass
    if wm:
        fc = chain + ("[bg];[1:v]split=2[l1][l2];[l1]scale=%s:-1[lg];[l2]scale=-1:%d[lg2];[bg][lg]overlay=%s[o1];[o1][lg2]overlay=%d:%d"
                      ",split=2[v][s];[s]fps=1/20,scale=640:-1[snap]" % (lw, wm[0], pos, wm[1], wm[2]))
    elif badge:                                                   # logo top-right, then the badge over the centre-bottom source mark
        bidx = 2 + (1 if use_img else 0)
        fc = chain + ("[bg];[1:v]scale=%s:-1[lg];[bg][lg]overlay=%s[o1];[%d:v]scale=%d:%d:flags=lanczos,format=rgba,colorchannelmixer=aa=%s[bdg];[o1][bdg]overlay=%d:%d"
                      ",split=2[v][s];[s]fps=1/20,scale=640:-1[snap]" % (lw, pos, bidx, badge[3], badge[4], wm_alpha, badge[1], badge[2]))
    else:
        fc = chain + "[bg];[1:v]scale=%s:-1[lg];[bg][lg]overlay=%s,split=2[v][s];[s]fps=1/20,scale=640:-1[snap]" % (lw, pos)
    return (base + ["-i", logo] + (["-i", tagimg] if use_img else []) + (["-i", badge[0]] if badge else []) + ["-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", os.environ.get("YTM_PRESET", "veryfast"), "-profile:v", "high", "-crf", "20",
            "-maxrate", "4000k" if fb else "6000k", "-bufsize", "8000k" if fb else "12000k",      # Facebook wants <= ~4 Mbps at 720p
            "-g", "60", "-pix_fmt", "yuv420p", "-c:a", "copy"] + (["-tag:a", "10", "-f", "tee", _tee(rtmp, fb)] if fb and yt else ["-f", "flv", fb if fb and not yt else rtmp]) + [
            "-map", "[snap]", "-q:v", "5", "-f", "image2", "-update", "1", "-y", "/tmp/ytm_frame.jpg"])


class Relay:
    def __init__(self, state, rtmp, _probe=probe, _popen=subprocess.Popen):
        self.st, self.rtmp, self._probe, self._popen = state, rtmp, _probe, _popen
        self.proc, self.restarts = None, 0

    def _spawn(self, hls, cand=None):
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
            if vid and lp.get("video_id") == vid and lp.get("ver") == LOGO_PROBE_VER and "spec" in lp \
                    and (lp["spec"] or time.time() - lp.get("at", 0) < 600):   # a failed/empty probe is retried after 10 min     # same match, restart/chain -> reuse, no extra delay
                spec = lp["spec"]
            else:
                spec = logo_probe(hls)
                self.st["logo_probe"] = {"video_id": vid, "spec": spec, "ver": LOGO_PROBE_VER, "at": int(time.time())}
        wm_spec = None
        if os.environ.get("YTM_WM_AUTO", "on").lower() != "off" and os.environ.get("YTM_WM", "off").strip().lower() != "off":
            vid = (self.st.get("relay") or {}).get("video_id") or getattr(self, "_cur_vid", None)
            wp = self.st.get("wm_probe") or {}
            if vid and wp.get("video_id") == vid and wp.get("ver") == WM_PROBE_VER and "spec" in wp \
                    and (wp["spec"] or time.time() - wp.get("at", 0) < 600):      # same match, restart/chain -> reuse; an empty probe is retried after 10 min
                wm_spec = wp["spec"]
            else:
                wm_spec = wm_probe(hls)
                self.st["wm_probe"] = {"video_id": vid, "spec": wm_spec, "ver": WM_PROBE_VER, "at": int(time.time())}
        _y, _f = outputs()
        self.st["fb_on"], self.st["yt_on"] = bool(_f), bool(_y)
        permitted = False                                    # no allowlist: source marks are never removed/covered; only our logo overlay is added
        self.st["strip_permitted"] = permitted
        return self._popen(ffmpeg_cmd(hls, self.rtmp, strip, spec, permitted, wm_spec), stderr=self._elog)

    def _tail(self):
        """last ffmpeg output (stream key masked) -> state, so it is visible without Actions logs"""
        try:
            with open("/tmp/ffmpeg.log", "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 600))
                t = f.read().decode("utf8", "replace").replace("\r", "\n")
            self.st["ffmpeg_tail"] = re.sub(r"rtmps?://\S+", "rtmp://***", t)[-350:]
            self.st["fb_dropped"] = bool(self.st.get("fb_on") and re.search(r"Slave .*(fail|ignor)", t, re.I))
        except Exception:
            pass
        now = time.time()
        if now - getattr(self, "_snap_at", 0) > 120 and os.path.isfile("/tmp/ytm_frame.jpg"):
            self._snap_at = now                    # what viewers really see -> ytm_frame.jpg on the data branch
            try:
                sha = None
                try:
                    sha = gh("GET", "contents/%s?ref=%s" % (FRAME_FILE, BRANCH)).get("sha")
                except urllib.error.HTTPError:
                    pass
                body = {"message": "frame", "branch": BRANCH,
                        "content": base64.b64encode(open("/tmp/ytm_frame.jpg", "rb").read()).decode()}
                if sha:
                    body["sha"] = sha
                gh("PUT", "contents/" + FRAME_FILE, body)
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
        if outcome in ("stopped", "ended"):          # not for "interrupted" (job chain resumes) / failover
            self._end_yt()
        if outcome == "ended":                        # match finished by itself -> highlights now (not on manual/cap stop)
            trigger_highlights()
        self.st.setdefault("history", []).insert(0, {
            "video_id": r.get("video_id"), "title": r.get("title"), "channel": r.get("channel"),
            "started": r.get("started"), "ended": int(time.time()), "outcome": outcome, "note": note})
        del self.st["history"][30:]
        self.st["relay"] = None

    def _end_yt(self):
        """Complete our live YouTube broadcast. On failure (relay 1) remember it: the next match must not stream into it."""
        try:
            res = end_broadcast()
        except Exception as e:
            log("end_broadcast: %s" % str(e)[:100])
            res = "error"
        if res == "error" and SLOT == "1":
            # old broadcast is still LIVE on YouTube: the next match must NOT stream into it (two matches in one video, old title)
            self.st["pending_end"] = self.st.get("pending_end") or int(time.time())
        elif res == "done":
            self.st.pop("pending_end", None)
            self.st.pop("pending_end_tries", None)
        return res

    def _clear_stale(self):
        """True = fine to start. After a failed end_broadcast (YouTube login expired / quota / API hiccup) the OLD broadcast is
        still live: retry ending it first, and if that still fails do not start the next match (it would be mixed into the old
        video under the old title). Transient failures give up after 5 tries so a relay is never blocked forever."""
        if not self.st.get("pending_end") or SLOT != "1":
            return True
        try:
            res = end_broadcast()
        except Exception as e:
            log("pending end_broadcast: %s" % str(e)[:100])
            res = "error"
        if res != "error":
            self.st.pop("pending_end", None)
            self.st.pop("pending_end_tries", None)
            return True
        n = self.st["pending_end_tries"] = self.st.get("pending_end_tries", 0) + 1
        if not AUTH["dead"] and n >= 5:
            log("pending end_broadcast failed %dx (not an auth problem) -> starting anyway" % n)
            self.st.pop("pending_end", None)
            self.st.pop("pending_end_tries", None)
            return True
        return False

    def start(self, c):
        try:
            is_live, hls = self._probe(c["url"])
        except Exception as e:
            self._record_fail(c, "probe: %s" % e)
            return
        if not is_live or not hls:
            return                       # not actually live (yet); try again next cycle
        if not self._clear_stale():
            log("hold: old YouTube broadcast could not be ended -> not starting " + c["title"][:60])
            return
        log("relay start: " + c["title"][:80])
        self.restarts = 0
        prep = None
        try:
            prep = prepare_broadcast(c["title"], c["channel"]) if outputs()[0] else "yt-off"
            self.st.pop("prep_err", None)
        except Exception as e:
            self.st["prep_err"] = _errtxt(e)
            log("prepare broadcast failed: %s" % self.st["prep_err"])
        self._cur_vid = c["video_id"]
        self.proc = self._spawn(hls, c)
        self.st["relay"] = {"video_id": c["video_id"], "title": c["title"], "channel": c["channel"],
                            "handle": c.get("handle") or "", "channel_id": c.get("channel_id") or "",
                            "url": c["url"], "started": int(time.time()), "spawned_at": int(time.time()), "not_live_since": None, "yt_off": not outputs()[0]}
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

    def _same_match(self, cur, c):
        a, b = self._tok(cur.get("title")), self._tok(c.get("title"))
        return bool(a and b) and len(a & b) / float(len(a | b) or 1) >= 0.6

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
        ended = False
        for c in ([] if getattr(self, "manual", False) else self._pick_alt(r, now)):
            if not ended and not self._same_match(r, c):
                # FIX: "failed" failover left the old broadcast LIVE -> the other match was relayed into it under the OLD match's name.
                # Different match = close the old broadcast first (a fresh one with the right title is created for it).
                self._end_yt()
                ended = True
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
            if now - r.get("spawned_at", r.get("started", now)) > RUN_OK_SECS:
                r["down_since"] = None                       # it ran fine for a while: this is a fresh drop, not a crash loop
            r["down_since"] = r.get("down_since") or now
        elif now < r.get("retry_at", 0):
            return                                           # waiting for the next retry
        try:
            is_live, hls = self._probe(r["url"])
        except Exception as e:
            is_live, hls = None, None
            log("probe error: %s" % str(e)[:100])
        if is_live is False:
            self._failover("ended", "source ended")          # really over -> next match
        elif now - r.get("down_since", now) > RETRY_WINDOW:
            self._failover("failed", "source not back in %ds" % RETRY_WINDOW)
        elif is_live and hls:
            # SIMPLE RULE: source dropped but its live is still on -> relay the SAME match again (no switching to another match)
            self.restarts += 1
            log("source still live -> restart same match (%d)" % self.restarts)
            r["spawned_at"] = now
            self.proc = self._spawn(hls)
        else:
            r["retry_at"] = now + 10                         # probe error / not ready yet: keep trying the same source

    def caption_tick(self, now, _set=None):
        """Retry every ~25s (max 12x) until our broadcast is active, then set title/description once."""
        r = self.st.get("relay")
        if r and r.get("yt_off"):
            return                                       # YouTube output switched off from the dashboard: no broadcast to title/watch
        if r and AUTH["dead"]:
            r["caption_try"] = now                       # YouTube login dead: title/live-check cannot work, do not hammer the API
            return
        if r and r.get("caption_done"):
            self._live_watch(now, r)
            return
        if not r or now - r.get("caption_try", 0) < (25 if r.get("caption_n", 0) < 12 else (120 if r.get("kick_n", 0) < 6 else 600)):
            return
        r["caption_try"] = now
        r["caption_n"] = r.get("caption_n", 0) + 1
        try:
            res = (_set or set_caption)(r.get("title", ""), r.get("channel", ""))
        except Exception as e:
            r["caption_err"] = _errtxt(e)               # visible in ytm_state.json (was only in the Actions log)
            log("caption error: %s" % r["caption_err"])
            res = "wait"
        if res == "done":
            r["yt_live"] = True                         # dashboard: really LIVE on YouTube
        elif res == "wait":
            yt0 = DIAG.get("yt")
            if isinstance(yt0, dict) and any(b[1] in ("ready", "testing", "created") for b in yt0.get("broadcasts", [])):
                r["yt_live"] = False                    # ffmpeg runs but YouTube broadcast is not live
        if res in ("done", "skip"):                     # never give up while broadcast is not live (retry slower after 12)
            r["caption_done"] = True
        elif ((not r.get("kicked") and r["caption_n"] >= (2 if r.get("kick_early") else 4))
              or (r.get("kicked") and now - r.get("kick_at", 0) > 240 and r.get("kick_n", 0) < 6)):
            yt = DIAG.get("yt")
            bcs = yt.get("broadcasts", []) if isinstance(yt, dict) else []
            if any(b[1] in ("ready", "testing") for b in bcs):   # still not live although a broadcast is waiting
                self._kick_ingest(r)

    def _live_watch(self, now, r, _chk=None):
        """After the caption is done: every ~5 min verify the broadcast is really LIVE. Two misses in a row (relay up, YouTube not live)
        -> restart the caption/kick cycle (re-creates/binds/kicks the broadcast) instead of showing LIVE forever."""
        if now - r.get("lv_at", 0) < 300 or now - r.get("started", now) < 120:
            return
        r["lv_at"] = now
        try:
            res = (_chk or check_live)()
        except Exception as e:
            log("live watch error: %s" % str(e)[:100])
            return
        if res is True:
            r["yt_live"], r["lv_bad"] = True, 0
        elif res is False:
            r["yt_live"], r["lv_bad"] = False, r.get("lv_bad", 0) + 1
            if r["lv_bad"] >= 2:
                log("relay up but broadcast NOT live (x%d) -> restarting caption/kick cycle" % r["lv_bad"])
                for k in ("caption_done", "kicked"):
                    r[k] = False
                r["caption_n"], r["kick_n"], r["lv_bad"], r["caption_try"] = 0, 0, 0, 0
                r["kick_early"] = True

    def _kick_ingest(self, r):
        """Stuck on 'ready' (stream was flowing when the broadcast got bound): pause ingest so YouTube marks the stream idle,
        then the normal restart path reconnects = a fresh stream start, which fires the bound broadcast's auto-start."""
        r["kicked"] = True
        r["kick_at"], r["kick_n"] = time.time(), r.get("kick_n", 0) + 1
        log("broadcast stuck on ready -> pausing ingest %ds to re-trigger auto-start" % KICK_DOWN_SECS)
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except Exception:
                self.proc.kill()
        self.proc = None
        r["retry_at"], r["probe_fail"] = time.time() + KICK_DOWN_SECS, 0

    def fb_sync(self):
        """Dashboard flipped YouTube and/or Facebook for this relay while ffmpeg runs: restart ffmpeg once on the same source
        (a few seconds of blip on the platform(s) that stay on). YouTube off -> its broadcast is completed; YouTube on again ->
        a fresh broadcast is prepared first."""
        self.st["fb_key"] = fb_key_set()
        if self.proc is None or self.proc.poll() is not None or "fb_on" not in self.st:
            return
        want_yt, want_fb = outputs()
        want_fb = bool(want_fb)
        have_fb, have_yt = bool(self.st.get("fb_on")), bool(self.st.get("yt_on", True))
        if want_fb == have_fb and want_yt == have_yt:
            return
        r = self.st.get("relay") or {}
        log("outputs change: YouTube %s->%s, Facebook %s->%s -> restart ffmpeg" % (have_yt, want_yt, have_fb, want_fb))
        self.restarts = max(0, self.restarts - 1)           # a deliberate restart must not eat the failure budget
        if want_yt and not have_yt and r:                   # YouTube back on: the broadcast must exist before ingest starts
            try:
                prepare_broadcast(r.get("title", ""), r.get("channel", ""))
            except Exception as e:
                log("prepare broadcast failed: %s" % _errtxt(e))
            for k in ("caption_done", "kicked"):
                r[k] = False
            r["caption_n"], r["kick_n"], r["lv_bad"], r["caption_try"] = 0, 0, 0, 0
            r["kick_early"], r["yt_off"], r["yt_live"] = True, False, None
        try:
            self.proc.terminate()
            self.proc.wait(5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        if have_yt and not want_yt and r:                   # YouTube switched off: complete the broadcast so the channel stops showing LIVE
            r["yt_off"], r["yt_live"] = True, None
            try:
                end_broadcast()
            except Exception as e:
                log("end_broadcast: %s" % str(e)[:100])

    def stop(self, outcome="stopped", note="manual"):
        if self.st.get("relay"):
            self._finish(outcome, note)

    # -- decisions on each detection
    def _blocked(self, vid, now):
        h = self.st.get("history", [])
        fails = [x for x in h if x["video_id"] == vid and x["outcome"] == "failed" and now - (x.get("ended") or 0) < FAIL_WINDOW]
        if len(fails) >= MAX_ATTEMPTS or (fails and now - fails[0]["ended"] < RETRY_COOLDOWN):
            return True
        for x in h[:10]:                                  # newest entry for this video decides:
            if x["video_id"] == vid and x["outcome"] in ("ended", "stopped", "interrupted"):
                return x["outcome"] == "stopped"          # only a manual/cap stop blocks; "ended"/"interrupted" may resume if the video is live again
        return False

    def step(self, live, cfg, now):
        self.live_last = live
        r = self.st.get("relay")
        cap = int(cfg.get("daily_cap_h", 20)) * 60
        if r:
            if any(x["video_id"] == r["video_id"] for x in live):
                r["not_live_since"] = None
            else:
                r["not_live_since"] = r.get("not_live_since") or int(now)
                if now - r["not_live_since"] > ENDED_GRACE:
                    v = getattr(self, "verify", None)
                    if v and v(r["video_id"]) is not False:   # still live (or unknown/API error) -> keep relaying, re-check in ~60s
                        r["not_live_since"] = int(now) - ENDED_GRACE + 60
                    else:
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
        skip = [k.strip().lower() for k in (cfg.get("auto_skip") or "").split(",") if k.strip()]
        for c in live:
            if skip and not skip_hit(c, skip):
                continue                                  # Relay 1 auto = ONLY cricket words; other sports go to Relay 3 auto / manual
            if not self._blocked(c["video_id"], now):
                self.start(c)
                if self.st.get("relay"):
                    return


CMD_WAIT = 180            # s: a start command waits this long for its match to show up in the live list (fresh run starts with an empty list)


def apply_commands(cfg, st, relay, live, now):
    acked = st.get("acked", 0)
    cmds = sorted(cfg.get("commands", []), key=lambda c: c["id"])
    for i, cmd in enumerate(cmds):
        if cmd["id"] <= acked:
            continue
        if cmd["type"] == "start" and not st.get("relay"):
            c = next((x for x in live if x["video_id"] == cmd.get("video_id")), None)
            if c is None:
                # live list not filled yet (new run) -> keep the command pending instead of acking it and losing it silently
                if i == len(cmds) - 1 and now - cmd["id"] / 1000.0 < CMD_WAIT:
                    break
            else:
                relay.start(c)
        st["acked"] = cmd["id"]
        if cmd["type"] == "stop":
            relay.stop("stopped", "manual stop")


def runner_flagged(st, now):
    """Probes keep failing with 'unavailable / sign in / bot' on >=2 different videos (>=4 fails in 30 min), nothing is relaying and no
    self-heal ran in the last 30 min -> this runner/IP is probably flagged by YouTube. The run ends so the queued successor starts on a fresh runner."""
    if st.get("relay") or now - st.get("selfheal_at", 0) < 1800 or now - st.get("run_started", now) < 300:
        return False
    f = [x for x in st.get("history", []) if x.get("outcome") == "failed" and now - (x.get("ended") or 0) < 1800
         and re.search(r"unavailable|sign in|bot", x.get("note") or "", re.I)]
    return len(f) >= 4 and len({x.get("video_id") for x in f}) >= 2


# ── entry points ─────────────────────────────────────────────────────────
# ── self-chaining (GitHub cron is best-effort and can silently skip runs) ─
WORKFLOW_FILE = "yt-mirror.yml"
WORKFLOW2_FILE = "yt-mirror2.yml"


def ensure_next_queued(wf=None, inputs=None):
    """Keep one run waiting behind this one (concurrency group 'yt-mirror'), so the chain never
    depends on the */15 cron firing. workflow_dispatch with GITHUB_TOKEN is allowed to trigger runs."""
    try:
        wf = wf or WORKFLOW_FILE
        for stt in ("queued", "pending", "waiting"):
            r = gh("GET", "actions/workflows/%s/runs?status=%s&per_page=5" % (wf, stt))
            if r.get("workflow_runs"):
                return False                                   # something already waiting
        body = {"ref": "main"}
        if inputs:
            body["inputs"] = inputs
        gh("POST", "actions/workflows/%s/dispatches" % wf, body)
        log("next run queued (self-chain) %s" % wf)
        return True
    except Exception as e:
        log("self-chain failed: %s" % e)
        return False

HL_WORKFLOW_FILE = "yt-highlights.yml"


def trigger_highlights():
    """Source stream ended -> start the highlights workflow right away (no 2h cron wait).
    The workflow itself waits for our VOD to finish processing. Dedup: skipped if one is already waiting;
    auto OFF in dashboard config (hl_config.json) is honoured by the workflow gate."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return False
    try:
        return ensure_next_queued(HL_WORKFLOW_FILE, {"after_end": "true"})
    except Exception as e:
        log("trigger_highlights: %s" % str(e)[:100])
        return False


def gate():
    out = "false"
    try:
        cfg, _ = read_json(CFG_PATH)
        st, _ = read_json(ST_PATH)
        if cfg and cfg.get("enabled") and cfg.get("channels") and in_window(cfg) \
                and minutes_today(st or {}) < int(cfg.get("daily_cap_h", 20)) * 60:
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
    st["fb_key"] = fb_key_set()
    # an interrupted previous run may have left a relay record: close it, new run resumes from live list
    if st.get("relay"):
        st["history"] = st.get("history", [])
        st["history"].insert(0, {**{k: st["relay"].get(k) for k in ("video_id", "title", "channel", "started")},
                                 "ended": int(time.time()), "outcome": "interrupted", "note": "job chained"})
        st["relay"] = None
    if _relay_factory is None:
        try:
            _oauth_token()                                # sets AUTH["dead"] right away so the dashboard shows it without waiting for a relay
        except Exception as e:
            log("oauth check: %s" % str(e)[:100])
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
            fb_cfg_apply(cfg)
            if hasattr(relay, "fb_sync"):
                relay.fb_sync()
            relay.manual = (cfg.get("mode") == "manual")
            relay.verify = det.is_live
            st["mode"] = "manual" if relay.manual else "auto"
            apply_commands(cfg, st, relay, live, now)
        if now - last_detect >= cfg_poll(st):                 # YouTube detection: every poll_secs
            last_detect = now
            try:
                live, errors = det.live(cfg)
                st["last_check"] = int(now)
                taken = {(cfg.get("relay%s" % n) or {}).get("video_id") for n in MANUAL_SLOTS} - {None}
                relay.step([x for x in live if x["video_id"] not in taken] if taken else live, cfg, now)
                if _relay_factory is None:
                    try:
                        auto_relay2(cfg, live, st, now, relay)
                    except Exception as e:
                        log("auto_relay2: %s" % str(e)[:100])
                    try:
                        auto_relay3(cfg, live, st, now, relay)
                    except Exception as e:
                        log("auto_relay3: %s" % str(e)[:100])
            except Exception as e:
                errors = ["%s: %s" % (type(e).__name__, str(e)[:120])]
            if minutes_today(st, now) >= int(cfg.get("daily_cap_h", 20)) * 60 and not st.get("relay"):
                log("daily cap reached -> stop")
                break
            if _chain and runner_flagged(st, now):
                st["selfheal_at"] = int(now)
                log("probe keeps failing on several videos -> runner looks flagged, ending job so a fresh runner takes over")
                ensure_next_queued()
                break
        shown = list(errors)
        h0 = (st.get("history") or [None])[0]
        if h0 and h0.get("outcome") == "failed" and not st.get("relay") and now - (h0.get("ended") or 0) < 300:
            shown.append("Relay start fail: %s - %s" % ((h0.get("title") or "")[:40], (h0.get("note") or "")[:90]))
        if st.get("pending_end") and not st.get("relay"):
            shown.insert(0, "Purana YouTube broadcast end nahi ho paya - naya match tab tak hold par hai (purane video me mix na ho).")
        if AUTH["dead"]:
            shown.insert(0, AUTH_MSG)
        st.update(live=live, errors=shown, heartbeat=int(now),
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


# ── relay 2: second parallel relay on the SAME channel (manual only, own job/runner/state) ──────────
def _mk_call():
    tok = _oauth_token()
    if not tok:
        return None
    H = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}

    def call(method, path, params, body=None):
        url = _API + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers=H, method=method)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    return call


def resolve_rtmp2(slot=None):
    """-> (rtmp url, stream key). YT_STREAM_KEY_<slot> secret if set, else a reusable stream titled 'YTM Relay <slot>' on our channel
    (created once through the API with the existing OAuth secrets; its key is never printed)."""
    base = os.environ.get("YT_RTMP_BASE", "rtmp://a.rtmp.youtube.com/live2").rstrip("/")
    slot = slot or SLOT
    title = "YTM Relay " + slot
    key = os.environ.get("YT_STREAM_KEY" + _sfx(slot), "").strip()
    if key:
        return base + "/" + key, key
    call = _mk_call()
    if not call:
        return None, None
    ls = call("GET", "liveStreams", {"part": "snippet,cdn", "mine": "true", "maxResults": 50}).get("items", [])
    it = next((i for i in ls if (i.get("snippet") or {}).get("title") == title), None)
    if it is None:
        it = call("POST", "liveStreams", {"part": "snippet,cdn,contentDetails,status"},
                  {"snippet": {"title": title},
                   "cdn": {"frameRate": "variable", "ingestionType": "rtmp", "resolution": "variable"},
                   "contentDetails": {"isReusable": True}})
        log("relay %s: created reusable stream '%s'" % (slot, title))
    ing = it["cdn"]["ingestionInfo"]
    return ing["ingestionAddress"].rstrip("/") + "/" + ing["streamName"], ing["streamName"]


def _clear_relay2(seq, slot=None):
    """relay 2 is over: drop the request from the config (only if it is still OUR request), so relay 1 may pick that match again."""
    rk = "relay" + (slot or SLOT)
    try:
        cfg, sha = read_json(CFG_PATH)
        if cfg and (cfg.get(rk) or {}).get("seq") == seq:
            cfg[rk] = None
            write_json(CFG_PATH, cfg, sha, "ytm %s done" % rk)
    except Exception as e:
        log("clear relay2: %s" % str(e)[:100])


def gate2(slot=None):
    out = "false"
    try:
        cfg, _ = read_json(CFG_PATH)
        if cfg and (cfg.get("relay" + (slot or SLOT)) or {}).get("video_id"):
            out = "true"
    except Exception as e:
        log("gate2 error: %s" % e)
    log("gate%s go=%s" % (slot or SLOT, out))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("go=%s\n" % out)
    return 0


def run2(_sleep=time.sleep, max_loops=None, slot=None):
    """One manually chosen live -> second stream of the same channel. Ends when the match ends, the dashboard stops it
    (cfg.relay2 removed / replaced) or the 6h job limit hits. No auto-pick, no failover, no chaining."""
    slot = slot or SLOT
    rk, stp = "relay" + slot, _st_path(slot)
    cfg, _ = read_json(CFG_PATH)
    cfg = cfg or {}
    fb_cfg_apply(cfg)                                       # dashboard platform switches BEFORE the first ffmpeg spawn
    r2 = cfg.get(rk) or {}
    if not r2.get("video_id"):
        log("relay%s: no request -> exit" % slot)
        return 0
    seq = r2.get("seq")
    st, st_sha = read_json(stp)
    st = st or {}
    st["fb_key"] = fb_key_set()
    st.update(slot=int(slot), run_started=int(time.time()), status="starting", error="")
    if st.get("relay"):                                    # interrupted previous run
        st.setdefault("history", []).insert(0, {**{k: st["relay"].get(k) for k in ("video_id", "title", "channel", "started")},
                                                "ended": int(time.time()), "outcome": "interrupted", "note": "job restarted"})
        st["relay"] = None

    def save(msg="ytm %s state" % rk):
        nonlocal st_sha
        try:
            st_sha = write_json(stp, st, st_sha, msg)
        except Exception as e:
            log("%s state write failed: %s" % (rk, e))

    for other in ("1", "2", "3"):                          # another relay already carries this match -> never double-stream it
        if other == slot:
            continue
        so, _ = read_json(_st_path(other))
        if ((so or {}).get("relay") or {}).get("video_id") == r2["video_id"] and (so or {}).get("status") in ("relaying", "starting"):
            log("%s: relay %s already carries this match -> exit" % (rk, other))
            st.update(status="idle", error="Relay %s pe ye match pehle se chal raha hai" % other, heartbeat=0)
            save()
            _clear_relay2(seq, slot)
            return 0
    cap = int(cfg.get("daily_cap_h", 20)) * 60
    if minutes_today(st) >= cap:
        log("%s: daily cap reached -> exit" % rk)
        st.update(status="idle", error="Relay %s ka daily cap poora ho gaya" % slot, heartbeat=0)
        save()
        _clear_relay2(seq, slot)
        return 0
    try:
        rtmp, key = resolve_rtmp2(slot)
    except Exception as e:
        rtmp, key = None, None
        log("%s stream setup failed: %s" % (rk, str(e)[:160]))
    if not rtmp:
        st.update(status="idle", error="Relay %s stream nahi ban paya (OAuth secrets / YT_STREAM_KEY%s check karo)" % (slot, _sfx(slot)), heartbeat=0)
        save()
        _clear_relay2(seq, slot)
        return 1
    print("::add-mask::" + key, flush=True)                 # public repo: keep the key out of the Actions log
    os.environ["YT_STREAM_KEY" + _sfx(slot)] = key
    signal.signal(signal.SIGTERM, lambda *a: _stop.update(flag=True))
    relay = Relay(st, rtmp)
    relay.manual = True
    det = Detector(st)
    relay.verify = det.is_live
    c = {"video_id": r2["video_id"], "title": r2.get("title") or "", "channel": r2.get("channel") or "",
         "handle": r2.get("handle") or "", "channel_id": r2.get("channel_id") or "",
         "url": r2.get("url") or "https://www.youtube.com/watch?v=" + r2["video_id"]}
    t0 = time.time()
    deadline = t0 + MAX_RUN_MIN * 60
    chain_at = t0 + max(60, MAX_RUN_MIN - 40) * 60          # queue the successor ~40 min before the 6h limit (like relay 1)
    queued = False
    started = False
    last_try = last_cfg = last_write = 0
    loops = 0
    reason = "match ended"
    while time.time() < deadline and not _stop["flag"]:
        now = time.time()
        if not st.get("relay"):
            if started:
                break                                       # relay finished by itself (source ended / failed)
            if now - t0 > 600:
                reason = "not live after 10 min"
                relay._record_fail(c, reason)
                break
            if now - last_try >= 20:
                last_try = now
                h = st.setdefault("history", [])
                n0 = len(h)
                relay.start(c)
                started = bool(st.get("relay"))
                if len(h) > n0 and len(h) > 1 and h[1].get("outcome") == "failed" and h[1].get("video_id") == c["video_id"]:
                    h.pop(0)                                # same probe failure again: do not spam the history
        else:
            relay.poll()
            relay.caption_tick(now)
        if now - last_cfg >= CFG_EVERY:
            last_cfg = now
            try:
                c2 = read_json(CFG_PATH)[0] or {}
                fb_cfg_apply(c2)
                relay.fb_sync()
                cur = c2.get(rk) or {}
                cap = int(c2.get("daily_cap_h", cap // 60)) * 60
            except Exception:
                cur = {"seq": seq}                          # GitHub hiccup: keep going
            if cur.get("seq") != seq:
                reason = "stopped"
                relay.stop("stopped", "relay %s stop" % slot)
                break
        if st.get("relay") and minutes_today(st, now) >= cap:
            reason = "Relay %s ka daily cap poora ho gaya" % slot
            relay.stop("stopped", "daily cap")
            break
        if st.get("relay") and not queued and now >= chain_at and os.environ.get("GITHUB_ACTIONS"):
            queued = True
            ensure_next_queued(_wf_file(slot))               # waits behind this run (concurrency group), takes over at the limit
        st.update(status="relaying" if st.get("relay") else "starting", heartbeat=int(now))
        if now - last_write >= HEARTBEAT or loops % 6 == 0:
            last_write = now
            save()
        loops += 1
        if max_loops and loops >= max_loops:
            break
        _sleep(5)
    chained = False
    if st.get("relay"):
        if time.time() >= deadline and not _stop["flag"] and reason == "match ended":
            relay.stop("interrupted", "job ended, next run resumes")   # broadcast stays up, next run carries on (gap ~1-2 min)
            chained = True
        else:
            relay.stop("ended", "cancelled")
    st.update(status="idle", heartbeat=0, error="" if reason in ("match ended", "stopped") else reason)
    save("ytm %s final" % rk)
    if chained:
        if os.environ.get("GITHUB_ACTIONS"):
            ensure_next_queued(_wf_file(slot))              # no-op if the successor is already waiting
    else:
        _clear_relay2(seq, slot)
    return 0


def skip_hit(c, skip):
    """True if live match c is a cricket match, i.e. matches an auto_skip word in its title OR channel name OR handle (a Marathi/Hindi title may not say
    'cricket' at all, but the channel does). 'cricket' also covers the Hindi/Marathi spelling."""
    if not skip:
        return False
    words = list(skip) + (["क्रिकेट"] if "cricket" in skip else [])
    text = " ".join(str(c.get(k) or "") for k in ("title", "channel", "handle")).lower()
    return any(w in text for w in words)


def auto_relay2(cfg, live, st, now, relay=None):
    """Relay 2 auto (same rules as Relay 1 auto): while Relay mode is Auto and cfg.r2_auto is on (default), hand relay 2 the next live
    match that matches auto_skip (cricket words; with an empty auto_skip any match qualifies), is not carried by relay 1/3 and was not
    tried in the last 30 min. Relay 1 always picks first (channel priority), so relay 2 gets the NEXT cricket match. Writes cfg.relay2
    (same request relay 2's dashboard button writes) and dispatches yt-mirror2.yml. Relay 3 auto takes the non-cricket ones."""
    if cfg.get("mode") == "manual" or not cfg.get("r2_auto", True):
        return
    if (cfg.get("relay2") or {}).get("video_id"):
        return
    skip = [k.strip().lower() for k in (cfg.get("auto_skip") or "").split(",") if k.strip()]
    taken = {(st.get("relay") or {}).get("video_id"), (cfg.get("relay3") or {}).get("video_id")} - {None}
    tried = st.setdefault("r2_tried", {})
    for v in [v for v, t in tried.items() if now - t > 1800]:
        tried.pop(v, None)
    for c in live:
        v = c["video_id"]
        if v in taken or v in tried or (skip and not skip_hit(c, skip)):
            continue
        if relay is not None and relay._blocked(v, now):
            continue
        tried[v] = now
        fresh, sha = read_json(CFG_PATH)
        if not fresh or (fresh.get("relay2") or {}).get("video_id"):
            return
        if v in {(fresh.get("relay3") or {}).get("video_id")}:      # relay 3 grabbed it between our read and now
            continue
        fresh["relay2"] = {"video_id": v, "title": c.get("title", ""), "channel": c.get("channel", ""), "handle": c.get("handle", ""),
                           "channel_id": c.get("channel_id", ""), "url": c.get("url", ""), "seq": int(now * 1000), "auto": True}
        write_json(CFG_PATH, fresh, sha, "ytm relay2 auto")
        log("relay2 auto: %s" % c.get("title", v)[:80])
        if os.environ.get("GITHUB_ACTIONS"):
            try:
                gh("POST", "actions/workflows/%s/dispatches" % _wf_file("2"), {"ref": "main"})
            except Exception as e:
                log("relay2 dispatch failed: %s" % str(e)[:100])
        return


def auto_relay3(cfg, live, st, now, relay=None):
    """Relay 3 auto: while Relay mode is Auto and cfg.r3_auto is on (default), hand relay 3 the next live match that is NOT in
    auto_skip (cricket), not carried by relay 1/2 and not tried in the last 30 min. Relay 1 always picks first (channel priority);
    this runs after it. Writes cfg.relay3 (same request relay 3's dashboard button writes) and dispatches yt-mirror3.yml."""
    if cfg.get("mode") == "manual" or not cfg.get("r3_auto", True):
        return
    if (cfg.get("relay3") or {}).get("video_id"):
        return
    skip = [k.strip().lower() for k in (cfg.get("auto_skip") or "").split(",") if k.strip()]
    taken = {(st.get("relay") or {}).get("video_id"), (cfg.get("relay2") or {}).get("video_id")} - {None}
    tried = st.setdefault("r3_tried", {})
    for v in [v for v, t in tried.items() if now - t > 1800]:
        tried.pop(v, None)
    for c in live:
        v = c["video_id"]
        if v in taken or v in tried or skip_hit(c, skip):
            continue
        if relay is not None and relay._blocked(v, now):
            continue
        tried[v] = now
        fresh, sha = read_json(CFG_PATH)
        if not fresh or (fresh.get("relay3") or {}).get("video_id"):
            return
        if v in {(fresh.get("relay2") or {}).get("video_id")}:      # relay 2 auto grabbed it between our read and now
            continue
        fresh["relay3"] = {"video_id": v, "title": c.get("title", ""), "channel": c.get("channel", ""), "handle": c.get("handle", ""),
                           "channel_id": c.get("channel_id", ""), "url": c.get("url", ""), "seq": int(now * 1000), "auto": True}
        write_json(CFG_PATH, fresh, sha, "ytm relay3 auto")
        log("relay3 auto: %s" % c.get("title", v)[:80])
        if os.environ.get("GITHUB_ACTIONS"):
            try:
                gh("POST", "actions/workflows/%s/dispatches" % _wf_file("3"), {"ref": "main"})
            except Exception as e:
                log("relay3 dispatch failed: %s" % str(e)[:100])
        return


def cfg_poll(st):
    return max(30, min(300, int(st.get("poll_secs", 60))))


if __name__ == "__main__":
    _cmd = (sys.argv[1:] or ["run"])[0]
    if _cmd == "endlive":                       # one-off: complete any ACTIVE broadcast on our channel
        print("endlive:", end_broadcast())
        sys.exit(0)
    if _cmd in ("gate2", "gate3"):
        SLOT = _cmd[-1]
        sys.exit(gate2(SLOT))
    if _cmd in ("run2", "run3"):
        SLOT = _cmd[-1]
        FRAME_FILE = "ytm_frame%s.jpg" % SLOT
        sys.exit(run2(slot=SLOT))
    sys.exit(gate() if _cmd == "gate" else run())
