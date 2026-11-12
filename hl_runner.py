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

import hl_review

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
    "finish": (r"last[\s-]?over|last[\s-]?ball|final[\s-]?over|आखिरी\s*ओवर|आख़िरी\s*ओवर|अंतिम\s*ओवर|आखिरी\s*गेंद|need(?:s|ed)?\s+\d+\s+(?:runs?\s+)?(?:from|off)|\d+\s*(?:रन|runs?)\s*(?:चाहिए|चाहिये|needed|to win)|super[\s-]?over|सुपर\s*ओवर", 0.9, "LAST OVER", "#lastover"),
    "ace": (r"\bace\b|match[\s-]?point|set[\s-]?point|tie[\s-]?break", 0.6, "BIG POINT", "#tennis"),
    "drop": (r"\bdrop(?:ped|s)?\b|butter[\s-]?fingers?|fumble|छोड़ा|छोड़\s*दिया|छूट\s*गया|ड्रॉप|मिस\s*फील्ड|miss(?:ed)?\s+(?:the\s+)?catch", 0.8, "CATCH DROPPED", "#catchdrop"),
}
EMOJI = {"finish": "😱", "hattrick": "🔥", "six": "💥", "wicket": "☝️", "milestone": "🎉", "catch": "🙌",
         "four": "🏏", "ace": "🎾", "moment": "⚡", "drop": "🤦", "sixes": "💥", "wickets": "☝️", "over": "🔥"}
STORY_TAG = {"sixes": "#sixes", "wickets": "#wickets", "over": "#bigover"}
OVER_GAP = 150          # s: confirmed events closer than this belong to the same over / passage of play
OVER_SPAN = 420         # s: max length of one story cluster
PART_MAX = 5            # clips per compilation short
SHORT_MAX = 58.0        # s per short
WHISPER_HINT = "छक्का चौका विकेट आउट कैच हैट्रिक बोल्ड आखिरी ओवर रन चाहिए six four wicket out catch hat-trick century last over"
GENERIC = ["CROWD GOES WILD", "WHAT A MOMENT", "BIG MOMENT", "SEE THIS", "UNBELIEVABLE"]
FONT = next((f for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                         "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf") if os.path.exists(f)), "")


# ---------------------------------------------------------------- helpers
def sh(cmd, check=True, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and r.returncode:
        raise RuntimeError("%s failed (%d): %s" % (cmd[0], r.returncode, (r.stderr or r.stdout)[-600:]))
    return r


_T0 = time.time()


def log(*a):
    print("[hl +%4ds]" % (time.time() - _T0), *a, flush=True)


def note(msg, level="notice", title="hl"):
    """GitHub annotation (readable through the API even when raw logs are not)."""
    m = str(msg).replace("%", "%25").replace("\r", "").replace("\n", "%0A")[:1800]
    print("::%s title=%s::%s" % (level, title, m), flush=True)


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


def find_candidates():
    """Ended, long-enough, not-yet-processed videos on our channel (newest first)."""
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
    return cands


def cmd_gate():
    """Decide what to do. Outputs: go, mode (run|apply), vid. Cheap (no ffmpeg)."""
    manual = os.environ.get("HL_VIDEO", "").strip()
    if os.environ.get("HL_MODE", "") == "apply" or (not manual and (hl_review.pending_commands() or hl_review.pending_uploads())):
        note("pending dashboard commands/uploads -> apply mode")
        return out_gh(go="true", mode="apply", vid="")
    if manual:
        m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/)([0-9A-Za-z_-]{11})", manual) or re.match(r"^([0-9A-Za-z_-]{11})$", manual)
        if not m:
            log("manual video id/url not understood:", manual)
            return out_gh(go="false", vid="")
        return out_gh(go="true", mode="run", vid=m.group(1))
    if not hl_review.load_cfg().get("auto", True):
        log("auto OFF in dashboard config")
        return out_gh(go="false", vid="")
    after_end = os.environ.get("HL_AFTER_END", "") == "true"
    # triggered right when the relay ended: our VOD needs a few minutes before YouTube reports its duration
    tries = 14 if after_end else 1
    cands = []
    for k in range(tries):
        cands = find_candidates()
        if cands or k == tries - 1:
            break
        log("stream-end trigger: VOD not ready yet, retry %d/%d in 90s" % (k + 1, tries - 1))
        time.sleep(90)
    if not cands:
        log("nothing new on @%s" % CHANNEL)
        return out_gh(go="false", vid="")
    log("candidates:", cands)
    note("picked %s | %.0f min | %s" % (cands[0][0], cands[0][2] / 60, cands[0][1][:80]))
    out_gh(go="true", mode="run", vid=cands[0][0])


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
    # broadcast audio is heavily compressed -> low floor; candidate count scales with length (~1 per 4 min)
    want = min(MAX_CAND, max(10, int(dur / 150)))
    thr = max(0.7, min(4.0, float(np.percentile(ex, 95))))
    min_fill = min(want, 8)                         # flat/compressed audio: still hand the ranker something to work with
    order = np.argsort(ex)[::-1]
    picks = []
    for i in order:
        if len(picks) >= want or (ex[i] < thr and len(picks) >= min_fill) or ex[i] <= 0.2:
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
    note("audio peaks %d (thr %.1f dB)" % (len(moments), thr))
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
    note("whisper %s done: %d windows in %.0fs" % (MODEL, len(moments), time.time() - t0))


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


def _ev_tag(ev):
    return EVENTS[ev][3] if ev in EVENTS else STORY_TAG.get(ev, "")


def _part(m, wide=False):
    """Clip around the audio peak: build-up + reaction (not the whole 45 s window)."""
    a, b = (10.0, 9.0) if wide else (6.0, 5.0)
    return [max(0.0, m["t"] - a), m["t"] + b]


def build_stories(moments, dur):
    """A short = a real story, never one random loud shot.
    hat-trick | catch drop | milestone | last-over | 3+ sixes/fours/wickets in one over (compiled)."""
    conf = sorted([m for m in moments if m["event"] != "moment" and m["kw"] >= 0.5], key=lambda m: m["t"])
    clusters = []
    for m in conf:
        if clusters and m["t"] - clusters[-1][-1]["t"] <= OVER_GAP and m["t"] - clusters[-1][0]["t"] <= OVER_SPAN:
            clusters[-1].append(m)
        else:
            clusters.append([m])
    stories = []
    for cl in clusters:
        ev = [m["event"] for m in cl]
        n6, n4, nw = ev.count("six"), ev.count("four"), ev.count("wicket")
        in_finish = cl[-1]["t"] > dur - 420
        kind, label, base = None, None, 0.0
        if "hattrick" in ev:
            kind, label, base = "hattrick", "HAT-TRICK!", 2.0
        elif n6 >= 3 or (n6 >= 2 and n6 + n4 >= 4):
            kind, label, base = "sixes", ("%d SIXES!" % n6 if n6 >= 3 else "SIX-HITTING OVER!"), 1.5 + 0.2 * n6
        elif "finish" in ev:
            kind, label, base = "finish", "LAST OVER", 1.4
        elif nw >= 2:
            kind, label, base = "wickets", "WICKETS FALL!", 1.2 + 0.2 * nw
        elif "drop" in ev:
            kind, label, base = "drop", "CATCH DROPPED!", 1.1
        elif "milestone" in ev:
            kind, label, base = "milestone", "MILESTONE!", 1.0
        elif len(cl) >= 3 and len(set(ev) & {"six", "four", "wicket"}) >= 2:
            kind, label, base = "over", "BIG OVER!", 1.0 + 0.15 * len(cl)
        elif in_finish and any(e in ("wicket", "catch") for e in ev):
            kind, label, base = "finish", "LAST OVER", 1.0
        if not kind:
            continue
        single = kind in ("hattrick", "drop", "milestone", "finish")
        pool = cl
        if kind == "sixes":
            pool = [m for m in cl if m["event"] in ("six", "four")] or cl
        elif kind == "wickets":
            pool = [m for m in cl if m["event"] in ("wicket", "catch")] or cl
        elif kind in ("hattrick", "drop", "milestone"):
            pool = [m for m in cl if m["event"] == kind] or cl
        pool = sorted(pool, key=score, reverse=True)[:(1 if kind in ("drop", "milestone") else 3 if single else PART_MAX)]
        pool = sorted(pool, key=lambda m: m["t"])
        parts = []
        for m in pool:
            a, b = _part(m, wide=single)
            b = min(b, dur)
            if parts and a <= parts[-1][1] + 1:
                parts[-1][1] = max(parts[-1][1], b)
            else:
                parts.append([a, b])
        while sum(b - a for a, b in parts) > SHORT_MAX and len(parts) > 1:
            parts.pop()
        tot = sum(b - a for a, b in parts)
        if tot > SHORT_MAX:
            parts = [[parts[0][1] - SHORT_MAX, parts[0][1]]]
            tot = SHORT_MAX
        if tot < 8:
            continue
        top = max(cl, key=score)
        stories.append({"event": kind, "label": label, "parts": parts, "start": parts[0][0], "end": parts[-1][1],
                        "t": top["t"], "audio": top["audio"], "db": max(m["db"] for m in cl),
                        "text": " | ".join(m["text"][:70] for m in pool if m["text"])[:400],
                        "score": round(base + 0.3 * max(score(m) for m in cl) + (0.15 if in_finish else 0.0), 3)})
    return sorted(stories, key=lambda x: x["score"], reverse=True)


def render_story(src, st, out):
    """1 part = plain vertical cut; several parts = vertical cuts joined into one short."""
    if len(st["parts"]) == 1:
        a, b = st["parts"][0]
        return cut(src, a, b, out, vertical=True, label=st["label"])
    tmp, lst = os.path.splitext(out)[0] + "_parts", []
    os.makedirs(tmp, exist_ok=True)
    for i, (a, b) in enumerate(st["parts"]):
        p = os.path.join(tmp, "p%02d.mp4" % i)
        cut(src, a, b, p, vertical=True, label=st["label"] if i == 0 else None)
        lst.append("file '%s'" % os.path.abspath(p))
    open(os.path.join(tmp, "list.txt"), "w").write("\n".join(lst) + "\n")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", os.path.join(tmp, "list.txt"),
        "-c", "copy", "-movflags", "+faststart", out])
    for f in glob.glob(os.path.join(tmp, "*")):
        os.remove(f)
    os.rmdir(tmp)


# ---------------------------------------------------------------- render
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", "-movflags", "+faststart"]


def overlay_label(event, k=0):
    """Short ALL-CAPS banner text (latin only: the runner font has no Devanagari)."""
    if event in EVENTS:
        return EVENTS[event][2] + ("!" if event != "finish" else "")
    return GENERIC[k % len(GENERIC)]


def _banner(label):
    if not label or not FONT:
        return ""
    t = re.sub(r"[^A-Z0-9 !?-]", "", label.upper())[:24]
    return (",drawbox=x=0:y=150:w=iw:h=190:color=black@0.55:t=fill,"
            "drawtext=fontfile=%s:text='%s':fontsize=104:fontcolor=yellow:borderw=5:bordercolor=black:"
            "x=(w-text_w)/2:y=190:enable='lt(t,6)'" % (FONT, t))


def cut(src, start, end, out, vertical=False, height=1080, label=None):
    d = end - start
    af = "afade=t=in:d=0.2,afade=t=out:st=%.2f:d=0.3" % max(0, d - 0.3)
    if vertical:
        fc = ("[0:v]split=2[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
              "boxblur=25:3,eq=brightness=-0.08[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30" + _banner(label) + "[v]")
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
    note("source %.1f min" % (dur / 60))
    wav = os.path.join(os.path.dirname(os.path.abspath(outdir)), "audio.s16")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", wav])
    moments = find_moments(energy_track(wav), dur)
    if not moments:
        raise RuntimeError("no audio peaks found (silent / flat source?)")
    transcribe_moments(moments, wav)
    os.remove(wav)
    for m in moments:
        m["score"] = round(score(m), 3)
        if m["start"] > dur - 360:                 # the finish of the match matters most (last ~6 min)
            m["score"] = round(m["score"] + 0.12, 3)
    ranked = sorted(moments, key=lambda m: m["score"], reverse=True)
    tail = [m for m in moments if m["end"] > dur - 420]
    finish = round(min(1.0, (max([m["audio"] for m in tail]) if tail else 0.0) * 0.6
                       + (0.4 if any(m["event"] == "finish" for m in tail) else 0.0)), 3)

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

    # ---- shorts: real stories only (over compilations / hat-trick / catch drop / last over)
    shorts, used = [], []
    heard_any = any(m["text"] for m in moments)
    if heard_any:
        cands = build_stories(moments, dur)
        log("stories found: %d" % len(cands))
        note("stories found: %d" % len(cands))
    else:                                           # no transcript (whisper off/failed): audio-only fallback
        cands = []
        for m in ranked:
            s_, e_ = m["start"], m["end"]
            if e_ - s_ > 50:
                s_ = e_ - 50
            cands.append(dict(m, parts=[[s_, e_]], label=overlay_label(m["event"], len(cands))))
    for m in cands:
        if len(shorts) >= N_SHORTS:
            break
        s, e = m["start"], m["end"]
        if any(s < u[1] and e > u[0] for u in used):
            continue
        used.append((s, e))
        name = "short_%02d_%s.mp4" % (len(shorts) + 1, m["event"])
        render_story(src, m, os.path.join(outdir, name))
        shorts.append((name, m, s, e))
        log("short", name, m["label"], "parts=%d" % len(m["parts"]))
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
        lab = m.get("label") or "BIG MOMENT"
        tag = _ev_tag(m["event"])
        L.append("%s | src %s-%s | score %.2f | audio +%.0fdB" % (name, fmt(s), fmt(e), m["score"], m["db"]))
        L.append("  title: %s %s | %s #shorts %s" % (EMOJI.get(m["event"], "⚡"), lab.title(), mt, tag))
        if m["text"]:
            L.append("  heard: %s" % m["text"][:140])
    L += ["", "Note: shorts = story-based (over compilation / hat-trick / catch drop / last over); needs keywords in commentary. Always check before upload."]
    open(os.path.join(outdir, "report.txt"), "w").write("\n".join(L) + "\n")
    json.dump(ranked, open(os.path.join(outdir, "moments.json"), "w"), ensure_ascii=False, indent=1)
    log("done: highlights %s + %d shorts" % (fmt(total), len(shorts)))
    note("rendered: highlights %s + %d shorts" % (fmt(total), len(shorts)))
    out_shorts = []
    for name, m, s_, e_ in shorts:
        lab = m.get("label") or "BIG MOMENT"
        tg = _ev_tag(m["event"])
        out_shorts.append({"id": name[:-4], "file": name, "event": m["event"], "score": m["score"],
                           "start": round(s_, 1), "end": round(e_, 1), "heard": m["text"][:140],
                           "label": lab, "title": ("%s %s | %s #shorts %s" % (EMOJI.get(m["event"], "⚡"), lab.title().rstrip("!"), mt, tg)).strip()})
    return {"dur": dur, "ranked": ranked, "finish": finish,
            "highlight": {"file": "highlights.mp4", "dur": round(total), "chapters": [[round(ct), ev] for ct, ev, at in chapters]},
            "shorts": out_shorts}


# ---------------------------------------------------------------- run (CI)
def run_vid(vid, target_min=None):
    global TARGET_MIN, N_SHORTS
    cfg = hl_review.load_cfg()
    N_SHORTS = min(14, max(6, 2 * int(cfg.get("daily_shorts", 5))))    # candidates; the dashboard picks the best N per day
    if target_min:
        TARGET_MIN = float(target_min)
    os.makedirs(WORK, exist_ok=True)
    src = os.path.join(WORK, "src.mp4")
    if os.path.exists(src):
        os.remove(src)
    url = "https://www.youtube.com/watch?v=" + vid
    info = sh(ytdlp_base() + ["--no-playlist", "--print", "%(title)s", url], check=False).stdout.strip()
    log("downloading", vid, info)
    fmt_sel = "bv*[height<=%d]+ba/b[height<=%d]/b" % (MAX_H, MAX_H)
    t0 = time.time()
    sh(ytdlp_base() + ["--no-playlist", "-f", fmt_sel, "--merge-output-format", "mp4", "-o", src, url])
    note("downloaded %s: %.0f MB in %.0fs" % (vid, os.path.getsize(src) / 1e6, time.time() - t0))
    out = os.path.join(WORK, "out_" + vid)
    res = process(src, out, {"title": info, "vid": vid})
    os.remove(src)
    # ---- full quality -> DRAFT release (visible only to repo writers = you)
    tag = "hl-" + vid
    for r in gh_releases():
        if r["tag_name"] == tag:
            sh(["gh", "api", "-X", "DELETE", "repos/%s/releases/%s" % (repo(), r["id"])], check=False)
    files = sorted(glob.glob(os.path.join(out, "*.mp4"))) + [os.path.join(out, "report.txt"), os.path.join(out, "moments.json")]
    sh(["gh", "release", "create", tag, "-R", repo(), "--draft", "--target", "main",
        "--title", "Highlights %s" % clean_title(info), "--notes-file", os.path.join(out, "report.txt")] + files)
    note("draft release created: %s (%d files)" % (tag, len(files)))
    # ---- previews (public, low-res) + dashboard state
    hl_review.publish_match(vid, clean_title(info) or info, out, res)
    note("previews + dashboard state updated")
    # ---- housekeeping: free assets of old full releases (keeps the shell so the video is not re-processed)
    cutoff = time.time() - KEEP_DAYS * 86400
    for r in gh_releases():
        ts = time.mktime(time.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
        if ts < cutoff:
            for aid in r["assets"]:
                sh(["gh", "api", "-X", "DELETE", "repos/%s/releases/assets/%s" % (repo(), aid)], check=False)
    hl_review.cleanup_old()
    sh(["rm", "-rf", out], check=False)


def cmd_run():
    if os.environ.get("MODE", "run") == "apply":
        hl_review.apply_commands(sys.modules[__name__])
        n = hl_review.upload_approved()
        note("uploads done: %d" % n)
        return
    vid = os.environ.get("VID", "").strip()
    if not vid:
        return log("no VID")
    run_vid(vid)


def main():
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


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                      # noqa
        import traceback
        note(("%s\n%s" % (e, traceback.format_exc()[-1200:])), "error", "hl failed")
        sys.exit(1)
