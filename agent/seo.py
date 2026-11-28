"""Search pages built from the same data as the site, on every deploy.

Each page answers one kind of question people type into Google:
  /fighter/<name>/        "kabayel next fight", "kabayel record", "kabayel height"
  /fights-tonight/        "fight tonight", "is there a fight tonight", "boxing tonight"
  /fights-this-weekend/   "fights this weekend", "boxing this weekend"
  /<sport>-schedule/      "boxing schedule", "ufc schedule 2026", "bare knuckle schedule"
Everything comes from site/data, so the pages are only as good (and as careful) as the robots'
data: nothing here is guessed.
"""
import datetime as dt
import json
import re
import unicodedata
from zoneinfo import ZoneInfo

import pages as P
import cal

ET = ZoneInfo("America/New_York")
SPORT_PAGES = {
    "boxing": ("boxing-schedule", "Boxing Schedule", "boxing"),
    "ufc": ("ufc-schedule", "UFC Schedule", "UFC"),
    "pfl": ("pfl-schedule", "PFL Schedule", "PFL"),
    "one": ("one-championship-schedule", "ONE Championship Schedule", "ONE Championship"),
    "bkfc": ("bare-knuckle-schedule", "Bare Knuckle (BKFC) Schedule", "bare knuckle"),
    "rizin": ("rizin-schedule", "RIZIN Schedule", "RIZIN"),
    "k1": ("kickboxing-schedule", "Kickboxing Schedule", "kickboxing"),
    "misfits": ("misfits-boxing-schedule", "Misfits Boxing Schedule", "Misfits Boxing"),
}
COUNTRY = {"USA": "United States", "GBR": "United Kingdom", "MEX": "Mexico", "DEU": "Germany", "ALB": "Albania",
           "IRL": "Ireland", "AUS": "Australia", "NZL": "New Zealand", "CAN": "Canada", "BRA": "Brazil",
           "RUS": "Russia", "UKR": "Ukraine", "JPN": "Japan", "PHL": "Philippines", "POL": "Poland",
           "FRA": "France", "ESP": "Spain", "ITA": "Italy", "NGA": "Nigeria", "CUB": "Cuba", "PRI": "Puerto Rico",
           "KAZ": "Kazakhstan", "UZB": "Uzbekistan", "ARM": "Armenia", "GEO": "Georgia", "SWE": "Sweden",
           "NLD": "Netherlands", "CHN": "China", "KOR": "South Korea", "THA": "Thailand", "ARG": "Argentina",
           "VEN": "Venezuela", "COL": "Colombia", "DOM": "Dominican Republic", "SCO": "Scotland",
           "WAL": "Wales", "ENG": "England", "ZAF": "South Africa", "GHA": "Ghana", "CMR": "Cameroon"}


def e(s):
    return P.e(s)


def fslug(name):
    plain = unicodedata.normalize("NFKD", (name or "").translate(P.SPECIAL)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:60]


def real_name(n):
    n = (n or "").strip()
    return n and len(n.split()) >= 2 and not P.re.match(r"^(tba|tbd|tbc|opponent)\b", n, re.I)


def start(f):
    if f.get("utc"):
        return dt.datetime.fromisoformat(f["utc"].replace("Z", "+00:00"))
    d = dt.date.fromisoformat(f["d"])
    return dt.datetime(d.year, d.month, d.day, 23, 0, tzinfo=dt.timezone.utc)


def result_text(b, res):
    if not res:
        return ""
    if res.get("w") == "d":
        return "Draw"
    if res.get("w") == "nc":
        return "No contest"
    win = b[0] if res.get("w") == "a" else b[2]
    return f"{win} won by {res.get('m', 'decision')}" + (f" in round {res['r']}" if res.get("r") else "")


def shell(title, desc, url, inner, ld=(), image=None, nav=("All fights", "/")):
    lds = "".join(f'<script type="application/ld+json">{json.dumps(x, ensure_ascii=False)}</script>' for x in ld)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<link rel="canonical" href="{e(url)}">
<meta name="theme-color" content="#0F0E0C">
<meta property="og:type" content="website"><meta property="og:site_name" content="FightWhen"><meta property="og:url" content="{e(url)}">
<meta property="og:title" content="{e(title.split(' | ')[0])}"><meta property="og:description" content="{e(desc)}">
<meta property="og:image" content="{e(image or P.BASE + '/og.png')}"><meta name="twitter:card" content="summary_large_image">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@600&display=swap" rel="stylesheet">
{lds}
<style>{P.CSS}{EXTRA_CSS}</style>
</head>
<body>
<nav><div class="wrap"><a class="logo" href="/">{P.LOGO}<span>Fight<b>When</b></span></a><a class="all" href="{nav[1]}">{e(nav[0])}</a></div></nav>
<main class="wrap">
{inner}
{FOOTER}
</main>
<script>{P.JS}</script>
</body>
</html>
"""


EXTRA_CSS = """
.list{list-style:none;margin:0;padding:0}
.list li{display:flex;gap:14px;align-items:center;padding:14px 0;border-bottom:1px solid var(--line)}
.list .dt{flex:0 0 74px;text-align:center;font:600 12px 'IBM Plex Mono',monospace;color:var(--gold);line-height:1.3}
.list .dt b{display:block;font:400 26px Anton,Impact,sans-serif;color:var(--ink)}
.list a{color:var(--ink);text-decoration:none;font-weight:700}
.list a:hover{color:var(--gold)}
.list small{display:block;color:var(--soft);font-size:13px;margin-top:2px}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin:0;padding:0;list-style:none}
.facts li{background:var(--card);border-radius:8px;padding:12px 14px}
.facts span{display:block;font:600 11px Inter;color:var(--soft);text-transform:uppercase;letter-spacing:.06em}
.facts b{font:600 20px 'IBM Plex Mono',monospace}
"""

FOOTER = ""   # filled in by build() once the sport pages are known


def faq_ld(pairs):
    return {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in pairs]}


def faq_html(pairs):
    return "<section><h2>Questions</h2>" + "".join(
        f"<details><summary>{e(q)}</summary><p>{e(a)}</p></details>" for q, a in pairs) + "</section>"


def crumbs(*items):
    ld = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": n, "item": P.BASE + u} for i, (n, u) in enumerate(items)]}
    html = '<p class="crumbs">' + " › ".join(f'<a href="{u}">{e(n)}</a>' for n, u in items) + "</p>"
    return html, ld


def row(f, extra=""):
    t = start(f).astimezone(ET)
    when = P.when(f["utc"], "America/New_York", False) + " ET" if f.get("utc") else "Time TBA"
    tv = f" · {f['w']}" if f.get("w") and f["w"] != "TBA" else ""
    return (f'<li><span class="dt">{t.strftime("%a %b").upper()}<b>{t.day}</b></span><div>'
            f'<a href="/fight/{e(P.page_slug(f))}/">{e(f["f"])}</a>'
            f'<small>{e(when)}{e(tv)}{" · " + e(f["v"]) if f.get("v") and "TBA" not in f["v"] else ""}{extra}</small></div></li>')


# ---------- fighters ----------

def collect_fighters(fights):
    """name -> appearances [(fight, bout row, side 'a'|'b')], merged across spellings by slug."""
    out = {}
    for f in fights:
        for s in f.get("card", []):
            for b in s.get("b", []):
                for side, i in (("a", 0), ("b", 2)):
                    if real_name(b[i]):
                        out.setdefault(fslug(b[i]), []).append((f, b, side))
    return out


def fighter_page(slug, apps, stories, now):
    apps = sorted(apps, key=lambda x: start(x[0]))
    name = max((b[0] if side == "a" else b[2] for _, b, side in apps), key=len)
    last = name.split()[-1]
    upcoming = [x for x in apps if start(x[0]) > now - dt.timedelta(hours=8) and not (len(x[1]) > 5 and isinstance(x[1][5], dict) and x[1][5].get("res"))]
    past = [x for x in apps if x not in upcoming]
    # most recent facts win
    rec = next((b[1] if side == "a" else b[3] for _, b, side in reversed(apps) if (b[1] if side == "a" else b[3])), "")
    tape = next(((b[5].get("tape") or {}).get(side) for _, b, side in reversed(apps)
                 if len(b) > 5 and isinstance(b[5], dict) and (b[5].get("tape") or {}).get(side)
                 and any((b[5]["tape"][side]))), None) or ["", "", "", ""]
    country = next((b[5].get("c" + side) for _, b, side in reversed(apps)
                    if len(b) > 5 and isinstance(b[5], dict) and b[5].get("c" + side)), "")
    sport = P.CATS.get(apps[-1][0]["c"], apps[-1][0]["c"])
    url = f"{P.BASE}/fighter/{slug}/"

    facts = [("Record", rec), ("Age", tape[0]), ("Height", tape[1]), ("Reach", tape[2]), ("Stance", tape[3]),
             ("From", COUNTRY.get(country, country))]
    facts_html = '<ul class="facts">' + "".join(f"<li><span>{k}</span><b>{e(v)}</b></li>" for k, v in facts if v) + "</ul>"

    qa = []
    if upcoming:
        f, b, side = upcoming[0]
        opp = b[2] if side == "a" else b[0]
        t = start(f)
        time_txt = (f"at {P.when(f['utc'], 'America/New_York', False)} ET / {P.when(f['utc'], 'America/Los_Angeles', False)} PT"
                    if f.get("utc") else "(start time not announced yet)")
        tv = f" It's on {f['w']}." if f.get("w") and f["w"] != "TBA" else ""
        next_txt = f"{name} fights {opp} on {P.nice_date(f['d'])} {time_txt}" + \
                   (f" at {f['v']}" if f.get("v") and "TBA" not in f["v"] else "") + f".{tv}"
        weight = (b[4] or "").split(" · ")[0]
        time_html = (f'<p class="big" data-utc="{e(f["utc"])}" data-fmt="time" style="font-size:22px">'
                     f'{e(P.when(f["utc"], "America/New_York", False))} ET</p>') if f.get("utc") else ""
        tv_txt = f' · {e(f["w"])}' if f.get("w") and f["w"] != "TBA" else ""
        hero = (f'<section class="box"><h2>Next fight</h2><p class="big" style="font-size:28px;margin:0">vs {e(opp)}</p>'
                f'<p class="muted" style="margin:6px 0 0">{e(P.nice_date(f["d"]))}{" · " + e(weight) if weight else ""}{tv_txt}</p>'
                f'{time_html}<a class="btn" href="/fight/{e(P.page_slug(f))}/">Start time, full card &amp; where to watch</a>{cal.buttons(f)}</section>')
        qa.append((f"When is {name}'s next fight?", next_txt))
        qa.append((f"Who is {name} fighting next?", f"{name} is scheduled to face {opp} on {P.nice_date(f['d'])}."))
        if f.get("w") and f["w"] != "TBA":
            qa.append((f"How can I watch {name}'s next fight?", f"{f['f']} is on {f['w']}" + (f" ({f['wp']})." if f.get("wp") else ".")))
        lede = next_txt
    else:
        hero = ('<section class="box"><h2>Next fight</h2><p class="muted">No fight announced yet. '
                'This page updates as soon as one is confirmed by two sources.</p></section>')
        qa.append((f"When is {name}'s next fight?", f"{name} has no fight announced right now. FightWhen adds it as soon as it is confirmed."))
        lede = f"{name} has no fight announced right now. This page updates automatically when one is confirmed."
    if rec:
        qa.append((f"What is {name}'s record?", f"{name}'s professional {sport.lower() if sport != 'UFC' else 'MMA'} record is {rec}."))
    if tape[1] or tape[2]:
        qa.append((f"How tall is {name} and what is his reach?",
                   " ".join(x for x in (f"{name} is {tape[1]} tall." if tape[1] else "",
                                        f"His reach is {tape[2]}." if tape[2] else "") if x)))
    if tape[0]:
        qa.append((f"How old is {name}?", f"{name} is {tape[0]}."))

    results = "".join(
        f'<li><span class="dt">{e(start(f).strftime("%b %Y").upper())}</span><div><a href="/fight/{e(P.page_slug(f))}/">'
        f'vs {e(b[2] if side == "a" else b[0])}</a><small>{e(result_text(b, b[5].get("res") if len(b) > 5 and isinstance(b[5], dict) else None) or f["f"])}</small></div></li>'
        for f, b, side in reversed(past))
    mine = [n for n in stories if any(P.re.sub(r"\W", "", t.lower()) == P.re.sub(r"\W", "", name.lower()) for t in n.get("tags", []))
            or (last.lower() in n["h"].lower() and n.get("fid") in {x[0]["id"] for x in apps})][:8]
    news = "".join(f'<li><a href="/news/{e(n["id"])}/">{e(n["h"])}</a></li>' for n in mine)
    bc_html, bc_ld = crumbs(("FightWhen", "/"), (SPORT_PAGES.get(apps[-1][0]["c"], ("", "Schedule"))[1], f"/{SPORT_PAGES.get(apps[-1][0]['c'], ('', ''))[0]}/"), (name, f"/fighter/{slug}/"))
    person = {"@context": "https://schema.org", "@type": "Person", "name": name, "url": url,
              "jobTitle": "Professional fighter", **({"nationality": COUNTRY.get(country, country)} if country else {}),
              **({"height": tape[1]} if tape[1] else {})}
    inner = (f'{bc_html}<p class="kick">{e(sport)} · Fighter</p><h1>{e(name)}: next fight, record &amp; stats</h1>'
             f'<p class="dek">{e(lede)}</p>{hero}'
             f'<section><h2>Fighter facts</h2>{facts_html}</section>'
             + (f'<section><h2>Fights</h2><ul class="list">{results}</ul></section>' if results else "")
             + (f'<section><h2>In the news</h2><ul class="news">{news}</ul></section>' if news else "")
             + faq_html(qa))
    title = f"{name} Next Fight: Date, Time, Opponent & Record | FightWhen"
    desc = f"{lede} Record{' ' + rec if rec else ''}, age, height, reach and the latest news."[:300]
    return shell(title, desc, url, inner, (person, faq_ld(qa), bc_ld))


# ---------- tonight / weekend / sport schedules ----------

def when_pages(fights, now):
    today = now.astimezone(ET).date()
    tonight = [f for f in fights if start(f).astimezone(ET).date() == today
               or now <= start(f) <= now + dt.timedelta(hours=18)]
    tonight = sorted({f["id"]: f for f in tonight}.values(), key=start)
    # this weekend = coming Friday to Sunday (or the current one)
    fri = today + dt.timedelta(days=(4 - today.weekday()) % 7) if today.weekday() < 4 else today - dt.timedelta(days=today.weekday() - 4)
    sun = fri + dt.timedelta(days=2)
    weekend = sorted([f for f in fights if fri <= start(f).astimezone(ET).date() <= sun and start(f) > now - dt.timedelta(hours=8)], key=start)
    nxt = sorted([f for f in fights if start(f) > now], key=start)[:8]
    out = {}

    def page(slug, h1, title, desc, items, empty, period):
        listing = f'<ul class="list">{"".join(row(f) for f in items)}</ul>' if items else \
            f'<p class="muted">{e(empty)}</p><h2>Coming up next</h2><ul class="list">{"".join(row(f) for f in nxt)}</ul>'
        names = ", ".join(P.short_name(f) for f in items[:4])
        qa = [(f"Is there a fight {period}?",
               (f"Yes: {names}. Times are shown in your time zone on each fight's page." if items else
                f"No major boxing, UFC or MMA card is scheduled {period}. The next one is {P.short_name(nxt[0])} on {P.nice_date(nxt[0]['d'])}." if nxt else
                f"No major card is scheduled {period}."))]
        if items and items[0].get("utc"):
            f0 = items[0]
            qa.append((f"What time does {P.short_name(f0)} start?",
                       f"{P.when(f0['utc'], 'America/New_York', False)} ET / {P.when(f0['utc'], 'America/Los_Angeles', False)} PT / "
                       f"{P.when(f0['utc'], 'Europe/London', False)} UK."))
        listld = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "url": f"{P.BASE}/fight/{P.page_slug(f)}/", "name": f["f"]} for i, f in enumerate(items)]}
        inner = (f'<p class="kick">Updated {e(now.astimezone(ET).strftime("%a %b %-d, %-I:%M %p ET"))}</p><h1>{e(h1)}</h1>'
                 f'<p class="dek">{e(desc)}</p>{listing}{cal.subscribe_box(None, "big")}{faq_html(qa)}')
        out[slug] = shell(title, desc, f"{P.BASE}/{slug}/", inner, (faq_ld(qa), listld))

    page("fights-tonight", "Fights tonight", "Is There a Fight Tonight? Boxing & UFC Start Times Tonight | FightWhen",
         "Every boxing, UFC, MMA and bare knuckle fight on tonight, with start times in your time zone and where to watch.",
         tonight, "No major fights tonight.", "tonight")
    page("fights-this-weekend", f"Fights this weekend · {fri.strftime('%b %-d')}–{sun.strftime('%-d')}",
         "Fights This Weekend: Boxing & UFC Schedule and Start Times | FightWhen",
         f"Every boxing, UFC, MMA and bare knuckle card this weekend ({fri.strftime('%b %-d')}–{sun.strftime('%b %-d')}), with start times in your time zone and where to watch.",
         weekend, "No major fights this weekend.", "this weekend")
    return out


def sport_pages(fights, now):
    out = {}
    for c, (slug, name, word) in SPORT_PAGES.items():
        items = sorted([f for f in fights if f["c"] == c and start(f) > now - dt.timedelta(hours=8)], key=start)
        done = sorted([f for f in fights if f["c"] == c and start(f) <= now - dt.timedelta(hours=8)], key=start, reverse=True)[:10]
        if not items and not done:
            continue
        yr = now.year if not items else start(items[0]).year
        yrs = f"{yr}" if not items or start(items[-1]).year == yr else f"{yr}–{str(start(items[-1]).year)[2:]}"
        def res_extra(f):
            b = next((s["b"][0] for s in f.get("card", []) if s.get("b")), None)
            r = b[5].get("res") if b and len(b) > 5 and isinstance(b[5], dict) else None
            return " · " + e(result_text(b, r)) if r else ""
        qa = []
        if items:
            f0 = items[0]
            qa.append((f"When is the next {word} fight?",
                       f"{f0['f']} on {P.nice_date(f0['d'])}" + (f" at {P.when(f0['utc'], 'America/New_York', False)} ET" if f0.get("utc") else "") +
                       (f" on {f0['w']}." if f0.get("w") and f0["w"] != "TBA" else ".")))
            qa.append((f"How many {word} events are scheduled?", f"{len(items)} upcoming {word} cards are on FightWhen's schedule right now."))
        inner = (f'<p class="kick">{e(name.split(" ")[0])} · {len(items)} upcoming</p><h1>{e(name)} {e(yrs)}</h1>'
                 f'<p class="dek">Every upcoming {e(word)} card with dates, start times in your time zone, the full fight card and where to watch. Updated automatically.</p>'
                 + (f'<ul class="list">{"".join(row(f) for f in items)}</ul>' if items else '<p class="muted">No upcoming cards announced yet.</p>')
                 + (f'<section><h2>Recent results</h2><ul class="list">{"".join(row(f, res_extra(f)) for f in done)}</ul></section>' if done else "")
                 + cal.subscribe_box(c, word)
                 + (faq_html(qa) if qa else ""))
        listld = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "url": f"{P.BASE}/fight/{P.page_slug(f)}/", "name": f["f"]} for i, f in enumerate(items)]}
        title = f"{name} {yrs}: Upcoming Fights, Dates & Start Times | FightWhen"
        desc = f"Full {word} schedule: {len(items)} upcoming cards with dates, start times in your time zone, fight cards and where to watch."
        out[slug] = shell(title, desc, f"{P.BASE}/{slug}/", inner, ((faq_ld(qa),) if qa else ()) + (listld,))
    return out


def build(fights, stories, now=None):
    """Write every search page. Returns [(url, lastmod)] for the sitemap and the fighter slug map."""
    global FOOTER
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    live = [c for c in SPORT_PAGES if any(f["c"] == c for f in fights)]
    FOOTER = ('<nav class="sitenav" aria-label="Schedules"><a href="/fights-tonight/">Fights tonight</a>'
              '<a href="/fights-this-weekend/">This weekend</a>'
              + "".join(f'<a href="/{SPORT_PAGES[c][0]}/">{e(SPORT_PAGES[c][1])}</a>' for c in live)
              + '<a href="/about/">About</a></nav>')
    urls = []
    for slug, html in {**when_pages(fights, now), **sport_pages(fights, now)}.items():
        d = P.SITE / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(html)
        urls.append((f"{P.BASE}/{slug}/", today))
    fdir = P.SITE / "fighter"
    if fdir.exists():
        P.shutil.rmtree(fdir)
    fighters = collect_fighters(fights)
    for slug, apps in fighters.items():
        if not slug:
            continue
        d = fdir / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(fighter_page(slug, apps, stories, now))
        urls.append((f"{P.BASE}/fighter/{slug}/", today))
    return urls, set(fighters), FOOTER
