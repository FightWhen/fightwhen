"""Calendar files: one event per fight, and a feed per sport people can subscribe to.

/fight/<page>/fight.ics   one fight, with a reminder an hour before (Apple, Outlook, phones)
/cal/<sport>.ics          every upcoming fight in that sport; subscribed calendars refresh
                          by themselves, so changed times reach people automatically
/cal/all.ics              every sport
Google Calendar gets a ready-made "add" link instead of a file.
"""
import datetime as dt
import urllib.parse

import pages as P

LENGTH = dt.timedelta(hours=4)          # cards run long; four hours covers a main card


def _esc(text):
    return str(text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line):
    """iCalendar lines must be at most 75 bytes; longer ones continue on a line starting with a space."""
    raw, out = line.encode(), []
    while len(raw) > 75:
        cut = 75 if not out else 74
        while cut and (raw[cut] & 0xC0) == 0x80:     # don't split a multi-byte character
            cut -= 1
        out.append(raw[:cut].decode())
        raw = raw[cut:]
    out.append(raw.decode())
    return "\r\n ".join(out)


def _when(f):
    if f.get("utc"):
        s = dt.datetime.fromisoformat(f["utc"].replace("Z", "+00:00"))
        return [f"DTSTART:{s:%Y%m%dT%H%M%SZ}", f"DTEND:{s + LENGTH:%Y%m%dT%H%M%SZ}"]
    d = dt.date.fromisoformat(f["d"])
    return [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + dt.timedelta(days=1):%Y%m%d}"]


def event(f, alarm=True):
    url = f"{P.BASE}/fight/{P.page_slug(f)}/"
    tv = f" · {f['w']}" if f.get("w") and f["w"] != "TBA" else ""
    note = (f"{f['f']}{tv}. Start time in your time zone, full card and where to watch: {url}"
            + ("" if f.get("utc") else " (start time not announced yet; this event updates if you subscribed)"))
    lines = ["BEGIN:VEVENT", f"UID:{f['id']}@fightwhen.com",
             f"DTSTAMP:{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}", *_when(f),
             f"SUMMARY:{_esc('🥊 ' + P.short_name(f) + tv)}", f"DESCRIPTION:{_esc(note)}", f"URL:{url}"]
    if f.get("v") and "TBA" not in f["v"]:
        lines.append(f"LOCATION:{_esc(f['v'])}")
    if alarm and f.get("utc"):
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_esc(P.short_name(f) + ' starts in 1 hour')}",
                  "TRIGGER:-PT1H", "END:VALARM"]
    return lines + ["END:VEVENT"]


def calendar(name, events):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//FightWhen//Fight times//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_esc(name)}", "X-WR-CALDESC:Fight times from fightwhen.com",
             "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    for ev in events:
        lines += ev
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"


def google_link(f):
    if f.get("utc"):
        s = dt.datetime.fromisoformat(f["utc"].replace("Z", "+00:00"))
        dates = f"{s:%Y%m%dT%H%M%SZ}/{s + LENGTH:%Y%m%dT%H%M%SZ}"
    else:
        d = dt.date.fromisoformat(f["d"])
        dates = f"{d:%Y%m%d}/{d + dt.timedelta(days=1):%Y%m%d}"
    q = {"action": "TEMPLATE", "text": "🥊 " + P.short_name(f), "dates": dates,
         "details": f"Start time in your time zone, full card and where to watch: {P.BASE}/fight/{P.page_slug(f)}/",
         "location": f.get("v") if f.get("v") and "TBA" not in f["v"] else ""}
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(q)


def subscribe_links(sport=None):
    """(apple/outlook webcal link, google link) for a sport feed or the all-sports feed."""
    path = f"fightwhen.com/cal/{sport or 'all'}.ics"
    return f"webcal://{path}", "https://calendar.google.com/calendar/r?cid=" + urllib.parse.quote(f"webcal://{path}", safe="")


def buttons(f):
    """HTML for the add-to-calendar buttons on a fight."""
    return (f'<div class="calbtns"><a class="cbtn" href="/fight/{P.e(P.page_slug(f))}/fight.ics">{CAL_ICON} Add to calendar</a>'
            f'<a class="cbtn" href="{P.e(google_link(f))}" target="_blank" rel="noopener">Google Calendar</a></div>')


def subscribe_box(sport, label):
    web, goog = subscribe_links(sport)
    return (f'<section class="box"><h2>Never miss a {P.e(label)} fight</h2>'
            f'<p class="muted">Subscribe once and every {P.e(label)} fight shows up in your calendar, with times that update by themselves.</p>'
            f'<div class="calbtns"><a class="cbtn" href="{web}">{CAL_ICON} iPhone / Mac / Outlook</a>'
            f'<a class="cbtn" href="{P.e(goog)}" target="_blank" rel="noopener">Google Calendar</a></div></section>')


CAL_ICON = ('<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">'
            '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/></svg>')

def build(fights):
    """Write every .ics file. Returns nothing; files sit beside the pages."""
    now = dt.datetime.now(dt.timezone.utc)
    keep = [f for f in fights if f.get("d") and dt.date.fromisoformat(f["d"]) >= now.date() - dt.timedelta(days=30)]
    for f in fights:
        if not f.get("d") or "/" in f["id"]:
            continue
        ics = calendar(P.short_name(f), [event(f)])
        for path in {P.page_slug(f), f["id"]}:
            d = P.SITE / "fight" / path
            d.mkdir(parents=True, exist_ok=True)
            (d / "fight.ics").write_text(ics)
    cal = P.SITE / "cal"
    cal.mkdir(exist_ok=True)
    (cal / "all.ics").write_text(calendar("FightWhen · All fights", [event(f, alarm=False) for f in keep]))
    for c in {f["c"] for f in fights}:
        name = P.CATS.get(c, c)
        (cal / f"{c}.ics").write_text(calendar(f"FightWhen · {name}", [event(f, alarm=False) for f in keep if f["c"] == c]))
