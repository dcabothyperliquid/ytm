#!/usr/bin/env python3
"""
Highlights + Shorts maker -- 100% GitHub Actions, no Render, no paid API.

Source  : finished live (VOD) on YOUR channel (the relay already put it there).
Detect  : audio excitement peaks (crowd roar / commentator shout) -> faster-whisper only on those
          windows -> keywords (six / wicket / hat-trick / four / century ...) re-rank the moments.
Output  : highlights.mp4 (chronological, ~HL_TARGET_MIN min) + N vertical 9:16 shorts (<=50s)
          + report.txt (titles, chapters, timestamps) -> DRAFT GitHub release (only you can see it).
You check, download, upload to YouTube yourself.

Commands:  gate | cookies | run | local <video.mp4> <outdir>
"""
import glob
import json
import os
import re
import subprocess
import sys
import time

# ---------------------------------------------------------------- config (env)
CHANNEL = os.environ.get("HL_CHANNEL", "localmatchlive").lstrip("@")
MIN_MIN = float(os.environ.get("HL_MIN_MIN", "20"))          # ignore streams shorter than this (minutes)
N_SHORTS = int(os.environ.get("HL_SHORTS", "8") or 8)
TARGET_MIN = float(os.environ.get("HL_TARGET_MIN", "10") or 10)
MODEL = os.environ.get("HL_MODEL", "small") or "small"
LANG = os.environ.get("HL_LANG", "hi") or None               # commentary language; "" = auto
MAX_CAND = int(os.environ.get("HL_MAX_CAND", "60"))
MAX_H = int(os.environ.get("HL_MAX_H", "1080"))
NO_WHISPER = os.environ.get("HL_NO_WHISPER", "") == "1"
KEEP_DAYS = 7                                                 # release assets older than this are deleted
SKIP_TITLE = re.compile(r"highlight|#shorts|\bshorts?\b", re.I)  # never reprocess our own uploads
WORK = "work"
COOKIES = "cookies.txt"
HOP = 0.5                                                     # s per energy frame
SR = 16000

EVENTS = {   # name: (regex, weight, label, hashtag)
    "hattrick": (r"hat[\s-]?trick|हैट[\s-]?ट्रिक|हेट[\s-]?ट्रिक|हैटट्रिक|hattrick", 1.6, "HAT-TRICK", "#hattrick"),
    "six": (r"\bsix(?:es|er)?\b|सिक्स|छक्का|छक्के|छक्‍का|chh?akka", 1.0, "SIX", "#six"),
    "wicket": (r"wicket|विकेट|\bout\b|आउट|bowled|बोल्ड|run[\s-]?out|रन[\s-]?आउट|\blbw\b|स्टंप|stump", 0.9, "WICKET", "#wicket"),
    "milestone": (r"century|सेंचुरी|शतक|fifty|फिफ्टी|अर्धशतक|half[\s-]?century", 0.7, "MILESTONE", "#century"),
    "catch": (r"\bcatch(?:ed)?\b|कैच", 0.6, "CATCH", "#catch"),
    "four": (r"\bfour\b|चौका|चौके|चौक्का|chauka|boundary|बाउंड्री", 0.5, "FOUR", "#four"),
    "ace": (r"\bace\b|match[\s-]?point|set[\s-]?point|tie[\s-]?break", 0.6, "BIG POINT", "#tennis"),
}
EMOJI = {"hattrick": "🔥", "six": "💥", "wicket": "☝️", "milestone": "🎉", "catch": "🙌",
         "four": "🏏", "ace": "🎾", "moment": "⚡"}
WHISPER_HINT = "छक्का चौका विकेट आउट कैच हैट्रिक बोल्ड six four wicket out catch hat-trick century"


# ---------------------------------------------------------------- helpers
def sh(cmd, check=True, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and r.returncode:
        raise RuntimeError("%s failed (%d): %s" % (cmd[0], r.returncode, (r.stderr or r.stdout)[-600:]))
    return r


def log(*a):
    print("[hl]", *a, flush=True)


def out_gh(**kv):
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a") as f:
            for k, v in kv.items():
                f.write("%s=%s\n" % (k, v))
    log("output", kv)


def ytdlp_base():
    cmd = ["yt-dlp", "--no-warnings", "--js-runtimes", "node", "--remote-components", "ejs:github"]
    if os.path.exists(COOKIES):
        cmd += ["--cookies", COOKIES]
    return cmd


def repo():
    return os.environ.get("GITHUB_REPOSITORY", "dcabothyperliquid/ytm")


def gh_releases():
    """[{id, tag_name, created_at, assets:[ids]}] for hl-* releases (drafts included)."""
    r = sh(["gh", "api", "repos/%s/releases" % repo(), "--paginate", "-q",
            '.[] | select(.tag_name|startswith("hl-")) | {id,tag_name,created_at,assets:[.assets[].id]}'],
           check=False)
    rows = []
    for line in (r.stdout or "").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


# ---------------------------------------------------------------- gate / cookies
def cmd_cookies():
    raw = os.environ.get("YT_COOKIES", "").strip()
    if not raw:
        return
    if raw[:1] in "[{":
        d = json.loads(raw)
        d = d.get("cookies", d) if isinstance(d, dict) else d
        lines = ["# Netscape HTTP Cookie File"]
        for c in d:
            dom = c.get("domain", "")
            exp = int(c.get("expirationDate") or c.get("expires") or 0)
            lines.append("\t".join([dom, "TRUE" if dom.startswith(".") else "FALSE", c.get("path", "/"),
                                    "TRUE" if c.get("secure") else "FALSE", str(max(exp, 0)), c["name"], c["value"]]))
        raw = "\n".join(lines)
    open(COOKIES, "w").write(raw + "\n")


def cmd_gate():
    """Pick the video to process. Sets outputs go / vid. Cheap (no ffmpeg)."""
    manual = os.environ.get("HL_VIDEO", "").strip()
    if manual:
        m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/)([0-9A-Za-z_-]{11})", manual) or re.match(r"^([0-9A-Za-z_-]{11})$", manual)
        if not m:
            log("manual video id/url not understood:", manual)
            return out_gh(go="false", vid="")
        return out_gh(go="true", vid=m.group(1))
    done = {r["tag_name"] for r in gh_releases()}
    cands = []
    for tab in ("streams", "videos"):
        r = sh(ytdlp_base() + ["--flat-playlist", "--playlist-end", "15", "--print",
                               "%(id)s|%(live_status)s|%(duration)s|%(title)s",
                               "https://www.youtube.com/@%s/%s" % (CHANNEL, tab)], check=False)
        for line in (r.stdout or "").splitlines():
            p = line.split("|", 3)
            if len(p) < 4:
                continue
            vid, live, dur, title = p
            if live in ("is_live", "is_upcoming"):
                continue
            try:
                dur = float(dur)
            except ValueError:
                continue                      # unknown duration (still processing)
            if dur < MIN_MIN * 60 or SKIP_TITLE.search(title) or "hl-" + vid in done:
                continue
            if all(c[0] != vid for c in cands):
                cands.append((vid, title, dur))
    if not cands:
        log("nothing new on @%s" % CHANNEL)
        return out_gh(go="false", vid="")
    log("candidates:", cands)
    out_gh(go="true", vid=cands[0][0])


# ---------------------------------------------------------------- analysis
def duration_of(path):
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path])
    return float(r.stdout.strip())


def energy_track(wav):
    """dB energy (pre-emphasised -> favours shouts / roars) per HOP seconds."""
    import numpy as np
    n = os.path.getsize(wav) // 2
    frame = int(SR * HOP)
    nfr = n // frame
    mm = np.memmap(wav, dtype=np.int16, mode="r", shape=(n,))
    db = np.empty(nfr, dtype=np.float32)
    step = 2400                                    # frames per chunk (=20 min)
    for a in range(0, nfr, step):
        b = min(nfr, a + step)
        x = np.asarray(mm[a * frame:b * frame], dtype=np.float32) / 32768.0
        x = np.concatenate([x[:1], x[1:] - 0.97 * x[:-1]])
        x = x[:(b - a) * frame].reshape(b - a, frame)
        db[a:b] = 20 * np.log10(np.sqrt((x * x).mean(axis=1)) + 1e-6)
    return db


def find_moments(db, dur):
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    k = 4                                          # 2 s smoothing
    sm = np.convolve(db, np.ones(k) / k, mode="same")
    w = 241                                        # ~2 min rolling baseline
    pad = np.pad(sm, (w // 2, w - w // 2 - 1), mode="edge")
    base = np.median(sliding_window_view(pad, w), axis=1)
    ex = sm - base
    thr = max(4.0, float(np.percentile(ex, 96)))
    order = np.argsort(ex)[::-1]
    picks = []
    for i in order:
        if ex[i] < thr or len(picks) >= MAX_CAND:
            break
        if i * HOP < 15 or i * HOP > dur - 5:
            continue
        if all(abs(int(i) - j) * HOP >= 30 for j in picks):
            picks.append(int(i))
    top = float(ex[picks[0]]) if picks else 1.0
    moments = []
    for i in picks:
        half = ex[i] * 0.5
        a = i
        while a > 0 and ex[a - 1] >= half and (i - a) * HOP < 20:
            a -= 1
        b = i
        while b < len(ex) - 1 and ex[b + 1] >= half and (b - i) * HOP < 25:
            b += 1
        start, end = a * HOP - 10.0, b * HOP + 6.0
        if end - start < 20:
            start = end - 20
        if end - start > 45:
            start = end - 45
        moments.append({"t": round(i * HOP, 1), "audio": round(float(ex[i]) / top, 3), "db": round(float(ex[i]), 1),
                        "start": max(0.0, round(start, 1)), "end": min(dur, round(end, 1)),
                        "event": "moment", "text": "", "kw": 0.0})
    log("audio peaks: %d (threshold %.1f dB over baseline)" % (len(moments), thr))
    return moments


def classify(text):
    best, kw, hits = "moment", 0.0, 0
    for name, (rx, wt, _l, _h) in EVENTS.items():
        n = len(re.findall(rx, text, flags=re.I))
        if n:
            hits += 1
            if wt > kw:
                best, kw = name, wt
            if n >= 2:
                kw += 0.1
    if hits > 1:
        kw += 0.1
    return best, kw


def transcribe_moments(moments, wav):
    if NO_WHISPER or not moments:
        return
    try:
        from faster_whisper import WhisperModel
        import numpy as np
    except Exception as e:                         # noqa
        log("faster-whisper unavailable -> audio-only ranking:", e)
        return
    log("loading whisper", MODEL)
    model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
    n = os.path.getsize(wav) // 2
    mm = np.memmap(wav, dtype=np.int16, mode="r", shape=(n,))
    t0 = time.time()
    for i, m in enumerate(moments):
        a, b = int(m["start"] * SR), int(m["end"] * SR)
        audio = np.asarray(mm[a:b], dtype=np.float32) / 32768.0
        try:
            segs, _ = model.transcribe(audio, language=LANG, beam_size=1, vad_filter=True,
                                       initial_prompt=WHISPER_HINT, condition_on_previous_text=False)
            m["text"] = " ".join(s.text.strip() for s in segs)[:400]
        except Exception as e:                     # noqa
            log("whisper fail on moment", i, e)
            continue
        m["event"], m["kw"] = classify(m["text"])
        if (i + 1) % 10 == 0:
            log("transcribed %d/%d (%.0fs)" % (i + 1, len(moments), time.time() - t0))


def score(m):
    return 0.5 * m["audio"] + 0.5 * min(m["kw"], 1.6) / 1.6 + (0.5 if m["event"] == "hattrick" else 0.0)


def merge_windows(ms):
    ms = sorted(ms, key=lambda m: m["start"])
    out = []
    for m in ms:
        if out and m["start"] <= out[-1]["end"] + 3:
            o = out[-1]
            o["end"] = max(o["end"], m["end"])
            if score(m) > score(o):
                o["event"], o["score"] = m["event"], score(m)
            o.setdefault("parts", 1)
            o["parts"] += 1
        else:
            out.append(dict(m, score=score(m)))
    return out


# ---------------------------------------------------------------- render
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", "-movflags", "+faststart"]


def cut(src, start, end, out, vertical=False, height=1080):
    d = end - start
    af = "afade=t=in:d=0.2,afade=t=out:st=%.2f:d=0.3" % max(0, d - 0.3)
    if vertical:
        fc = ("[0:v]split=2[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
              "boxblur=25:3,eq=brightness=-0.08[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30[v]")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", "%.2f" % start, "-i", src, "-t", "%.2f" % d,
               "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0?", "-af", af] + ENC + [out]
    else:
        vf = "scale=-2:'min(%d,ih)',fps=30" % height
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", "%.2f" % start, "-i", src, "-t", "%.2f" % d,
               "-vf", vf, "-af", af] + ENC + [out]
    sh(cmd)


def fmt(t):
    t = int(t)
    return "%d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60) if t >= 3600 else "%d:%02d" % (t // 60, t % 60)


def clean_title(t):
    t = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", t or "")
    t = re.sub(r"\b(LIVE|लाइव)\b", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" |-")[:70]


def process(src, outdir, meta):
    os.makedirs(outdir, exist_ok=True)
    dur = duration_of(src)
    log("source %.1f min" % (dur / 60))
    wav = os.path.join(os.path.dirname(os.path.abspath(outdir)), "audio.s16")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", wav])
    moments = find_moments(energy_track(wav), dur)
    if not moments:
        raise RuntimeError("no audio peaks found (silent / flat source?)")
    transcribe_moments(moments, wav)
    os.remove(wav)
    for m in moments:
        m["score"] = round(score(m), 3)
    ranked = sorted(moments, key=lambda m: m["score"], reverse=True)

    # ---- highlights: best moments until target length, chronological
    chosen, total = [], 0.0
    for m in ranked:
        if total >= TARGET_MIN * 60:
            break
        chosen.append(m)
        total += m["end"] - m["start"]
    segs = merge_windows(chosen)
    segdir = os.path.join(outdir, "_seg")
    os.makedirs(segdir, exist_ok=True)
    lst, chapters, t = [], [], 0.0
    for i, s in enumerate(segs):
        p = os.path.join(segdir, "s%03d.mp4" % i)
        cut(src, s["start"], s["end"], p, height=MAX_H)
        lst.append("file '%s'" % os.path.abspath(p))
        chapters.append((t, s["event"], s["start"]))
        t += s["end"] - s["start"]
        log("highlight seg %d/%d" % (i + 1, len(segs)))
    open(os.path.join(segdir, "list.txt"), "w").write("\n".join(lst) + "\n")
    hl = os.path.join(outdir, "highlights.mp4")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", os.path.join(segdir, "list.txt"),
        "-c", "copy", "-movflags", "+faststart", hl])

    # ---- shorts: top K non-overlapping, each <= 50 s
    shorts, used = [], []
    for m in ranked:
        if len(shorts) >= N_SHORTS:
            break
        if any(m["start"] < u[1] and m["end"] > u[0] for u in used):
            continue
        s, e = m["start"], m["end"]
        if e - s > 50:
            s = e - 50
        used.append((s, e))
        name = "short_%02d_%s.mp4" % (len(shorts) + 1, m["event"])
        cut(src, s, e, os.path.join(outdir, name), vertical=True)
        shorts.append((name, m, s, e))
        log("short", name)
    for f in glob.glob(os.path.join(segdir, "*")):
        os.remove(f)
    os.rmdir(segdir)

    # ---- report
    mt = clean_title(meta.get("title", ""))
    L = ["SOURCE: %s  (%s)" % (meta.get("title", "-"), meta.get("vid", "-")), "",
         "== HIGHLIGHTS (highlights.mp4, %s) ==" % fmt(total),
         "Suggested title: Match Highlights | %s" % mt, "Chapters (paste in description):"]
    for ct, ev, at in chapters:
        L.append("%s %s %s" % (fmt(ct), EMOJI.get(ev, "⚡"), EVENTS[ev][2].title() if ev in EVENTS else "Big moment"))
    L += ["", "== SHORTS (check, then upload) =="]
    for name, m, s, e in shorts:
        lab = EVENTS[m["event"]][2] if m["event"] in EVENTS else "BIG MOMENT"
        tag = EVENTS[m["event"]][3] if m["event"] in EVENTS else ""
        L.append("%s | src %s-%s | score %.2f | audio +%.0fdB" % (name, fmt(s), fmt(e), m["score"], m["db"]))
        L.append("  title: %s %s | %s #shorts %s" % (EMOJI.get(m["event"], "⚡"), lab.title(), mt, tag))
        if m["text"]:
            L.append("  heard: %s" % m["text"][:140])
    L += ["", "Note: hat-trick = keyword match only (needs 'hat-trick' in commentary). Always check before upload."]
    open(os.path.join(outdir, "report.txt"), "w").write("\n".join(L) + "\n")
    json.dump(ranked, open(os.path.join(outdir, "moments.json"), "w"), ensure_ascii=False, indent=1)
    log("done: highlights %s + %d shorts" % (fmt(total), len(shorts)))


# ---------------------------------------------------------------- run (CI)
def cmd_run():
    vid = os.environ.get("VID", "").strip()
    if not vid:
        return log("no VID")
    os.makedirs(WORK, exist_ok=True)
    src = os.path.join(WORK, "src.mp4")
    url = "https://www.youtube.com/watch?v=" + vid
    info = sh(ytdlp_base() + ["--no-playlist", "--print", "%(title)s", url], check=False).stdout.strip()
    log("downloading", vid, info)
    fmt_sel = "bv*[height<=%d]+ba/b[height<=%d]/b" % (MAX_H, MAX_H)
    sh(ytdlp_base() + ["--no-playlist", "-f", fmt_sel, "--merge-output-format", "mp4", "-o", src, url])
    out = os.path.join(WORK, "out")
    process(src, out, {"title": info, "vid": vid})
    os.remove(src)
    # ---- publish as DRAFT release (visible only to repo writers = you)
    tag = "hl-" + vid
    for r in gh_releases():
        if r["tag_name"] == tag:
            sh(["gh", "api", "-X", "DELETE", "repos/%s/releases/%s" % (repo(), r["id"])], check=False)
    files = sorted(glob.glob(os.path.join(out, "*.mp4"))) + [os.path.join(out, "report.txt"), os.path.join(out, "moments.json")]
    sh(["gh", "release", "create", tag, "-R", repo(), "--draft", "--target", "main",
        "--title", "Highlights %s" % clean_title(info), "--notes-file", os.path.join(out, "report.txt")] + files)
    log("draft release created:", tag)
    # ---- housekeeping: free assets of old releases (keeps the shell so the video is not re-processed)
    cutoff = time.time() - KEEP_DAYS * 86400
    for r in gh_releases():
        ts = time.mktime(time.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
        if ts < cutoff:
            for aid in r["assets"]:
                sh(["gh", "api", "-X", "DELETE", "repos/%s/releases/assets/%s" % (repo(), aid)], check=False)


if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else ""
    if c == "gate":
        cmd_gate()
    elif c == "cookies":
        cmd_cookies()
    elif c == "run":
        cmd_run()
    elif c == "local":
        process(sys.argv[2], sys.argv[3], {"title": "Local test", "vid": "local"})
    else:
        print(__doc__)
