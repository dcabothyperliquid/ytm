"""
Scoreboard scout: find every match in a VOD from the SCOREBOARD OVERLAY (not from commentary).

Why: the commentary-only scout (hl_scout.find_start) missed a whole match because the commentator is
not saying ball-by-ball words that whisper(base) can catch. The overlay is always there and always
clear: "TOSS PENDING", "<team> WON THE TOSS AND OPTED TO BOWL", "TARGET - 134", "NEED 41 MORE FROM 11
BALLS", "<team> WON BY 7 WICKETS", "MATCH NO. - 11".

How (cheap, no 720p download):
  1. download <=360p video-only,
  2. ffmpeg: 1 frame / STEP s, only the bottom scoreboard strip, upscaled x3, gray,
  3. tesseract on each strip (parallel),
  4. segment: toss block -> play frames -> first result banner = one match.
     start = first toss frame - 20 s, end = first result banner + 50 s (celebration), capped by the next toss.
"""
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import hl_review

STEP = int(os.environ.get("HL_BOARD_STEP", "15") or 15)     # s between sampled frames
TOSS_GAP = 150          # toss frames closer than this (s) are one toss block
PLAY_AFTER_TOSS = 30 * 60   # a toss only starts a match if live play follows within this
MIN_PLAY_FRAMES = 8     # ~2 min of live scoreboard after the toss
LEAD_IN = 20.0          # s before the first toss frame
CELEBRATION = 150.0     # s after the first result banner (winning run + celebration; the banner shows a few s after the last ball)
FINAL_TAIL = 600.0      # a FINAL keeps the trophy / cup lift too
BOARD_H = int(os.environ.get("HL_BOARD_H", "480") or 480)   # 360p was too blurry for the small banner text
TOSS_PENDING_MAX = 360  # if "TOSS PENDING" sits longer than this before "won the toss", start 60 s before the won-toss frame

TOSS_RX = re.compile(r"\bT[O0]SS\b", re.I)
TOSSDONE_RX = re.compile(r"WON\s+THE\s+T[O0]SS|(?:OPTED|ELECTED|CHOSE|CHOOSE)\s+TO\s+(?:BAT|BOWL|FIELD)", re.I)
RESULT_RX = re.compile(r"\bWON\s+BY\b|MATCH\s+(?:TIED|DRAWN)|\bNO\s+RESULT\b|\bABANDONED\b|\bSUPER\s+OVER\b", re.I)
CHASE_RX = re.compile(r"\bNEED\s+\d+|TARGET\s*[-:]?\s*\d+|\bRRR\b|\bCRR\b", re.I)
OVERS_RX = re.compile(r"\b\d{1,2}\.\d\s*\(\s*\d{1,2}\s*\)")
TEAMV_RX = re.compile(r"\b[A-Z]{3}\s?V\s?[A-Z]{3}\b|\b\d{1,3}\s*[-–]\s*\d{1,2}\s+\d{1,2}\.\d\b")
PLAYWORD_RX = re.compile(r"\bRUN\s+RATE\b|\bCRR\b|\bRRR\b|\bFOURS\b|\bSIXES\b|\bTARGET\b|\bNEED\s+\d+|\bPARTNERSHIP\b|\bINNING\s+\d", re.I)
MATCHNO_RX = re.compile(r"MATCH\s*N[O0GQ]\.?\s*[-:]?\s*(\d{1,3})", re.I)
FINAL_RX = re.compile(r"(?<!SEMI )(?<!SEMI-)(?<!SEMI)(?<!QUARTER )(?<!QUARTER-)\bFINAL\b")
WINNER_RX = re.compile(r"([A-Z0-9][A-Z0-9 .&'-]{2,40}?)\s+WON\s+BY\s+([A-Z0-9 ]{3,30})", re.I)


def fmt(t):
    t = int(t)
    return "%d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60)


def flags(txt):
    u = (txt or "").upper()
    return {"toss": bool(TOSS_RX.search(u)),
            "tossdone": bool(TOSSDONE_RX.search(u)),
            "result": bool(RESULT_RX.search(u)),
            "chase": bool(CHASE_RX.search(u)),
            "play": bool(OVERS_RX.search(u) or PLAYWORD_RX.search(u) or TEAMV_RX.search(u))}


# ---------------------------------------------------------------- pure logic (unit-testable)
def segment(rows, dur, title=""):
    """rows = [(t_seconds, flags_dict, text)] sorted by t. Returns list of match dicts."""
    rows = sorted(rows, key=lambda r: r[0])
    toss = [r for r in rows if r[1]["toss"]]
    blocks = []                                           # [first_t, last_t, tossdone_t|None]
    for t, f, _ in toss:
        if blocks and t - blocks[-1][1] <= TOSS_GAP:
            blocks[-1][1] = t
            if f["tossdone"] and blocks[-1][2] is None:
                blocks[-1][2] = t
        else:
            blocks.append([t, t, t if f["tossdone"] else None])
    valid = []
    for b in blocks:
        n_play = sum(1 for t, f, _ in rows if b[1] < t <= b[1] + PLAY_AFTER_TOSS and f["play"])
        if n_play < MIN_PLAY_FRAMES:
            continue
        # a "toss" ticker in the middle of a match is not a new match: a real new match needs a result banner after the previous start
        if valid and not any(f["result"] for t, f, _ in rows if valid[-1][1] < t < b[0]):
            continue
        valid.append(b)

    out = []
    for i, b in enumerate(valid):
        limit = valid[i + 1][0] if i + 1 < len(valid) else dur
        out.append(_one_match(rows, b[0], b[1], b[2], limit, dur, toss_seen=True, title=title))
    # a result before the first toss block = the VOD began in the middle of a match (no toss in the VOD)
    first_lim = valid[0][0] if valid else dur
    early = [r for r in rows if r[0] < first_lim and r[1]["result"]]
    play_before = [r for r in rows if r[0] < (early[0][0] if early else 0) and r[1]["play"]]
    if early and len(play_before) >= MIN_PLAY_FRAMES:
        out.insert(0, _one_match(rows, play_before[0][0], play_before[0][0], None, first_lim, dur, toss_seen=False, title=title))
    return out


def _one_match(rows, t_first, t_last, t_done, limit, dur, toss_seen, title=""):
    if toss_seen:
        ref = t_done - 60.0 if (t_done is not None and t_done - t_first > TOSS_PENDING_MAX) else t_first
        start = max(0.0, ref - LEAD_IN)
    else:
        start = max(0.0, t_first - LEAD_IN)
    span = [r for r in rows if t_last <= r[0] < limit]
    res = [r for r in span if r[1]["result"]]
    play_n = sum(1 for r in span if r[1]["play"])
    m = {"start": round(start, 1), "end": None, "toss_seen": toss_seen, "result": False,
         "play_frames": play_n, "chase_frames": 0, "match_no": None, "winner": "", "result_at": None, "final": False}
    nos = Counter(MATCHNO_RX.search(r[2].upper()).group(1) for r in span if MATCHNO_RX.search(r[2].upper()))
    if nos:
        m["match_no"] = nos.most_common(1)[0][0]
    if res:
        t_res = res[0][0]
        m["result"] = True
        m["result_at"] = t_res
        n_final = sum(1 for r in span if FINAL_RX.search(r[2].upper()))
        m["final"] = n_final >= 2 or bool(re.search(r"(?<!semi)(?<!semi )(?<!semi-)\bfinal\b", (title or "").lower()))
        tail = FINAL_TAIL if m["final"] else CELEBRATION
        m["end"] = round(min(t_res + tail, limit - 10.0 if limit < dur else dur, dur), 1)
        w = WINNER_RX.search(res[0][2].upper())
        m["winner"] = ("%s WON BY %s" % (w.group(1).strip(), w.group(2).strip())) if w else res[0][2][:60]
        mid = start + (t_res - start) * 0.5
        m["chase_frames"] = sum(1 for r in span if r[0] >= mid and r[0] <= t_res and r[1]["chase"])
    return m


# ---------------------------------------------------------------- video -> strips -> text
def _download_lowres(H, vid):
    os.makedirs(H.WORK, exist_ok=True)
    for f in os.listdir(H.WORK):
        if f.startswith("board_src"):
            os.remove(os.path.join(H.WORK, f))
    hl_review.set_live("download", "Scout: scoreboard ke liye chhota (360p) video download", vid=vid, force=True)
    H.sh(H.ytdlp_base() + ["--no-playlist", "-f", "bv*[height<=%d]/bv*[height<=720]/worst" % BOARD_H,
                           "-o", os.path.join(H.WORK, "board_src.%(ext)s"),
                           "https://www.youtube.com/watch?v=" + vid])
    src = next((os.path.join(H.WORK, f) for f in os.listdir(H.WORK) if f.startswith("board_src")), None)
    if not src:
        raise RuntimeError("scoreboard scout: video download failed")
    return src


def _strips(H, src, out_dir):
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir)
    H.sh(["ffmpeg", "-y", "-loglevel", "error", "-skip_frame", "nokey", "-i", src, "-an",
          "-vf", "fps=1/%d,crop=iw:ih*0.17:0:ih*0.83,scale=1920:-2,format=gray" % STEP,
          os.path.join(out_dir, "b_%05d.png")])
    return sorted(os.listdir(out_dir))


_OCR_ENV = dict(os.environ, OMP_THREAD_LIMIT="1", OMP_WAIT_POLICY="PASSIVE")   # N tesseract procs x N OpenMP threads each = 50x slowdown on a 4-core runner
_OCR_STAT = {"n": 0, "timeouts": 0, "secs": 0.0}


def _ocr(path):
    t0 = time.time()
    try:
        r = subprocess.run(["tesseract", path, "-", "--psm", "6"], capture_output=True, text=True, timeout=25, env=_OCR_ENV)
        txt = re.sub(r"\s+", " ", r.stdout).strip()
    except subprocess.TimeoutExpired:
        _OCR_STAT["timeouts"] += 1
        txt = ""
    except Exception:                                      # noqa
        txt = ""
    _OCR_STAT["n"] += 1
    _OCR_STAT["secs"] += time.time() - t0
    return txt


def scan(H, vid, title="", dur_hint=None):
    """Returns {"dur", "frames", "hits", "matches":[...]} or None when no scoreboard could be read."""
    if not shutil.which("tesseract"):
        H.note("tesseract not installed - scoreboard scout skipped", "warning", "hl scout")
        return None
    src = _download_lowres(H, vid)
    out_dir = os.path.join(H.WORK, "board_strips")
    try:
        dur = H.duration_of(src) or float(dur_hint or 0)
        names = _strips(H, src, out_dir)
        try:
            os.remove(src)
        except OSError:
            pass
        H.log("scoreboard: %d strips, OCR..." % len(names))
        rows = []
        n = len(names)
        with ThreadPoolExecutor(max_workers=max(2, os.cpu_count() or 2)) as ex:
            for i, txt in enumerate(ex.map(lambda nm: _ocr(os.path.join(out_dir, nm)), names)):
                rows.append((i * float(STEP), flags(txt), txt))
                if i % 30 == 0:
                    hl_review.set_live("scout", "Scout: scoreboard padh raha hai (%s)" % fmt(i * STEP), cur=i + 1, total=n)
                    H.log("scoreboard OCR %d/%d, %.2fs/strip, %d timeouts" % (i + 1, n, _OCR_STAT["secs"] / max(1, _OCR_STAT["n"]), _OCR_STAT["timeouts"]))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
        for f in os.listdir(H.WORK):
            if f.startswith("board_src"):
                try:
                    os.remove(os.path.join(H.WORK, f))
                except OSError:
                    pass
    hits = sum(1 for _, f, _ in rows if f["toss"] or f["result"] or f["play"])
    H.log("scoreboard: %d/%d strips had scoreboard text" % (hits, len(rows)))
    if hits < 5:
        return None
    return {"dur": dur, "frames": len(rows), "hits": hits, "matches": segment(rows, dur or rows[-1][0] + STEP, title)}
