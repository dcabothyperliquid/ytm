"""
Step 1 of the highlights flow: SCOUT (cheap, no video download).

  bot picks a finished stream -> finds where the match STARTED and where it FINISHED (end of 2nd innings)
  -> writes it to hl_state.json (scouts[]) -> dashboard shows it -> you check on YouTube and CONFIRM
  -> only then hl_runner makes highlights, and only from [start, end].

Detection is a heuristic on the commentary (faster-whisper on short sampled windows of low-bitrate audio):
  start  = first stretch of ball-by-ball talk (runs / four / six / over / ball ...)
  end    = result talk after the last ball ("won by", "match tied", "player of the match" ...)
  finished = result words found AND 2nd-innings signs found (target / need X runs / innings break)
If start (toss OR first ball) or end (last ball / result) is not found the match is marked "incomplete" and is NOT offered for highlights unless you force it.
The numbers are a starting point: the dashboard lets you edit start/end before confirming.
"""
import json
import os
import re
import time

import hl_review

SCOUT_MODEL = os.environ.get("HL_SCOUT_MODEL", "base") or "base"
WIN = 20            # s transcribed per sampled window
STEP = 90           # s between sampled windows
START_SCAN_MAX = 3 * 3600   # look for the first ball only in the first 3 h
END_SCAN_MAX = 75 * 60      # look for the result only in the last 75 min
SR = 16000

PLAY_RX = re.compile(
    r"\b(four|six|sixes|single|dot ball|no run|wicket|bowled|caught|lbw|run[s]?|over|ball|boundary|delivery|"
    r"strike|pitch|bouncer|yorker|wide|no[\s-]?ball|appeal|review)\b|चौका|छक्का|छक्के|विकेट|आउट|रन|ओवर|गेंद|बॉल|बाउंड्री|कैच|बोल्ड",
    re.I)
RESULT_RX = re.compile(
    r"won\s+by|wins?\s+(?:the\s+)?(?:match|game|by)|victory|match\s+tied|it'?s\s+a\s+tie|super[\s-]?over|"
    r"(?:player|man|woman)\s+of\s+the\s+match|presentation|no\s+result|abandoned|"
    r"जीत|जीता|जीती|मैच\s*टाई|टाई|मैन\s*ऑफ़?\s*द\s*मैच|प्लेयर\s*ऑफ़?\s*द\s*मैच|विजेता", re.I)
CHASE_RX = re.compile(
    r"\btarget\b|need(?:s|ed)?\s+\d+|require(?:s|d)?\s+\d+|chas(?:e|ing)|second\s+innings|2nd\s+innings|innings\s+break|"
    r"required\s+rate|run\s+rate|टारगेट|लक्ष्य|चेज़?|चेस|दूसरी\s+पारी|इनिंग्स?\s+ब्रेक|रन\s*चाहिए|रन\s*चाहिये", re.I)
TOSS_RX = re.compile(r"\btoss\b|elected\s+to\s+(?:bat|bowl|field)|chose\s+to\s+(?:bat|bowl|field)|won\s+the\s+toss|टॉस|पहले\s+(?:बल्लेबाज़ी|बैटिंग|गेंदबाज़ी|बॉलिंग)", re.I)
LASTOVER_RX = re.compile(r"last[\s-]?over|final[\s-]?over|last[\s-]?ball|आखिरी\s*ओवर|आख़िरी\s*ओवर|अंतिम\s*ओवर|आखिरी\s*गेंद", re.I)


def _fmt(t):
    t = int(t)
    return "%d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60)


def _audio_to_wav(H, vid, dur):
    """Low-bitrate audio of the whole VOD -> 16 kHz mono s16le raw file. Returns path."""
    os.makedirs(H.WORK, exist_ok=True)
    raw = os.path.join(H.WORK, "scout_src")
    for f in os.listdir(H.WORK):
        if f.startswith("scout_src"):
            os.remove(os.path.join(H.WORK, f))
    url = "https://www.youtube.com/watch?v=" + vid
    hl_review.set_live("download", "Scout: sirf audio download (match ka time dhoondhne ke liye)", vid=vid, force=True)
    H.sh(H.ytdlp_base() + ["--no-playlist", "-f", "worstaudio/worst", "-o", raw + ".%(ext)s", url])
    src = next((os.path.join(H.WORK, f) for f in os.listdir(H.WORK) if f.startswith("scout_src")), None)
    if not src:
        raise RuntimeError("scout audio download failed")
    wav = os.path.join(H.WORK, "scout.s16")
    H.sh(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", wav])
    os.remove(src)
    return wav


class _Listener:
    def __init__(self, H, wav):
        import numpy as np
        from faster_whisper import WhisperModel
        self.H, self.np = H, np
        H.log("scout: loading whisper", SCOUT_MODEL)
        self.model = WhisperModel(SCOUT_MODEL, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
        n = os.path.getsize(wav) // 2
        self.mm = np.memmap(wav, dtype=np.int16, mode="r", shape=(n,))
        self.dur = n / float(SR)
        self.cache = {}

    def text(self, t):
        t = max(0.0, min(t, self.dur - WIN))
        key = int(t)
        if key in self.cache:
            return self.cache[key]
        a, b = int(t * SR), int((t + WIN) * SR)
        audio = self.np.asarray(self.mm[a:b], dtype=self.np.float32) / 32768.0
        try:
            segs, _ = self.model.transcribe(audio, language=self.H.LANG, beam_size=1, vad_filter=True,
                                            initial_prompt=self.H.WHISPER_HINT, condition_on_previous_text=False)
            txt = " ".join(s.text.strip() for s in segs)
        except Exception as e:                      # noqa
            self.H.log("scout whisper fail @%d: %s" % (key, e))
            txt = ""
        self.cache[key] = txt
        return txt


def _play_score(txt):
    return len(PLAY_RX.findall(txt or ""))


def find_start(L, dur):
    """First sustained ball-by-ball talk. Returns (start_s, evidence) or (None, why)."""
    hi = min(dur - WIN, START_SCAN_MAX)
    ts = [float(t) for t in range(0, int(hi), STEP)]
    hits = []
    # 1) toss: first window that talks about the toss and is followed by ball-by-ball play within 45 min
    toss_t = None
    for i, t in enumerate(ts):
        hl_review.set_live("scout", "Scout: toss dhoondh raha hai (%s)" % _fmt(t), cur=i + 1, total=len(ts))
        if TOSS_RX.search(L.text(t)):
            first_play = None
            for t2 in range(int(t), int(min(hi, t + 45 * 60)), STEP):
                if _play_score(L.text(float(t2))) >= 2:
                    first_play = t2
                    break
            if first_play is not None:
                toss_t = t
                break
    if toss_t is not None:
        back = toss_t
        for k in range(1, 4):                         # walk back up to 60 s to catch the start of the toss talk
            if TOSS_RX.search(L.text(max(0.0, toss_t - k * WIN))):
                back = max(0.0, toss_t - k * WIN)
            else:
                break
        return max(0.0, back - 15.0), "toss ~%s" % _fmt(back)
    hits = []
    for i, t in enumerate(ts):
        s = _play_score(L.text(t))
        hits.append(s)
        hl_review.set_live("scout", "Scout: match ka start dhoondh raha hai (%s)" % _fmt(t), cur=i + 1, total=len(ts))
        # 3 of 4 consecutive windows with >= 2 play words = the match is on
        if i >= 3 and sum(1 for h in hits[-4:] if h >= 2) >= 3:
            first = ts[i - 3 + next(k for k, h in enumerate(hits[-4:]) if h >= 2)]
            return refine_start(L, first), "ball-by-ball commentary %s se" % _fmt(first)
    return None, "ball-by-ball commentary nahi mili (first %d min)" % (hi / 60)


def refine_start(L, first):
    """Walk back from the first dense window in small steps to the first window that has any play talk."""
    t = first
    for back in range(1, 7):                          # up to 6 * 20 s = 2 min back
        cand = first - back * WIN
        if cand < 0:
            break
        if _play_score(L.text(cand)) >= 1:
            t = cand
        else:
            break
    return max(0.0, t - 10.0)


def find_end(L, dur):
    """Result talk after the last ball: last 75 min first, widened to 3 h if no result found (long post-match show)."""
    r = _find_end(L, dur, END_SCAN_MAX)
    if not r["finished"] and dur > END_SCAN_MAX + 600:
        r2 = _find_end(L, dur, 3 * 3600)
        if r2["finished"]:
            return r2
    return r


def _find_end(L, dur, span):
    """Returns dict(end, finished, evidence[])."""
    lo = max(0.0, dur - span)
    ts = [float(t) for t in range(int(lo), int(max(lo, dur - WIN)), STEP)]
    rows = []
    for i, t in enumerate(ts):
        txt = L.text(t)
        rows.append((t, _play_score(txt), bool(RESULT_RX.search(txt)), bool(LASTOVER_RX.search(txt)), bool(CHASE_RX.search(txt))))
        hl_review.set_live("scout", "Scout: match ka finish dhoondh raha hai (%s)" % _fmt(t), cur=i + 1, total=len(ts))
    play_t = [r[0] for r in rows if r[1] >= 2]
    last_play = max(play_t) if play_t else None
    res_t = [r[0] for r in rows if r[2] and (last_play is None or r[0] >= last_play - STEP)]
    result_at = min(res_t) if res_t else None
    lastover = any(r[3] for r in rows)
    ev = []
    if last_play is not None:
        ev.append("aakhri ball-by-ball commentary ~%s" % _fmt(last_play))
    if result_at is not None:
        ev.append("result/trophy baat ~%s" % _fmt(result_at))
    if lastover:
        ev.append("'last over' bola gaya")
    if last_play is None:
        return {"end": None, "finished": False, "evidence": ev + ["end ke 75 min me ball-by-ball commentary nahi"]}
    # fine scan (20 s) around the last coarse play window -> real last ball / winning run
    last_ball = last_play
    for t in range(int(last_play), int(min(dur - WIN, last_play + 2 * STEP)), WIN):
        if _play_score(L.text(float(t))) >= 2:
            last_ball = float(t)
    end = last_ball + WIN + 45.0                      # winning run / last wicket + ~45 s celebration
    return {"end": min(end, dur), "finished": result_at is not None, "evidence": ev,
            "last_play": last_play, "result_at": result_at, "lastover": lastover}


def chase_seen(L, start, end):
    """2nd-innings signs (target / need N runs / innings break) in the second half of the match window."""
    mid = start + (end - start) * 0.55
    ts = [float(t) for t in range(int(mid), int(max(mid + 1, end - WIN)), STEP * 3)]
    n = 0
    for t in ts[:12]:
        if CHASE_RX.search(L.text(t)):
            n += 1
    return n


def scout_vid(H, vid, title="", dur_hint=None):
    """Run the whole scout for one VOD and store the result in hl_state.scouts."""
    hl_review.set_live("scout", "Scout shuru: %s" % (H.clean_title(title) or vid), vid=vid, title=H.clean_title(title), force=True)
    # 1) scoreboard OCR first (reliable); commentary scan below is only the fallback
    try:
        import hl_board
        bm = hl_board.scan(H, vid, title, dur_hint)
    except Exception as e:                          # noqa
        H.note("scoreboard scout failed: %s" % e, "warning", "hl scout")
        bm = None
    if bm and bm["matches"]:
        return _entry_from_board(H, vid, title, bm)
    wav = _audio_to_wav(H, vid, dur_hint)
    try:
        L = _Listener(H, wav)
        dur = L.dur
        H.log("scout audio %.1f min" % (dur / 60))
        start, why_s = find_start(L, dur)
        end_info = find_end(L, dur)
        end = end_info["end"]
        ev = [why_s] if start is not None else [why_s]
        ev += end_info["evidence"]
        chase = 0
        if start is not None and end:
            chase = chase_seen(L, start, end)
            ev.append("2nd innings ke signs: %d window" % chase if chase else "2nd innings / target ki baat nahi mili")
    finally:
        if os.path.exists(wav):
            os.remove(wav)
    toss_ok = start is not None and why_s.startswith("toss")
    res_ok = bool(end and end_info["finished"])
    last_ok = bool(end)                              # last ball-by-ball commentary found (+ celebration)
    # RULE (Naem): toss is NOT required. A match is ready when its START (toss OR first ball) and its END
    # (winning run / last ball / result talk) are both found. A 4-5 min hole in the middle is fine.
    finished = bool(start is not None and last_ok and (res_ok or chase >= 1 or end_info.get("lastover")))
    status = "pending" if finished else "incomplete"
    if start is None or not end:
        reason = "Match ka start/finish detect nahi hua — time khud daalo ya skip karo"
    elif not finished:
        reason = "Start aur last ball mili par result / chase / last-over ke signs nahi — pakka check karo"
    elif toss_ok:
        reason = "Pura match laga: toss se last ball tak (beech ke 4-5 min miss ho to chalega)"
    else:
        reason = "Toss nahi mila par first ball se last ball tak match mila — time check karke Confirm karo"
    entry = {"vid": vid, "title": H.clean_title(title) or title or vid, "vod_min": round(dur / 60.0, 1),
             "found": int(time.time()), "status": status, "start": round(start, 1) if start is not None else None,
             "end": round(end, 1) if end else None, "finished": finished, "reason": reason, "evidence": ev[:8]}

    def mut(st):
        st["scouts"] = [entry] + [s for s in st.get("scouts", []) if s["vid"] != vid]
        st["scouts"] = st["scouts"][:30]
    hl_review.save_state(mut, "hl scout %s" % vid)
    H.note("scout %s: %s | start %s end %s | %s" % (vid, status, entry["start"], entry["end"], reason))
    return entry


def _entry_from_board(H, vid, title, bm):
    import hl_board
    ms = bm["matches"]
    best = next((m for m in ms if m["result"] and m["toss_seen"]), None) or next((m for m in ms if m["result"]), None) or ms[0]
    done = bool(best["result"] and best["end"])
    ev = []
    for m in ms:
        tag = "Match %s" % (m["match_no"] or "?")
        ev.append("%s%s: %s se %s (%s)" % (tag, " <- ye wala" if m is best else "", hl_board.fmt(m["start"]),
                                           hl_board.fmt(m["end"]) if m["end"] else "result nahi mila",
                                           m["winner"] or ("toss nahi mila, first ball se" if not m["toss_seen"] else "result banner nahi")))
    if best["chase_frames"]:
        ev.append("2nd innings ke signs (target/need) mile")
    if done:
        reason = "Scoreboard se match mila: toss se result tak — time check karke Confirm karo" if best["toss_seen"] \
            else "Scoreboard se match mila (toss nahi mila, first ball se) — time check karke Confirm karo"
    else:
        reason = "Toss mila par result banner nahi — match poora nahi laga, pakka check karo"
    entry = {"vid": vid, "title": H.clean_title(title) or title or vid, "vod_min": round(bm["dur"] / 60.0, 1),
             "found": int(time.time()), "status": "pending" if done else "incomplete", "start": best["start"],
             "end": best["end"], "finished": done, "reason": reason, "evidence": ev[:8], "source": "scoreboard",
             "others": [{"start": m["start"], "end": m["end"], "match_no": m["match_no"]} for m in ms if m is not best]}

    def mut(st):
        st["scouts"] = [entry] + [x for x in st.get("scouts", []) if x["vid"] != vid]
        st["scouts"] = st["scouts"][:30]
    hl_review.save_state(mut, "hl scout %s" % vid)
    H.note("scout(board) %s: %s | start %s end %s | %s" % (vid, entry["status"], entry["start"], entry["end"], "; ".join(ev)))
    return entry


def scout_vids():
    """vids already scouted (any status) -> never auto-scouted again unless 'rescout'."""
    st, _ = hl_review.load_state()
    return {s["vid"] for s in st.get("scouts", [])}
