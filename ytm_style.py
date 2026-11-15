"""LML Sports - pro style titles + thumbnail banner (original design, nothing copied from other channels).

  live_title(src)            -> "Live 🔴🏆 (SEMIFINAL-2) UMERKOTE PREMIER LEAGUE 2026 | KIRTI 11 vs SLOW POISON 11 | LML Sports"
  highlight_title(src, seed) -> FOMO style, rotates between 6 templates (seed = video id, so it is stable per match)
  short_title(event, src, seed)
  make_banner(frame, src, out) -> 1280x720 JPEG (<2 MB) ready for thumbnails.set

Kill switch: env YTM_STYLE=off (old plain titles, no banner).  Banner only: YTM_BANNER=off.
Everything here is pure (no network); callers wrap it in try/except so a styling bug can never stop a relay.
"""
import hashlib
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
BRAND = os.environ.get("YTM_BRAND", "LML Sports")
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]")
_VS_RE = re.compile(r"^(.{1,40}?)\s+(?:vs\.?|v/s|versus|v)\s+(.{1,40}?)$", re.I)
_STAGE_RE = re.compile(r"(semi\s*-?\s*final\s*:?-?\s*\d?|quarter\s*-?\s*final\s*:?-?\s*\w?|final|qualifier\s*-?\s*\d?|eliminator|"
                       r"league\s+match|match\s*(?:no\.?)?\s*\d+|group\s+[a-d]|pool\s+[a-d]|round\s+\d+|super\s*(?:4|8|six))", re.I)
_TOUR_RE = re.compile(r"(league|cup|trophy|premier|tournament|series|championship|mahotsav|pratiyogita|mela|ipl|cl\b)", re.I)
_SPORTS = [("kho kho", "Kho Kho"), ("kabaddi", "Kabaddi"), ("cricket", "Cricket"), ("football", "Football"),
           ("volleyball", "Volleyball"), ("hockey", "Hockey"), ("badminton", "Badminton")]


def _seed(x):
    return int(hashlib.md5(str(x).encode()).hexdigest()[:8], 16)


def _clean(t):
    t = _EMOJI_RE.sub(" ", t or "")
    t = re.sub(r"\b(?:live|लाइव)\b", " ", t, flags=re.I)
    t = re.sub(r"#\w+", " ", t)
    t = re.sub(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b", " ", t)               # dates
    return " ".join(t.replace("‑", "-").split())


def _stage_norm(s):
    s = re.sub(r"\s*[:\-]+\s*", "-", s.strip()).upper()
    s = re.sub(r"SEMI\s*-?\s*FINAL", "SEMIFINAL", s)
    s = re.sub(r"QUARTER\s*-?\s*FINAL", "QUARTERFINAL", s)
    return s.strip("- ")


def parse_match(src, channel=""):
    """Source stream title -> dict(stage, tour, a, b, sport). Missing parts are ''. Never raises."""
    raw = _clean(src)
    raw = re.sub(r"(%s)\s*-\s*(?=[A-Za-z])" % _STAGE_RE.pattern, r"\1 | ", raw, count=1, flags=re.I)   # "Quarter Final-A vs B" -> "Quarter Final | A vs B"
    segs = [s.strip(" :-()[]") for s in re.split(r"\|\||\||\s[-–—]\s|:\s", raw) if s.strip(" :-()[]")]
    a = b = stage = tour = ""
    for s in segs:
        m = _VS_RE.match(s)
        if m and not a:
            a, b = m.group(1).strip(" -:"), m.group(2).strip(" -:")
            continue
    if not a:                                              # "Quarter Final-A vs B" glued into one segment
        m = re.search(r"(?:^|[-:])\s*([^-:|]{2,40}?)\s+(?:vs\.?|v/s|versus)\s+([^-:|]{2,40})", raw, re.I)
        if m:
            a, b = m.group(1).strip(), m.group(2).strip()
    for s in segs:
        if (a and a in s and b and b in s):
            continue
        if not stage and _STAGE_RE.search(s) and len(s) <= 28:
            stage = _stage_norm(_STAGE_RE.search(s).group(0))
            continue
        if not tour and (_TOUR_RE.search(s) or len(s) > 12):
            tour = s
    if not stage:                                          # stage glued in front of the team segment
        m = _STAGE_RE.search(raw)
        if m:
            stage = _stage_norm(m.group(0))
    if a:                                                  # remove stage words that stuck to team A
        a = _STAGE_RE.sub("", a).strip(" -:()") or a
    up = lambda t: " ".join(w if (w.isupper() or any(c.isdigit() for c in w)) else (w.upper() if len(w) <= 2 else w.title()) for w in t.split())
    low = (src or "").lower() + " " + (channel or "").lower()
    sport = next((n for k, n in _SPORTS if k in low), "")
    return {"stage": stage, "tour": up(tour) if tour else "", "a": up(a), "b": up(b), "sport": sport or "Cricket"}


def _fit(parts, limit=100, sep=" | "):
    """join parts, dropping the least important from the end until it fits"""
    parts = [p for p in parts if p]
    while len(parts) > 1 and len(sep.join(parts)) > limit:
        parts.pop(-2 if len(parts) > 2 else -1)
    t = sep.join(parts)
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0]


def live_title(src, channel=""):
    """Phone shows ~60 chars: so '🔴 teams | stage' comes first; tournament, prize, brand follow and are cut from the end."""
    p = parse_match(src, channel)
    pr = facts_for(src).get("prize")
    head = "Live \U0001F534\U0001F3C6"
    teams = "%s vs %s" % (p["a"], p["b"]) if p["a"] and p["b"] else ""
    if not (teams or p["tour"]):
        return "%s %s Match | %s" % (head, p["sport"], BRAND)
    first = ("%s %s" % (head, teams)) if teams else ("%s %s" % (head, p["tour"].upper()))
    parts = [first, p["stage"], p["tour"].upper() if teams else "", ("\U0001F4B0 Prize %s" % pr) if pr else "", BRAND]
    parts = [x for x in parts if x]
    while len(" | ".join(parts)) > 100 and len(parts) > 1:
        # drop brand first, then tournament, then prize... never the teams/stage
        drop = next((i for i in (len(parts) - 1, len(parts) - 2) if i > 1), None)
        if drop is None:
            break
        parts.pop(drop)
    t = " | ".join(parts)
    return t if len(t) <= 100 else t[:100].rsplit(" ", 1)[0]


_HL = [
    "\U0001F525 Last Over Thriller! {m} \u2013 Kisne Jeeta? | Full Highlights",
    "\U0001F631 Yeh Match Dekh Ke Hosh Ud Jayenge! {m} | Highlights",
    "\u26A1 Aakhri Over Ka Drama! {m} | Full Highlights",
    "\U0001F4A5 Chhakke Hi Chhakke! {m} | Match Highlights",
    "\U0001F3C6 {m} \u2013 Aisa Match Dobara Nahi Milega! | Highlights",
    "\U0001F6A8 Jeet Ya Haar? {m} \u2013 Pura Match 1 Video Mein | Highlights",
]
_SHORT = {
    "six": ["\U0001F4A5 Yeh Six Dekh Ke Hosh Ud Jayenge! \U0001F525 {m}", "\U0001F680 Kya Chhakka Hai Yeh! {m}"],
    "wicket": ["\u2620\uFE0F Kya Wicket Hai Yeh! {m}", "\u261D\uFE0F Wicket Alert! Dekho Kya Hua {m}"],
    "hattrick": ["\U0001F525 HAT-TRICK! Believe Nahi Hoga {m}", "\U0001F3A9 Hat-Trick Alert! {m}"],
    "finish": ["\U0001F631 Last Over Ka Thriller! {m}", "\U0001F62E Last Over Mein Kya Hua Dekho! {m}"],
    "catch": ["\U0001F64C Kya Catch Pakda Hai! {m}", "\U0001F9E4 Catch Of The Match! {m}"],
}


def live_desc(src, channel=""):
    p = parse_match(src, channel)
    head = " - ".join(x for x in (p["tour"], p["stage"]) if x) or "Live %s" % p["sport"]
    tag = "".join(re.sub(r"[^0-9A-Za-z]", "", w).capitalize() for w in (p["tour"] or "").split()[:3])
    L = ["\U0001F534 LIVE NOW: " + head]
    pr = facts_for(src).get("prize")
    if pr:
        L.append("\U0001F4B0 Winner prize: %s" % pr)
    if p["a"] and p["b"]:
        L.append("\U0001F3CF %s vs %s" % (p["a"], p["b"]))
    L += ["", "Watch the full %s match live on %s." % (p["sport"].lower(), BRAND),
          "\U0001F514 Subscribe + press the bell so you never miss a local match.", "",
          ((("#%s " % tag) if tag else "") + "#Live%s #%s" % (p["sport"].replace(" ", ""), BRAND.replace(" ", "")))]
    return "\n".join(L)


def _mtxt(p, short=False):
    if p["a"] and p["b"]:
        return "%s vs %s" % (p["a"], p["b"])
    return p["tour"] or "LML Sports"


def highlight_title(src, seed="", channel=""):
    p = parse_match(src, channel)
    pr = facts_for(src).get("prize")
    pool = _HL + (["\U0001F4B0 %s Ka Inaam Kisne Jeeta? {m} | Full Highlights" % pr] * 2 if pr else [])
    t = pool[_seed(seed or src) % len(pool)].format(m=_mtxt(p))
    return t if len(t) <= 100 else _HL[0].format(m=_mtxt(p))[:100].rsplit(" ", 1)[0]


def short_title(event, src, seed="", channel=""):
    p = parse_match(src, channel)
    opts = _SHORT.get(event) or ["\U0001F525 Yeh Moment Miss Mat Karna! {m}"]
    t = opts[_seed(seed or src) % len(opts)].format(m=_mtxt(p))
    t = t[:88].rsplit(" ", 1)[0] if len(t) > 88 else t
    return t + " #Shorts"


# ─────────────────────────── real facts only ───────────────────────────
_AMT = r"(?:₹|rs\.?|inr)\s*([\d][\d,]*(?:\.\d+)?)\s*(k|lakh|lac|lakhs|hazar|हज़ार|हजार|लाख)?|([\d][\d,]*)\s*(?:/-|rs\b|rupees|रुपये|रुपए)"
_PRIZE_KW = re.compile(r"(prize|inaam|inam|winner|winning|first|trophy|इनाम|विजेता|पुरस्कार)", re.I)
SRC_DESC = {}      # source title -> source description (filled by the runner from videos.list, 0 extra quota)


def _amount(m):
    num = (m.group(1) or m.group(3) or "").replace(",", "")
    try:
        v = float(num)
    except ValueError:
        return 0
    mult = (m.group(2) or "").lower()
    if mult in ("k", "hazar", "हज़ार", "हजार"):
        v *= 1000
    elif mult in ("lakh", "lac", "lakhs", "लाख"):
        v *= 100000
    return int(v)


def extract_facts(*texts):
    """Only what the source text really says. Today: winner prize money. Returns {} when nothing reliable is found -
    callers then simply show nothing (no guessing, no invented numbers)."""
    best = 0
    for line in re.split(r"[\n|]+", " \n ".join(t for t in texts if t)):
        if not _PRIZE_KW.search(line):
            continue
        for m in re.finditer(_AMT, line, re.I):
            v = _amount(m)
            if 1000 <= v <= 50000000:                       # ignore entry-fee sized / absurd numbers
                best = max(best, v)
    if not best:
        return {}
    n = str(best)                                           # Indian grouping: 1,50,000
    s = n if len(n) <= 3 else re.sub(r"(\d+?)(?=(\d\d)+(\d)$)", r"\1,", n)
    return {"prize": "\u20b9" + s}


def facts_for(src):
    return extract_facts(src, SRC_DESC.get(src, ""))


# ─────────────────────────── thumbnail banner ───────────────────────────
W, H = 1280, 720
_PALETTE = [((220, 38, 38), (127, 29, 29)), ((37, 99, 235), (30, 58, 138)), ((22, 163, 74), (20, 83, 45)),
            ((234, 88, 12), (124, 45, 18)), ((147, 51, 234), (88, 28, 135)), ((8, 145, 178), (22, 78, 99)),
            ((219, 39, 119), (131, 24, 67)), ((202, 138, 4), (113, 63, 18))]


def _font(size, bold=True):
    from PIL import ImageFont
    for p in (os.environ.get("YTM_FONT", ""), os.path.join(HERE, "fonts", "Poppins-Bold.ttf"),
              "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if p and os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _fit_font(draw, text, max_w, start, min_size=26):
    s = start
    while s > min_size and draw.textlength(text, font=_font(s)) > max_w:
        s -= 2
    return _font(s)


def _wrap2(draw, text, max_w, start):
    """one line if it fits at a decent size, else two balanced lines"""
    f = _fit_font(draw, text, max_w, start, int(start * 0.62))
    if draw.textlength(text, font=f) <= max_w:
        return [text], f
    words = text.split()
    best = None
    for i in range(1, len(words)):
        l1, l2 = " ".join(words[:i]), " ".join(words[i:])
        w = max(draw.textlength(l1, font=f), draw.textlength(l2, font=f))
        if best is None or w < best[0]:
            best = (w, l1, l2)
    if not best:
        return [text], _fit_font(draw, text, max_w, start)
    f = _fit_font(draw, max(best[1], best[2], key=len), max_w, start)
    return [best[1], best[2]], f


def _shadow_text(img, xy, text, font, fill, anchor="la", stroke=0, stroke_fill=(0, 0, 0), shadow=4):
    from PIL import ImageDraw, ImageFilter, Image
    lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    d.text((xy[0] + shadow, xy[1] + shadow), text, font=font, fill=(0, 0, 0, 200), anchor=anchor, stroke_width=stroke,
           stroke_fill=(0, 0, 0, 200))
    lay = lay.filter(ImageFilter.GaussianBlur(3))
    img.alpha_composite(lay)
    ImageDraw.Draw(img).text(xy, text, font=font, fill=fill, anchor=anchor, stroke_width=stroke, stroke_fill=stroke_fill)


def _team_colors(name):
    return _PALETTE[_seed(name.lower()) % len(_PALETTE)]


def _slant_box(draw, x0, y0, x1, y1, col, skew=34):
    draw.polygon([(x0 + skew, y0), (x1, y0), (x1 - skew, y1), (x0, y1)], fill=col)


def make_banner(frame, src, out, channel="", logo=None, badge="LIVE", strip="LIVE ON:"):
    """frame = path of a stream snapshot (or None -> plain dark gradient). Returns out path."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    p = parse_match(src, channel)
    base = None
    if frame and os.path.isfile(frame):
        try:
            base = Image.open(frame).convert("RGB")
            sc = max(W / base.width, H / base.height)
            base = base.resize((int(base.width * sc) + 1, int(base.height * sc) + 1), Image.LANCZOS)
            l, t = (base.width - W) // 2, (base.height - H) // 2
            base = base.crop((l, t, l + W, t + H))
            base = ImageEnhance.Brightness(ImageEnhance.Contrast(ImageEnhance.Color(base).enhance(1.55)).enhance(1.18)).enhance(1.08)
            base = base.filter(ImageFilter.GaussianBlur(0.8))
        except Exception:
            base = None
    if base is None:
        base = Image.new("RGB", (W, H), (12, 18, 40))
    img = base.convert("RGBA")
    # dark top->bottom gradient so text always reads, plus a vignette
    grad = Image.new("RGBA", (W, H))
    gd = ImageDraw.Draw(grad)
    for y in range(H):
        a = int(120 * (1 - y / H) ** 1.7 + 10 + 150 * (y / H) ** 2.6)
        gd.line([(0, y), (W, y)], fill=(6, 10, 28, min(225, a)))
    img.alpha_composite(grad)
    d = ImageDraw.Draw(img)

    (c1a, c1b), (c2a, c2b) = _team_colors(p["a"] or "A"), _team_colors((p["b"] or "B") + "x")
    if c1a == c2a:
        c2a, c2b = _PALETTE[(_PALETTE.index((c1a, c1b)) + 3) % len(_PALETTE)]

    # tournament (top)
    tour = (p["tour"] or ("%s Live" % p["sport"])).upper()
    lines, f = _wrap2(d, tour, W - 150, 104)
    y = 100
    for ln in lines:
        _shadow_text(img, (W // 2, y), ln, f, (255, 214, 10), anchor="ma", stroke=5, shadow=5)
        y += int(f.size * 1.08)
    # stage pill
    if p["stage"]:
        sf = _font(50)
        tw = d.textlength(p["stage"], font=sf)
        px0, px1 = W // 2 - tw / 2 - 52, W // 2 + tw / 2 + 52
        _slant_box(d, px0, y + 10, px1, y + 82, (255, 255, 255, 245), 26)
        d.text((W // 2, y + 46), p["stage"], font=sf, fill=(15, 23, 42), anchor="mm")
        y += 92

    # teams: two slanted ribbons with a VS badge in the middle
    ty0, ty1 = 452, 566
    if p["a"] and p["b"]:
        for side, name, (ca, cb) in ((0, p["a"], (c1a, c1b)), (1, p["b"], (c2a, c2b))):
            x0, x1 = (36, 640) if side == 0 else (640, W - 36)
            lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ld = ImageDraw.Draw(lay)
            _slant_box(ld, x0 - 6, ty0 + 8, x1 + 6, ty1 + 8, (0, 0, 0, 150), 40)
            img.alpha_composite(lay.filter(ImageFilter.GaussianBlur(5)))
            d = ImageDraw.Draw(img)
            _slant_box(d, x0, ty0, x1, ty1, ca, 40)
            _slant_box(d, x0, ty1 - 20, x1 - 6, ty1, cb, 40)
            nf = _fit_font(d, name.upper(), (x1 - x0) - 200, 80, 28)
            _shadow_text(img, ((x0 + x1) // 2 + (-34 if side == 0 else 34), (ty0 + ty1) // 2 - 6), name.upper(), nf, (255, 255, 255), anchor="mm", stroke=3, shadow=3)
            d = ImageDraw.Draw(img)
        d = ImageDraw.Draw(img)
        # VS badge
        cx, cy, r = W // 2, (ty0 + ty1) // 2, 70
        d.ellipse([cx - r - 8, cy - r - 8, cx + r + 8, cy + r + 8], fill=(255, 255, 255, 255))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(220, 38, 38, 255))
        _shadow_text(img, (cx, cy + 2), "VS", _font(78), (255, 255, 255), anchor="mm", stroke=2, shadow=3)
        d = ImageDraw.Draw(img)
    else:
        mf = _font(70)
        _shadow_text(img, (W // 2, (ty0 + ty1) // 2), "LIVE MATCH", mf, (255, 255, 255), anchor="mm", stroke=4)
        d = ImageDraw.Draw(img)

    pr = facts_for(src).get("prize")
    if pr:
        pf = _font(46)
        txt = "WINNER PRIZE  %s" % pr
        tw = d.textlength(txt, font=pf)
        bx0, bx1, by0, by1 = W // 2 - tw / 2 - 56, W // 2 + tw / 2 + 56, 372, 444
        _slant_box(d, bx0, by0, bx1, by1, (255, 214, 10, 255), 24)
        d.text((W // 2, (by0 + by1) // 2 + 2), txt, font=pf, fill=(120, 20, 20), anchor="mm")
    # bottom strip: LIVE ON + brand + logo
    sy0 = 600
    d.rectangle([0, sy0, W, H], fill=(8, 12, 30, 245))
    d.rectangle([0, sy0, W, sy0 + 6], fill=(255, 214, 10, 255))
    d.text((220, (sy0 + H) // 2 + 4), strip, font=_font(54), fill=(255, 255, 255), anchor="lm")
    d.text((220 + d.textlength(strip, font=_font(54)) + 22, (sy0 + H) // 2 + 4), BRAND.upper(), font=_font(66),
           fill=(255, 214, 10), anchor="lm")
    sf2 = _font(34)
    sw = d.textlength("SUBSCRIBE", font=sf2)
    d.rounded_rectangle([W - sw - 90, sy0 + 28, W - 28, H - 28], radius=12, fill=(220, 38, 38, 255))
    d.text((W - 28 - (sw + 62) / 2, (sy0 + H) // 2 + 4), "SUBSCRIBE", font=sf2, fill=(255, 255, 255), anchor="mm")
    lp = logo or os.environ.get("YTM_LOGO", os.path.join(HERE, "logo.png"))
    if os.path.isfile(lp):
        try:
            lg = Image.open(lp).convert("RGBA")
            sz = 150
            lg = lg.resize((sz, int(sz * lg.height / lg.width)), Image.LANCZOS)
            mask = Image.new("L", lg.size, 0)
            ImageDraw.Draw(mask).ellipse([0, 0, lg.width - 1, lg.height - 1], fill=255)
            ring = Image.new("RGBA", (lg.width + 12, lg.height + 12), (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse([0, 0, ring.width - 1, ring.height - 1], fill=(255, 255, 255, 255))
            img.alpha_composite(ring, (24, sy0 - 52))
            img.paste(lg, (30, sy0 - 46), mask)
        except Exception:
            pass
    d = ImageDraw.Draw(img)
    # LIVE badge (top-left)
    bw = d.textlength(badge, font=_font(36))
    d.rounded_rectangle([26, 20, 26 + bw + 90, 80], radius=14, fill=(220, 38, 38, 255))
    d.ellipse([42, 39, 62, 59], fill=(255, 255, 255, 255))
    d.text((78, 50), badge, font=_font(36), fill=(255, 255, 255), anchor="lm")

    rgb = img.convert("RGB")
    q = 90
    while True:                                              # YouTube limit: 2 MB
        rgb.save(out, "JPEG", quality=q, optimize=True)
        if os.path.getsize(out) < 1_900_000 or q <= 60:
            break
        q -= 10
    return out

