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
    ctype = "video/mp4" if name.endswith(".mp4") else "text/plain"
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


def upload_approved():
    """Upload every approved item once. Result is written into hl_state (matches[].shorts[].uploaded / highlight.uploaded)."""
    cfg = load_cfg()
    privacy = cfg.get("privacy") or "public"
    rv = cfg.get("reviews") or {}
    st, _ = load_state()
    todo = []
    for m in st["matches"]:
        items = [("highlight", m["highlight"])] + [(s["id"], s) for s in m.get("shorts", [])]
        for sid, it in items:
            if rv.get("%s/%s" % (m["vid"], sid), {}).get("status") == "approved" and not it.get("uploaded"):
                todo.append((m, sid, it))
    log("approved & not uploaded yet:", len(todo))
    os.makedirs("/tmp/hlup", exist_ok=True)
    done = 0
    for _i, (m, sid, it) in enumerate(todo, 1):
        set_live("uploading", "YouTube upload: %s" % sid, vid=m["vid"], title=m.get("title", ""), cur=_i, total=len(todo), force=True)
        path = os.path.join("/tmp/hlup", it["file"])
        try:
            _download_asset(m["release_id"], it["file"], path)
            if sid == "highlight":
                title = ("%s | Highlights" % m["title"])[:100]
                ch = it.get("chapters") or []
                desc = "Match highlights.\n\n" + "\n".join("%s %s" % (_fmt_ts(t), ev) for t, ev in ch) \
                    + "\n\nFull match: https://www.youtube.com/watch?v=%s" % m["vid"]
                tags = ["highlights", "live", "match"]
            else:
                title = it["title"] if "#shorts" in it["title"].lower() else (it["title"][:92] + " #Shorts")
                desc = "%s\n\nFull match: https://www.youtube.com/watch?v=%s\n#Shorts" % (it["title"], m["vid"])
                tags = ["shorts", "highlights", "live"]
            yid = _youtube_upload(path, title, desc, privacy, tags)
            log("uploaded", sid, "->", yid)

            def mut(st2, vid=m["vid"], sid=sid, yid=yid):
                for mm in st2["matches"]:
                    if mm["vid"] != vid:
                        continue
                    tgt = mm["highlight"] if sid == "highlight" else next((x for x in mm["shorts"] if x["id"] == sid), None)
                    if tgt is not None:
                        tgt["uploaded"] = {"yid": yid, "at": int(time.time()), "privacy": privacy}
                        tgt.pop("upload_error", None)
            save_state(mut, "hl uploaded %s" % sid)
            done += 1
        except Exception as e:                      # noqa
            log("upload failed", sid, e)

            def mut(st2, vid=m["vid"], sid=sid, err=str(e)[:300]):
                for mm in st2["matches"]:
                    if mm["vid"] != vid:
                        continue
                    tgt = mm["highlight"] if sid == "highlight" else next((x for x in mm["shorts"] if x["id"] == sid), None)
                    if tgt is not None:
                        tgt["upload_error"] = err
            save_state(mut, "hl upload error %s" % sid)
        finally:
            if os.path.exists(path):
                os.remove(path)
    return done


def pending_uploads():
    cfg = load_cfg()
    rv = cfg.get("reviews") or {}
    st, _ = load_state()
    n = 0
    for m in st["matches"]:
        for sid, it in [("highlight", m["highlight"])] + [(s["id"], s) for s in m.get("shorts", [])]:
            if rv.get("%s/%s" % (m["vid"], sid), {}).get("status") == "approved" and not it.get("uploaded") and not it.get("upload_error"):
                n += 1
    return n
