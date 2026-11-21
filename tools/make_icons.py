#!/usr/bin/env python3
"""Draw the Arrgus emblem (the compass-ring eye from the dashboard) into PNG app icons, using only the
standard library. Run from the repo root: python3 tools/make_icons.py  (writes web/app/*.png)."""
import math
import struct
import sys
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "web" / "app"


def hexrgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


GOLD, TEAL, EYE_BG = hexrgb("#d9b45a"), hexrgb("#2fb8a8"), hexrgb("#071a20")
PUPIL, SHINE = hexrgb("#06101f"), hexrgb("#fff6dc")
IRIS = [(0.0, hexrgb("#3fd6c3")), (0.55, hexrgb("#2a8fd0")), (1.0, hexrgb("#1a3f9a"))]
BG_IN, BG_OUT = hexrgb("#0e3440"), hexrgb("#02070b")


def lerp(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def ramp(stops, t):
    t = max(0.0, min(1.0, t))
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            return lerp(c0, c1, (t - t0) / (t1 - t0) if t1 > t0 else 0)
    return stops[-1][1]


# Upper lid of the eye: the two cubic curves of the dashboard's SVG path, sampled into a lookup table.
def _cubic(p0, p1, p2, p3, n=200):
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        yield tuple(u ** 3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t ** 3 * d for a, b, c, d in zip(p0, p1, p2, p3))


LID = sorted(list(_cubic((13, 32), (18.2, 24), (24.6, 20), (32, 20))) + list(_cubic((32, 20), (39.4, 20), (45.8, 24), (51, 32))))


def lid_h(x):
    """Half-height of the almond eye at x (0 outside), and the slope there."""
    if x <= 13 or x >= 51:
        return 0.0, 0.0
    lo, hi = 0, len(LID) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if LID[mid][0] <= x:
            lo = mid
        else:
            hi = mid
    (x0, y0), (x1, y1) = LID[lo], LID[hi]
    t = (x - x0) / (x1 - x0) if x1 > x0 else 0
    y = y0 + (y1 - y0) * t
    slope = (y1 - y0) / (x1 - x0) if x1 > x0 else 0
    return 32 - y, slope


def over(dst, src, a):
    return lerp(dst, src, a)


def emblem_px(x, y, base):
    """Colour at emblem coordinates (0..64) painted over `base`."""
    c = base
    dx, dy = x - 32, y - 32
    r = math.hypot(dx, dy)
    if abs(r - 29) <= 0.75:                                   # outer ring
        c = over(c, GOLD, 0.6)
    if abs(r - 24.5) <= 0.5:                                  # dotted inner ring
        s = (math.atan2(dy, dx) % (2 * math.pi)) * 24.5
        if s % 4.7 < 1.5:
            c = over(c, TEAL, 0.5)
    for ax, ay in ((dx, dy), (dy, -dx), (-dx, -dy), (-dy, dx)):   # compass ticks, 4 directions
        if abs(ax) <= 0.8 and -30.5 - 0.8 <= ay <= -24.5 + 0.8 and math.hypot(ax, max(0, abs(ay + 27.5) - 3)) <= 0.8:
            c = over(c, GOLD, 1.0)
    for sgn in (1, -1):                                       # north / south pointers
        ty = sgn * dy                                         # distance from centre towards the pole
        tip, base_y = -28.5, -22.2
        if tip <= ty <= base_y and abs(dx) <= 2.2 * (ty - tip) / (base_y - tip):
            c = over(c, GOLD, 0.9)
    h, slope = lid_h(x)
    if h > 0:
        d = abs(dy) - h
        dist = d / math.sqrt(1 + slope * slope)
        if d < 0:
            c = over(c, EYE_BG, 1.0)                          # inside the eye
            if r <= 8.4:
                c = over(c, GOLD, 1.0)
            if r <= 7:
                gx, gy = x - 31.3, y - 30.6
                c = ramp(IRIS, math.hypot(gx, gy) / 8.68)
            if r <= 3.1:
                c = PUPIL
            if math.hypot(x - 34.4, y - 29.6) <= 1.4:
                c = SHINE
        if abs(dist) <= 1.0:                                  # gold outline of the lids
            c = over(c, GOLD, 1.0)
    return c


def render(size, emblem_frac, ss=3):
    """emblem_frac: how much of the icon's width the 64-unit emblem spans."""
    k = size * emblem_frac / 64                               # pixels per emblem unit
    off = (size - 64 * k) / 2
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            acc = [0.0, 0.0, 0.0]
            for sy in range(ss):
                for sx in range(ss):
                    fx, fy = px + (sx + 0.5) / ss, py + (sy + 0.5) / ss
                    rr = math.hypot(fx - size / 2, fy - size / 2) / (size * 0.72)
                    base = lerp(BG_IN, BG_OUT, min(1.0, rr))
                    ex, ey = (fx - off) / k, (fy - off) / k
                    col = emblem_px(ex, ey, base) if -2 <= ex <= 66 and -2 <= ey <= 66 else base
                    for i in range(3):
                        acc[i] += col[i]
            n = ss * ss
            row += bytes(int(max(0, min(1, v / n)) * 255 + 0.5) for v in acc)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


ICONS = [  # file, size, emblem width as a fraction of the icon
    ("icon-512.png", 512, 0.86),
    ("icon-192.png", 192, 0.86),
    ("maskable-512.png", 512, 0.70),     # Android crops these to a circle/squircle: keep inside the safe zone
    ("apple-touch-icon.png", 180, 0.80),  # iOS rounds the corners itself
    ("favicon-64.png", 64, 0.96),
]

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[1:])
    for name, size, frac in ICONS:
        if only and name not in only:
            continue
        (OUT / name).write_bytes(render(size, frac))
        print("wrote", OUT / name)
