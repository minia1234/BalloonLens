"""Draw the extension icons (blue tile + speech balloon). Usage: python tools/make_icons.py"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "extension" / "icons"
BLUE = (37, 99, 235, 255)


def icon(size: int) -> Image.Image:
    s = size * 8  # supersample, then downscale for smooth edges
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.23), fill=BLUE)
    d.ellipse([s * 0.14, s * 0.16, s * 0.86, s * 0.70], fill="white")
    d.polygon([(s * 0.30, s * 0.58), (s * 0.20, s * 0.86), (s * 0.50, s * 0.66)], fill="white")
    if size >= 48:
        font = ImageFont.truetype("C:/Windows/Fonts/malgunbd.ttf", int(s * 0.34))
        d.text((s * 0.5, s * 0.43), "가", font=font, fill=BLUE, anchor="mm")
    else:
        r = s * 0.055
        for cx in (0.34, 0.5, 0.66):
            d.ellipse([s * cx - r, s * 0.43 - r, s * cx + r, s * 0.43 + r], fill=BLUE)
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        icon(size).save(OUT / f"{size}.png")
    print(f"icons -> {OUT}")
