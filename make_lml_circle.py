"""Renders lml_circle.png - our round, fully OPAQUE channel logo (navy disc + gold ring + the LML tiger wordmark).
Used as the top-right logo: opaque, so nothing from underneath shows through.   python3 make_lml_circle.py"""
from PIL import Image, ImageDraw

S = 4
OUT = 480                                   # on-air size is scaled down by ffmpeg
W = OUT * S
NAVY_TOP, NAVY_BOT, GOLD = (28, 42, 78), (6, 10, 22), (255, 204, 51)


def main():
    disc = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    px = disc.load()
    for y in range(W):
        t = y / (W - 1)
        c = tuple(int(NAVY_TOP[i] * (1 - t) + NAVY_BOT[i] * t) for i in range(3)) + (255,)
        for x in range(W):
            px[x, y] = c
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, W - 1, W - 1), fill=255)
    out = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    out.paste(disc, (0, 0), mask)
    d = ImageDraw.Draw(out)
    ring = 7 * S
    d.ellipse((ring // 2, ring // 2, W - 1 - ring // 2, W - 1 - ring // 2), outline=GOLD, width=ring)
    lg = Image.open("logo.png").convert("RGBA")
    tw = int(W * 0.80)
    lg = lg.resize((tw, int(lg.height * tw / lg.width)), Image.LANCZOS)
    out.alpha_composite(lg, ((W - lg.width) // 2, (W - lg.height) // 2))
    out.resize((OUT, OUT), Image.LANCZOS).save("lml_circle.png")


if __name__ == "__main__":
    main()
