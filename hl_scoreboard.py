"""
SCOREBOARD reader (OCR of the broadcast overlay at the bottom of the frame).

The LML streams show a scoreboard strip all the time:
  * a rotating text line: "<TEAM> WON THE TOSS AND OPTED TO BOWL", "<TEAM> PLAYERS ...", "TOSS PENDING",
    "TARGET - 134", "<TEAM> NEED 41 MORE FROM 11 BALLS", "<TEAM> WON BY 7 WICKETS", "MATCH NO. 11"
  * the score box  "PRA v SAT  93-2   5.1 (7)"

That is exact where commentary keywords are not (Hindi/Hinglish commentary, one VOD = many matches):

  (finding the matches inside a whole-day VOD is hl_board.py)
  analyze_window(H, src, dur)   -> ball-by-ball events (four / six / wicket / wide-noball / dot) of ONE match window,
                                   innings break and where the last over starts (for the cut plan in hl_cutter)

Needs: tesseract-ocr (apt), pillow (pip), ffmpeg. Pure parsing helpers are testable without video.
"""
import glob
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

SCAN_STEP = int(os.environ.get("HL_SCAN_STEP", "30"))       # s between frames when looking for matches in the whole VOD
BALL_STEP = int(os.environ.get("HL_BALL_STEP", "4"))        # s between frames when reading the score inside one match
WORKERS = max(2, min(6, (os.cpu_count() or 4)))
RESULT_TAIL = 45.0            # s of celebration kept after the "WON BY" banner first shows (normal match)
FINAL_TAIL = 60.0             # final: winning run + short celebration (the cup ceremony is not part of the highlight)
TOSS_BACK = 35.0              # s before the toss banner (captain / commentator talk starts earlier)
MIN_PLAY_FRAMES = 12          # a match needs at least this many scoreboard frames before a result counts (~4 min at 20 s)

# strip = bottom 20% of the frame, full width
STRIP_Y0 = 0.80
# score box / overs box, as fractions of the FULL frame: (x0, y0, x1, y1). Two broadcast layouts seen so far.
LAYOUTS = [
    {"name": "A", "score": (0.445, 0.888, 0.559, 0.947), "overs": (0.564, 0.888, 0.625, 0.947)},
    {"name": "B", "score": (0.176, 0.886, 0.289, 0.942), "overs": (0.326, 0.886, 0.377, 0.942)},
    # C: North Goa Premier League final (2026-11-15): yellow bar in the 1st innings (blue digits), dark bar in the 2nd (yellow digits);
    #    "51-2  4.4(8)" sits left of the batters; A and B read 0 of 56 frames of that VOD -> commentary fallback -> 26 fake wickets, 19 min
    {"name": "C", "score": (0.186, 0.880, 0.256, 0.938), "overs": (0.262, 0.884, 0.312, 0.934), "auto": True},
    # D: same NGPL final, FIRST ~4 BALLS of the innings (2026-11-15): the score sits in the white CENTRE panel "SHRU 11  4-0  0.2 (8)"
    #    (the yellow left bar only shows the batters until the 4th ball). C read nothing there and A read it with the "0." cut off
    #    (".3 (8)" -> 3 overs = 18 balls), so the first 4 balls were invisible and the highlight opened on the 5th ball.
    #    strict: both texts must be a COMPLETE reading (digits-digit / d.d(d)) or the layout does not count.
    {"name": "D", "score": (0.465, 0.890, 0.550, 0.955), "overs": (0.549, 0.905, 0.602, 0.945), "strict": True},
]
RX_STRICT_SCORE = re.compile(r"^\d{1,3}-\d$")
RX_STRICT_OVERS = re.compile(r"^\d{1,2}\.\d\(\d{1,2}\)$")
_LAYOUT_HINT = [0]          # index of the layout that parsed last: tried first (saves 4 OCR calls per frame)

# ------------------------------------------------------------------ text parsing (pure)
RX_TOSS = re.compile(r"T[O0]SS")
RX_TOSS_DONE = re.compile(r"W[O0]N\s*THE\s*T[O0]SS|OPTED|ELECTED\s*T[O0]|CHOSE\s*T[O0]")
RX_PRE = re.compile(r"PLAYERS|T[O0]SS\s*PEND|NEXT\s*MATCH|UP\s*NEXT|PLAYING\s*(?:XI|11)|LINE\s*UPS?")
RX_RESULT = re.compile(r"W[O0]N\s*[BE8][YV]\s*\d+\s*(?:WICKET|RUN|WKT|W\b|R\b)|MATCH\s*(?:TIED|DRAW)|\bTIED\b|SUPER\s*[O0]VER")
RX_MNO = re.compile(r"MATCH\s*N[O0G]?\.?\W{0,3}([0-9ILT|]{1,2})\b")
RX_FINAL = re.compile(r"(?<!SEMI )(?<!SEMI-)(?<!SEMI)(?<!QUARTER )(?<!QUARTER-)\bFINAL\b")
RX_TEAMS = re.compile(r"([A-Z]{3})\s*[vV]\s*([A-Z]{3})")
RX_SCORE = re.compile(r"(\d{1,3})\s*-\s*(\d)")
RX_OVERS = re.compile(r"(\d{1,2})(?:\s*[.,]\s*(\d))?\s*\(\s*(\d{1,2})\s*\)")


def norm(text):
    return re.sub(r"[^A-Z0-9.|\- ]+", " ", (text or "").upper())


def parse_flags(text):
    """text of the strip (any case) -> dict of booleans/values used by the match finder."""
    t = norm(text)
    mno = None
    m = RX_MNO.search(t)
    if m:
        digits = m.group(1).replace("I", "1").replace("L", "1").replace("T", "1").replace("|", "1")
        if digits.isdigit() and 1 <= int(digits) <= 99:
            mno = int(digits)
    tm = RX_TEAMS.search(text or "")
    return {"toss": bool(RX_TOSS.search(t)), "toss_done": bool(RX_TOSS_DONE.search(t)), "pre": bool(RX_PRE.search(t)),
            "result": bool(RX_RESULT.search(t)), "final": bool(RX_FINAL.search(t)), "mno": mno,
            "teams": (tm.group(1), tm.group(2)) if tm else None}


def parse_score(score_txt, overs_txt):
    """OCR of the two small boxes -> (runs, wickets, balls, max_overs) or None."""
    ms = RX_SCORE.search(score_txt or "")
    mo = RX_OVERS.search(overs_txt or "")
    if not ms or not mo:
        return None
    runs, wk = int(ms.group(1)), int(ms.group(2))      # first digit after '-' only (OCR often adds a stray 7)
    o, b, mx = int(mo.group(1)), int(mo.group(2) or 0), int(mo.group(3))
    if b > 5 or mx < 1 or mx > 50 or o > mx or runs > 400:
        return None
    return runs, wk, o * 6 + b, mx


def stable_states(rows, need=2):
    """rows [(t, runs, wk, balls, mx)] -> list of states that persisted for >= `need` consecutive samples (kills OCR glitches)."""
    out = []
    i = 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and rows[j + 1][1:4] == rows[i][1:4]:
            j += 1
        if j - i + 1 >= need:
            out.append({"t": rows[i][0], "t_last": rows[j][0], "runs": rows[i][1], "wk": rows[i][2], "balls": rows[i][3], "mx": rows[i][4]})
        i = j + 1
    return out


def _ok_step(a, g):
    """is scoreboard state g a believable next state after a (same innings)?"""
    dr, dw, db = g["runs"] - a["runs"], g["wk"] - a["wk"], g["balls"] - a["balls"]
    # one ball = at most 6 (+1 for a no-ball): 87 -> 99 in ONE ball is an OCR misread (89 read as 99), not a state
    # 2026-11-16: db up to 8 / dw up to 2 -- in the NGPL final's last over the operator typed 18 runs + a wicket after 6 balls
    return 0 <= dr <= 7 * max(1, db) + 1 and 0 <= dw <= 2 and 0 <= db <= 8


def consistent_states(rows, need=2):
    """rows [(t, runs, wk, balls, mx)] -> believable states. Within an innings runs / wickets / balls can only go up by a
    little, so a misread number (48 -> 46 -> 48, 3 -> 8 wickets ...) is dropped instead of becoming a fake six.
    A new innings (everything resets) or a lost track is accepted only after 3 readings in a row agree with each other."""
    groups = []
    i = 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and rows[j + 1][1:4] == rows[i][1:4]:
            j += 1
        if j - i + 1 >= need:
            groups.append({"t": rows[i][0], "t_last": rows[j][0], "runs": rows[i][1], "wk": rows[i][2],
                           "balls": rows[i][3], "mx": rows[i][4]})
        i = j + 1
    out, pend = [], []
    for g in groups:
        if not out:
            out.append(g)
            continue
        a = out[-1]
        if g["runs"] == a["runs"] and g["wk"] == a["wk"] and g["balls"] == a["balls"]:
            a["t_last"] = g["t_last"]
            pend = []
        elif _ok_step(a, g):
            out.append(g)
            pend = []
        else:
            pend.append(g)
            if len(pend) >= 3 and _ok_step(pend[0], pend[1]) and _ok_step(pend[1], pend[2]):
                out.extend(pend)                 # new innings / re-synced: three readings agree
                pend = []
            elif len(pend) > 6:
                pend = pend[-3:]
    return out


def events_from_states(states, lag=4.0):
    """consecutive stable scoreboard states -> ball events + innings split. Pure function (unit tested).
    A four / six needs exactly ONE legal ball between the two states (4+2 over two balls is not a six)."""
    events, inn = [], 0
    innings = [[]]
    prev = None
    for s in states:
        if prev is not None:
            # new innings = overs AND score (or wickets) really fell back. A single misread digit (99 for 89) is not an innings.
            if s["balls"] + 6 < prev["balls"] and (s["runs"] + 6 < prev["runs"] or s["wk"] < prev["wk"]):
                inn += 1
                innings.append([])
                prev = None
        innings[inn].append(s)
        if prev is not None:
            dr, dw, db = s["runs"] - prev["runs"], s["wk"] - prev["wk"], s["balls"] - prev["balls"]
            t = max(prev["t_last"], s["t"] - lag)
            kind = None
            if dw >= 1:
                kind = "wicket"          # any state jump that holds a wicket (was: only dr <= 4 and db <= 2 -> a wicket typed after 3+ balls vanished)
            elif dw == 0 and db == 1 and dr in (4, 5):
                kind = "four"
            elif dw == 0 and db == 1 and dr in (6, 7):
                kind = "six"
            elif dw == 0 and db == 0 and dr >= 1:
                kind = "noball" if dr >= 2 else "wide"
            elif dw == 0 and db == 1 and dr in (0, 1, 2, 3):
                kind = "dot"            # a quiet LEGAL ball (dot / 1 / 2 / 3 runs). Before 2026-11-15 only dr == 0 counted: every single / double
                                        # was invisible, so the match's FIRST ball and the WINNING ball (2 runs) never became events
            elif dw == 0 and db >= 2 and dr <= 3 * db:
                kind = "dot"            # the operator typed the score only every 2-3 balls: still a legal ball at that moment
            if kind:
                events.append({"kind": kind, "t": t, "t_end": t + 2.0, "t_state": s["t"], "inn": inn, "balls": s["balls"], "mx": s["mx"],
                               "runs": dr, "text": "%d-%d %s" % (s["runs"], s["wk"], "%d.%d" % divmod(s["balls"], 6)), "db": 0.0})
            # several balls typed at once with big runs and / or a wicket: which of them were fours / sixes is unknown -> placeholder;
            # analyze_window scans the time between the two score readings for the FOUR / SIX / WICKET graphics and turns them into events
            if db >= 2 and (dr > 3 * db or dw >= 1) and dr >= 4:
                events.append({"kind": "burst", "t": max(prev["t_last"] - 2.0, 0.0), "t_end": s["t"], "t_state": s["t"], "t_prev": prev["t_last"], "inn": inn,
                               "balls": s["balls"], "mx": s["mx"], "runs": dr, "db": 0.0,
                               "text": "%d-%d %s (+%d in %d balls)" % (s["runs"], s["wk"], "%d.%d" % divmod(s["balls"], 6), dr, db)})
        prev = s
    return events, innings


# ------------------------------------------------------------------ banner (SIX / FOUR / WICKET graphic) -> real ball time
RX_BANNER = (("six", re.compile(r"\bS[1I]X(?:ES)?\b")),
             ("four", re.compile(r"\bF[O0]UR\b")),
             ("wicket", re.compile(r"W[1I]CKET|\bOUT\b|B[O0]WLED|CAUGHT")))
BANNER_BOX = (0.30, 0.885, 0.70, 0.975)          # centre of the bottom strip (fractions of the full frame)


RX_BANNER_WORD = (("six", re.compile(r"S[1I]X(?:ES)?")), ("four", re.compile(r"F[O0]UR")), ("wicket", re.compile(r"W[1I]CKET")))


def banner_kind(text):
    """the graphic is a STANDALONE word in the middle of the bar ("SIX", "WICKET"); the ticker also has "FOURS 1 SIXES 1" /
    "NOT OUT" in longer text, so only a short text counts.
    NGPL final (2026-11-15): the graphic is the word repeated across the bar ("FOUR  FOUR  FOUR  F" = 15 letters, the old length
    limit threw it away) or spaced out ("S I X"): a text that is ONLY the same keyword repeated (>= 2 times, <= 2 stray letters) counts."""
    t = norm(text)
    comp = re.sub(r"[^A-Z0-9]", "", t)
    if len(comp) <= 11:
        for k, rx in RX_BANNER:
            if rx.search(t):
                return k
        for k, rx in RX_BANNER_WORD:                  # letters spaced out: "S I X"
            if rx.fullmatch(comp):
                return k
        return None
    for k, rx in RX_BANNER_WORD:
        n = len(rx.findall(comp))
        if n >= 2 and len(rx.sub("", comp)) <= 2:
            return k
    return None


def read_banner(path):
    Image, _ = _pil()
    img = Image.open(path)
    return banner_kind(_ocr_img(_box(img, BANNER_BOX), psm=7, scale=3))


def banner_present(path):
    """OCR cannot reliably read the SIX / FOUR / WICKET graphic (white letters on a dark gradient: "S I X" came out as "SX", "CIY"),
    but the graphic is unmistakable in the middle of the bar: it turns DARK (57-74 % of the pixels) with big WHITE letters (15-25 %),
    while the normal bar is yellow / grey (dark ~14 %, white ~0 %). Measured on the NGPL final (2026-11-15), 6 of 6 graphics.
    -> True when a graphic of ANY kind is on screen (the kind is known from the score change)."""
    Image, _ = _pil()
    try:
        import numpy as np
    except Exception:                                   # noqa
        return False
    img = Image.open(path)
    c = np.asarray(_box(img, (0.43, 0.905, 0.57, 0.955)).convert("RGB")).astype(int)
    if c.size == 0:
        return False
    white = float((c.min(axis=2) > 215).mean())
    dark = float((c.mean(axis=2) < 110).mean())
    return dark > 0.45 and 0.08 < white < 0.40


def banner_onsets(frames):
    """[(t, kind|None)] sorted by t -> [(t_first, kind)] for every run of the same banner kind (first frame it shows)."""
    out, last = [], None
    for t, k in frames:
        if k and k != last:
            out.append((t, k))
        last = k
    return out


# The SIX / FOUR / WICKET graphic shows AFTER the ball has been played (measured on the 2026-11-14 match, frame by frame):
# six ~9 s after the shot (ball flight + landing first), four ~10 s, wicket 10-18 s (catch / celebration first). The old
# back=2.0 put the "hit" 7-16 s too late, so every clip started with the ball already in the air / the players celebrating.
BACK_BY_KIND = {"six": 9.0, "four": 9.5, "wicket": 13.0}


def align_events(events, onsets, default_lag=8.0, back=None, state_ts=None):
    """Put every event at the moment the ball was hit, not when the operator typed the score.
    The scoreboard updates several seconds AFTER the shot; the SIX / FOUR / WICKET graphic comes right after it.
    For events that have a matching graphic: t = graphic onset - `back`. The median (score - ball) delay of those is applied to
    the others (wide / no ball / dot)."""
    bk = dict(BACK_BY_KIND) if back is None else ({k: float(back) for k in BACK_BY_KIND} if not isinstance(back, dict) else dict(back))
    col, last_on = [], {}
    for tb, k in sorted(onsets):                           # same graphic again within 20 s = replay / repeat, not a new ball
        if tb - last_on.get(k, -999.0) >= 20.0:
            col.append((tb, k))
        last_on[k] = tb if tb - last_on.get(k, -999.0) >= 20.0 else last_on[k]
    onsets = col
    lags = []
    for e in events:
        if e["kind"] in ("six", "four", "wicket"):
            cand = [(abs(e["t_state"] - tb), tb) for tb, k in onsets if k == e["kind"] and e["t_state"] - 30.0 <= tb <= e["t_state"] + 4.0]
            if cand:
                tb = min(cand)[1]
                e["t_banner"] = tb
                e["t_hit"] = max(0.0, tb - bk.get(k, 2.0))
                lags.append(e["t_state"] - e["t_hit"])
    lags.sort()
    lag = min(20.0, max(3.0, lags[len(lags) // 2])) if len(lags) >= 3 else default_lag
    for e in events:
        if "t_hit" not in e:
            e["t_hit"] = max(0.0, e["t_state"] - lag)
        e["t"] = e["t_hit"]
        e["t_end"] = e["t_hit"]
    # the operator sometimes types the score only after 2-3 balls: the state jump cannot be pinned to one ball, but the
    # SIX / FOUR / WICKET graphic still shows for every one of them -> a graphic with no event near it IS an event
    added = 0
    for tb, k in onsets:
        if any(e["kind"] == k and abs(e["t_hit"] - (tb - bk.get(k, 2.0))) <= 30.0 for e in events):
            continue
        # a graphic with the score frozen all around it (crowd on the pitch, operator testing) is not a ball: the scoreboard
        # must change within 45 s before / 90 s after it (the operator types the score up to ~2-3 balls late)
        if state_ts is not None and not any(tb - 45.0 <= st <= tb + 90.0 for st in state_ts):
            continue
        events.append({"kind": k, "t": max(0.0, tb - bk.get(k, 2.0)), "t_end": max(0.0, tb - bk.get(k, 2.0)), "t_hit": max(0.0, tb - bk.get(k, 2.0)),
                       "t_state": tb + lag, "inn": (min(events, key=lambda e: abs(e["t_hit"] - tb))["inn"] if events else 0),
                       "balls": 0, "mx": 0, "text": "graphic %s" % k, "db": 0.0, "from_banner": True, "t_banner": tb})
        added += 1
    events.sort(key=lambda e: e["t"])
    return lag, len(lags), added


# ------------------------------------------------------------------ ffmpeg + tesseract plumbing
def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def have_ocr():
    return bool(shutil.which("tesseract")) and _pil() is not None


def _pil():
    try:
        from PIL import Image, ImageOps   # noqa
        return Image, ImageOps
    except Exception:                      # noqa
        return None


def sample_frames(video, t0, t1, step, outdir, strip=True):
    """one PNG every `step` s of [t0, t1], keyframes only (fast on a 4 h file). Returns [(t, path)].
    strip=True crops the bottom 20% (scoreboard) and keeps it full width."""
    os.makedirs(outdir, exist_ok=True)
    for f in glob.glob(os.path.join(outdir, "*.png")):
        os.remove(f)
    vf = "fps=1/%d" % step
    if strip:
        vf += ",crop=iw:ih*%.3f:0:ih*%.3f" % (1 - STRIP_Y0, STRIP_Y0)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-skip_frame", "nokey", "-ss", "%.2f" % t0, "-to", "%.2f" % t1, "-i", video,
           "-an", "-sn", "-vf", vf, os.path.join(outdir, "f_%06d.png")]
    r = _run(cmd)
    files = sorted(glob.glob(os.path.join(outdir, "f_*.png")))
    if not files:
        raise RuntimeError("frame sampling failed: %s" % (r.stderr or "")[-300:])
    return [(t0 + i * step, p) for i, p in enumerate(files)]


def _ocr_img(img, psm=6, whitelist=None, scale=3, invert=False, auto_invert=False):
    Image, ImageOps = _pil()
    g = img.convert("L")
    g = g.resize((g.width * scale, g.height * scale), Image.LANCZOS)
    g = ImageOps.autocontrast(g)
    if auto_invert:                                  # dark bar + light digits -> make it dark digits on a light bar
        px = list(g.resize((16, 4)).getdata())
        invert = (sum(px) / float(len(px))) < 120
    if invert:
        g = ImageOps.invert(g)
    fd, p = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        g.save(p)
        cmd = ["tesseract", p, "-", "--psm", str(psm)]
        if whitelist:
            cmd += ["-c", "tessedit_char_whitelist=" + whitelist]
        env = dict(os.environ, OMP_THREAD_LIMIT="1")
        return _run(cmd, env=env).stdout.strip()
    finally:
        try:
            os.remove(p)
        except OSError:
            pass


def ocr_strip_text(path):
    Image, _ = _pil()
    return " ".join(_ocr_img(Image.open(path), psm=6, scale=3).split())


def _box(img, frac_box, strip_full=False):
    """frac_box is relative to the FULL frame; `img` is the bottom strip (STRIP_Y0..1) unless strip_full."""
    x0, y0, x1, y1 = frac_box
    W, H = img.size
    if not strip_full:
        y0 = (y0 - STRIP_Y0) / (1 - STRIP_Y0)
        y1 = (y1 - STRIP_Y0) / (1 - STRIP_Y0)
    return img.crop((int(x0 * W), max(0, int(y0 * H)), int(x1 * W), min(H, int(y1 * H))))


def read_score(path):
    """-> (runs, wk, balls, mx) from whichever layout parses, else None."""
    Image, _ = _pil()
    img = Image.open(path)
    order = list(range(len(LAYOUTS)))
    order.insert(0, order.pop(_LAYOUT_HINT[0] if _LAYOUT_HINT[0] < len(order) else 0))
    strict_first = [i for i in order if LAYOUTS[i].get("strict")]        # a strict (complete-reading-only) layout can never be fooled: always try it first
    order = strict_first + [i for i in order if i not in strict_first]
    for i in order:
        L = LAYOUTS[i]
        if L.get("strict"):
            sc = _ocr_img(_box(img, L["score"]), psm=7, whitelist="0123456789-", scale=4)
            ov = _ocr_img(_box(img, L["overs"]), psm=7, whitelist="0123456789.()", scale=4)
            if not (RX_STRICT_SCORE.match(sc.replace(" ", "")) and RX_STRICT_OVERS.match(ov.replace(" ", ""))):
                continue
            r = parse_score(sc, ov)
            if r:
                return r                     # D is not remembered as the hint: it only covers the first balls
            continue
        if L.get("auto"):
            sc = _ocr_img(_box(img, L["score"]), psm=7, whitelist="0123456789-", scale=4, auto_invert=True)
            ov = _ocr_img(_box(img, L["overs"]), psm=7, whitelist="0123456789.()", scale=4, auto_invert=True)
        else:
            sc = _ocr_img(_box(img, L["score"]), psm=7, whitelist="0123456789-", scale=4)
            ov = _ocr_img(_box(img, L["overs"]), psm=7, whitelist="0123456789.()", scale=4, invert=True)
        r = parse_score(sc, ov)
        if r:
            _LAYOUT_HINT[0] = i
            return r
    return None


def _pmap(fn, items):
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return list(ex.map(fn, items))


def fine_banner_scan(H, src, events, banners, step=1.0, back=30.0, fwd=4.0):
    """The coarse scan (every BALL_STEP s) sees the SIX / FOUR / WICKET graphic only if a frame happens to fall inside the 3-4 s it
    is on screen (2-over test of the NGPL final: 3 of 7 found). For every four / six / wicket without a graphic of its own kind near
    its score change, look at [t_state-back, t_state+fwd] once per second. Appends (t, kind) to `banners`; -> number of events helped."""
    onsets = banner_onsets(sorted(banners, key=lambda b: b[0]))
    todo = []
    for e in events:
        if e["kind"] == "burst":                           # several balls typed at once: look at the whole stretch between the two readings
            todo.append(e)
            continue
        if e["kind"] not in ("six", "four", "wicket"):
            continue
        ts = e["t_state"]
        if any(k == e["kind"] and ts - back <= tb <= ts + fwd for tb, k in onsets):
            continue
        todo.append(e)
    if not todo:
        return 0
    base = os.path.join(os.path.dirname(os.path.abspath(src)), "sb_fine")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)
    helped = [0]

    def one(item):
        idx, e = item
        a = max(0.0, e["t_state"] - back)
        b = e["t_state"] + fwd
        if e["kind"] == "burst":
            a = max(0.0, e.get("t_prev", e["t_state"] - back) - 3.0)
        d = os.path.join(base, "e%03d" % idx)
        os.makedirs(d, exist_ok=True)
        vf = "fps=1/%g,crop=iw:ih*%.3f:0:ih*%.3f" % (step, 1 - STRIP_Y0, STRIP_Y0)
        r = _run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "%.2f" % a, "-to", "%.2f" % b, "-i", src, "-an", "-sn", "-vf", vf,
                  os.path.join(d, "f_%04d.png")])
        got = []
        for i, pth in enumerate(sorted(glob.glob(os.path.join(d, "f_*.png")))):
            try:
                k = read_banner(pth)
            except Exception:                              # noqa
                k = None
            if k is None:
                try:
                    if banner_present(pth) and e["kind"] != "burst":   # a graphic is up that OCR cannot read: it is THIS event's (kind known from the score)
                        k = e["kind"]
                except Exception:                          # noqa
                    pass
            if e["kind"] == "burst":
                got.append((a + i * step, k if k in ("six", "four", "wicket") else None))
            else:
                got.append((a + i * step, k if k == e["kind"] else None))
        return idx, got

    for idx, got in _pmap(one, list(enumerate(todo))):
        hit = [(t, k) for t, k in got if k]
        if hit:
            banners.extend(hit)
            helped[0] += 1
    shutil.rmtree(base, ignore_errors=True)
    H.log("scoreboard: fine banner scan found a graphic for %d of %d boundary / wicket events the coarse scan missed" % (helped[0], len(todo)))
    return helped[0]


# ------------------------------------------------------------------ one match window -> ball events
def analyze_window(H, src, dur):
    """ball-by-ball events of the (already trimmed) match window `src`. None if the scoreboard cannot be read."""
    if not have_ocr():
        H.log("scoreboard: tesseract/pillow missing -> commentary fallback")
        return None
    work = os.path.join(os.path.dirname(os.path.abspath(src)), "sb_ball")
    t0 = time.time()
    fr = sample_frames(src, 0.0, dur, BALL_STEP, work)
    H.log("scoreboard balls: %d frames every %ds" % (len(fr), BALL_STEP))
    done = [0]
    banners = []
    toss_seen = []                      # (t, done_flag) of the first frames that show the toss banner

    def one(item):
        t, p = item
        if t <= 160.0:                  # where does THIS match start? (the window may still show the previous match's result for ~30 s)
            try:
                fl = parse_flags(ocr_strip_text(p))
                if fl["toss_done"] or fl["toss"]:
                    toss_seen.append((float(t), bool(fl["toss_done"])))
            except Exception:           # noqa
                pass
        try:
            sc = read_score(p)
        except Exception:                # noqa
            sc = None
        try:
            bk = read_banner(p)
        except Exception:                # noqa
            bk = None
        banners.append((float(t), bk))
        done[0] += 1
        if done[0] % 80 == 0:
            try:
                H.hl_review.set_live("transcribe", "Har ball ka scoreboard padh raha hai (%d/%d)" % (done[0], len(fr)), cur=done[0], total=len(fr))
            except Exception:            # noqa
                pass
        return (float(t),) + (sc if sc else (None, None, None, None))

    rows = [r for r in _pmap(one, fr) if r[1] is not None]
    shutil.rmtree(work, ignore_errors=True)
    H.log("scoreboard balls: %d readable of %d frames (%.0fs)" % (len(rows), len(fr), time.time() - t0))
    if len(rows) < 30:
        return None
    states = consistent_states(rows)
    events, innings = events_from_states(states)
    innings = [i for i in innings if len(i) >= 3]
    if not events or not innings:
        return None
    kinds = {}
    for e in events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    try:
        fine_banner_scan(H, src, events, banners)
    except Exception as ex:                                  # noqa -- the coarse banners still work
        H.log("fine banner scan failed:", ex)
    n_burst = sum(1 for e in events if e["kind"] == "burst")
    events = [e for e in events if e["kind"] != "burst"]
    if n_burst:
        H.log("scoreboard: %d multi-ball score jumps (operator typed several balls at once) -> their graphics are added as events" % n_burst)
    lag, n_al, n_add = align_events(events, banner_onsets(sorted(banners, key=lambda b: b[0])), state_ts=[st["t"] for st in states])
    H.log("scoreboard: score->ball delay %.1fs (%d events matched to their SIX/FOUR/WICKET graphic, %d more found only by the graphic)" % (lag, n_al, n_add))
    kinds = {}
    for e in events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    if len(innings) > 2:
        H.log("WARNING: this window holds %d innings = more than one match; check the scout split" % len(innings))
    H.log("scoreboard events:", kinds, "innings:", [(i[0]["t"], i[-1]["t_last"], i[-1]["runs"], i[-1]["wk"]) for i in innings])
    mx = innings[0][0]["mx"]
    brk = (innings[0][-1]["t_last"], innings[1][0]["t"]) if len(innings) > 1 else (dur * 0.5 - 60, dur * 0.5 + 60)
    last_inn = innings[-1]
    last_over = max(0, last_inn[-1]["balls"] - 1) // 6      # the over the match ended in (42 balls = 7.0 overs -> over index 6)
    lo_t = next((s["t"] for s in last_inn if s["balls"] // 6 >= last_over), last_inn[-1]["t"])
    death = []
    for inn in innings:
        d_from = next((s["t"] for s in inn if s["balls"] >= max(0, inn[0]["mx"] - 2) * 6), None)
        if d_from is not None:
            death.append((d_from, inn[-1]["t_last"] + 1.0))
    toss_start = None
    if toss_seen:
        done_t = sorted(t for t, d in toss_seen if d)
        toss_start = max(0.0, (done_t[0] if done_t else min(t for t, _ in toss_seen)) - 3.0)
        H.log("scoreboard: toss banner first seen at %.0fs -> match starts at %.0fs" % (toss_start + 3.0, toss_start))
    toss_done = bool(toss_seen) and any(d for _, d in toss_seen)
    return {"toss_start": toss_start, "toss_done": toss_done, "t_final": last_inn[-1]["t"], "events": events, "innings": [[i[0]["t"], i[-1]["t_last"]] for i in innings], "break": brk, "mx": mx,
            "last_over_start": lo_t, "death": death, "kinds": kinds}
