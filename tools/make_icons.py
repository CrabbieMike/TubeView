#!/usr/bin/env python3
"""Draws TubeView's icons for every platform from one design. Needs Pillow.

Usage: python tools/make_icons.py   (writes into packaging/icons and android/app/src/main/res)
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
BG, BG2, ACCENT = (11, 11, 15), (32, 32, 46), (232, 232, 255)


def icon(size, round_=False):
    s = size * 4  # draw large, then downscale for smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # vertical gradient background
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        k = y / s
        gd.line([(0, y), (s, y)], fill=tuple(int(BG2[i] * (1 - k) + BG[i] * k) for i in range(3)) + (255,))
    mask = Image.new("L", (s, s), 0)
    md = ImageDraw.Draw(mask)
    if round_:
        md.ellipse([0, 0, s - 1, s - 1], fill=255)
    else:
        md.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)
    # "screen" outline with a play triangle
    m = s * 0.2
    d.rounded_rectangle([m, s * 0.27, s - m, s * 0.73], radius=int(s * 0.07), outline=ACCENT, width=int(s * 0.045))
    cx, cy, r = s * 0.5, s * 0.5, s * 0.11
    d.polygon([(cx - r * 0.75, cy - r), (cx - r * 0.75, cy + r), (cx + r * 1.05, cy)], fill=ACCENT)
    return img.resize((size, size), Image.LANCZOS)


def banner(w=320, h=180):
    """Android TV home-screen banner."""
    img = Image.new("RGBA", (w, h), BG + (255,))
    img.alpha_composite(icon(96), (24, (h - 96) // 2))
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=40)
    d.text((136, h // 2), "Tube", font=font, fill=(244, 244, 247), anchor="lm")
    tw = d.textlength("Tube", font=font)
    d.text((136 + tw, h // 2), "View", font=font, fill=(154, 154, 171), anchor="lm")
    return img


def main():
    out = ROOT / "packaging" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    icon(512).save(out / "tubeview.png")
    icon(256).save(out / "tubeview.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    res = ROOT / "android" / "app" / "src" / "main" / "res"
    for name, px in {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}.items():
        (res / f"mipmap-{name}").mkdir(parents=True, exist_ok=True)
        icon(px).save(res / f"mipmap-{name}" / "ic_launcher.png")
        icon(px, round_=True).save(res / f"mipmap-{name}" / "ic_launcher_round.png")
    (res / "drawable-xhdpi").mkdir(parents=True, exist_ok=True)
    banner().save(res / "drawable-xhdpi" / "banner.png")
    print("icons written")


if __name__ == "__main__":
    main()
