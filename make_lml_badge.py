"""Renders lml_badge.png - the broadcast-style LML SPORTS watermark badge the relay puts over a
permitted source's centre-bottom watermark.   python3 make_lml_badge.py

Output is 2x the on-air size (660x76 for the default 330x38 box); ffmpeg scales it down, so edges stay crisp.
Fully opaque inside the pill (it has to hide what is under it); only the rounded corners are transparent."""
from PIL import Image, ImageDraw, ImageFont, ImageFilter

S = 4                       # supersampling for the draw, downsampled to OUT at the end
OUT_W, OUT_H = 660, 76
W, H = OUT_W * S, OUT_H * S
FONT = "fonts/Poppins-Bold.ttf"
NAVY_TOP, NAVY_BOT = (28, 42, 78), (6, 10, 22)
RED, GOLD, WHITE = (226, 24, 44), (255, 204, 51), (255, 255, 255)


def spaced(draw, xy, text, font, fill, spacing):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + spacing
    return x - spacing


def spaced_w(draw, text, font, spacing):
    return sum(draw.textlength(c, font=font) for c in text) + spacing * (len(text) - 1)


def main():
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    r = H // 2 - 2 * S                                   # corner radius (near full pill, a touch squarer = more modern)
    r = int(H * 0.30)

    # body: vertical navy gradient clipped to a rounded rectangle
    grad = Image.new("RGBA", (W, H))
    gp = grad.load()
    for y in range(H):
        t = y / (H - 1)
        c = tuple(int(NAVY_TOP[i] * (1 - t) + NAVY_BOT[i] * t) for i in range(3)) + (255,)
        for x in range(W):
            gp[x, y] = c
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), r, fill=255)
    img.paste(grad, (0, 0), mask)

    # gloss on the upper half
    gloss = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(gloss)
    gd.rounded_rectangle((S * 3, S * 3, W - S * 3, H // 2), r - S * 2, fill=(255, 255, 255, 26))
    img = Image.alpha_composite(img, Image.composite(gloss, Image.new("RGBA", (W, H), (0, 0, 0, 0)), mask))

    d = ImageDraw.Draw(img)
    # thin silver rim + red accent line along the bottom
    d.rounded_rectangle((0, 0, W - 1, H - 1), r, outline=(150, 160, 185, 255), width=S * 2)
    d.rounded_rectangle((S * 2, S * 2, W - 1 - S * 2, H - 1 - S * 2), r - S * 2, outline=(255, 255, 255, 40), width=S)

    f_main = ImageFont.truetype(FONT, 33 * S)
    f_sub = ImageFont.truetype(FONT, 14 * S)
    f_live = ImageFont.truetype(FONT, 19 * S)

    logo = Image.open("logo.png").convert("RGBA")
    lh = 54 * S
    lw = int(lh * logo.width / logo.height)
    logo = logo.resize((lw, lh), Image.LANCZOS)

    t_main, t_sub = "LML SPORTS", "LOCAL MATCH LIVE"
    w_main = spaced_w(d, t_main, f_main, 2 * S)
    w_sub = spaced_w(d, t_sub, f_sub, 3 * S)
    w_txt = max(w_main, w_sub)
    live_w, live_h = 92 * S, 36 * S
    gap, sep = 20 * S, 2 * S
    total = lw + gap + sep + gap + w_txt + gap + sep + gap + live_w
    x0 = (W - total) / 2

    # logo
    img.alpha_composite(logo, (int(x0), (H - lh) // 2))
    d = ImageDraw.Draw(img)
    x = x0 + lw + gap
    d.rectangle((x, 12 * S, x + sep, H - 12 * S), fill=(255, 255, 255, 70))
    x += sep + gap

    # two-line lockup, vertically centred as a block
    bm, bs = f_main.getbbox("LML SPORTS"), f_sub.getbbox("LOCAL MATCH LIVE")
    hm, hs = bm[3] - bm[1], bs[3] - bs[1]
    block = hm + 7 * S + hs
    ty = (H - block) / 2
    spaced(d, (x + (w_txt - w_main) / 2, ty - bm[1]), t_main, f_main, WHITE, 2 * S)
    spaced(d, (x + (w_txt - w_sub) / 2, ty + hm + 7 * S - bs[1]), t_sub, f_sub, GOLD, 3 * S)
    x += w_txt + gap
    d.rectangle((x, 12 * S, x + sep, H - 12 * S), fill=(255, 255, 255, 70))
    x += sep + gap

    # LIVE pill with a dot
    ly = (H - live_h) // 2
    d.rounded_rectangle((x, ly, x + live_w, ly + live_h), live_h // 2, fill=RED)
    dot = 7 * S
    d.ellipse((x + 12 * S, H // 2 - dot, x + 12 * S + 2 * dot, H // 2 + dot), fill=WHITE)
    bl = f_live.getbbox("LIVE")
    d.text((x + 12 * S + 2 * dot + 7 * S, H / 2 - (bl[1] + bl[3]) / 2), "LIVE", font=f_live, fill=WHITE)

    out = img.resize((OUT_W, OUT_H), Image.LANCZOS)
    out.save("lml_badge.png", optimize=True)
    print("lml_badge.png", out.size)


if __name__ == "__main__":
    main()
