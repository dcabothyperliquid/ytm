"""Per-clip frame check for the highlights reel (2026-11-16) -- NO API key, NO Render, runs inside the Actions job.

Why: every earlier fix GUESSED the shot time from indirect signals (score typing delay, graphic, camera cut) and each match needed other
constants. On the NGPL final 16 of 43 clips started with the ball already in the air (frames of the finished reel, second by second).
Here the bot LOOKS at the clip instead:
  * 2 frames per second of the candidate clip -> each frame is PITCH view (the normal broadcast camera: pitch strip, batsmen, bowler)
    or TRACK (camera follows the ball: sky, boundary, floodlights, crowd). "Pitch colour" is learned from the match itself
    (k-means on ~150 frames spread over the window), so it is not tied to one ground.
  * frames before the first TRACK are the RUNUP, frames after it are AFTER.
  * plain rules (judge(), unit tested): clip starts inside the tracking -> start earlier; run-up longer than 7 s -> start later;
    no tracking / result inside the clip -> extend the end. The edges move, the clip is looked at again (max 8 tries).
    A clip is accepted only when it starts on the run-up, shows the ball leaving and the result. One clip at a time, in order.
Log line per clip: `[verify] four 97.0-108.5 OK (2 tries) PPPPPPttPPPPPPPP` (P = pitch view, t = tracking).

Env: HL_VERIFY=0 switches it off | HL_VERIFY_TRIES (default 8) | HL_VERIFY_BUDGET_S (default 1800: later clips keep their planned edges)
"""
import os
import subprocess
import time

LABELS = ("RUNUP", "SHOT", "TRACK", "AFTER", "REPLAY", "OTHER")
BALL_EVENTS = ("four", "six", "wicket", "noball", "wide", "dot", "first_ball", "first_ball2", "last_ball")
PLAY_EVENTS = ("four", "six", "wicket")          # these must show the shot itself

N_FRAMES = 8              # only used by the unit-test simulation; the real check samples FPS frames per second
MAX_LEAD_S = 7.0          # more than this of plain run-up before the ball = start too early
KEEP_LEAD_S = 4.5         # what we trim a too-early start back to (run-up before the ball)
STEP_LATE = {"TRACK": 4.0, "SHOT": 3.5, "AFTER": 6.0}
MAX_CLIP_S = 45.0

# ------------------------------------------------------------------ the rules (pure, tested)
def judge(labels, kind, dt):
    """labels: one label per frame (time order), dt = seconds between frames. -> dict
    action: ok | earlier | later | extend | trim_end | unknown ; shift = seconds ; score = quality (higher = better)."""
    L = [l if l in LABELS else "OTHER" for l in labels]
    n = len(L)
    if n == 0:
        return {"action": "unknown", "shift": 0.0, "score": -9.0, "why": "no labels", "labels": L}
    run = [i for i, l in enumerate(L) if l == "RUNUP"]
    play = [i for i, l in enumerate(L) if l in ("SHOT", "TRACK")]
    after = [i for i, l in enumerate(L) if l == "AFTER"]
    first = L[0]
    lead = 0
    while lead < n and L[lead] == "RUNUP":
        lead += 1
    need_play = kind in PLAY_EVENTS

    score = 0.0
    if first == "RUNUP":
        score += 3.0
    if run and (play or after) and run[0] < min(play + after):
        score += 1.0
    if (not need_play and (play or after)) or (need_play and after):
        score += 1.0
    if first in ("SHOT", "TRACK"):
        score -= 2.0
    if first == "AFTER" and not run:
        score -= 1.5
    if "REPLAY" in L:
        score -= 2.0
    if lead * dt > MAX_LEAD_S:
        score -= 1.0
    if all(l == "OTHER" for l in L):
        score -= 3.0

    def out(action, shift, why):
        return {"action": action, "shift": round(float(shift), 2), "score": score, "why": why, "labels": L}

    if all(l == "OTHER" for l in L):
        return out("unknown", 0.0, "nothing recognisable")
    # a replay somewhere after the first frame: the clip runs into a replay -> cut the end before it
    if "REPLAY" in L[1:] and L.index("REPLAY") >= 1 and first != "REPLAY":
        k = L.index("REPLAY")
        if k * dt >= 5.0:
            return out("trim_end", k * dt - 0.3, "replay inside at frame %d" % (k + 1))
    # the next ball's run-up shows after the play: end is too late (only frames after THIS ball's run-up count: an AFTER
    # at the very start is the previous ball's aftermath)
    seen_play = False
    for i in range(run[0] if run else n, n):
        l = L[i]
        if l in ("SHOT", "TRACK", "AFTER"):
            seen_play = True
        elif l == "RUNUP" and seen_play and i >= 3:
            return out("trim_end", i * dt - 0.3, "next ball's run-up at frame %d" % (i + 1))
    # start problems
    if first in ("SHOT", "TRACK"):
        lt = 0
        while lt < n and L[lt] in ("SHOT", "TRACK"):
            lt += 1
        return out("earlier", max(STEP_LATE[first], lt * dt + 4.0), "starts after the shot (%s first, %.1f s of it)" % (first, lt * dt))
    if first == "AFTER" and not run:
        return out("earlier", STEP_LATE["AFTER"], "starts after the play, no run-up in the clip")
    if first in ("AFTER", "REPLAY", "OTHER") and run and run[0] <= max(1, n // 4):
        return out("later", max(1.0, run[0] * dt - 1.2), "starts in the previous ball (%s), run-up at frame %d" % (first, run[0] + 1))
    if first in ("REPLAY", "OTHER", "AFTER") and (run or play):
        # AFTER / REPLAY first, the ball only later: previous ball's aftermath (start too early) OR this ball's own aftermath
        # followed by the next run-up (start too late). The loop probes one direction and flips when it brings nothing.
        alt = max(1.0, run[0] * dt - 1.2) if run else max(1.0, play[0] * dt - 5.0)
        r = out("ambiguous", STEP_LATE["AFTER"], "starts in %s, ball/run-up only at frame %d" % (first, (run or play)[0] + 1))
        r["alt_later"] = alt
        return r
    if first in ("REPLAY", "OTHER"):
        return out("unknown", 0.0, "starts in %s, ball not found" % first)
    if first == "RUNUP" and lead * dt > MAX_LEAD_S and (play or after):
        return out("later", lead * dt - KEEP_LEAD_S, "run-up too long (%.0f s)" % (lead * dt))
    # end problems: the shot never shows
    if need_play and not play and not after:
        if n * dt >= 12.0:                                              # 12 s of pitch camera and the ball never left it: bowled / lbw / no tracking cut
            return out("ok", 0.0, "no tracking cut in %.0f s (bowled / lbw)" % (n * dt))
        return out("extend", 4.0, "clip ends before the shot")
    if need_play and not after:                                          # shot / flight shown but not the payoff (graphic, celebration)
        return out("extend", 3.0, "clip ends before the result shows")
    return out("ok", 0.0, "run-up -> ball -> after")


def next_edges(a, b, j, lo, hi, prev_end, next_start, dur, amax=None):
    """apply judge() action to the edges with all clamps. -> (a2, b2)"""
    act, sh = j["action"], j["shift"]
    a2, b2 = a, b
    floor = max(0.0, lo, prev_end + 0.2)
    ceil = min(dur, hi, next_start - 0.1)
    if act == "earlier":
        a2 = max(floor, a - sh)
    elif act == "later":
        a2 = a + sh
        if amax is not None and a < amax - 1.0:             # the planned shot time is never EARLIER than the real shot: do not start after it
            a2 = min(a2, amax - 1.0)
        if b - a2 < 6.0:                                   # moved past most of the clip: keep ~8 s of it (end moves along)
            b2 = min(ceil, a2 + 8.0)
            if b2 - a2 < 4.0:
                a2 = b2 - 4.0
    elif act == "extend":
        b2 = min(ceil, b + sh)
    elif act == "trim_end":
        b2 = max(a + 4.0, min(b, a + sh))
    if b2 - a2 > MAX_CLIP_S:
        b2 = a2 + MAX_CLIP_S
    return a2, b2


# ------------------------------------------------------------------ frames -> PITCH / TRACK (local, no model download, no key)
FPS = 2.0
CW, CH = 160, 90


def _frames(src, a, b, fps=FPS):
    """frames of [a, b) at `fps` -> uint8 array (n, CH, CW, 3) BGR (one ffmpeg call)"""
    import numpy as np
    cmd = ["ffmpeg", "-loglevel", "error", "-ss", "%.2f" % max(0.0, a), "-t", "%.2f" % max(0.1, b - a), "-i", src, "-an", "-sn",
           "-vf", "fps=%g,scale=%d:%d" % (fps, CW, CH), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    r = subprocess.run(cmd, capture_output=True)
    n = len(r.stdout) // (CW * CH * 3)
    return np.frombuffer(r.stdout[:n * CW * CH * 3], dtype=np.uint8).reshape(n, CH, CW, 3)


def _descr(frames):
    """colour descriptor of the central / lower part of each frame (where the pitch is): HSV histogram + 'tan' fraction"""
    import cv2
    import numpy as np
    out, tan = [], []
    y0, y1, x0, x1 = int(CH * .35), int(CH * .80), int(CW * .15), int(CW * .85)
    for f in frames:
        hsv = cv2.cvtColor(f[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        h = cv2.calcHist([hsv], [0, 1, 2], None, [12, 4, 4], [0, 180, 0, 256, 0, 256]).ravel()
        out.append(h / max(1.0, h.sum()))
        H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        tan.append(float(((H >= 8) & (H <= 25) & (S >= 35) & (S <= 170) & (V >= 80)).mean()))
    return np.array(out, dtype=np.float32), np.array(tan, dtype=np.float32)


class PitchModel:
    """is this frame the normal pitch camera? Learned from the match: the biggest colour cluster of frames spread over the window.
    Falls back to a fixed 'tan pitch strip' rule when the clusters are not clearly apart."""

    def __init__(self):
        self.proto = None
        self.thr = None
        self.mode = "tan"
        self.info = ""

    def fit(self, frames):
        import cv2
        import numpy as np
        if len(frames) < 30:
            self.info = "too few frames, fixed rule"
            return self
        d, tan = _descr(frames)
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 0.01)
        _, lab, cen = cv2.kmeans(d, 2, None, crit, 5, cv2.KMEANS_PP_CENTERS)
        lab = lab.ravel()
        big = int(np.bincount(lab, minlength=2).argmax())
        proto = d[lab == big].mean(axis=0)
        sim = np.minimum(d, proto).sum(axis=1)
        sp, so = float(sim[lab == big].mean()), float(sim[lab != big].mean()) if (lab != big).any() else 0.0
        frac = float((lab == big).mean())
        agree = float(((sim > (sp + so) / 2) == (tan > 0.15)).mean())
        # trust the learned model only when the clusters are clearly apart AND mostly agree with the colour rule on this ground
        if sp - so >= 0.12 and 0.30 <= frac <= 0.97 and agree >= 0.8:
            self.proto, self.thr, self.mode = proto, (sp + so) / 2.0, "learned"
        self.info = "%s: pitch cluster %.0f%% of frames, sim %.2f vs %.2f, agrees with colour rule %.0f%%" % (self.mode, frac * 100, sp, so, agree * 100)
        return self

    def is_pitch(self, frames):
        import numpy as np
        if len(frames) == 0:
            return np.zeros(0, dtype=bool)
        d, tan = _descr(frames)
        if self.mode == "learned":
            return np.minimum(d, self.proto).sum(axis=1) > self.thr
        return tan > 0.15


def cv_labels(pitch):
    """pitch: list of bool per frame (time order) -> RUNUP / TRACK / AFTER (3-frame median first: one odd frame is not a cut)"""
    p = [bool(x) for x in pitch]
    n = len(p)
    sm = [p[i] if i in (0, n - 1) else (sum(p[i - 1:i + 2]) >= 2) for i in range(n)]
    out, seen_t = [], False
    for v in sm:
        if not v:
            seen_t = True
            out.append("TRACK")
        else:
            out.append("AFTER" if seen_t else "RUNUP")
    return out


class Looker:
    def __init__(self, src, dur, log=print):
        self.src, self.dur, self.log = src, dur, log
        self.model = PitchModel()

    def calibrate(self, n=150):
        """~n single frames spread over the window, each by its own fast seek (decoding a whole 2.5 h file would take minutes)"""
        import numpy as np
        from concurrent.futures import ThreadPoolExecutor
        ts = [(i + 0.5) * self.dur / n for i in range(n)]

        def one(t):
            f = _frames(self.src, t, t + 1.2, fps=1.0)
            return f[0] if len(f) else None
        with ThreadPoolExecutor(max_workers=4) as ex:
            got = [f for f in ex.map(one, ts) if f is not None]
        frames = np.array(got) if got else np.zeros((0, CH, CW, 3), dtype=np.uint8)
        self.model.fit(frames)
        self.log("[verify] pitch model:", self.model.info, "(%d frames)" % len(frames))
        return self.model.mode

    def labels(self, a, b):
        fr = _frames(self.src, a, b)
        return cv_labels(self.model.is_pitch(fr)), 1.0 / FPS


# ------------------------------------------------------------------ the loop
def verify_seg(seg, prev_end, next_start, dur, label_fn, tries=8, log=print):
    """Look at the clip, move its edges, look again ... until judge() says ok (or tries run out: the best attempt wins).
    label_fn(a, b) -> (labels, dt). Mutates and returns seg (+ seg['verify'] = {...}).
    Ambiguous start (AFTER first, ball later): probe EARLIER once; if that shows no run-up / shot, flip to LATER from the original."""
    kind = seg.get("event")
    a, b = float(seg["start"]), float(seg["end"])
    lo, hi = float(seg.get("lo", 0.0)), float(seg.get("hi", dur))
    amax = seg.get("t")
    amax = float(amax) if amax is not None else None
    best, seen, hist = None, set(), []
    n_ext = 0
    probe = None                                         # {"orig": (a, b), "later": s, "flipped": bool}
    for k in range(tries):
        key = (round(a, 1), round(b, 1))
        if key in seen:
            break
        seen.add(key)
        labels, dt = label_fn(a, b)
        j = judge(labels, kind, dt)
        hist.append((round(a, 1), round(b, 1), j["action"], j["why"]))
        rank = (j["action"] == "ok", j["score"])
        if best is None or rank > best[3]:
            best = (a, b, j, rank)
        if j["action"] in ("ok", "unknown"):
            break
        if amax is not None and a > amax - 0.5 and j["action"] in ("later", "ambiguous", "extend"):
            # the clip starts after the planned shot time: it can only be too LATE, never "in the previous ball"
            j = dict(j, action="earlier", shift=max(4.0, a - amax + 4.0), why=j["why"] + " (start is after the planned shot)")
        if probe is not None and not probe["flipped"] and j["action"] in ("ambiguous", "earlier") and \
                not any(l in ("RUNUP", "SHOT", "TRACK") for l in labels):
            # the probe (earlier) found nothing new: wrong direction -> later, from the original edges
            probe["flipped"] = True
            a2, b2 = next_edges(probe["orig"][0], probe["orig"][1], {"action": "later", "shift": probe["later"]}, lo, hi, prev_end, next_start, dur, amax)
        elif j["action"] == "ambiguous":
            if probe is None:
                probe = {"orig": (a, b), "later": j["alt_later"], "flipped": False}
                a2, b2 = next_edges(a, b, {"action": "earlier", "shift": j["shift"]}, lo, hi, prev_end, next_start, dur, amax)
                if (round(a2, 1), round(b2, 1)) == key:                 # cannot go earlier (previous clip / replay in the way)
                    probe["flipped"] = True
                    a2, b2 = next_edges(a, b, {"action": "later", "shift": j["alt_later"]}, lo, hi, prev_end, next_start, dur, amax)
            else:                                                       # already flipped: keep going later
                a2, b2 = next_edges(a, b, {"action": "later", "shift": j["alt_later"]}, lo, hi, prev_end, next_start, dur, amax)
        else:
            a2, b2 = next_edges(a, b, j, lo, hi, prev_end, next_start, dur, amax)
        if j["action"] == "extend":
            n_ext += 1
            if n_ext > 2 or b2 - a2 > 26.0:                              # the result never shows: do not keep growing the clip
                break
        if (round(a2, 1), round(b2, 1)) == key:
            break
        a, b = a2, b2
    a, b, j, _ = best
    seg["start"], seg["end"] = a, b
    pt = "".join("P" if x in ("RUNUP", "AFTER") else "t" for x in j["labels"])
    seg["verify"] = {"ok": j["action"] == "ok", "tries": len(hist), "last": j["why"], "labels": pt}
    log("[verify] %s %.1f-%.1f %s (%d tries) %s | %s" % (kind, a, b, "OK" if j["action"] == "ok" else "UNSURE", len(hist), j["why"], pt))
    return seg


def verify_plan(H, src, plan, dur):
    """check every ball clip of the plan in order; clips of other kinds (toss, recap, finish) are left alone. -> summary dict"""
    if os.environ.get("HL_VERIFY", "1") == "0":
        return {"state": "off"}
    look = Looker(src, dur, log=H.log)
    try:
        mode = look.calibrate()
    except Exception as e:                                               # noqa -- no calibration, no check; the plan stays as planned
        H.log("[verify] calibration failed, clips keep the planned edges:", e)
        return {"state": "failed", "error": str(e)[:80]}
    tries = int(os.environ.get("HL_VERIFY_TRIES", "8"))
    budget = float(os.environ.get("HL_VERIFY_BUDGET_S", "1800"))
    t0 = time.time()
    segs = plan["segs"]
    ok = unsure = skipped = 0
    prev_end = 0.0
    for i, s in enumerate(segs):
        nxt = segs[i + 1]["start"] if i + 1 < len(segs) else dur
        if s.get("event") not in BALL_EVENTS or time.time() - t0 > budget:
            if s.get("event") in BALL_EVENTS:
                skipped += 1
            prev_end = s["end"]
            continue
        try:
            H.hl_review.set_live("cut", "Clip frames check ho rahe hain (%d/%d)" % (i + 1, len(segs)), cur=i + 1, total=len(segs))
        except Exception:                                                # noqa
            pass
        try:
            verify_seg(s, prev_end, nxt, dur, look.labels, tries=tries, log=H.log)
            if s["verify"]["ok"]:
                ok += 1
            else:
                unsure += 1
        except Exception as e:                                           # noqa -- a failed check never loses the clip
            H.log("[verify] clip %d skipped: %s" % (i + 1, e))
            skipped += 1
        prev_end = s["end"]
    # edges moved: keep the list chronological and overlap free
    merged = []
    for c in sorted(segs, key=lambda c: c["start"]):
        if merged and c["start"] <= merged[-1]["end"]:
            m = merged[-1]
            m["end"] = max(m["end"], c["end"])
            m["parts"] = m.get("parts", 1) + c.get("parts", 1)
            ts = [x for x in (m.get("t"), c.get("t")) if x is not None]
            m["t"] = min(ts) if ts else None
            m["verify"] = None
        else:
            merged.append(c)
    plan["segs"] = merged
    plan["total"] = sum(c["end"] - c["start"] for c in merged)
    res = {"state": "done", "model": mode, "ok": ok, "unsure": unsure, "skipped": skipped, "secs": round(time.time() - t0)}
    plan["info"]["verify"] = res
    H.log("[verify] done:", res, "new total %.0fs" % plan["total"])
    return res
