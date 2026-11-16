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
CELEBRATION = 45.0      # s after the first result banner (winning run + a short celebration; Naem: 3 min extra at the end was too long)
FINAL_TAIL = 60.0       # a FINAL: winning run + short celebration only (600 s of trophy ceremony made a 19 min highlight with 4-5 min of nothing at the end)
BOARD_H = int(os.environ.get("HL_BOARD_H", "480") or 480)   # 360p was too blurry for the small banner text
BACK_GAP = 480.0        # walking back from the toss: live-play frames this close (s) still belong to the same match
BACK_MAX = 60 * 60.0    # ... but never further back than this from the toss frame
TOSS_PENDING_MAX = 360  # if "TOSS PENDING" sits longer than this before "won the toss", start 60 s before the won-toss frame

TOSS_RX = re.compile(r"\bT[O0]SS\b", re.I)
TOSSDONE_RX = re.compile(r"WON\s+THE\s+T[O0]SS|(?:OPTED|ELECTED|CHOSE|CHOOSE)\s+TO\s+(?:BAT|BOWL|FIELD)", re.I)
RESULT_RX = re.compile(r"\bWON\s+BY\b|MATCH\s+(?:TIED|DRAWN)|\bNO\s+RESULT\b|\bABANDONED\b|\bSUPER\s+OVER\b", re.I)
CHASE2_RX = re.compile(r"\bNEED\s+\d+|TARGET\s*[-:]?\s*\d+|\bRRR\b", re.I)   # 2nd innings only (CRR is shown in the 1st innings too)
CHASE_RX = re.compile(r"\bNEED\s+\d+|TARGET\s*[-:]?\s*\d+|\bRRR\b|\bCRR\b", re.I)
OVERS_RX = re.compile(r"\b\d{1,2}\.\d\s*\(\s*\d{1,2}\s*\)")
OVERS_PAIR = re.compile(r"\b(\d{1,2})\.([0-6])\s*\(\s*(\d{1,2})\s*\)")
TIE_RX = re.compile(r"MATCH\s+TIED|SUPER\s+OVER|\bTIE\b", re.I)
LASTOVER_WINDOW = 300.0   # s before the result banner in which the 2nd-innings over counter is read
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



STOP3 = {"THE", "WON", "AND", "FOR", "TOSS", "RUN", "RUNS", "OVER", "OVERS", "NEED", "MORE", "FROM", "BALL", "BALLS", "LML", "KCC",
         "LIVE", "WITH", "OPT", "BAT", "BOWL", "NOT", "OUT", "TOP", "WIN", "SIX", "TEN", "NEW", "ALL", "MAN", "CUP", "DAY", "MAX",
         "AND", "ARE", "HAS", "WAS", "OFF", "ONE", "TWO", "FOUR", "FREE", "HIT", "WIDE", "SPORTS", "INDIA", "ADS", "NOW", "NEXT"}
IDENT_WINDOW = 1200.0   # s looked at on each side of a toss block to decide whether the match changed


def _ident(rows, a, b):
    """what match is on screen in rows with a <= t < b -> (match_no or None, set of the 2 most frequent 3-letter team codes)."""
    nos, codes = Counter(), Counter()
    for t, f, txt in rows:
        if not (a <= t < b):
            continue
        u = (txt or "").upper()
        m = MATCHNO_RX.search(u)
        if m:
            nos[m.group(1)] += 1
        for c in set(re.findall(r"\b[A-Z]{3}\b", u)):
            if c not in STOP3:
                codes[c] += 1
    no = nos.most_common(1)[0][0] if nos and nos.most_common(1)[0][1] >= 3 else None
    top = {c for c, n in codes.most_common(2) if n >= 4}
    return no, top


def ident_changed(rows, t_prev_start, t_block_first, t_block_last):
    """True when the scoreboard shows a DIFFERENT match after this toss block than before it (other match number or other teams).
    Used when the previous match's result banner was not read by OCR, so two matches would be glued into one window."""
    no_a, tm_a = _ident(rows, max(t_prev_start, t_block_first - IDENT_WINDOW), t_block_first)
    no_b, tm_b = _ident(rows, t_block_last, t_block_last + IDENT_WINDOW)
    if no_a and no_b and no_a != no_b:
        return True
    return len(tm_a) >= 2 and len(tm_b) >= 2 and not (tm_a & tm_b)


# ---------------------------------------------------------------- pure logic (unit-testable)
NEW_MATCH_MIN_GAP = 35 * 60      # a new match starts at least this long after the previous one's toss
NEW_MATCH_PLAY = 100             # ... and has this many live-scoreboard frames (15 s each, ~25 min) in the 40 min after its toss



# ---------------------------------------------------------------- over-counter logic (toss text is unreliable, the counter is not)
def _readings(rows, a, b):
    """[(t, balls_bowled, match_overs)] from the over counter "4.1 (8)" in rows a <= t < b (only the most common match-overs value)."""
    out = []
    for t, f, txt in rows:
        if not (a <= t < b):
            continue
        hit = None
        for m in OVERS_PAIR.finditer((txt or "").upper()):
            ov, ball, mx = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= mx <= 50 and ov <= mx:
                hit = (t, ov * 6 + ball, mx)
        if hit:
            out.append(hit)
    if not out:
        return out
    mx0 = Counter(r[2] for r in out).most_common(1)[0][0]
    return [r for r in out if r[2] == mx0]


def _smooth(reads, k=5):
    """median filter over neighbouring readings: one misread digit must not look like a reset."""
    if len(reads) < k:
        return list(reads)
    h = k // 2
    out = []
    for i, (t, _, mx) in enumerate(reads):
        w = sorted(r[1] for r in reads[max(0, i - h):i + h + 1])
        out.append((t, w[len(w) // 2], mx))
    return out


def _innings_runs(reads):
    """split the counter readings into runs of rising overs (= innings); a drop of more than 2 overs starts a new run. Runs < 4 readings = noise."""
    runs = []
    for r in _smooth(reads):
        if runs and r[1] >= runs[-1][-1][1] - 12:
            runs[-1].append(r)
        else:
            runs.append([r])
    return [x for x in runs if len(x) >= 4]


def _counter_reset(rows, t0, t1):
    """Does a NEW MATCH start around the toss block t0..t1?  A new match = the over counter falls back to ~0 after a 2nd innings
    (chase banners just before). True / False; None = counter unreadable around here (caller falls back to the old heuristics).
    Why: a 'TOSS' ticker in the middle of the chase made the scout cut the final in two."""
    reads = _smooth(_readings(rows, t0 - 1200.0, t1 + 2400.0))
    if sum(1 for r in reads if r[0] < t0) < 3 or sum(1 for r in reads if r[0] > t1) < 3:
        return None
    for a, b in zip(reads, reads[1:]):
        if b[1] <= 6 and a[1] - b[1] >= 12 and any(CHASE2_RX.search(x or "") for t, f, x in rows if b[0] - 1200.0 <= t < b[0]):
            return True
    return False


def _first_ball(rows, floor, t_res):
    """First ball of the match that ends with the result banner at t_res, from the over counter alone.
    -> (start_estimate_s, first_reading_t, first_reading_balls) or None.
    1st innings = the counter run before the chase run; its first reading minus (balls bowled so far x seconds-per-ball) = first ball."""
    raw = _readings(rows, floor, t_res + 1.0)
    runs = _innings_runs(raw)
    if len(runs) < 2:
        return None
    last, prev = runs[-1], runs[-2]
    if t_res - last[-1][0] > 900.0 or last[0][0] - prev[-1][0] > 45 * 60.0:
        return None                                         # the chase does not reach the result / far too long a break
    if any(f["result"] for t, f, _ in rows if prev[0][0] < t < t_res):
        return None                                         # a result banner in between -> prev run is an older match
    if sum(1 for t, f, x in rows if prev[0][0] <= t <= prev[-1][0] and CHASE2_RX.search(x or "")) >= 3:
        return None                                         # prev run has target/need banners -> it was a 2nd innings
    t1, b1 = prev[0][0], prev[0][1]
    for rt, rb, _ in raw:                                   # the median filter drags the first readings up (0.0 -> 0.3): the earliest RAW reading of the run is the truth
        if prev[0][0] - 300.0 <= rt <= prev[0][0] + 600.0 and rb <= b1:
            t1, b1 = rt, rb
            break
    span_b = prev[-1][1] - b1
    rate = (prev[-1][0] - t1) / span_b if span_b >= 12 else 40.0
    rate = min(max(rate, 8.0), 90.0)
    est = max(t1 - b1 * rate, t1 - 40 * 60.0, floor)
    return est, t1, b1


def _play_run_start(rows, t_ref, floor=0.0, gap=BACK_GAP, max_back=BACK_MAX):
    """Earliest t of the unbroken run of live-scoreboard ("play") frames that ends at t_ref (walk backwards).
    Stops at a hole bigger than `gap`, at a result banner, at `floor` or at `max_back`. None = no play frames right before t_ref.
    Why: the "TOSS" text can show up on the strip only some overs AFTER the real start (ticker / late banner) -> the first ball is earlier."""
    first, last = None, t_ref
    for t, f, _ in sorted((r for r in rows if r[0] < t_ref), key=lambda r: -r[0]):
        if t <= floor or t < t_ref - max_back or f["result"]:
            break
        if f["play"]:
            if last - t > gap:
                break
            first, last = t, t
        elif last - t > gap:
            break
    return first


def _floor(rows, t_first, t_prev=0.0):
    """Nothing of THIS match can be before the previous result banner / the previous toss block."""
    return max([t for t, f, _ in rows if f["result"] and t < t_first] + [t_prev, 0.0])


def _long_new_match(rows, prev, b):
    """Neither a result banner nor a team change was read between two toss blocks (OCR misses on some broadcasts).
    Still a NEW match when the previous one clearly had a 2nd innings (chase banners) and a long live stretch follows this toss -
    a 'toss' ticker in the middle of the chase is followed by the end of the match only (too short)."""
    if b[0] - prev[0] < NEW_MATCH_MIN_GAP:
        return False
    if not any(f["chase"] for t, f, _ in rows if prev[1] < t < b[0]):
        return False
    return sum(1 for t, f, _ in rows if b[1] < t <= b[1] + 40 * 60 and f["play"]) >= NEW_MATCH_PLAY


def segment(rows, dur, title="", last=False, debug=None):
    """rows = [(t_seconds, flags_dict, text)] sorted by t. Returns list of match dicts.
    last=True: only the LAST match of the VOD (a relay holds 4-5 matches, the final is the last one) - see _last_match."""
    rows = sorted(rows, key=lambda r: r[0])
    if last:
        m = _last_match(rows, dur, title, debug)
        if m:
            return [m]
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
            cr = _counter_reset(rows, b[0], b[1])
            if cr is False:                               # over counter keeps rising through this "toss" text -> same match (ticker), whatever the OCR of team names says
                continue
            if cr is None and not ident_changed(rows, valid[-1][0], b[0], b[1]) and not _long_new_match(rows, valid[-1], b):
                continue
        valid.append(b)

    out = []
    for i, b in enumerate(valid):
        limit = valid[i + 1][0] if i + 1 < len(valid) else dur
        out.append(_one_match(rows, b[0], b[1], b[2], limit, dur, toss_seen=True, title=title,
                              floor=_floor(rows, b[0], valid[i - 1][1] if i else 0.0)))
    # a result before the first toss block = the VOD began in the middle of a match (no toss in the VOD)
    first_lim = valid[0][0] if valid else dur
    early = [r for r in rows if r[0] < first_lim and r[1]["result"]]
    play_before = [r for r in rows if r[0] < (early[0][0] if early else 0) and r[1]["play"]]
    if early and len(play_before) >= MIN_PLAY_FRAMES:
        out.insert(0, _one_match(rows, play_before[0][0], play_before[0][0], None, first_lim, dur, toss_seen=False, title=title))
    return out


def _last_match(rows, dur, title="", debug=None):
    """The last match of a VOD whose scoreboard banners were only partly readable (first-result logic then glues 4-5 matches into one).
    end   = the LAST result banner cluster (+ cup-lift tail for a final).
    start = toss block of that match: among the toss blocks before the result that have live play after them, walk back from the
            nearest one while the scoreboard still shows the final's teams / match number (same match = same two team codes);
            teams unreadable -> the last block that is at least 25 min before the result (a match is never shorter)."""
    res_rows = [r for r in rows if r[1]["result"]]
    if not res_rows:
        return None
    clusters = []
    for r in res_rows:
        if clusters and r[0] - clusters[-1][-1] <= TOSS_GAP:
            clusters[-1].append(r[0])
        else:
            clusters.append([r[0]])
    t_res = clusters[-1][0]
    blocks = []
    for t, f, _ in [r for r in rows if r[1]["toss"]]:
        if blocks and t - blocks[-1][1] <= TOSS_GAP:
            blocks[-1][1] = t
            if f["tossdone"] and blocks[-1][2] is None:
                blocks[-1][2] = t
        else:
            blocks.append([t, t, t if f["tossdone"] else None])
    cands = [b for b in blocks if b[0] < t_res - 25 * 60
             and sum(1 for t, f, _ in rows if b[1] < t <= b[1] + PLAY_AFTER_TOSS and f["play"]) >= MIN_PLAY_FRAMES]
    if debug is not None:
        debug.append("result banners at: " + ", ".join(fmt(c[0]) for c in clusters))
        debug.append("toss blocks: " + ", ".join("%s-%s%s" % (fmt(b[0]), fmt(b[1]), "*" if b[2] is not None else "") for b in blocks))
        debug.append("candidates for the last match: " + ", ".join(fmt(b[0]) for b in cands))
    if not cands:
        # toss text never read: the match still has a first ball -> start from the unbroken live-scoreboard run that ends at the result
        t0 = _play_run_start(rows, t_res, clusters[-2][-1] if len(clusters) > 1 else 0.0, gap=900.0, max_back=4 * 3600.0)
        if t0 is None or t_res - t0 < 25 * 60:
            return None
        m = _one_match(rows, t0, t0, None, dur, dur, toss_seen=False, title=title)
        m["last_pick"] = True
        return m
    no_f, codes_f = _ident(rows, t_res - IDENT_WINDOW, t_res)
    pick = cands[-1]
    if len(codes_f) >= 2 or no_f:
        i = len(cands) - 1
        same = []
        while i >= 0:
            b = cands[i]
            no_b, codes_b = _ident(rows, b[1], b[1] + IDENT_WINDOW)
            if (no_f and no_b and no_f == no_b) or (len(codes_f) >= 2 and len(codes_b & codes_f) >= 2):
                same.append(b)
                i -= 1
            else:
                break
        if same:
            pick = same[-1]                      # earliest block of the unbroken run that shows the final's teams
        if debug is not None:
            debug.append("final's teams %s / match no %s -> start block %s" % (sorted(codes_f), no_f, fmt(pick[0])))
    m = _one_match(rows, pick[0], pick[1], pick[2], dur, dur, toss_seen=True, title=title, floor=_floor(rows, pick[0]))
    m["last_pick"] = True
    return m


def _last_over_finish(rows, t_res, text_res=""):
    """Did the 2nd innings end in the LAST over?  -> (True|False|None, "5.2/6" | "").
    Reads the over counter "5.2 (6)" (overs bowled, match overs) from the frames just before the result banner; a tie / super over counts."""
    if TIE_RX.search(text_res or ""):
        return True, "tie"
    best = None
    for t, f, txt in rows:
        if t_res - LASTOVER_WINDOW <= t < t_res:
            for m in OVERS_PAIR.finditer((txt or "").upper()):
                ov, ball, mx = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 1 <= mx <= 50 and ov <= mx:
                    best = (t, ov, ball, mx)               # the LAST readable counter before the result
    if not best:
        return None, ""
    _, ov, ball, mx = best
    return ov >= mx - 1, "%d.%d/%d" % (ov, ball, mx)


def _one_match(rows, t_first, t_last, t_done, limit, dur, toss_seen, title="", floor=0.0):
    if toss_seen:
        ref = t_done - 60.0 if (t_done is not None and t_done - t_first > TOSS_PENDING_MAX) else t_first
        early = _play_run_start(rows, t_first, floor)
        if early is not None and early < ref:
            no_a, tm_a = _ident(rows, early, t_first)
            no_b, tm_b = _ident(rows, t_first, t_first + IDENT_WINDOW)
            other = (no_a and no_b and no_a != no_b) or (len(tm_a) >= 2 and len(tm_b) >= 2 and not (tm_a & tm_b))
            if not other:
                ref = early                       # play was already on screen before the toss text: start at the first ball
        start = max(0.0, ref - LEAD_IN)
    else:
        start = max(0.0, t_first - LEAD_IN)
    span = [r for r in rows if t_last <= r[0] < limit]
    res = [r for r in span if r[1]["result"]]
    play_n = sum(1 for r in span if r[1]["play"])
    m = {"start": round(start, 1), "end": None, "toss_seen": toss_seen, "result": False,
         "play_frames": play_n, "chase_frames": 0, "match_no": None, "winner": "", "result_at": None, "final": False, "last_over": None, "overs_at_end": ""}
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
        m["last_over"], m["overs_at_end"] = _last_over_finish(rows, t_res, res[0][2])
        m["start_src"] = "toss" if toss_seen else "first_ball"
        cb = _first_ball(rows, floor, t_res)
        if cb:
            cb_start = round(max(0.0, cb[0] - LEAD_IN), 1)
            # toss found: the counter estimate may only pull the start EARLIER (1st-innings board unread -> counter starts late -> never push start later than the toss start)
            if not toss_seen or cb_start < m["start"]:
                m["start"] = cb_start
                m["start_src"] = "counter"
                m["start_note"] = "pehli ball over counter se: pehla reading %s (%.1f over), start %s" % (fmt(cb[1]), cb[2] // 6 + (cb[2] % 6) / 10.0, fmt(m["start"]))
    elif limit < dur and play_n >= MIN_PLAY_FRAMES and any(r[1]["chase"] for r in span):
        # no "WON BY" banner was read, but another match starts right after and this one had a chase (2nd innings):
        # it ended where its last live-scoreboard frame is
        last_play = [r[0] for r in span if r[1]["play"] and r[0] < limit - 60.0]
        if last_play:
            m["result"] = True
            m["result_at"] = last_play[-1]
            m["end"] = round(min(last_play[-1] + CELEBRATION, limit - 10.0), 1)
            m["winner"] = "(result banner nahi padha, agle match se pehle ka last ball)"
            m["chase_frames"] = sum(1 for r in span if r[1]["chase"])
            m["last_over"], m["overs_at_end"] = _last_over_finish(rows, last_play[-1] + 30.0)
    return m


# ---------------------------------------------------------------- video -> strips -> text
def _download_lowres(H, vid):
    os.makedirs(H.WORK, exist_ok=True)
    for f in os.listdir(H.WORK):
        if f.startswith("board_src"):
            os.remove(os.path.join(H.WORK, f))
    hl_review.set_live("download", "Scout: scoreboard ke liye chhota (360p) video download", vid=vid, force=True)
    H.ytdlp_dl("bv*[height<=%d]/bv*[height<=720]/worst" % BOARD_H,
               ["-o", os.path.join(H.WORK, "board_src.%(ext)s"),
                "https://www.youtube.com/watch?v=" + vid], "board download")
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


def scan(H, vid, title="", dur_hint=None, last=False):
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
    for t, f, txt in rows:
        if txt:
            H.log("OCR %s %s%s%s%s%s %s" % (fmt(t), "T" if f["toss"] else "-", "D" if f["tossdone"] else "-", "R" if f["result"] else "-",
                                           "C" if f["chase"] else "-", "P" if f["play"] else "-", txt[:90]))
    dbg = []
    ms = segment(rows, dur or rows[-1][0] + STEP, title, last=last, debug=dbg)
    for d in dbg:
        H.log("board debug: " + d)
    return {"dur": dur, "frames": len(rows), "hits": hits, "matches": ms, "debug": dbg}
