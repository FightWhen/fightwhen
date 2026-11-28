"""Share images (1200x630) for each fight page: what people see when a link is posted.

Uses the site's own fonts (Anton, IBM Plex Mono), which the publish workflow downloads
into agent/fonts. If they're missing it falls back to any bold font, and if Pillow is
missing it makes nothing and pages keep the general share image.
"""
import datetime as dt
import math
import pathlib
import re
from zoneinfo import ZoneInfo

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:          # pages still build without share images
    Image = None

FONTS = pathlib.Path(__file__).resolve().parent / "fonts"
BG, CARD, INK, SOFT, RED, GOLD = "#0F0E0C", "#1A1815", "#F2EBDD", "#A39A86", "#E03A3E", "#C9A24A"
W, H = 1200, 630
FALLBACK = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]


def font(name, size):
    for path in [FONTS / name, *FALLBACK]:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


def fit(draw, text, name, start, max_w, min_size=40):
    size = start
    while size > min_size:
        f = font(name, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return font(name, min_size)


def sides(f):
    main = next((s["b"][0] for s in f.get("card", []) if s.get("b")), None)
    if main:
        return main[0], main[2]
    part = max(f["f"].split(":"), key=lambda p: " vs" in p.lower())
    bits = re.split(r"\s+vs\.?\s+", part.strip(), flags=re.I)
    return (bits[0], bits[1]) if len(bits) == 2 else (f["f"], "")


def last(name):
    words = [w for w in name.split() if w.lower().strip(".") not in ("jr", "sr", "ii", "iii")]
    return (words[-1] if words else name).upper()


def clock(d, cx, cy, r):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=GOLD, width=5)
    for i in range(12):
        a = math.radians(i * 30)
        inner = r - (14 if i % 3 == 0 else 9)
        d.line([cx + inner * math.sin(a), cy - inner * math.cos(a),
                cx + (r - 4) * math.sin(a), cy - (r - 4) * math.cos(a)], fill=GOLD, width=4 if i % 3 == 0 else 2)
    d.ellipse([cx - 6, cy - r + 16, cx + 6, cy - r + 28], fill=RED)


def make(f, sport, out_path):
    """Draw the share image for fight `f` to out_path. Returns True on success."""
    if Image is None:
        return False
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 10], fill=RED)

    # brand
    clock(d, 92, 92, 38)
    brand = font("Anton-Regular.ttf", 44)
    d.text((148, 66), "FIGHT", font=brand, fill=INK)
    d.text((148 + d.textlength("FIGHT", font=brand), 66), "WHEN", font=brand, fill=RED)
    mono = font("IBMPlexMono-SemiBold.ttf", 24)
    d.text((W - 60, 80), sport.upper(), font=mono, fill=GOLD, anchor="ra")

    # names
    a, b = sides(f)
    if b:
        a, b = last(a), last(b)
        name_f = fit(d, max(a, b, key=len), "Anton-Regular.ttf", 132, W - 220)
        top, bottom = 150, H - 160                   # space between the brand row and the time bar
        h = d.textbbox((0, 0), "KAH", font=name_f)[3] - d.textbbox((0, 0), "KAH", font=name_f)[1]
        gap = (bottom - top - 2 * h) / 3
        off = d.textbbox((0, 0), "KAH", font=name_f)[1]
        ya, yb = top + gap - off, top + 2 * gap + h - off
        d.text((60, ya), a, font=name_f, fill=INK)
        vs = font("Anton-Regular.ttf", 40)
        d.text((60, top + 1.5 * gap + h), "VS", font=vs, fill=RED, anchor="lm")
        d.text((130, yb), b, font=name_f, fill=INK)
    else:
        d.text((60, 190), f["f"].upper(), font=fit(d, f["f"].upper(), "Anton-Regular.ttf", 110, W - 120), fill=INK)

    # when
    if f.get("utc"):
        t = dt.datetime.fromisoformat(f["utc"].replace("Z", "+00:00"))
        et, pt = t.astimezone(ZoneInfo("America/New_York")), t.astimezone(ZoneInfo("America/Los_Angeles"))
        hm = lambda x: x.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
        when = f"{et.strftime('%a %b')} {et.day} · {hm(et)} ET / {hm(pt)} PT"
    else:
        day = dt.date.fromisoformat(f["d"])
        when = f"{day.strftime('%a %b')} {day.day} · time TBA"
    d.rounded_rectangle([60, H - 130, W - 60, H - 54], radius=12, fill=CARD, outline="#3A3323", width=2)
    d.text((88, H - 92), when, font=fit(d, when, "IBMPlexMono-SemiBold.ttf", 34, W - 420, 22), fill=INK, anchor="lm")
    d.text((W - 88, H - 92), "fightwhen.com", font=font("IBMPlexMono-SemiBold.ttf", 26), fill=GOLD, anchor="rm")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return True
