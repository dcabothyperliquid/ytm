"""
Review layer for the highlights maker (runs on GitHub Actions only).

  hl_config.json  (branch ytm-data)  <- written by the dashboard proxy: daily targets, auto flag, reviews, commands
  hl_state.json   (branch ytm-data)  -> written HERE: matches, shorts, preview URLs, applied command id

Previews (small 360-480p) are published as a PUBLIC pre-release `hlp-<vid>` so the dashboard can play them
straight from GitHub (nothing streams through Render). Full-quality files stay in the DRAFT release `hl-<vid>`.
"""
import base64
import glob
import json
import os
import shutil
import re
import subprocess
import time

BRANCH = "ytm-data"
CFG, ST = "hl_config.json", "hl_state.json"
IST = 19800
KEEP_DAYS = 7
MAX_MATCHES = 20
FMT_H = int(os.environ.get("HL_MAX_H", "1080"))


def log(*a):
    print("[hl-review]", *a, flush=True)


def repo():
    return os.environ.get("GITHUB_REPOSITORY", "dcabothyperliquid/ytm")


def sh(cmd, inp=None, check=True):
    r = subprocess.run(cmd, input=inp, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError("%s failed (%d): %s" % (cmd[0], r.returncode, (r.stderr or r.stdout)[-500:]))
    return r


def api(method, path, body=None, check=True):
    cmd = ["gh", "api", "-X", method, "repos/%s/%s" % (repo(), path)]
    if body is not None:
        cmd += ["--input", "-"]
    return sh(cmd, inp=json.dumps(body) if body is not None else None, check=check)


# ---------------------------------------------------------------- json files on ytm-data
def read_file(path):
    r = api("GET", "contents/%s?ref=%s" % (path, BRANCH), check=False)
    if r.returncode:
        return None, None
    d = json.loads(r.stdout)
    return json.loads(base64.b64decode(d["content"]).decode() or "null"), d["sha"]


def write_file(path, obj, sha, msg):
    body = {"message": msg, "branch": BRANCH,
            "content": base64.b64encode(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()).decode()}
    if sha:
        body["sha"] = sha
    return json.loads(api("PUT", "contents/" + path, body).stdout)["content"]["sha"]


def load_cfg():
    c, _ = read_file(CFG)
    return dict({"daily_shorts": 5, "daily_highlights": 2, "auto": True, "commands": [], "reviews": {}}, **(c or {}))


def load_state():
    s, sha = read_file(ST)
    return dict({"matches": [], "applied": 0, "worker": {}}, **(s or {})), sha


def save_state(mutator, msg="hl state"):
    """read-modify-write (retry once on a stale sha)."""
    for _ in range(2):
        st, sha = load_state()
        mutator(st)
        st["updated"] = int(time.time())
        try:
            return write_file(ST, st, sha, msg)
        except RuntimeError as e:
            log("state write retry:", e)
    raise RuntimeError("hl_state write failed")


# ---------------------------------------------------------------- live status (hl_live.json)
# Separate small file so frequent progress writes never collide with hl_state's read-modify-write.
LIVE = "hl_live.json"
_L = {"sha": None, "phase": None, "last": 0.0, "started": 0.0}


def set_live(phase, detail="", vid="", title="", cur=None, total=None, force=False):
    """Dashboard 'kya ho raha hai' feed. phase: waiting_vod|download|audio|transcribe|stories|cut|shorts|release|preview|
    applying|uploading|done|idle|error. Throttled (same phase: max 1 write / 25s). Never raises."""
    try:
        now = time.time()
        if not force and phase == _L["phase"] and now - _L["last"] < 25:
            return
        if phase not in ("idle", "done", "error") and not _L["started"]:
            _L["started"] = now
        obj = {"phase": phase, "detail": str(detail)[:200], "vid": vid or _L.get("vid", ""),
               "title": str(title or _L.get("title", ""))[:120], "cur": cur, "total": total,
               "run_id": os.environ.get("GITHUB_RUN_ID", ""), "started": int(_L["started"] or now),
               "at": int(now)}
        _L["vid"], _L["title"] = obj["vid"], obj["title"]
        for attempt in range(2):
            try:
                if _L["sha"] is None or attempt:
                    _L["sha"] = (read_file(LIVE)[1])
                _L["sha"] = write_file(LIVE, obj, _L["sha"], "hl live %s" % phase)
                break
            except Exception as e:                   # noqa
                _L["sha"] = None
                log("live write retry:", e)
        _L["phase"], _L["last"] = phase, now
        if phase in ("idle", "done", "error"):
            _L["started"] = 0.0
    except Exception as e:                           # noqa -- status must never break a run
        log("set_live failed:", e)


# ---------------------------------------------------------------- releases / assets
def release_id(tag):
    r = sh(["gh", "api", "repos/%s/releases" % repo(), "--paginate", "-q",
            '.[] | select(.tag_name=="%s") | .id' % tag], check=False)
    ids = [x for x in (r.stdout or "").split() if x.isdigit()]
    return int(ids[0]) if ids else None


def delete_asset_named(rel_id, name):
    r = sh(["gh", "api", "repos/%s/releases/%s/assets" % (repo(), rel_id), "--paginate", "-q",
            '.[] | select(.name=="%s") | .id' % name], check=False)
    for aid in (r.stdout or "").split():
        sh(["gh", "api", "-X", "DELETE", "repos/%s/releases/assets/%s" % (repo(), aid)], check=False)


def upload_asset(rel_id, path, name=None):
    name = name or os.path.basename(path)
    delete_asset_named(rel_id, name)
    ctype = "video/mp4" if name.endswith(".mp4") else ("image/jpeg" if name.endswith(".jpg") else "text/plain")
    sh(["curl", "-sS", "--fail", "-X", "POST", "-H", "Authorization: Bearer " + os.environ["GH_TOKEN"],
        "-H", "Content-Type: " + ctype, "--data-binary", "@" + path,
        "https://uploads.github.com/repos/%s/releases/%s/assets?name=%s" % (repo(), rel_id, name)])


def key_sfx(match=None):
    """one VOD can hold several matches (Deoria = 10, Kaushambi = 11): their releases / state entries must not overwrite each other."""
    m = (os.environ.get("HL_MATCH", "") if match is None else str(match or "")).strip()
    return ("-m" + re.sub(r"\W", "", m)) if m else ""


def preview_url(vid, name, match=None):
    return "https://github.com/%s/releases/download/hlp-%s%s/%s" % (repo(), vid, key_sfx(match), name)


def make_preview(src, out, height=480, crf=32):
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vf", "scale=-2:%d,fps=24" % height,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart", out])


# ---------------------------------------------------------------- publish one processed match
def match_score(ranked):
    top = sorted((m["score"] for m in ranked), reverse=True)
    if not top:
        return 0.0
    strong = len([s for s in top if s >= 0.5])
    return round(0.7 * sum(top[:3]) / min(3, len(top)) + 0.3 * min(strong / 10.0, 1.0), 3)


def publish_match(vid, title, outdir, res, tag_full="hl-", window=None):
    """res = hl_runner.process() result. Creates the public preview release, then records the match in hl_state."""
    prev = os.path.join(outdir, "_prev")
    os.makedirs(prev, exist_ok=True)
    files = []
    hl_prev = os.path.join(prev, "highlights_p.mp4")
    make_preview(os.path.join(outdir, res["highlight"]["file"]), hl_prev, height=360, crf=34)
    files.append(hl_prev)
    bn_url = None
    bn_src = os.path.join(outdir, res["highlight"].get("banner") or "banner.jpg")
    if res["highlight"].get("banner") and os.path.isfile(bn_src):      # banner goes to the public preview release too -> dashboard can show it
        bn_p = os.path.join(prev, "banner.jpg")
        shutil.copyfile(bn_src, bn_p)
        files.append(bn_p)
        bn_url = preview_url(vid, "banner.jpg")
    for s in res["shorts"]:
        s["preview_name"] = s["id"] + "_p.mp4"
        p = os.path.join(prev, s["preview_name"])
        make_preview(os.path.join(outdir, s["file"]), p, height=480, crf=33)
        files.append(p)
    tag = "hlp-" + vid + key_sfx()
    sh(["gh", "release", "delete", tag, "-R", repo(), "--cleanup-tag", "-y"], check=False)
    sh(["gh", "release", "create", tag, "-R", repo(), "--prerelease", "--target", "main",
        "--title", "Preview %s" % vid, "--notes", "low-res previews for the dashboard"] + files)
    now = int(time.time())
    entry = {"vid": vid, "title": title, "processed": now, "day": time.strftime("%Y-%m-%d", time.gmtime(now + IST)),
             "dur_min": round(res["dur"] / 60.0, 1), "score": match_score(res["ranked"]),
             "finish": res.get("finish", 0.0), "window": list(window) if window else None,
             "release_id": release_id(tag_full + vid + key_sfx()), "prev_release_id": release_id(tag),
             "match": os.environ.get("HL_MATCH", "").strip() or None,
             "highlight": {"file": res["highlight"]["file"], "dur": res["highlight"]["dur"],
                           "chapters": res["highlight"]["chapters"],
                           "banner": res["highlight"].get("banner"), "yt_caption": res["highlight"].get("yt_caption"),
                           "fb_caption": res["highlight"].get("fb_caption"), "banner_url": bn_url,
                           "preview": preview_url(vid, "highlights_p.mp4")},
             "shorts": [{k: s[k] for k in ("id", "file", "event", "score", "start", "end", "title", "heard", "preview_name", "label") if k in s}
                        | {"preview": preview_url(vid, s["preview_name"])} for s in res["shorts"]]}

    def mut(st):
        st["matches"] = [entry] + [m for m in st["matches"] if not (m["vid"] == vid and (m.get("match") or None) == entry["match"])]
        st["matches"] = st["matches"][:MAX_MATCHES]
    save_state(mut, "hl match %s" % vid)
    log("state updated for", vid)


def cleanup_old():
    cutoff = time.time() - KEEP_DAYS * 86400
    r = sh(["gh", "api", "repos/%s/releases" % repo(), "--paginate", "-q",
            '.[] | select(.tag_name|startswith("hlp-")) | {tag_name,created_at}'], check=False)
    for line in (r.stdout or "").splitlines():
        try:
            d = json.loads(line)
            if time.mktime(time.strptime(d["created_at"], "%Y-%m-%dT%H:%M:%SZ")) < cutoff:
                sh(["gh", "release", "delete", d["tag_name"], "-R", repo(), "--cleanup-tag", "-y"], check=False)
        except (ValueError, KeyError):
            pass

    def mut(st):
        st["matches"] = [m for m in st["matches"] if m.get("processed", 0) >= cutoff]
    save_state(mut, "hl cleanup")


# ---------------------------------------------------------------- commands from the dashboard
def pending_commands():
    cfg, (st, _) = load_cfg(), load_state()
    return sorted([c for c in cfg.get("commands", []) if c.get("id", 0) > st.get("applied", 0)], key=lambda c: c["id"])


def _mark(cid, err=None):
    def mut(st):
        st["applied"] = max(st.get("applied", 0), cid)
        st["worker"] = {"last_cmd": cid, "error": err or "", "at": int(time.time())}
    save_state(mut, "hl cmd %s" % cid)


def do_edit(H, c):
    st, _ = load_state()
    m = next((x for x in st["matches"] if x["vid"] == c["vid"]), None)
    s = next((x for x in (m or {}).get("shorts", []) if x["id"] == c.get("sid")), None)
    if not s:
        raise RuntimeError("short not found (older than %d days?)" % KEEP_DAYS)
    start, end = float(c["start"]), float(c["end"])
    end = min(end, start + 59.0)
    if end - start < 5:
        raise RuntimeError("clip too short (min 5s)")
    pad = 3.0
    a = max(0.0, start - pad)
    for f in glob.glob(os.path.join(H.WORK, "sec.*")):
        os.remove(f)
    os.makedirs(H.WORK, exist_ok=True)
    full = H.ensure_full(c["vid"])                  # whole VOD once per job (cached), then an exact stream-copy trim
    sec = os.path.join(H.WORK, "sec.mp4")
    a = H.trim(full, a, end + pad, sec)             # a = real keyframe start of the trimmed file
    if not os.path.exists(sec):
        raise RuntimeError("section trim failed")
    base = s["id"].split("_e")[0]
    nid = "%s_e%d" % (base, c["id"] % 100000)
    out = os.path.join(H.WORK, nid + ".mp4")
    pout = os.path.join(H.WORK, nid + "_p.mp4")
    H.cut(sec, start - a, end - a, out, vertical=True, label=s.get("label") or H.overlay_label(s.get("event", "")))
    make_preview(out, pout, height=480, crf=33)
    upload_asset(m["release_id"], out)
    upload_asset(m["prev_release_id"], pout)
    delete_asset_named(m["release_id"], s["file"])
    delete_asset_named(m["prev_release_id"], s["preview_name"])
    title = (c.get("title") or s["title"])[:100]

    def mut(st2):
        for mm in st2["matches"]:
            if mm["vid"] == c["vid"]:
                for ss in mm["shorts"]:
                    if ss["id"] == s["id"]:
                        ss.update(id=nid, file=nid + ".mp4", preview_name=nid + "_p.mp4", start=round(start, 1),
                                  end=round(end, 1), title=title, preview=preview_url(c["vid"], nid + "_p.mp4", c.get("match") or ""), edited=True)
    save_state(mut, "hl edit %s" % nid)
    for f in (sec, out, pout):
        if os.path.exists(f):
            os.remove(f)



def _scout_status(vid, status, **extra):
    def mut(st):
        for sc in st.get("scouts", []):
            if sc["vid"] == vid:
                sc["status"] = status
                sc.update(extra)
    save_state(mut, "hl scout %s %s" % (vid, status))


def do_confirm(H, c):
    """Dashboard confirmed (and maybe corrected) the match window -> NOW make highlights, only from [start, end]."""
    start, end = float(c["start"]), float(c["end"])
    if end - start < 10 * 60:
        raise RuntimeError("match window bahut chhota (%d s) — start/end check karo" % (end - start))
    _scout_status(c["vid"], "making", start=round(start, 1), end=round(end, 1), confirmed=int(time.time()))
    try:
        H.run_vid(c["vid"], window=(start, end))
    except Exception:
        _scout_status(c["vid"], "pending")          # back to pending so it can be retried from the dashboard
        raise
    _scout_status(c["vid"], "done")


def apply_commands(H):
    n = 0
    cmds = pending_commands()
    for c in cmds:
        n += 1
        set_live("applying", "%s command" % c.get("type"), vid=c.get("vid", ""), cur=n, total=len(cmds), force=True)
        try:
            H.note("applying %s on %s" % (c.get("type"), c.get("vid")))
            if c["type"] == "edit":
                do_edit(H, c)
            elif c["type"] == "rerun":
                m = next((x for x in load_state()[0]["matches"] if x["vid"] == c["vid"]), None)
                H.run_vid(c["vid"], float(c.get("target_min") or H.TARGET_MIN), window=(m or {}).get("window"))
            elif c["type"] == "confirm":
                do_confirm(H, c)
            elif c["type"] == "fb_backfill":
                do_fb_backfill(c)
            elif c["type"] == "skip":
                _scout_status(c["vid"], "skipped")
            elif c["type"] == "rescout":
                _scout_status(c["vid"], "rescouting")
                import hl_scout
                hl_scout.scout_vid(H, c["vid"], H.sh(H.ytdlp_base() + ["--no-playlist", "--print", "%(title)s",
                                                                      "https://www.youtube.com/watch?v=" + c["vid"]], check=False).stdout.strip())
            else:
                raise RuntimeError("unknown command %s" % c.get("type"))
            _mark(c["id"])
        except Exception as e:                       # noqa
            H.note("command %s failed: %s" % (c["id"], e), "error", "hl cmd")
            _mark(c["id"], str(e))
    log("commands applied:", n)
    return n


# ---------------------------------------------------------------- approved -> YouTube upload
def _oauth_token():
    import urllib.parse
    import urllib.request
    cid, sec, rt = (os.environ.get(k, "").strip() for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))
    if not (cid and sec and rt):
        raise RuntimeError("YT_CLIENT_ID/YT_CLIENT_SECRET/YT_REFRESH_TOKEN secrets missing")
    data = urllib.parse.urlencode({"client_id": cid, "client_secret": sec, "refresh_token": rt,
                                   "grant_type": "refresh_token"}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=data), timeout=30) as r:
        return json.loads(r.read())["access_token"]


def _download_asset(rel_id, name, dest):
    r = sh(["gh", "api", "repos/%s/releases/%s/assets" % (repo(), rel_id), "--paginate", "-q",
            '.[] | select(.name=="%s") | .id' % name], check=False)
    ids = [x for x in (r.stdout or "").split() if x.isdigit()]
    if not ids:
        raise RuntimeError("file %s not found in release (older than %d days?)" % (name, KEEP_DAYS))
    with open(dest, "wb") as f:
        p = subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                            "repos/%s/releases/assets/%s" % (repo(), ids[0])], stdout=f, stderr=subprocess.PIPE)
    if p.returncode or os.path.getsize(dest) < 10000:
        raise RuntimeError("asset download failed: %s" % (p.stderr or b"")[-300:].decode("utf-8", "ignore"))


def _fmt_ts(sec):
    sec = int(sec)
    return "%d:%02d" % (sec // 60, sec % 60) if sec < 3600 else "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


def _youtube_upload(path, title, desc, privacy, tags):
    tok = _oauth_token()
    meta = {"snippet": {"title": title[:100], "description": desc[:4900], "tags": tags[:15], "categoryId": "17"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    size = os.path.getsize(path)
    r = sh(["curl", "-sS", "-i", "-X", "POST",
            "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
            "-H", "Authorization: Bearer " + tok, "-H", "Content-Type: application/json; charset=UTF-8",
            "-H", "X-Upload-Content-Type: video/mp4", "-H", "X-Upload-Content-Length: %d" % size,
            "-d", json.dumps(meta)])
    head = r.stdout
    m = re.search(r"(?im)^location:\s*(\S+)", head)
    if not m:
        raise RuntimeError("upload init failed: %s" % head[-400:].replace(tok, "***"))
    r = sh(["curl", "-sS", "--fail-with-body", "-X", "PUT", m.group(1), "-H", "Content-Type: video/mp4",
            "-H", "Content-Length: %d" % size, "--data-binary", "@" + path, "--max-time", "1500"], check=False)
    if r.returncode:
        raise RuntimeError("upload failed: %s" % (r.stdout or r.stderr)[-400:])
    return json.loads(r.stdout)["id"]


# ---------------------------------------------------------------- Facebook Page (LML) upload
G_API = "https://graph.facebook.com/v21.0"


def _fb_creds():
    return (os.environ.get("LML_FB_PAGE_ID", "").strip(), os.environ.get("LML_FB_PAGE_TOKEN", "").strip())


def _fb_enabled(cfg=None):
    """Dashboard master switch (cfg.fb_post, default ON) + credentials present."""
    cfg = cfg if cfg is not None else load_cfg()
    page, tok = _fb_creds()
    return bool(page and tok and cfg.get("fb_post", True))


def _fb_call(args, tok):
    r = sh(["curl", "-sS", "--max-time", "1500"] + args, check=False)
    try:
        j = json.loads(r.stdout or "{}")
    except Exception:                                   # noqa
        raise RuntimeError("FB bad response: %s" % ((r.stdout or r.stderr or "")[-300:]).replace(tok, "***"))
    if isinstance(j, dict) and j.get("error"):
        raise RuntimeError("FB error: %s" % json.dumps(j["error"])[:300].replace(tok, "***"))
    return j


def _fb_upload(path, kind, title, desc):
    """kind = 'short' -> Facebook Reel (vertical, <=90s); 'highlight' -> normal Page video. Returns the FB video id."""
    page, tok = _fb_creds()
    if not (page and tok):
        return None
    size = os.path.getsize(path)
    if kind == "short":
        j = _fb_call(["-X", "POST", "%s/%s/video_reels" % (G_API, page), "-d", "upload_phase=start",
                      "-d", "access_token=" + tok], tok)
        vid, up = j.get("video_id"), j.get("upload_url")
        if not (vid and up):
            raise RuntimeError("FB reel start failed: %s" % json.dumps(j)[:200])
        j = _fb_call(["-X", "POST", up, "-H", "Authorization: OAuth " + tok, "-H", "offset: 0",
                      "-H", "file_size: %d" % size, "--data-binary", "@" + path], tok)
        if not j.get("success"):
            raise RuntimeError("FB reel upload failed: %s" % json.dumps(j)[:200])
        j = _fb_call(["-X", "POST", "%s/%s/video_reels" % (G_API, page), "-d", "upload_phase=finish",
                      "-d", "video_id=" + vid, "-d", "video_state=PUBLISHED",
                      "--data-urlencode", "description=" + desc[:2000], "-d", "access_token=" + tok], tok)
        if not j.get("success"):
            raise RuntimeError("FB reel publish failed: %s" % json.dumps(j)[:200])
        return vid
    if size > 1000 * 1024 * 1024:
        raise RuntimeError("FB highlight file %d MB > 1 GB (simple upload limit)" % (size // 1048576))
    j = _fb_call(["-X", "POST", "https://graph-video.facebook.com/v21.0/%s/videos" % page,
                  "-F", "source=@" + path, "-F", "title=" + title[:200], "-F", "description=" + desc[:5000],
                  "-F", "access_token=" + tok], tok)
    if not j.get("id"):
        raise RuntimeError("FB video upload failed: %s" % json.dumps(j)[:200])
    return j["id"]


def _fb_status(fb_id):
    """-> ('ready'|'processing'|'error', detail). Facebook accepts a file first and processes/publishes it later."""
    _, tok = _fb_creds()
    try:
        j = _fb_call(["-G", "%s/%s" % (G_API, fb_id), "--data-urlencode", "fields=status", "-d", "access_token=" + tok], tok)
    except Exception as e:                              # noqa
        return "processing", str(e)[:120]
    stt = j.get("status") or {}
    vs = (stt.get("video_status") or "").lower()
    ph = ((stt.get("publishing_phase") or {}).get("status") or "").lower()
    if vs == "error" or ph == "error":
        det = json.dumps(stt.get("processing_phase") or stt.get("uploading_phase") or stt)[:160]
        return "error", det
    if vs == "ready" and ph in ("", "complete", "published"):
        return "ready", ""
    return "processing", vs or ph


def _fb_wait(fb_id, tries=6, delay=10):
    stt, det = "processing", ""
    for i in range(tries):
        stt, det = _fb_status(fb_id)
        if stt != "processing":
            break
        time.sleep(delay)
    return stt, det


def _fb_link(fb_id, kind):
    return ("https://www.facebook.com/reel/%s" if kind == "short" else "https://www.facebook.com/watch/?v=%s") % fb_id


def _fb_target(cfg, vid, sid):
    t = (cfg.get("targets") or {}).get("%s/%s" % (vid, sid), "both")
    return t if t in ("both", "yt", "fb") else "both"


def _fb_post_item(path, kind, title, desc):
    """Upload to Facebook and wait for processing. Returns (record|None, error|None)."""
    try:
        fid = _fb_upload(path, kind, title, desc)
        if not fid:
            return None, None
        stt, det = _fb_wait(fid)
        if stt == "error":
            return None, "FB processing failed: %s" % det
        return {"id": fid, "at": int(time.time()), "kind": kind, "link": _fb_link(fid, kind), "status": stt, "checks": 1}, None
    except Exception as e:                              # noqa
        return None, str(e)[:300]


def _item(st2, vid, sid):
    for mm in st2["matches"]:
        if mm["vid"] == vid:
            return mm["highlight"] if sid == "highlight" else next((x for x in mm.get("shorts", []) if x["id"] == sid), None)
    return None


def _hl_title(m):
    """FOMO-style highlight title (ytm_style); YTM_STYLE=off or any problem -> the old '<title> | Highlights'."""
    old = "%s | Highlights" % m["title"]
    if os.environ.get("YTM_STYLE", "on").lower() == "off":
        return old
    try:
        import ytm_style
        p = ytm_style.parse_match(m["title"])
        return ytm_style.highlight_title(m["title"], seed=m["vid"]) if (p["a"] and p["b"]) or p["tour"] else old
    except Exception:
        return old


def _hl_thumb(path, yid, m, it):
    """Highlights thumbnail: frame of the best moment in the cut (first six, else wicket, last over, else the middle)
    + the same banner style (HIGHLIGHTS badge). Best effort: any failure is only logged. Needs a phone-verified channel."""
    if os.environ.get("YTM_STYLE", "on").lower() == "off" or os.environ.get("YTM_BANNER", "on").lower() == "off":
        return
    try:
        import urllib.request
        import ytm_style
        ch = it.get("chapters") or []
        pick = None
        for want in (("six", "sixes"), ("wicket", "wickets"), ("hattrick",), ("finish",), ("catch",)):
            pick = next((t for t, ev in ch if ev in want), None)
            if pick is not None:
                break
        t = (pick + 3) if pick is not None else max(3, (it.get("dur") or 120) // 2)
        frame, out = "/tmp/hlthumb_frame.jpg", "/tmp/hlthumb.jpg"
        sh(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", path, "-frames:v", "1", "-q:v", "2", frame], check=False)
        ytm_style.make_banner(frame if os.path.isfile(frame) else None, m["title"], out, badge="HIGHLIGHTS", strip="WATCH ON:")
        req = urllib.request.Request("https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId=%s&uploadType=media" % yid,
                                     data=open(out, "rb").read(), method="POST",
                                     headers={"Authorization": "Bearer " + _oauth_token(), "Content-Type": "image/jpeg"})
        urllib.request.urlopen(req, timeout=40).read()
        log("highlight thumbnail set", yid)
    except Exception as e:                          # noqa
        log("highlight thumbnail failed (custom thumbnails need a phone-verified channel):", str(e)[:160])
    finally:
        for f in ("/tmp/hlthumb_frame.jpg", "/tmp/hlthumb.jpg"):
            if os.path.exists(f):
                os.remove(f)


def _set_ready_banner(m, it, yid):
    """Use the banner.jpg that was made with the highlight (release asset) as the YouTube thumbnail. False -> caller falls back."""
    if not (it.get("banner") or it.get("banner_url")):
        return False
    try:
        import urllib.request
        out = "/tmp/hlbanner_ready.jpg"
        if it.get("banner_url"):                    # banner kept on the ytm-data branch (public raw url)
            with open(out, "wb") as fh:
                fh.write(urllib.request.urlopen(it["banner_url"], timeout=40).read())
        else:
            _download_asset(m["release_id"], it["banner"], out)
        req = urllib.request.Request("https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId=%s&uploadType=media" % yid,
                                     data=open(out, "rb").read(), method="POST",
                                     headers={"Authorization": "Bearer " + _oauth_token(), "Content-Type": "image/jpeg"})
        urllib.request.urlopen(req, timeout=40).read()
        log("highlight banner set as thumbnail", yid)
        return True
    except Exception as e:                          # noqa
        log("ready banner failed, falling back to auto thumbnail:", str(e)[:160])
        return False
    finally:
        if os.path.exists("/tmp/hlbanner_ready.jpg"):
            os.remove("/tmp/hlbanner_ready.jpg")


def _fb_texts(m, sid, it):
    if sid == "highlight":
        t = _hl_title(m)
    else:
        t = it["title"].replace("#Shorts", "").replace("#shorts", "").strip()
    d = "%s\n\nFull match: https://www.youtube.com/watch?v=%s\n\n#cricket #highlights #LML" % (t, m["vid"])
    return t[:100], d


def upload_approved():
    """Upload every approved item once. YouTube and/or Facebook per item target (cfg.targets: both|yt|fb)."""
    cfg = load_cfg()
    privacy = cfg.get("privacy") or "public"
    rv = cfg.get("reviews") or {}
    fb_on = _fb_enabled(cfg)
    st, _ = load_state()
    todo = []
    for m in st["matches"]:
        items = [("highlight", m["highlight"])] + [(s["id"], s) for s in m.get("shorts", [])]
        for sid, it in items:
            if rv.get("%s/%s" % (m["vid"], sid), {}).get("status") != "approved" or it.get("uploaded"):
                continue
            tg = _fb_target(cfg, m["vid"], sid)
            if tg == "fb" and not fb_on:
                log("skip", sid, "(target FB only but Facebook is OFF/not connected)")
                continue
            todo.append((m, sid, it, tg))
    log("approved & not uploaded yet:", len(todo))
    os.makedirs("/tmp/hlup", exist_ok=True)
    done = 0
    for _i, (m, sid, it, tg) in enumerate(todo, 1):
        set_live("uploading", "Upload: %s (%s)" % (sid, {"both": "YouTube+FB", "yt": "YouTube", "fb": "Facebook"}[tg]),
                 vid=m["vid"], title=m.get("title", ""), cur=_i, total=len(todo), force=True)
        path = os.path.join("/tmp/hlup", it["file"])
        try:
            _download_asset(m["release_id"], it["file"], path)
            if sid == "highlight":
                yc = it.get("yt_caption") or {}
                title = (yc.get("title") or _hl_title(m))[:100]
                ch = it.get("chapters") or []
                desc = yc.get("description") or ("Match highlights.\n\n" + "\n".join("%s %s" % (_fmt_ts(t), ev) for t, ev in ch)
                                                 + "\n\nFull match: https://www.youtube.com/watch?v=%s" % m["vid"])
                tags = ["highlights", "live", "match"]
            else:
                title = it["title"] if "#shorts" in it["title"].lower() else (it["title"][:92] + " #Shorts")
                desc = "%s\n\nFull match: https://www.youtube.com/watch?v=%s\n#Shorts" % (it["title"], m["vid"])
                tags = ["shorts", "highlights", "live"]
            yid = ""
            if tg != "fb":
                yid = _youtube_upload(path, title, desc, privacy, tags)
                log("uploaded", sid, "->", yid)
                if sid == "highlight":
                    if not _set_ready_banner(m, it, yid):
                        _hl_thumb(path, yid, m, it)
            fb_rec, fb_err = None, None
            if tg != "yt" and fb_on:                    # Facebook is best-effort: never breaks the YouTube result
                ft, fd = _fb_texts(m, sid, it)
                if sid == "highlight" and it.get("fb_caption"):
                    ft = ((it.get("yt_caption") or {}).get("title") or ft)[:100]
                    fd = it["fb_caption"]
                fb_rec, fb_err = _fb_post_item(path, "highlight" if sid == "highlight" else "short", ft, fd)
                log("facebook", sid, "->", (fb_rec or {}).get("id"), (fb_rec or {}).get("status"), fb_err or "")

            def mut(st2, vid=m["vid"], sid=sid, yid=yid, fb_rec=fb_rec, fb_err=fb_err, tg=tg):
                tgt = _item(st2, vid, sid)
                if tgt is None:
                    return
                tgt["uploaded"] = {"yid": yid, "at": int(time.time()), "privacy": privacy if yid else "fb-only"}
                tgt.pop("upload_error", None)
                if fb_rec:
                    tgt["fb_uploaded"] = fb_rec
                    tgt.pop("fb_error", None)
                elif fb_err:
                    tgt["fb_error"] = fb_err
            save_state(mut, "hl uploaded %s" % sid)
            done += 1
        except Exception as e:                      # noqa
            log("upload failed", sid, e)

            def mut(st2, vid=m["vid"], sid=sid, err=str(e)[:300]):
                tgt = _item(st2, vid, sid)
                if tgt is not None:
                    tgt["upload_error"] = err
            save_state(mut, "hl upload error %s" % sid)
        finally:
            if os.path.exists(path):
                os.remove(path)
    for fn in (fb_recheck, fb_retry):
        try:
            fn()
        except Exception as e:                      # noqa
            log(fn.__name__, "error", e)
    return done


def fb_recheck():
    """Facebook finishes processing later: re-poll items still 'processing' (young ones only, max 12 polls)."""
    if not _fb_enabled():
        return 0
    st, _ = load_state()
    now = time.time()
    todo = [(m["vid"], sid, it["fb_uploaded"]) for m in st["matches"]
            for sid, it in [("highlight", m["highlight"])] + [(x["id"], x) for x in m.get("shorts", [])]
            if (it.get("fb_uploaded") or {}).get("status") == "processing"
            and now - it["fb_uploaded"].get("at", 0) < 12 * 3600 and int(it["fb_uploaded"].get("checks") or 1) < 12]
    res = []
    for vid, sid, rec in todo:
        stt, det = _fb_status(rec["id"])
        res.append((vid, sid, stt, det))
    if res:
        def mut(st2):
            for vid, sid, stt, det in res:
                tgt = _item(st2, vid, sid)
                if tgt is None or not tgt.get("fb_uploaded"):
                    continue
                rec = tgt["fb_uploaded"]
                rec["checks"] = int(rec.get("checks") or 1) + 1
                if stt == "ready":
                    rec["status"] = "ready"
                elif stt == "error":                    # failed after acceptance -> goes into the retry path
                    tgt.pop("fb_uploaded", None)
                    tgt["fb_error"] = "FB processing failed: %s" % det
        save_state(mut, "hl fb recheck")
    return len(res)


def fb_retry():
    """Items on YouTube (or fb-only) whose Facebook post failed (fb_error): try again, max 3 times."""
    if not _fb_enabled():
        return 0
    cfg = load_cfg()
    st, _ = load_state()
    todo = []
    for m in st["matches"]:
        for sid, it in [("highlight", m["highlight"])] + [(x["id"], x) for x in m.get("shorts", [])]:
            if it.get("uploaded") and it.get("fb_error") and not it.get("fb_uploaded") and int(it.get("fb_tries") or 1) < 3 \
                    and _fb_target(cfg, m["vid"], sid) != "yt":
                todo.append((m, sid, it))
    done = 0
    os.makedirs("/tmp/hlup", exist_ok=True)
    for m, sid, it in todo:
        path = os.path.join("/tmp/hlup", it["file"])
        rec, err = None, None
        try:
            _download_asset(m["release_id"], it["file"], path)
            ft, fd = _fb_texts(m, sid, it)
            rec, err = _fb_post_item(path, "highlight" if sid == "highlight" else "short", ft, fd)
        except Exception as e:                          # noqa
            err = str(e)[:300]
        finally:
            if os.path.exists(path):
                os.remove(path)
        log("facebook retry", sid, (rec or {}).get("id"), err or "")

        def mut(st2, vid=m["vid"], sid=sid, rec=rec, err=err):
            tgt = _item(st2, vid, sid)
            if tgt is None:
                return
            if rec:
                tgt["fb_uploaded"] = rec
                tgt.pop("fb_error", None)
            else:
                tgt["fb_error"] = err or "unknown"
                tgt["fb_tries"] = int(tgt.get("fb_tries") or 1) + 1
        save_state(mut, "hl fb retry %s" % sid)
        done += 1 if rec else 0
    return done


def do_fb_backfill(c):
    """Dashboard 'purane shorts bhi FB pe bhejo': mark YouTube-uploaded items without a Facebook post for the retry path."""
    cfg = load_cfg()
    want = (c.get("vid") or "").strip()

    def mut(st):
        for m in st["matches"]:
            if want and m["vid"] != want:
                continue
            for sid, it in [("highlight", m["highlight"])] + [(x["id"], x) for x in m.get("shorts", [])]:
                if it.get("uploaded") and (it["uploaded"] or {}).get("yid") and not it.get("fb_uploaded") \
                        and not it.get("fb_error") and _fb_target(cfg, m["vid"], sid) != "yt":
                    it["fb_error"] = "backfill"
                    it["fb_tries"] = 1
    save_state(mut, "hl fb backfill")


def pending_uploads():
    cfg = load_cfg()
    rv = cfg.get("reviews") or {}
    st, _ = load_state()
    n = 0
    now = time.time()
    for m in st["matches"]:
        for sid, it in [("highlight", m["highlight"])] + [(s["id"], s) for s in m.get("shorts", [])]:
            if rv.get("%s/%s" % (m["vid"], sid), {}).get("status") == "approved" and not it.get("uploaded") and not it.get("upload_error"):
                n += 1
            fu = it.get("fb_uploaded") or {}
            if fu.get("status") == "processing" and now - fu.get("at", 0) < 12 * 3600 and int(fu.get("checks") or 1) < 12:
                n += 1
    return n
