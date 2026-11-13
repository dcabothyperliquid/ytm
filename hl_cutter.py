"""
Highlights CUT PLAN (see HIGHLIGHTS_SPEC.md section 2) built from the commentary of the confirmed match window.

  1. TOSS    banner/toss talk + who bats/bowls first, commentary never cut mid-sentence (gap between toss and decision cut)
  2. EVENTS  every wicket, six, four, no-ball, wide, in match order. Wicket / six / no-ball / wide are NEVER dropped.
  3. DOTS    filler only: when a long stretch has no clip, one dot ball (first/middle/last of that stretch);
             in the death overs (last ~18% of each innings) dots that decide the chase are kept.
  4. SIXES   clear commentary of the whole six; a replay window is added only for the top few sixes (not all).
  5. END     last over complete + winning run + celebration (the confirmed window already ends ~45 s after the last ball).
  6. LENGTH  over budget -> drop in this order: filler dots, lowest-energy fours, death dots, six replays. Never the must-keep set.

No scoreboard / ball-by-ball API exists for these local matches, so events come from the Hindi/English commentary
(faster-whisper over the whole window) + audio energy. Everything is a pure function of the transcript segments
(`plan_from_segments`) so it can be tested without video.
"""
import os
import re

import hl_scoreboard



class _LazyNP:
    """numpy is imported on first use: the Gate step imports hl_runner (-> hl_cutter) BEFORE numpy is installed on the runner."""
    def __getattr__(self, name):
        import numpy
        return getattr(numpy, name)


np = _LazyNP()

FULL_MODEL = os.environ.get("HL_FULL_MODEL", "base") or "base"
CHUNK = 600                 # s of audio per whisper call
SR = 16000

RX = {
    "six": re.compile(r"\bsix(?:es|er)?\b|सिक्स|छक्का|छक्के|chh?akka", re.I),
    "four": re.compile(r"\bfour\b|चौका|चौके|चौक्का|chauka|boundary|बाउंड्री", re.I),
    "wicket": re.compile(r"wicket|विकेट|bowled|बोल्ड|run[\s-]?out|रन[\s-]?आउट|\blbw\b|स्टंप|stump|\bout\b|आउट|caught|कैच|\bcatch(?:ed)?\b", re.I),
    "noball": re.compile(r"no[\s-]?ball|नो[\s-]?बॉल|नो[\s-]?बाल|free[\s-]?hit|फ्री[\s-]?हिट", re.I),
    "wide": re.compile(r"\bwide\b|वाइड|वाईड", re.I),
    "dot": re.compile(r"dot[\s-]?ball|no run|कोई रन नहीं|डॉट|डाट|\bdot\b", re.I),
}
NEED_RX = re.compile(r"need(?:s|ed)?\s+\w+\s+(?:runs?|from)|require|चाहिए|चाहिये|target|टारगेट|required", re.I)
TOSS_RX = re.compile(r"\btoss\b|टॉस|won\s+the\s+toss|elected|chose\s+to", re.I)
DECIDE_RX = re.compile(r"elected|chose\s+to|decid|bat\s+first|bowl\s+first|field\s+first|बल्लेबाज़ी|बल्लेबाजी|बैटिंग|गेंदबाज़ी|गेंदबाजी|बॉलिंग|फील्डिंग", re.I)
LASTOVER_RX = re.compile(r"last[\s-]?over|final[\s-]?over|आखिरी\s*ओवर|आख़िरी\s*ओवर|अंतिम\s*ओवर", re.I)
PLAY_RX = re.compile(r"\b(?:four|six|run|runs|over|ball|wicket|single)\b|चौका|छक्का|रन|ओवर|गेंद|बॉल|विकेट", re.I)

# clip lengths: (seconds before the event, seconds after)
PAD = {"wicket": (6, 11), "six": (6, 9), "four": (5, 6), "noball": (4, 5), "wide": (3, 4), "dot": (3, 4)}
SIX_REPLAY = 10             # extra seconds after a TOP six (replay usually follows within ~10-25 s)
TOP_SIXES = 4
MUST = ("wicket", "six", "noball", "wide")
DEATH_FRAC = 0.18           # last 18% of an innings = death overs
FILL_GAP = 100              # s without any clip in the early overs -> add one dot ball as filler
TOSS_MAX = 90.0
END_MAX = 360.0
SB_END_MAX = 480.0           # scoreboard mode: the last over (+ win + celebration) is kept whole, up to this long


# ---------------------------------------------------------------- transcript
def transcribe_full(H, wav, dur, progress=None):
    """whole window -> [{start,end,text}] (10 min chunks, base model by default)."""
    from faster_whisper import WhisperModel
    H.log("cutter: loading whisper", FULL_MODEL)
    model = WhisperModel(FULL_MODEL, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
    n = os.path.getsize(wav) // 2
    mm = np.memmap(wav, dtype=np.int16, mode="r", shape=(n,))
    out, chunks = [], int(np.ceil(dur / CHUNK))
    for ci in range(chunks):
        a, b = int(ci * CHUNK * SR), int(min(n, (ci + 1) * CHUNK * SR))
        audio = np.asarray(mm[a:b], dtype=np.float32) / 32768.0
        try:
            segs, _ = model.transcribe(audio, language=H.LANG, beam_size=1, vad_filter=True,
                                       initial_prompt=H.WHISPER_HINT, condition_on_previous_text=False)
            for s in segs:
                out.append({"start": ci * CHUNK + s.start, "end": ci * CHUNK + s.end, "text": s.text.strip()})
        except Exception as e:                      # noqa
            H.log("cutter whisper fail chunk %d: %s" % (ci, e))
        if progress:
            progress(ci + 1, chunks)
    return out


# ---------------------------------------------------------------- events from segments
def classify_segment(text):
    """-> event name or None. Priority: six > wicket > noball > wide > four > dot."""
    t = text or ""
    need = bool(NEED_RX.search(t))
    if RX["six"].search(t) and not need:
        return "six"
    if RX["wicket"].search(t):
        return "wicket"
    if RX["noball"].search(t):
        return "noball"
    if RX["wide"].search(t):
        return "wide"
    if RX["four"].search(t) and not need:
        return "four"
    if RX["dot"].search(t):
        return "dot"
    return None


def events_from_segments(segs, energy=None):
    """merge consecutive same-event segments (< 6 s apart) into one event. energy(t)->excess dB around t (optional)."""
    ev = []
    for s in segs:
        k = classify_segment(s["text"])
        if not k:
            continue
        if ev and ev[-1]["kind"] == k and s["start"] - ev[-1]["t_end"] < 6:
            ev[-1]["t_end"] = s["end"]
            continue
        ev.append({"kind": k, "t": s["start"], "t_end": s["end"], "text": s["text"][:100]})
    for e in ev:
        e["db"] = float(energy(e["t"])) if energy else 0.0
    return ev


def innings_break(segs, dur):
    """longest stretch of no cricket talk in the middle of the window -> (start, end). Fallback: the middle."""
    nb = int(dur // 60) + 1
    play = np.zeros(nb)
    for s in segs:
        if PLAY_RX.search(s["text"]) or classify_segment(s["text"]):
            play[min(nb - 1, int(s["start"] // 60))] += 1
    best, cur_s, best_s = 0, None, None
    for i in range(int(nb * 0.25), int(nb * 0.75)):
        if play[i] == 0:
            cur_s = i if cur_s is None else cur_s
            if i - cur_s + 1 > best:
                best, best_s = i - cur_s + 1, cur_s
        else:
            cur_s = None
    if best >= 4 and best_s is not None:
        return best_s * 60.0, (best_s + best) * 60.0
    return dur * 0.5 - 60.0, dur * 0.5 + 60.0


# ---------------------------------------------------------------- toss + end blocks
def toss_block(segs, dur):
    """Whole sentences only. Returns list of (start,end) (1 or 2 clips) and the time where play talk starts."""
    early = [s for s in segs if s["start"] < min(dur, 2400)]
    t0 = next((s for s in early if TOSS_RX.search(s["text"])), None)
    if not t0:
        return [(0.0, min(40.0, dur))], 40.0
    dec = next((s for s in early if s["start"] >= t0["start"] and DECIDE_RX.search(s["text"])
                and s["start"] - t0["start"] < 240), t0)
    a = max(0.0, t0["start"] - 6.0)
    if dec["start"] - t0["end"] <= 30:
        b = dec["end"] + 5.0
        clips = [(a, min(b, a + TOSS_MAX))]
    else:                                              # cut the gap between "who won the toss" and "decision"
        c1 = (a, t0["end"] + 3.0)
        c2 = (max(c1[1], dec["start"] - 2.0), dec["end"] + 5.0)
        clips = [c1, c2]
        while sum(y - x for x, y in clips) > TOSS_MAX and clips[0][1] - clips[0][0] > 15:
            clips[0] = (clips[0][0], clips[0][1] - 5.0)
    return clips, clips[-1][1]


def end_block(segs, dur):
    """last over complete -> end of window (winning run + celebration)."""
    tail = [s for s in segs if s["start"] > dur - 900 and LASTOVER_RX.search(s["text"])]
    start = max(tail[0]["start"] - 5.0, dur - END_MAX) if tail else dur - 170.0
    return max(0.0, start), dur


# ---------------------------------------------------------------- the plan
def plan_from_segments(segs, dur, target_s, energy=None, sb=None):
    """-> dict(segs=[{start,end,event,...}], total, dropped={...}, info={...})
    sb = scoreboard analysis (hl_scoreboard.analyze_window): exact ball events / innings / last over instead of commentary keywords."""
    if sb:
        toss_clips = [(0.0, min(TOSS_MAX, dur * 0.25))]            # the window starts ~35 s before the toss banner: banner + captains + decision, uninterrupted
        play_from = toss_clips[-1][1]
        end_b = dur
        end_a = max(play_from, sb["last_over_start"] - 8.0, dur - SB_END_MAX)   # the match-deciding over is shown complete, then win + celebration
        br_a, br_b = sb["break"]
        ev = [dict(e) for e in sb["events"] if play_from <= e["t"] < end_a]
        for e in ev:
            e["db"] = float(energy(e["t"])) if energy else 0.0
    else:
        toss_clips, play_from = toss_block(segs, dur)
        end_a, end_b = end_block(segs, dur)
        br_a, br_b = innings_break(segs, dur)
        ev = [e for e in events_from_segments(segs, energy) if play_from <= e["t"] < end_a]

    def in_death(t):
        if sb:
            return any(a <= t < b for a, b in sb["death"])
        if t < br_a:
            lo, hi = play_from, br_a
        elif t >= br_b:
            lo, hi = br_b, end_b
        else:
            return False
        return t >= hi - DEATH_FRAC * (hi - lo)

    clips = []
    for a, b in toss_clips:
        clips.append({"start": a, "end": b, "event": "toss", "must": True, "cost": b - a})
    for e in ev:
        if e["kind"] == "dot":
            continue
        pa, pb = PAD[e["kind"]]
        c = {"start": max(0.0, e["t"] - pa), "end": min(end_a, e["t_end"] + pb), "event": e["kind"], "db": e["db"],
             "must": e["kind"] in MUST, "t": e["t"], "text": e["text"]}
        clips.append(c)
    sixes = sorted([c for c in clips if c["event"] == "six"], key=lambda c: c["db"], reverse=True)
    for c in sixes[:TOP_SIXES]:                        # replay only for the biggest few sixes
        c["end"] = min(end_a, c["end"] + SIX_REPLAY)
        c["replay"] = True
    # dots: kept in the death overs, otherwise only as filler for a long stretch with nothing shown
    dots = [e for e in ev if e["kind"] == "dot"]
    keep_t = sorted(c["start"] for c in clips) + [end_a]
    death_dots = [d for d in dots if in_death(d["t"])]
    for d in death_dots:
        pa, pb = PAD["dot"]
        clips.append({"start": max(0.0, d["t"] - pa), "end": min(end_a, d["t_end"] + pb), "event": "dot",
                      "must": False, "death": True, "db": d["db"], "t": d["t"]})
    shown = sorted((c["start"], c["end"]) for c in clips)
    fillers = []
    last_end = play_from
    for a, b in shown + [(end_a, end_a)]:
        if a - last_end > FILL_GAP:
            mid = (last_end + a) / 2.0
            cand = [d for d in dots if last_end <= d["t"] <= a and not in_death(d["t"])]
            if cand:
                d = min(cand, key=lambda x: abs(x["t"] - mid))      # first / middle / last of the stretch: closest to the middle
                pa, pb = PAD["dot"]
                fillers.append({"start": max(0.0, d["t"] - pa), "end": min(end_a, d["t_end"] + pb), "event": "dot",
                                "must": False, "filler": True, "db": d["db"], "t": d["t"]})
        last_end = max(last_end, b)
    clips += fillers
    clips.append({"start": end_a, "end": end_b, "event": "finish", "must": True, "cost": end_b - end_a})

    def length(cs):
        return sum(c["end"] - c["start"] for c in cs)

    dropped = {"filler_dot": 0, "four": 0, "death_dot": 0, "replay": 0}
    # ---- length control: never the must-keep set
    if length(clips) > target_s:
        for c in [c for c in clips if c.get("filler")]:
            if length(clips) <= target_s:
                break
            clips.remove(c)
            dropped["filler_dot"] += 1
    if length(clips) > target_s:
        for c in sorted([c for c in clips if c["event"] == "four"], key=lambda c: c["db"]):
            if length(clips) <= target_s:
                break
            clips.remove(c)
            dropped["four"] += 1
    if length(clips) > target_s:
        for c in sorted([c for c in clips if c.get("death")], key=lambda c: c["db"]):
            if length(clips) <= target_s:
                break
            clips.remove(c)
            dropped["death_dot"] += 1
    if length(clips) > target_s:
        for c in [c for c in clips if c.get("replay")]:
            if length(clips) <= target_s:
                break
            c["end"] -= SIX_REPLAY
            c["replay"] = False
            dropped["replay"] += 1

    # ---- merge overlaps (keep the strongest label), chronological
    clips.sort(key=lambda c: c["start"])
    pri = {"toss": 9, "finish": 9, "wicket": 8, "six": 7, "noball": 6, "wide": 5, "four": 4, "dot": 1}
    out = []
    for c in clips:
        if out and c["start"] <= out[-1]["end"] + 1.5:
            o = out[-1]
            o["end"] = max(o["end"], c["end"])
            if pri.get(c["event"], 0) > pri.get(o["event"], 0):
                o["event"] = c["event"]
            o["parts"] = o.get("parts", 1) + 1
        else:
            out.append({"start": c["start"], "end": c["end"], "event": c["event"], "parts": 1})
    total = sum(o["end"] - o["start"] for o in out)
    counts = {}
    for c in clips:
        counts[c["event"]] = counts.get(c["event"], 0) + 1
    return {"segs": out, "total": total, "dropped": dropped,
            "info": {"counts": counts, "innings_break": [round(br_a), round(br_b)], "play_from": round(play_from),
                     "over_budget": total > target_s, "source": "scoreboard" if sb else "commentary"}}


def build_plan(H, wav, dur, db, target_min, hop=0.5, src=None):
    """called from hl_runner.process(): transcribe the whole window, build the plan. db = energy track (dB per hop)."""
    def energy(t):
        i = int(t / hop)
        lo, hi = max(0, i - 6), min(len(db), i + 14)
        return float(np.max(db[lo:hi]) - np.median(db)) if hi > lo else 0.0

    def prog(cur, tot):
        H.hl_review.set_live("transcribe", "Poora match sun raha hai (cut plan) %d/%d" % (cur, tot), cur=cur, total=tot)
    if src and os.environ.get("HL_SB", "1") != "0":                 # scoreboard first: exact, and no whole-match whisper run needed
        try:
            sb = hl_scoreboard.analyze_window(H, src, dur)
        except Exception as e:                                      # noqa
            H.log("scoreboard analysis failed:", e)
            sb = None
        if sb and sum(1 for e in sb["events"] if e["kind"] != "dot") >= 4:
            plan = plan_from_segments([], dur, target_min * 60.0 * 1.5, energy, sb=sb)
            H.log("cutter plan (scoreboard):", plan["info"], "dropped", plan["dropped"], "total %.0fs" % plan["total"])
            return plan
        H.log("scoreboard gave too few events -> commentary fallback")
    segs = transcribe_full(H, wav, dur, prog)
    H.log("cutter: %d transcript segments" % len(segs))
    # budget: target length x1.5 (the must-keep set may exceed it, it is never trimmed)
    plan = plan_from_segments(segs, dur, target_min * 60.0 * 1.5, energy)
    H.log("cutter plan:", plan["info"], "dropped", plan["dropped"], "total %.0fs" % plan["total"])
    return plan
