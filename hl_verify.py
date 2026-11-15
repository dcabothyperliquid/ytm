"""Per-clip frame check for the highlights reel (2026-11-16).

Why: every earlier fix guessed the shot time from indirect signals (score typing delay, graphic, camera cut) and each match
needed other constants. Here the bot LOOKS at the clip instead: it samples frames of the candidate clip, a vision model labels
every frame (run-up / shot / ball tracking / after the play / replay / other), plain rules decide "starts too late",
"starts in the previous ball", "ends before the shot" ... and the clip edges are moved and re-checked. A clip is accepted
only when it starts on the bowler's run-up, shows the shot and ends after it. One clip at a time, in order.

The model only CLASSIFIES frames (easy job); the decisions are deterministic (judge()), so they are unit tested.

Config (env; the key is the only thing that must exist):
  GROQ_API_KEY | GEMINI_API_KEY | HL_VISION_KEY   key of any OpenAI-compatible vision API
  HL_VISION_URL    base url (default: Groq / Gemini by key)
  HL_VISION_MODEL  model id (default: auto-discovered from the provider's /models)
  HL_VERIFY=0      switch the check off
  HL_VERIFY_TRIES  attempts per clip (default 8)
  HL_VERIFY_BUDGET_S  stop checking after this many seconds, remaining clips keep their planned edges (default 1800)
"""
import base64
import io
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

LABELS = ("RUNUP", "SHOT", "TRACK", "AFTER", "REPLAY", "OTHER")
BALL_EVENTS = ("four", "six", "wicket", "noball", "wide", "dot", "first_ball", "first_ball2", "last_ball")
PLAY_EVENTS = ("four", "six", "wicket")          # these must show the shot itself

N_FRAMES = 8
MAX_LEAD_S = 7.0          # more than this of plain run-up before the ball = start too early
KEEP_LEAD_S = 4.5         # what we trim a too-early start back to (run-up before the ball)
STEP_LATE = {"TRACK": 4.0, "SHOT": 3.5, "AFTER": 6.0}
MAX_CLIP_S = 45.0

PROMPT = (
    "These are %d frames (numbered 1-%d, left to right, top to bottom) of ONE short cricket broadcast clip, in time order. "
    "Label EVERY frame with exactly one of:\n"
    "RUNUP  = normal pitch camera BEFORE the ball is hit: bowler running in or about to bowl, batsman waiting at the crease.\n"
    "SHOT   = the moment of the delivery or the batsman playing / hitting the ball, ball near the bat.\n"
    "TRACK  = camera is following the ball or fielders: ball in the air, outfield, boundary rope, crowd, sky, floodlights, no pitch in view.\n"
    "AFTER  = pitch camera AFTER the play: batsmen / fielders standing, talking, celebrating, umpire signalling; a FOUR / SIX / WICKET "
    "graphic on the bottom bar also counts as AFTER.\n"
    "REPLAY = slow-motion replay or a replay graphic / swoosh card.\n"
    "OTHER  = anything else (advert, slate, empty ground, crowd close-up, black frame).\n"
    "Answer with ONLY one JSON object like {\"1\":\"RUNUP\",\"2\":\"RUNUP\",...} with all %d keys. No other text."
)


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
        return out("earlier", STEP_LATE[first], "starts after the shot (%s first)" % first)
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


# ------------------------------------------------------------------ frames + vision call
def _pil():
    from PIL import Image, ImageDraw
    return Image, ImageDraw


def grab_frames(src, a, b, n=N_FRAMES, w=320, h=180):
    """n frames evenly over [a, b) -> (jpeg bytes of one numbered tile, dt)"""
    Image, ImageDraw = _pil()
    dt = (b - a) / float(n)
    cells = []
    tmp = tempfile.mkdtemp(prefix="hlv_")
    for i in range(n):
        t = a + i * dt
        p = os.path.join(tmp, "f%02d.jpg" % i)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "%.2f" % t, "-i", src, "-frames:v", "1",
                        "-vf", "scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2" % (w, h, w, h), "-q:v", "4", p],
                       capture_output=True)
        try:
            im = Image.open(p).convert("RGB")
        except Exception:                                   # noqa -- frame past the end of the file
            im = Image.new("RGB", (w, h), (0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 22, 16], fill=(0, 0, 0))
        d.text((6, 2), str(i + 1), fill=(255, 255, 0))
        cells.append(im)
    cols = 4
    rows = (n + cols - 1) // cols
    tile = Image.new("RGB", (cols * w, rows * h))
    for i, im in enumerate(cells):
        tile.paste(im, ((i % cols) * w, (i // cols) * h))
    buf = io.BytesIO()
    tile.save(buf, "JPEG", quality=70)
    for f in os.listdir(tmp):
        try:
            os.remove(os.path.join(tmp, f))
        except OSError:
            pass
    try:
        os.rmdir(tmp)
    except OSError:
        pass
    return buf.getvalue(), dt


class Vision:
    """tiny OpenAI-compatible client (Groq, Gemini, OpenRouter ...). label_frames(jpeg) -> [label]*N"""

    def __init__(self, log=print):
        self.log = log
        self.key = os.environ.get("HL_VISION_KEY") or os.environ.get("GROQ_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
        gem = bool(os.environ.get("GEMINI_API_KEY")) and not os.environ.get("HL_VISION_KEY") and not os.environ.get("GROQ_API_KEY")
        self.base = (os.environ.get("HL_VISION_URL") or ("https://generativelanguage.googleapis.com/v1beta/openai" if gem else "https://api.groq.com/openai/v1")).rstrip("/")
        self.model = os.environ.get("HL_VISION_MODEL", "")
        self.gap = float(os.environ.get("HL_VISION_MIN_GAP", "2.2"))      # free tiers: ~30 requests / min
        self._last = 0.0
        self.calls = 0
        self.dead = False
        self.extra = {"reasoning_format": "hidden"}                      # dropped automatically when the model rejects it

    @property
    def ready(self):
        return bool(self.key) and not self.dead

    def _http(self, path, body=None, timeout=90):
        req = urllib.request.Request(self.base + path, data=(json.dumps(body).encode() if body is not None else None),
                                     headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json", "User-Agent": "hl-verify/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def discover(self):
        """no model configured: ask the provider which models it has and pick a vision-capable one"""
        try:
            ids = [m.get("id", "") for m in self._http("/models", timeout=30).get("data", [])]
        except Exception as e:                                           # noqa
            self.log("[verify] cannot list models:", e)
            return False
        pref = [r"qwen.*vl", r"qwen3\.\d.*27b", r"llama-4", r"gemini.*flash", r"vision", r"pixtral", r"gpt-4o"]
        for rx in pref:
            hit = [i for i in ids if re.search(rx, i, re.I) and not re.search(r"guard|whisper|tts|embed|prompt", i, re.I)]
            if hit:
                self.model = sorted(hit)[-1]
                self.log("[verify] vision model auto-picked:", self.model)
                return True
        self.log("[verify] no vision model found among:", ids[:20])
        return False

    def label_frames(self, jpeg, n=N_FRAMES):
        if not self.model and not self.discover():
            self.dead = True
            raise RuntimeError("no vision model")
        b64 = base64.b64encode(jpeg).decode()
        for attempt in range(5):
            wait = self.gap - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            body = {"model": self.model, "temperature": 0, "max_tokens": 700,
                    "messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT % (n, n, n)},
                                                              {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]}
            body.update(self.extra)
            try:
                res = self._http("/chat/completions", body)
                self.calls += 1
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:300]
                if e.code == 429:
                    time.sleep(min(30.0, 6.0 * (attempt + 1)))
                    continue
                if e.code in (400, 422) and self.extra and re.search(r"reasoning|unsupported|unknown|not support", msg, re.I):
                    self.extra = {}
                    continue
                if e.code in (401, 403):
                    self.dead = True
                    raise RuntimeError("vision key rejected (%s)" % e.code)
                if e.code in (400, 404) and re.search(r"model", msg, re.I):
                    old = self.model
                    self.model = ""
                    if not self.discover() or self.model == old:
                        self.dead = True
                        raise RuntimeError("vision model %s unusable: %s" % (old, msg))
                    continue
                self.log("[verify] http %s %s" % (e.code, msg))
                continue
            except Exception as e:                                       # noqa -- network hiccup
                self.log("[verify] call failed:", e)
                time.sleep(3.0)
                continue
            txt = (res.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            lab = parse_labels(txt, n)
            if lab:
                return lab
            self.log("[verify] unreadable answer:", txt[:120].replace("\n", " "))
        raise RuntimeError("vision gave no usable answer")


def parse_labels(txt, n):
    txt = re.sub(r"<think>.*?</think>", "", txt or "", flags=re.S)
    m = re.findall(r"\{[^{}]*\}", txt, re.S)
    for blob in reversed(m):
        try:
            d = json.loads(blob)
        except Exception:                                                # noqa
            continue
        out = []
        for i in range(1, n + 1):
            v = str(d.get(str(i), d.get(i, "OTHER"))).strip().upper()
            out.append(v if v in LABELS else "OTHER")
        if sum(1 for k in d if str(k).isdigit()) >= max(2, n // 2):
            return out
    return None


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
        if (round(a2, 1), round(b2, 1)) == key:
            break
        a, b = a2, b2
    a, b, j, _ = best
    seg["start"], seg["end"] = a, b
    seg["verify"] = {"ok": j["action"] == "ok", "tries": len(hist), "last": j["why"], "labels": "".join(x[0] for x in j["labels"])}
    log("[verify] %s %.1f-%.1f %s (%d tries) %s" % (kind, a, b, "OK" if j["action"] == "ok" else "UNSURE", len(hist), j["why"]))
    return seg


def verify_plan(H, src, plan, dur):
    """check every ball clip of the plan in order; clips of other kinds (toss, recap, finish) are left alone. -> summary dict"""
    if os.environ.get("HL_VERIFY", "1") == "0":
        return {"state": "off"}
    vis = Vision(log=H.log)
    if not vis.key:
        H.log("[verify] OFF: no vision key (set GROQ_API_KEY or GEMINI_API_KEY as an Actions secret) -> clips keep the planned edges")
        return {"state": "no_key"}
    tries = int(os.environ.get("HL_VERIFY_TRIES", "8"))
    budget = float(os.environ.get("HL_VERIFY_BUDGET_S", "1800"))
    t0 = time.time()
    segs = plan["segs"]
    ok = unsure = skipped = 0

    def label_fn(a, b):
        jpg, dt = grab_frames(src, a, b)
        return vis.label_frames(jpg), dt

    prev_end = 0.0
    for i, s in enumerate(segs):
        nxt = segs[i + 1]["start"] if i + 1 < len(segs) else dur
        if s.get("event") not in BALL_EVENTS or not vis.ready or time.time() - t0 > budget:
            if s.get("event") in BALL_EVENTS:
                skipped += 1
            prev_end = s["end"]
            continue
        try:
            H.hl_review.set_live("cut", "Clip frames check ho rahe hain (%d/%d)" % (i + 1, len(segs)), cur=i + 1, total=len(segs))
        except Exception:                                                # noqa
            pass
        try:
            verify_seg(s, prev_end, nxt, dur, label_fn, tries=tries, log=H.log)
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
            m["t"] = min([x for x in (m.get("t"), c.get("t")) if x is not None] or [None]) if (m.get("t") is not None or c.get("t") is not None) else None
            m["verify"] = None
        else:
            merged.append(c)
    plan["segs"] = merged
    plan["total"] = sum(c["end"] - c["start"] for c in merged)
    res = {"state": "done", "ok": ok, "unsure": unsure, "skipped": skipped, "calls": vis.calls, "secs": round(time.time() - t0)}
    plan["info"]["verify"] = res
    H.log("[verify] done:", res, "new total %.0fs" % plan["total"])
    return res
