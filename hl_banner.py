"""Highlight banner (= YouTube thumbnail) + separate YouTube / Facebook captions. Best effort: every failure -> None, never breaks a run.
Players: face crops from the best moments of THIS match (opencv Haar, needs opencv-python-headless; no cv2 -> banner without faces)."""
import os
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
    c = (info or {}).get("counts") or _stats(segs)
    six, wk = c.get("six", 0), c.get("wicket", 0)
    bits = []
    if six >= 5:
        bits.append("%d SIXES" % six)
    if wk >= 3:
        bits.append("%d WICKETS" % wk)
    if any(s["event"] == "finish" for s in segs):
        bits.append("LAST OVER THRILLER")
    return "  •  ".join(bits[:3]) or "MUST WATCH"


def captions(title, vid, segs, chapters, info=None):
    """-> (youtube dict(title, description), facebook text)"""
    p = ytm_style.parse_match(title)
    c = (info or {}).get("counts") or _stats(segs)
    six, wk, fo = c.get("six", 0), c.get("wicket", 0), c.get("four", 0)
    teams = "%s vs %s" % (p["a"], p["b"]) if p["a"] and p["b"] else (p["tour"] or title)
    tour = p["tour"] or ""
    hook = []
    if six:
        hook.append("%d chhakke" % six)
    if wk:
        hook.append("%d wicket" % wk)
    stats = " + ".join(hook)
    yt_t = ytm_style.highlight_title(title, seed=vid)
    ch = "\n".join("%s %s" % ("%d:%02d" % (t // 60, t % 60), ev.title()) for t, ev in chapters if ev not in ("dot",))
    yt_d = ("\U0001F525 %s ke full highlights - %s!\n%s\n\nToss se lekar last over tak har chhakka, har wicket aur aakhri ball ka rona-dhona - sab kuch ek video me.\n"
            "Pura match miss kiya? Ab yahin dekho.\n\n⏱ Chapters:\n%s\n\n\U0001F3AC Full match: https://www.youtube.com/watch?v=%s\n"
            "\U0001F514 Subscribe karo aur aise hi local cricket ke highlights na miss karo.\n\n#cricket #highlights #localcricket #LML %s") % (
        teams, stats or "full match", tour, ch, vid, "#" + p["tour"].replace(" ", "") if p["tour"] else "")
    fb = ("\U0001F525 %s\n\n\U0001F4A5 %s%s\n\nJisne ye match nahi dekha wo sach me kuch bada miss kar gaya \U0001F62E\n"
          "Poora highlight dekho aur batao - sabse bada shot kaunsa tha? \U0001F447\n\nFull match: https://www.youtube.com/watch?v=%s\n\n"
          "#cricket #highlights #localcricket #LML") % (teams.upper(), stats.upper() or "FULL HIGHLIGHTS", (" | %d chauke" % fo) if fo else "", vid)
    return {"title": yt_t[:100], "description": yt_d[:4900]}, fb


def make(src, title, vid, segs, chapters, outdir, info=None):
    """-> dict(banner=<file name or None>, yt_caption={title,description}, fb_caption=str)"""
    res = {"banner": None, "yt_caption": None, "fb_caption": None}
    try:
        res["yt_caption"], res["fb_caption"] = captions(title, vid, segs, chapters, info)
    except Exception:
        pass
    try:
        pick = next((s for s in sorted([s for s in segs if s["event"] == "six"], key=lambda s: -s.get("db", 0.0))), None) \
            or next((s for s in segs if s["event"] == "wicket"), None) or segs[len(segs) // 2]
        frame = os.path.join(outdir, "_bn.jpg")
        _grab(src, (pick["start"] + pick["end"]) / 2.0 + 1.0, frame)
        faces = []
        try:
            faces = find_faces(src, segs, outdir)
        except Exception:
            faces = []
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
