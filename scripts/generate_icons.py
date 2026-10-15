"""Render the integration icon, README header and GitHub social preview.

Original artwork (a radiator-thermostat dial), not the Danfoss logo.
Run from the repo root: python3 scripts/generate_icons.py (needs Pillow).
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "custom_components" / "danfoss_link" / "brand"
IMAGES = ROOT / "docs" / "images"

SS = 4  # supersampling factor
DISK = (30, 41, 59)  # slate
TRACK = (51, 65, 85)
STOPS = [(0.0, (56, 189, 248)), (0.5, (250, 204, 21)), (1.0, (239, 68, 68))]
WHITE = (248, 250, 252)
ARC_START, ARC_SWEEP = 135.0, 270.0  # degrees, PIL convention (0 = 3 o'clock, clockwise)
SETPOINT = 0.68  # position of the knob along the arc


def _mix(t: float) -> tuple[int, int, int]:
    for (t0, c0), (t1, c1) in zip(STOPS, STOPS[1:], strict=False):
        if t <= t1:
            f = (t - t0) / (t1 - t0)
            return tuple(round(a + (b - a) * f) for a, b in zip(c0, c1, strict=True))
    return STOPS[-1][1]


def _polar(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
    rad = math.radians(deg)
    return cx + r * math.cos(rad), cy + r * math.sin(rad)


def draw_icon(size: int) -> Image.Image:
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = s / 2
    pad = s * 0.02
    d.ellipse((pad, pad, s - pad, s - pad), fill=DISK)

    r = s * 0.36
    w = s * 0.075
    # PIL draws arc strokes inward from the box, so pad it to centre the stroke on r.
    ro = r + w / 2
    box = (c - ro, c - ro, c + ro, c + ro)

    def cap(deg: float, color: tuple[int, int, int]) -> None:
        x, y = _polar(c, c, r, deg)
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=color)

    # Background track, then the coloured part up to the setpoint.
    d.arc(box, ARC_START, ARC_START + ARC_SWEEP, fill=TRACK, width=round(w))
    cap(ARC_START + ARC_SWEEP, TRACK)
    steps = 240
    for i in range(steps):
        t0, t1 = i / steps, (i + 1.5) / steps
        if t0 > SETPOINT:
            break
        d.arc(
            box,
            ARC_START + ARC_SWEEP * t0,
            ARC_START + ARC_SWEEP * min(t1, SETPOINT),
            fill=_mix(t0),
            width=round(w),
        )
    cap(ARC_START, _mix(0.0))

    # Knob marking the setpoint.
    kx, ky = _polar(c, c, r, ARC_START + ARC_SWEEP * SETPOINT)
    kr = w * 0.85
    d.ellipse((kx - kr, ky - kr, kx + kr, ky + kr), fill=WHITE, outline=DISK, width=round(w * 0.25))

    # Thermometer glyph in the centre.
    bulb_r = s * 0.085
    bx, by = c, c + s * 0.10
    stem_w = s * 0.07
    stem_top = c - s * 0.17
    d.rounded_rectangle(
        (bx - stem_w / 2, stem_top, bx + stem_w / 2, by),
        radius=stem_w / 2,
        fill=WHITE,
    )
    d.ellipse((bx - bulb_r, by - bulb_r, bx + bulb_r, by + bulb_r), fill=WHITE)
    # Mercury.
    m = s * 0.022
    hot = _mix(SETPOINT)
    d.rounded_rectangle((bx - m, c - s * 0.06, bx + m, by), radius=m, fill=hot)
    mr = bulb_r * 0.62
    d.ellipse((bx - mr, by - mr, bx + mr, by + mr), fill=hot)

    return img.resize((size, size), Image.LANCZOS)


def draw_social(width: int = 1280, height: int = 640) -> Image.Image:
    img = Image.new("RGB", (width, height), (15, 23, 42))
    d = ImageDraw.Draw(img)
    icon = draw_icon(360)
    img.paste(icon, (100, (height - 360) // 2), icon)
    fonts = Path("/usr/share/fonts/opentype/inter")
    title = ImageFont.truetype(str(fonts / "Inter-Bold.otf"), 72)
    sub = ImageFont.truetype(str(fonts / "Inter-Regular.otf"), 30)
    x = 520
    d.text((x, 210), "Danfoss Link", font=title, fill=WHITE)
    d.text((x, 300), "for Home Assistant", font=title, fill=(148, 163, 184))
    d.text(
        (x, 410),
        "Unofficial HACS integration for Link CC thermostats",
        font=sub,
        fill=(148, 163, 184),
    )
    return img


def main() -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    IMAGES.mkdir(parents=True, exist_ok=True)
    draw_icon(256).save(BRAND / "icon.png", optimize=True)
    draw_icon(512).save(BRAND / "icon@2x.png", optimize=True)
    draw_icon(512).save(IMAGES / "icon.png", optimize=True)
    draw_social().save(IMAGES / "social-preview.png", optimize=True)


if __name__ == "__main__":
    main()
