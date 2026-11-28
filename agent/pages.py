"""Builds one page per fight, plus sitemap.xml and robots.txt.

Each page answers "What time is X vs Y?" for search engines: the start time in US
time zones (the visitor's own browser swaps in their local time), the full card,
where to watch, bars showing it, and news about it. It reads only site/data, needs
no keys and no extra packages, and runs on every deploy, so the pages always match
the schedule.
"""
import datetime as dt
import os
import html
import json
import pathlib
import re
import shutil
import sys
import unicodedata
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import cards  # noqa: E402

SITE = pathlib.Path(__file__).resolve().parent.parent / "site"
DATA = SITE / "data"
BASE = "https://fightwhen.com"
SPECIAL = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "ß": "ss", "æ": "ae"})
# Email shown to bars that want to be listed. Leave empty to hide the line.
CONTACT = "hello@fightwhen.com"

CATS = {"boxing": "Boxing", "ufc": "UFC", "pfl": "PFL", "one": "ONE", "bkfc": "Bare knuckle",
        "misfits": "Misfits", "k1": "Kickboxing", "rizin": "RIZIN"}
ZONES = [("ET", "America/New_York"), ("CT", "America/Chicago"), ("MT", "America/Denver"),
         ("PT", "America/Los_Angeles"), ("UK", "Europe/London")]
TITLE_RE = re.compile(r"\b(WBA|WBC|IBF|WBO|BMF|title|unification|championship|belt)\b", re.I)


def e(s):
    return html.escape(str(s or ""), quote=True)


def load(name, default):
    try:
        return json.loads((DATA / name).read_text())
    except (OSError, ValueError):
        return default


def when(utc, zone, with_day=True):
    t = dt.datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(ZoneInfo(zone))
    clock = t.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    return f"{t.strftime('%a %b')} {t.day}, {clock}" if with_day else clock


def page_slug(f):
    """The fight's page address. Old short ids (like "fj") get a readable one."""
    if re.match(r"^\d{4}-\d{2}-\d{2}-", f["id"]):
        return f["id"]
    plain = unicodedata.normalize("NFKD", short_name(f).split(":")[-1].translate(SPECIAL))
    words = re.sub(r"[^a-z0-9]+", "-", plain.encode("ascii", "ignore").decode().lower()).strip("-")
    return f"{f['d']}-{words}"[:80]


def nice_date(d):
    day = dt.date.fromisoformat(d)
    return f"{day.strftime('%A, %B')} {day.day}, {day.year}"


def short_name(f):
    """'UFC 335: Charles Oliveira vs Diego Lopes' -> 'UFC 335: Oliveira vs Lopes'."""
    name = f["f"]
    m = re.match(r"^(.*?:\s*)?(.+?)\s+vs\.?\s+(.+)$", name)
    if not m:
        return name
    pre, a, b = m.group(1) or "", m.group(2), m.group(3)
    last = lambda n: n.split()[-1] if len(n.split()) > 1 else n
    return f"{pre}{last(a)} vs {last(b)}"


def watch_html(f):
    rows = f.get("watch") or ([{"r": "", "n": f["w"], "p": f.get("wp", "")}] if f.get("w") and f["w"] != "TBA" else [])
    if not rows:
        return ('<p class="muted">Broadcaster not announced yet. We add it as soon as two sources '
                'confirm it.</p>')
    out = []
    for r in rows:
        sub = " · ".join(x for x in (r.get("r") and {"US": "United States", "UK": "United Kingdom"}.get(r["r"], r["r"]),
                                     r.get("p")) if x)
        out.append(f'<div class="wrow"><span class="wico" aria-hidden="true">{TV}</span><div><b>{e(r["n"])}</b>'
                   f'{f"<small>{e(sub)}</small>" if sub else ""}</div></div>')
    return "".join(out)


def bars_for(f, bars):
    ppv = "ppv" in (f.get("w", "") + " " + f.get("wp", "")).lower() or f.get("big")
    return [b for b in bars if f["id"] in b.get("fights", []) or (ppv and b.get("ppv"))]


def bars_html(f, bars):
    mine = bars_for(f, bars)
    rows = "".join(
        f'<li><div><b>{e(b["name"])}</b><small>{e(" · ".join(x for x in (b.get("town"), b.get("note")) if x))}</small></div>'
        + (f'<a href="{e(b["map"])}" target="_blank" rel="noopener">Map</a>' if b.get("map") else "")
        + "</li>" for b in mine)
    ask = (f'<p class="muted">Own a bar showing this fight? <a href="mailto:{e(CONTACT)}?subject='
           f'{e("List my bar for " + short_name(f))}">Get listed</a>.</p>') if CONTACT else ""
    if not rows and not ask:
        return ""
    body = f'<ul class="bars">{rows}</ul>' if rows else '<p class="muted">No bars listed for this one yet.</p>'
    return f'<section class="box" id="bars"><h2>Watch it at a bar</h2>{body}{ask}</section>'


def card_html(f):
    secs = f.get("card") or []
    if not secs:
        return ""
    out = []
    for si, s in enumerate(secs):
        rows = []
        for bi, b in enumerate(s.get("b", [])):
            a, ra, bb, rb, wt = (list(b) + ["", "", "", "", ""])[:5]
            ex = b[5] if len(b) > 5 and isinstance(b[5], dict) else {}
            lab = "Main event" if si == 0 and bi == 0 else "Co-main" if si == 0 and bi == 1 else ""
            res = ex.get("res")
            result = ""
            if res:
                who = {"a": a, "b": bb}.get(res.get("w"))
                result = ("Draw" if res.get("w") == "d" else "No contest" if res.get("w") == "nc"
                          else f"{who.split()[-1]} by {res.get('m', '')}") + (f", round {res['r']}" if res.get("r") else "")
            rows.append(
                f'<li>{f"<span class=lab>{lab}</span>" if lab else ""}'
                f'{"<span class=belt>Title</span>" if TITLE_RE.search(wt or "") else ""}'
                f'<p class="bout"><b>{e(a)}</b>{f" <em>{e(ra)}</em>" if ra else ""} <span class="vs">vs</span> '
                f'<b>{e(bb)}</b>{f" <em>{e(rb)}</em>" if rb else ""}</p>'
                f'{f"<small>{e(wt)}</small>" if wt else ""}{f"<small class=res>{e(result)}</small>" if result else ""}</li>')
        if not rows:
            rows.append('<li><small>Bouts to be announced</small></li>')
        t = f' <span class="t" data-utc="{e(s["t"])}">{e(when(s["t"], "America/New_York", False))} ET</span>' if s.get("t") else ""
        out.append(f'<h3>{e(s.get("sec", "Card"))}{t}</h3><ul class="card">{"".join(rows)}</ul>')
    note = f'<p class="muted">{e(f["cn"])}</p>' if f.get("cn") else ""
    return f'<section><h2>Full card</h2>{"".join(out)}{note}</section>'


def page(f, stories, bars, others, image=None):
    name = short_name(f)
    sport = CATS.get(f["c"], f["c"].upper())
    date_txt = nice_date(f["d"])
    utc = f.get("utc")
    if utc:
        times = "".join(f'<li><span>{z}</span><b>{e(when(utc, tz, False))}</b></li>' for z, tz in ZONES)
        big_time = (f'<p class="big" data-utc="{e(utc)}" data-fmt="time">{e(when(utc, "America/New_York", False))} ET</p>'
                    f'<p class="lab2" data-utc="{e(utc)}" data-fmt="day">{e(when(utc, "America/New_York"))} ET</p>'
                    f'<p class="lab2">{e(f.get("lab") or "Start")}</p>')
        answer = (f"{f['f']} starts at {when(utc, 'America/New_York', False)} ET / "
                  f"{when(utc, 'America/Los_Angeles', False)} PT on {date_txt}")
        zones = f'<ul class="zones">{times}</ul>'
        cd = f'<p class="cd" data-cd="{e(utc)}"></p>'
    else:
        big_time = f'<p class="big">Time TBA</p><p class="lab2">{e(date_txt)}</p>'
        answer = f"{f['f']} is on {date_txt}. The start time has not been announced yet"
        zones = '<p class="muted">We add the start time as soon as two sources confirm it.</p>'
        cd = ""
    watch_plain = f.get("w") if f.get("w") and f["w"] != "TBA" else ""
    desc = f"{answer}{', live on ' + watch_plain if watch_plain else ''}. Full card and where to watch, in your time zone."
    title = f"What time is {name}? Start time, full card & where to watch | FightWhen"
    url = f"{BASE}/fight/{page_slug(f)}/"
    news = [s for s in stories if s.get("fid") == f["id"]]
    news_html = ("<section><h2>In the news</h2><ul class=news>" + "".join(
        f'<li><a href="/news/{e(s["id"])}/">{e(s["h"])}</a></li>' for s in news) + "</ul></section>") if news else ""
    more = "".join(f'<li><a href="/fight/{e(page_slug(o))}/">{e(short_name(o))}</a><small>{e(nice_date(o["d"]))}</small></li>'
                   for o in others)
    ld = {"@context": "https://schema.org", "@type": "SportsEvent", "name": f["f"], "url": url,
          "startDate": utc or f["d"], "sport": sport, "description": desc,
          "eventStatus": "https://schema.org/EventScheduled",
          "eventAttendanceMode": "https://schema.org/MixedEventAttendanceMode",
          "location": {"@type": "Place", "name": f.get("v") or "TBA", "address": f.get("v") or "TBA"}}
    faq = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": f"What time is {name}?",
         "acceptedAnswer": {"@type": "Answer", "text": answer + "."}}] + ([
        {"@type": "Question", "name": f"Where can I watch {name}?",
         "acceptedAnswer": {"@type": "Answer", "text": f"{f['f']} is on {watch_plain}{' (' + f['wp'] + ')' if f.get('wp') else ''}."}}]
        if watch_plain else [])}
    title_tag = '<span class="belt">Title fight</span>' if TITLE_RE.search((f.get("t") or "") + " " + (f.get("u") or "")) else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<link rel="canonical" href="{e(url)}">
<meta name="theme-color" content="#0F0E0C">
<meta property="og:type" content="website"><meta property="og:site_name" content="FightWhen">
<meta property="og:url" content="{e(url)}"><meta property="og:title" content="{e('What time is ' + name + '?')}">
<meta property="og:description" content="{e(desc)}"><meta property="og:image" content="{image or BASE + '/og.png'}"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@600&display=swap" rel="stylesheet">
<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>
<script type="application/ld+json">{json.dumps(faq, ensure_ascii=False)}</script>
<style>{CSS}</style>
</head>
<body>
<nav><div class="wrap"><a class="logo" href="/">{LOGO}<span>Fight<b>When</b></span></a><a class="all" href="/#fight-{e(f['id'])}">All fights</a></div></nav>
<main class="wrap">
<p class="kick">{e(sport)}{' · ' + e(f['t']) if f.get('t') else ''}</p>
<h1>What time is {e(name)}?</h1>
<p class="dek">{e(answer)}.</p>
<div class="ticket"><div class="in">{title_tag}{big_time}{cd}</div></div>
<p class="where">{e(f.get('v') or 'Venue TBA')}</p>
<section class="box" id="watch"><h2>Where to watch</h2>{watch_html(f)}
{'<a class="btn" href="#bars">Watch it at a bar near you</a>' if bars_for(f, bars) else ''}</section>
{card_html(f)}
{bars_html(f, bars)}
{news_html}
<section><h2>More fights</h2><ul class="more">{more}</ul></section>
<p class="muted foot">Times are shown in your own time zone once the page loads. Ring walks are estimates and move with the undercard. Every detail is confirmed by two sources before it goes live. <a href="/">FightWhen</a> · When’s the fight?</p>
</main>
<script>{JS}</script>
</body>
</html>
"""


def news_page(n, fight, more):
    """A real, crawlable page for one news story (the home page shows them in place)."""
    url = f"{BASE}/news/{n['id']}/"
    sport = CATS.get(n.get("c"), (n.get("c") or "").upper())
    when = (n.get("published") or "")[:10]
    pub = f"{nice_date(when)}" if re.match(r"\d{4}-\d{2}-\d{2}$", when) else ""
    img = f"https://i.ytimg.com/vi/{n['vid']}/hqdefault.jpg" if n.get("vid") else f"{BASE}/og.png"
    quote = (f'<blockquote>“{e(n["q"])}”<cite>{e(n.get("who"))}{" · " + e(n["ctx"]) if n.get("ctx") else ""}</cite></blockquote>'
             if n.get("q") else "")
    body = "".join(f"<p>{e(p)}</p>" for p in n.get("body", []))
    fight_link = (f'<section class="box"><h2>The fight</h2><p><a href="/fight/{e(page_slug(fight))}/">What time is '
                  f'{e(short_name(fight))}? Start time, full card and where to watch</a></p></section>') if fight else ""
    others = "".join(f'<li><a href="/news/{e(o["id"])}/">{e(o["h"])}</a></li>' for o in more)
    ld = {"@context": "https://schema.org", "@type": "NewsArticle", "headline": n["h"][:110], "description": n.get("sum", ""),
          "image": [img], "datePublished": n.get("published"), "mainEntityOfPage": url,
          "author": {"@type": "Organization", "name": "FightWhen", "url": BASE},
          "publisher": {"@type": "Organization", "name": "FightWhen", "logo": {"@type": "ImageObject", "url": f"{BASE}/apple-touch-icon.png"}},
          "isBasedOn": n.get("clip") or n.get("url")}
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(n["h"])} | FightWhen</title>
<meta name="description" content="{e(n.get("sum", ""))}">
<link rel="canonical" href="{e(url)}">
<meta name="theme-color" content="#0F0E0C">
<meta property="og:type" content="article"><meta property="og:site_name" content="FightWhen"><meta property="og:url" content="{e(url)}">
<meta property="og:title" content="{e(n["h"])}"><meta property="og:description" content="{e(n.get("sum", ""))}"><meta property="og:image" content="{e(img)}">
<meta name="twitter:card" content="summary_large_image">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@600&display=swap" rel="stylesheet">
<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>
<style>{CSS}
.art p{{color:#D8D0C0;font-size:17px;line-height:1.65}}
blockquote{{margin:22px 0;padding:6px 0 6px 18px;border-left:4px solid var(--red);font-size:20px;line-height:1.45}}
blockquote cite{{display:block;font:500 14px Inter;color:var(--soft);margin-top:8px;font-style:normal}}
.thumb{{display:block;margin:20px 0 0;border-radius:10px;overflow:hidden;aspect-ratio:16/9;background:var(--card)}}
.thumb img{{width:100%;height:100%;object-fit:cover;display:block}}
.src{{font-size:14px;color:var(--soft)}}</style>
</head>
<body>
<nav><div class="wrap"><a class="logo" href="/">{LOGO}<span>Fight<b>When</b></span></a><a class="all" href="/#story-{e(n["id"])}">All news</a></div></nav>
<main class="wrap art">
<p class="kick">{e(sport)}{" · " + e(n["mood"]) if n.get("mood") else ""}</p>
<h1>{e(n["h"])}</h1>
<p class="dek">{e(n.get("sum", ""))}</p>
<p class="src">{e(pub)}{" · " if pub else ""}From <a href="{e(n.get("clip") or n.get("url"))}" target="_blank" rel="noopener">{e(n.get("src"))}</a></p>
{f'<a class="thumb" href="{e(n.get("url"))}" target="_blank" rel="noopener"><img src="{e(img)}" alt="" loading="lazy" width="480" height="270"></a>' if n.get("vid") else ""}
{quote}
{body}
<p class="src">Written by FightWhen from <a href="{e(n.get("url"))}" target="_blank" rel="noopener">{e(n.get("src"))}’s video</a>. Watch the full interview there.</p>
{fight_link}
{f'<section><h2>More fight news</h2><ul class="news">{others}</ul></section>' if others else ""}
<p class="muted foot"><a href="/">FightWhen</a> · When’s the fight? · <a href="/about/">About &amp; contact</a></p>
</main>
</body>
</html>
"""


def prerender_home(fights, stories, today):
    """Put plain, crawlable versions of the schedule and news into the home page.
    The page's script replaces them with the full design as soon as it runs."""
    home = SITE / "index.html"
    html = home.read_text()
    up = sorted((f for f in fights if f.get("d", "") >= today), key=lambda f: f["d"])[:40]
    rows = "".join(
        f'<li class="pre"><a href="/fight/{e(page_slug(f))}/"><b>{e(f["f"])}</b> · {e(nice_date(f["d"]))}'
        f'{" · " + e(when(f["utc"], "America/New_York", False)) + " ET" if f.get("utc") else ""}'
        f'{" · " + e(f["w"]) if f.get("w") and f["w"] != "TBA" else ""}</a></li>' for f in up)
    news = "".join(f'<p class="pre"><a href="/news/{e(n["id"])}/"><b>{e(n["h"])}</b></a> {e(n.get("sum", ""))}</p>'
                   for n in stories[:30])
    html = re.sub(r'<ul class="rows" id="rows">.*?</ul>', f'<ul class="rows" id="rows">{rows}</ul>', html, count=1, flags=re.S)
    html = re.sub(r'<div class="quotes" id="quotes">.*?</div>', f'<div class="quotes" id="quotes">{news}</div>', html, count=1, flags=re.S)
    html = re.sub(r"<title>.*?</title>", "<title>FightWhen · Boxing &amp; UFC Fight Times in Your Time Zone</title>", html, count=1)
    if 'rel="canonical"' not in html:
        ld = {"@context": "https://schema.org", "@graph": [
            {"@type": "WebSite", "name": "FightWhen", "url": BASE + "/"},
            {"@type": "Organization", "name": "FightWhen", "url": BASE + "/", "logo": BASE + "/apple-touch-icon.png",
             "email": CONTACT}]}
        html = html.replace("<title>", f'<link rel="canonical" href="{BASE}/">\n'
                            f'<script type="application/ld+json">{json.dumps(ld)}</script>\n<title>', 1)
    if ".pre{" not in html:
        html = html.replace("</style>", ".pre{list-style:none;padding:10px 0;border-bottom:1px solid #2A261D;color:#A39A86;font-size:15px}"
                            ".pre a{color:#F2EBDD;text-decoration:none}</style>", 1)
    home.write_text(html)


TV = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="5" width="20" height="13" rx="2"/><path d="M8 21h8"/></svg>'
LOGO = ('<svg width="34" height="34" viewBox="0 0 120 120" aria-hidden="true"><circle cx="60" cy="60" r="58" fill="#0F0E0C"/>'
        '<circle cx="60" cy="60" r="50" fill="none" stroke="#C9A24A" stroke-width="4"/>'
        '<text x="60" y="74" text-anchor="middle" font-family="Anton,Impact,sans-serif" font-size="36" fill="#F2EBDD">FW</text>'
        '<circle cx="60" cy="26" r="5" fill="#E03A3E"/></svg>')
CSS = """
:root{color-scheme:dark;--bg:#0F0E0C;--card:#1A1815;--ink:#F2EBDD;--soft:#A39A86;--line:#2A261D;--line2:#3A3323;--red:#E03A3E;--gold:#C9A24A}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 Inter,system-ui,sans-serif}
a{color:var(--gold)}a:hover{color:#e2c57a}
.wrap{max-width:760px;margin:0 auto;padding:0 18px}
nav{border-bottom:1px solid var(--line);padding:10px 0}nav .wrap{display:flex;align-items:center;justify-content:space-between}
.logo{display:inline-flex;align-items:center;gap:8px;text-decoration:none;color:var(--ink);font:26px/1 Anton,Impact,sans-serif;text-transform:uppercase}
.logo b{color:var(--red);font-weight:400}.all{font-weight:600;font-size:14px}
.kick{font:600 12px 'IBM Plex Mono',monospace;letter-spacing:.18em;text-transform:uppercase;color:var(--gold);margin:26px 0 0}
h1{font:clamp(36px,7vw,56px)/1.02 Anton,Impact,sans-serif;text-transform:uppercase;margin:8px 0 0;text-wrap:balance}
.dek{color:#C8BFAE;margin:10px 0 0}
.ticket{background:var(--red);padding:4px;border-radius:10px;margin:20px 0 0}
.ticket .in{border:2px solid var(--bg);border-radius:7px;padding:18px 20px}
.big{font:600 clamp(34px,7vw,48px)/1.1 'IBM Plex Mono',monospace;margin:4px 0 0}
.lab2{font:600 13px Inter;color:var(--bg);margin:4px 0 0;letter-spacing:.04em}
.cd{display:inline-block;font:600 13px 'IBM Plex Mono',monospace;background:var(--bg);color:var(--ink);padding:6px 10px;border-radius:4px;margin:12px 0 0}
.cd:empty{display:none}
.belt{display:inline-block;font:600 11px 'IBM Plex Mono',monospace;color:var(--bg);background:var(--gold);padding:3px 7px;border-radius:3px;letter-spacing:.06em;text-transform:uppercase}
.where{color:var(--soft);font-size:14px;margin:10px 0 0}
section{margin:32px 0 0}
h2{font:26px/1 Anton,Impact,sans-serif;text-transform:uppercase;margin:0 0 12px;padding-bottom:8px;border-bottom:2px solid var(--ink)}
.box{background:var(--card);border:1px solid var(--line2);border-radius:10px;padding:18px}
.box h2{border:0;padding:0}
h3{font:600 13px Inter;letter-spacing:.08em;text-transform:uppercase;color:var(--soft);margin:18px 0 6px;display:flex;justify-content:space-between}
h3 .t{color:var(--ink)}
.zones{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}
.zones li{background:var(--card);border-radius:8px;padding:10px 12px}.zones span{display:block;font:600 11px Inter;color:var(--soft)}
.zones b{font:600 18px 'IBM Plex Mono',monospace}
.wrow{display:flex;gap:12px;align-items:center;padding:12px;background:var(--bg);border:1px solid var(--line);border-radius:8px;margin-top:8px}
.wico{width:40px;height:40px;border-radius:8px;background:var(--line);display:flex;align-items:center;justify-content:center;color:var(--gold)}
.wrow b{display:block}.wrow small{color:var(--soft)}
.btn{display:flex;align-items:center;justify-content:center;min-height:46px;margin-top:14px;border-radius:8px;background:var(--gold);color:var(--bg);font-weight:700;text-decoration:none}
.btn:hover{color:var(--bg);filter:brightness(1.08)}
.card,.bars,.news,.more{list-style:none;margin:0;padding:0}
.card li{padding:12px 0;border-bottom:1px solid var(--line)}
.card .bout{margin:2px 0 0;font-size:16px}.card em{font-style:normal;color:var(--soft);font-size:13px}.vs{color:var(--soft);font-size:13px}
.card small{display:block;color:var(--soft);font-size:13px}.card .res{color:var(--gold)}
.lab{font:600 10px 'IBM Plex Mono',monospace;letter-spacing:.1em;text-transform:uppercase;color:var(--red);margin-right:8px}
.bars li{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--line)}
.bars small{display:block;color:var(--soft);font-size:13px}
.news li,.more li{padding:10px 0;border-bottom:1px solid var(--line)}.more small{display:block;color:var(--soft);font-size:13px}
.muted{color:var(--soft);font-size:14px}.foot{margin:40px 0 30px;padding-top:16px;border-top:1px solid var(--line);font-size:13px}
"""
JS = """
(()=>{const tz=Intl.DateTimeFormat().resolvedOptions().timeZone;const f=(iso,o)=>new Intl.DateTimeFormat("en-US",{timeZone:tz,...o}).format(new Date(iso));
const ab=iso=>new Intl.DateTimeFormat("en-US",{timeZone:tz,timeZoneName:"short"}).formatToParts(new Date(iso)).find(p=>p.type==="timeZoneName").value;
try{document.querySelectorAll("[data-utc]").forEach(el=>{const u=el.dataset.utc,k=el.dataset.fmt;
el.textContent=k==="day"?f(u,{weekday:"short",month:"short",day:"numeric"})+" · your time ("+ab(u)+")":f(u,{hour:"numeric",minute:"2-digit"})+" "+ab(u)});
const c=document.querySelector("[data-cd]");if(c){const tick=()=>{const ms=new Date(c.dataset.cd)-Date.now();if(ms<=0){c.textContent=ms>-6*36e5?"ON NOW":"";return}
const d=Math.floor(ms/864e5),h=Math.floor(ms%864e5/36e5),m=Math.floor(ms%36e5/6e4);c.textContent=(d?d+"D ":"")+h+"H "+m+"M TO GO"};tick();setInterval(tick,3e4)}}catch(e){}})();
"""


ABOUT = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>About FightWhen · Contact</title>
<meta name="description" content="FightWhen shows when every big boxing, UFC, MMA and bare knuckle fight starts in your time zone, where to watch it, and the news around it.">
<link rel="canonical" href="{base}/about/">
<meta name="theme-color" content="#0F0E0C">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@600&display=swap" rel="stylesheet">
<style>{css}
.prose p,.prose li{{color:#C8BFAE}}.prose b{{color:var(--ink)}}.prose ul{{padding-left:20px}}
.mail{{display:inline-flex;align-items:center;min-height:46px;padding:0 18px;border-radius:8px;background:var(--gold);color:var(--bg);font-weight:700;text-decoration:none;margin-top:6px}}
.mail:hover{{color:var(--bg);filter:brightness(1.08)}}</style>
</head>
<body>
<nav><div class="wrap"><a class="logo" href="/">{logo}<span>Fight<b>When</b></span></a><a class="all" href="/">All fights</a></div></nav>
<main class="wrap prose">
<p class="kick">About</p>
<h1>When’s the fight?</h1>
<p class="dek">FightWhen answers that in one place: every big boxing, UFC, MMA, kickboxing and bare knuckle fight, in your own time zone, with where to watch it, the full card, the tale of the tape and the news around it.</p>

<section><h2>How it stays up to date</h2>
<ul>
<li><b>Schedules</b> come from promoters’ own sites and public event pages. A date or time goes live only when two sources agree; anything less is checked by a person first.</li>
<li><b>Breaking changes</b> like withdrawals and replacements are picked up from news headlines within the hour and checked before they go live.</li>
<li><b>Fighter stats and records</b> come from public sources, and each number must match the source’s own words. If sources disagree, we leave it blank rather than guess.</li>
<li><b>News</b> is written from fighters’ and promoters’ own interviews and videos, and every story credits and links its source.</li>
</ul></section>

<section><h2>Contact</h2>
<p>Spotted something wrong, want your channel credited differently, or want to work together? Email us.</p>
<a class="mail" href="mailto:{mail}">{mail}</a>
<ul>
<li><b>Corrections:</b> tell us the fight and what’s wrong; we fix it fast.</li>
<li><b>Creators:</b> if we summarize your video and you’d like it credited differently or removed, just ask.</li>
<li><b>Bars and restaurants:</b> showing a big fight? <a href="mailto:{mail}?subject=List%20my%20bar">Get listed</a> on that fight’s card.</li>
<li><b>Advertising and partnerships:</b> we’re open to it.</li>
</ul></section>

<p class="muted foot">FightWhen isn’t affiliated with any promoter, league or broadcaster. Fight times are shown in your own time zone and can change; check the broadcaster on the night. <a href="/">Back to all fights</a></p>
</main>
</body>
</html>
"""


def main():
    fights = load("fights.json", [])
    stories = load("stories.json", [])
    bars = load("bars.json", [])
    out = SITE / "fight"
    if out.exists():
        shutil.rmtree(out)
    today = dt.date.today().isoformat()
    upcoming = sorted((f for f in fights if f.get("d", "") >= today), key=lambda f: f["d"])
    urls = [(f"{BASE}/", today), (f"{BASE}/about/", today)]
    (SITE / "about").mkdir(exist_ok=True)
    (SITE / "about" / "index.html").write_text(ABOUT.format(base=BASE, css=CSS, logo=LOGO, mail=CONTACT))
    for f in fights:
        if not f.get("id") or not f.get("d") or "/" in f["id"]:
            continue
        others = [o for o in upcoming if o["id"] != f["id"]][:5]
        path = page_slug(f)
        d = out / path
        d.mkdir(parents=True, exist_ok=True)
        try:
            made = cards.make(f, CATS.get(f["c"], f["c"]), d / "share.png")
        except Exception as err:          # a bad image never stops the site from publishing
            print(f"Share image for {f['id']} failed: {err.__class__.__name__}")
            made = False
        image = f"{BASE}/fight/{path}/share.png" if made else None
        (d / "index.html").write_text(page(f, stories, bars, others, image))
        urls.append((f"{BASE}/fight/{path}/", today))
        if path != f["id"]:
            # The home page links by id; send those visitors to the readable address.
            r = out / f["id"]
            r.mkdir(parents=True, exist_ok=True)
            (r / "index.html").write_text(
                f'<!DOCTYPE html><meta charset="utf-8"><link rel="canonical" href="{BASE}/fight/{path}/">'
                f'<meta http-equiv="refresh" content="0; url=/fight/{path}/"><title>FightWhen</title>'
                f'<a href="/fight/{path}/">Continue</a>')
    (SITE / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"<url><loc>{e(u)}</loc><lastmod>{m}</lastmod></url>\n" for u, m in urls) + "</urlset>\n")
    (SITE / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {BASE}/sitemap.xml\n")
    news_dir = SITE / "news"
    if news_dir.exists():
        shutil.rmtree(news_dir)
    by_id = {f["id"]: f for f in fights}
    for i, n in enumerate(stories):
        if not n.get("id") or "/" in n["id"]:
            continue
        same = [o for o in stories if o is not n and (o.get("fid") and o.get("fid") == n.get("fid") or o.get("c") == n.get("c"))][:6]
        d = news_dir / n["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(news_page(n, by_id.get(n.get("fid")), same))
        urls.append((f"{BASE}/news/{n['id']}/", (n.get("published") or today)[:10]))
    prerender_home(fights, stories, today)
    (SITE / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"<url><loc>{e(u)}</loc><lastmod>{m}</lastmod></url>\n" for u, m in urls) + "</urlset>\n")
    # IndexNow (Bing, Yandex, Naver…): only pages that change often, so pings stay meaningful.
    soon = (dt.date.today() + dt.timedelta(days=14)).isoformat()
    ping = [f"{BASE}/"] + [f"{BASE}/fight/{page_slug(f)}/" for f in upcoming if f["d"] <= soon] + \
           [f"{BASE}/news/{n['id']}/" for n in stories if (n.get("published") or "")[:10] >= (dt.date.today() - dt.timedelta(days=2)).isoformat()]
    (SITE / "urls.txt").write_text("\n".join(dict.fromkeys(ping)) + "\n")
    add_analytics()
    print(f"Built {len(urls)} pages (fights, news, about, home) and the sitemap.")


def add_analytics():
    """Cloudflare Web Analytics (no cookies, no banner needed). Turned on by the repository
    variable CF_BEACON_TOKEN; the token is public by design, it only says which site counts."""
    token = re.sub(r"[^A-Za-z0-9]", "", os.environ.get("CF_BEACON_TOKEN", ""))
    if not token:
        return
    tag = ('<script defer src="https://static.cloudflareinsights.com/beacon.min.js" '
           f'data-cf-beacon=\'{{"token": "{token}"}}\'></script>')
    for page in [SITE / "index.html", *SITE.glob("about/index.html"), *SITE.glob("fight/*/index.html"), *SITE.glob("news/*/index.html")]:
        html = page.read_text()
        if "cloudflareinsights" not in html and "</body>" in html:
            page.write_text(html.replace("</body>", tag + "\n</body>", 1))


if __name__ == "__main__":
    main()
