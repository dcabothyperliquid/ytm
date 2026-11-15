"""Highlight banner (= YouTube thumbnail) + separate YouTube / Facebook captions. Best effort: every failure -> None, never breaks a run.
Players: face crops from the best moments of THIS match (opencv Haar, needs opencv-python-headless; no cv2 -> banner without faces)."""
import os
import re
import subprocess

import ytm_style


def _grab(src, t, path):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "%.2f" % t, "-i", src, "-frames:v", "1", "-q:v", "2", path],
                   capture_output=True)
    return os.path.isfile(path)


def find_faces(src, segs, workdir, want=3):
    """segs = plan segs (start,end,event,db). Sample frames inside the best sixes / wickets, keep the biggest sharp face of each."""
    try:
        import cv2
    except Exception:
        return []
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if casc.empty():
        return []
    pri = sorted([s for s in segs if s["event"] in ("six", "wicket", "finish", "toss")],
                 key=lambda s: (-(s["event"] == "six") * s.get("db", 0.0) - (s["event"] == "wicket") * 5.0))
    out, tmp = [], os.path.join(workdir, "_f.jpg")
    for s in pri[:14]:
        best = None
        a, b = s["start"], s["end"]
        for k in range(8):
            t = a + (b - a) * (k + 0.5) / 8.0
            if not _grab(src, t, tmp):
                continue
            im = cv2.imread(tmp)
            if im is None:
                continue
            g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
            for (x, y, w, h) in casc.detectMultiScale(g, 1.1, 9, minSize=(100, 100)):
                if y > im.shape[0] * 0.8:
                    continue
                ctr = im[y + h // 4:y + 3 * h // 4, x + w // 4:x + 3 * w // 4].reshape(-1, 3).mean(axis=0)     # BGR: skin = R>G>B, not grass / sky
                if not (ctr[2] > ctr[1] > ctr[0] and ctr[2] > 90 and ctr[2] - ctr[0] > 25):
                    continue
                sharp = cv2.Laplacian(g[y:y + h, x:x + w], cv2.CV_64F).var()
                sc = w * (1 if sharp > 40 else 0.4)
                if best is None or sc > best[0]:
                    m = int(w * 0.45)
                    x0, y0 = max(0, x - m), max(0, y - m)
                    best = (sc, im[y0:min(im.shape[0], y + h + m), x0:min(im.shape[1], x + w + m)].copy())
        if best is not None and best[1].shape[0] > 110:
            out.append(best[1])
        if len(out) >= want:
            break
    if os.path.isfile(tmp):
        os.remove(tmp)
    return out


def _stats(segs):
    n = {}
    for s in segs:
        k = s["event"]
        n[k] = n.get(k, 0) + 1 + (s.get("parts", 1) - 1 if k in ("six", "four", "wicket") else 0)
    return n


def pill_text(segs, info=None):
    """banner pill: no six / four / wicket counts (Naem: not wanted) - only the finish hook."""
    if any(s["event"] == "finish" for s in segs):
        return "LAST BALL THRILLER" if (info or {}).get("last_ball") else "LAST OVER THRILLER"
    return "MUST WATCH"


_BAD_TOUR = re.compile(r"prize|distribution|ceremony|award|presentation|inaugur|opening", re.I)


def fb_text(teams, hook, vid, dur=None):
    """Facebook post text: 'HIGHLIGHTS' + teams in the first line (FB cuts after ~2 lines -> '...more'), then FOMO + comment bait."""
    mins = int(round(dur / 60.0)) if dur else 0
    len_line = ("\U0001F3AC Toss se aakhri ball tak - poora match sirf %d min me!" % mins) if mins else "\U0001F3AC Toss se aakhri ball tak - poora match ek hi video me!"
    return ("\U0001F525\U0001F3CF HIGHLIGHTS | %s\n\n"
            "\u26A1 %s \u26A1\n"
            "%s\n\n"
            "\U0001F631 Jisne ye match miss kiya, usne sabse bada moment miss kar diya!\n"
            "\U0001F4A5 Chhakke, wicket, pressure - sab kuch isme hai.\n\n"
            "\U0001F447 Comment me batao: sabse bada shot kaunsa tha?\n"
            "\U0001F4FA Full match: https://www.youtube.com/watch?v=%s\n\n"
            "\U0001F514 Follow karo LML - Localmatchlive: aise highlights sabse pehle yahin.\n\n"
            "#cricket #highlights #matchhighlights #localcricket #LML #LMLSports") % (teams.upper(), hook.upper(), len_line, vid)


def captions(title, vid, segs, chapters, info=None, dur=None):
    """-> (youtube dict(title, description), facebook text)"""
    p = ytm_style.parse_match(title)
    if p.get("tour") and _BAD_TOUR.search(p["tour"]):       # 'Prize Distribution' is the VOD name, not the tournament
        p = dict(p, tour="")
    c = (info or {}).get("counts") or _stats(segs)
    teams = "%s vs %s" % (p["a"], p["b"]) if p["a"] and p["b"] else (p["tour"] or title)
    tour = p["tour"] or ""
    fin = any(s_["event"] == "finish" for s_ in segs)
    stats = ("last ball tak gaya thriller" if (info or {}).get("last_ball") else "last over ka thriller") if fin else ""
    yt_t = ytm_style.highlight_title(title, seed=vid)
    yt_d = ("\U0001F525 %s ke full highlights - %s!\n%s\n\nToss se lekar aakhri ball tak ka pura thriller - sab kuch ek video me.\n"
            "Pura match miss kiya? Ab yahin dekho.\n\n\U0001F3AC Full match: https://www.youtube.com/watch?v=%s\n"
            "\U0001F514 Subscribe karo aur aise hi local cricket ke highlights na miss karo.\n\n#cricket #highlights #localcricket #LML %s") % (
        teams, stats or "full match", tour, vid, "#" + p["tour"].replace(" ", "") if p["tour"] else "")
    fb = fb_text(teams, stats or "full highlights", vid, dur)
    return {"title": yt_t[:100], "description": yt_d[:4900]}, fb


def make(src, title, vid, segs, chapters, outdir, info=None):
    """-> dict(banner=<file name or None>, yt_caption={title,description}, fb_caption=str)"""
    res = {"banner": None, "yt_caption": None, "fb_caption": None}
    try:
        res["yt_caption"], res["fb_caption"] = captions(title, vid, segs, chapters, info, (info or {}).get("dur"))
    except Exception:
        pass
    try:
        pick = next((s for s in segs if s["event"] == "finish"), None) \
            or next((s for s in sorted([s for s in segs if s["event"] == "six"], key=lambda s: -s.get("db", 0.0))), None) \
            or next((s for s in segs if s["event"] == "wicket"), None) or segs[len(segs) // 2]
        frame = os.path.join(outdir, "_bn.jpg")
        _grab(src, (pick["start"] + pick["end"]) / 2.0 + 1.0, frame)
        faces = []                              # Haar face finder gave mostly false hits on wide match shots: players on the banner come later (captain photos)
        out = os.path.join(outdir, "banner.jpg")
        ytm_style.make_banner(frame if os.path.isfile(frame) else None, title, out, badge="HIGHLIGHTS", strip="WATCH ON:",
                              pill=pill_text(segs, info), faces=faces)
        res["banner"] = "banner.jpg"
        res["faces"] = len(faces)
        if os.path.isfile(frame):
            os.remove(frame)
    except Exception as e:                      # noqa
        res["error"] = str(e)[:200]
    return res
