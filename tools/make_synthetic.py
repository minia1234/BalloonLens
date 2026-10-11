"""Generate synthetic comic pages with known dialogue for smoke tests (not a quality benchmark).

Writes <out>/<name>.png and <out>/<name>.truth.json ({"src": lang, "reading": ..., "lines": [...]})
Usage: python tools/make_synthetic.py samples/synthetic
"""
import json
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS = Path("C:/Windows/Fonts")
JA_FONT = FONTS / "YuGothM.ttc"
EN_FONT = FONTS / "comicbd.ttf"

JA_LINES = ["まだ終わってない", "どこへ行くの？", "三時に駅で会おう", "それは違うよ", "もう一度やってみる", "静かにして！"]
EN_LINES = ["WE ARE NOT DONE YET!", "WHERE ARE YOU GOING?", "MEET ME AT THE STATION AT THREE.",
            "THAT'S NOT WHAT I MEANT.", "LET'S TRY ONE MORE TIME.", "BE QUIET!"]


def background(w, h, rng):
    img = Image.new("RGB", (w, h), (235, 235, 235))
    d = ImageDraw.Draw(img)
    for _ in range(40):  # panel-ish strokes so the detector sees non-text edges too
        x, y = rng.randrange(w), rng.randrange(h)
        d.line([x, y, x + rng.randint(-200, 200), y + rng.randint(-200, 200)], fill=(90, 90, 90), width=3)
    return img


def balloon(d, box):
    d.ellipse(box, fill="white", outline="black", width=4)


def draw_ja(d, text, cx, cy, size):
    font = ImageFont.truetype(str(JA_FONT), size)
    cols = [text[i:i + 6] for i in range(0, len(text), 6)]
    col_w = int(size * 1.3)
    total_w = col_w * len(cols)
    height = size * max(len(c) for c in cols)
    box = (cx - total_w / 2 - size * 1.2, cy - height / 2 - size * 1.2, cx + total_w / 2 + size * 1.2,
           cy + height / 2 + size * 1.2)
    balloon(d, box)
    x = cx + total_w / 2 - col_w  # first column is rightmost
    for col in cols:
        y = cy - height / 2
        for ch in col:
            d.text((x + (col_w - size) / 2, y), ch, font=font, fill="black")
            y += size
        x -= col_w


def draw_en(d, text, cx, cy, size):
    font = ImageFont.truetype(str(EN_FONT), size)
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) > 14 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    widths = [d.textlength(line, font=font) for line in lines]
    lh = int(size * 1.25)
    h = lh * len(lines)
    balloon(d, (cx - max(widths) / 2 - size * 1.5, cy - h / 2 - size * 1.3,
                cx + max(widths) / 2 + size * 1.5, cy + h / 2 + size * 1.3))
    for i, (line, lw) in enumerate(zip(lines, widths)):
        d.text((cx - lw / 2, cy - h / 2 + i * lh), line, font=font, fill="black")


def page(name, lang, out, rng, w=1200, h=1700, slots=None, size=36):
    img = background(w, h, rng)
    d = ImageDraw.Draw(img)
    pool = JA_LINES if lang == "ja" else EN_LINES
    lines = rng.sample(pool, len(slots))
    for text, (cx, cy) in zip(lines, slots):
        (draw_ja if lang == "ja" else draw_en)(d, text, cx, cy, size)
    img.save(out / f"{name}.png")
    reading = "rtl" if lang == "ja" else ("ttb" if h > 3 * w else "ltr")
    (out / f"{name}.truth.json").write_text(
        json.dumps({"src": lang, "reading": reading, "lines": lines}, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "samples/synthetic")
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(7)
    grid = [(900, 350), (320, 380), (880, 1100), (330, 1250)]
    page("ja_page", "ja", out, rng, slots=grid)
    page("ja_small", "ja", out, rng, slots=grid, size=22)
    page("en_page", "en", out, rng, slots=grid, size=40)
    # webtoon strip: 800x6000, one balloon sits on the first tile boundary (tile_h=1200, overlap=180)
    page("en_webtoon", "en", out, rng, w=800, h=6000, slots=[(400, 500), (400, 1110), (400, 2600), (400, 4700)],
         size=34)
    print(f"wrote samples to {out}")


if __name__ == "__main__":
    main()
