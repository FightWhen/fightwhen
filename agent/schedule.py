"""FightWhen schedule agent.

Keeps site/data/fights.json current: new events, new bouts, start times,
venues and broadcasters. It reads the pages in agent/sources.json (Wikipedia
and the promotions' own schedule pages, only where robots.txt allows) and
never reads BoxRec, Tapology or ESPN.

The rule: nothing goes live from one website alone. A change is only made when
two different websites agree on it, and every fact the model reads must come
with the exact words it was read from, checked against the page.

Safe changes go live straight away (filling in something that was blank or
TBA, a new event or bout two sources agree on). Risky changes are written to a
separate file that becomes a pull request for you to approve: a date change,
a different opponent, a bout that disappeared, a cancellation, or anything that
would overwrite a value already on the site. One-source sightings are listed in
the report so you can see what's coming, but never applied.
"""

import copy
import datetime as dt
import os
import re
import sys

import requests

from common import (AGENT, FIGHTS_PATH, NOW, TODAY, ask_json, domain, fetch_page, fight_start,
                    fingerprint, in_source, load_json, main_event, norm, same_bout, same_fighter,
                    save_json, slug, stamp, surname, wiki_clean, wiki_search, wiki_wikitext)

SOURCES = load_json(AGENT / "sources.json", {}).get("schedule", [])
CACHE_PATH = AGENT / "schedule_cache.json"
REVIEW_PATH = AGENT / "fights_review.json"
REPORT_PATH = AGENT / "schedule_report.md"
DAYS_AHEAD = int(os.environ.get("SCHEDULE_DAYS_AHEAD", "120"))
MAX_CHARS = 22000
FORCE = os.environ.get("FORCE_RUN", "").lower() == "true"

report, review_notes = [], []


def log(msg):
    print(msg)
    report.append(msg)


# ---------- reading the sources ----------

def wiki_window(text):
    """The part of a long Wikipedia list that covers upcoming events."""
    text = wiki_clean(text)
    heads = [m for m in re.finditer(r"(?m)^==+\s*(.+?)\s*==+\s*$", text)]
    month = TODAY.strftime("%B")
    for m in heads:
        h = m.group(1).lower()
        if "scheduled" in h or "upcoming" in h or h.startswith(month.lower()):
            return text[m.start():m.start() + MAX_CHARS]
    i = text.find(month)
    return text[max(0, i - 500):i - 500 + MAX_CHARS] if i >= 0 else text[:MAX_CHARS]


def source_text(src):
    if src.get("wiki"):
        title = src["wiki"].format(year=TODAY.year, next_year=TODAY.year + 1)
        try:
            raw = wiki_wikitext(title)
        except requests.RequestException as e:
            log(f"- Couldn't read Wikipedia “{title}”: {e.__class__.__name__}")
            return "", ""
        if not raw:
            log(f"- Wikipedia has no page called “{title}”.")
        return wiki_window(raw) if raw else "", "en.wikipedia.org/wiki/" + title.replace(" ", "_")
    text = fetch_page(src["url"], log)
    if text:
        i = text.find(TODAY.strftime("%b"))
        text = text[max(0, i - 2000):][:MAX_CHARS] if i > 2000 else text[:MAX_CHARS]
    return text, src["url"]


EXTRACT = """You read one web page and list the combat sports events it shows as scheduled
from {today} to {until}. Use ONLY this page. Do not use outside knowledge, do not guess.

For each event give:
- "name": the event's name as the page writes it
- "date": YYYY-MM-DD (the local date at the venue)
- "date_words": the exact words from the page that give the date, copied character for character
- "start_utc": the main card's start time converted to UTC as YYYY-MM-DDTHH:MM:00Z, ONLY if the
  page states a time with a time zone; otherwise null
- "time_words": the exact words giving that time and zone, or null
- "venue": venue and city if stated, else null
- "broadcaster": the TV or streaming service if stated, else null
- "price": the US pay-per-view price exactly as the page writes it (like "$59.99"), only if the
  page states one, else null
- "status": "scheduled", "cancelled" or "postponed", as the page says
- "bouts": every announced bout, main event first, each as
  {{"a": fighter name, "b": fighter name, "weight": weight class or null,
   "title": true only if the page says a title or belt is on the line,
   "words": the exact words from the page naming both fighters, copied character for character}}

Reply with JSON only: {{"events": [ ... ]}}. If the page shows no scheduled events in that window,
reply {{"events": []}}."""


def extract(src, text, where):
    until = TODAY + dt.timedelta(days=DAYS_AHEAD)
    data = ask_json(EXTRACT.format(today=TODAY.isoformat(), until=until.isoformat()),
                    f"Page: {where}\n\n{text}", max_tokens=16000)
    if not data:
        return []
    page = norm(text)
    good = []
    for ev in data.get("events", []):
        try:
            day = dt.date.fromisoformat(ev["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (TODAY <= day <= until) or not in_source(ev.get("date_words", ""), page):
            continue
        if str(day.day) not in ev["date_words"]:
            continue
        bouts = []
        for b in ev.get("bouts", []):
            words = norm(b.get("words", ""))
            if b.get("a") and b.get("b") and in_source(words, page) \
                    and surname(b["a"]) in words and surname(b["b"]) in words:
                bouts.append({k: b.get(k) for k in ("a", "b", "weight", "title")})
        if ev.get("start_utc") and not in_source(ev.get("time_words") or "", page):
            ev["start_utc"] = None
        price = ev.get("price")
        if price and (not re.search(r"\d", price) or norm(price) not in page):
            price = None
        if not bouts and not ev.get("name"):
            continue
        good.append({"name": ev.get("name") or f"{bouts[0]['a']} vs {bouts[0]['b']}",
                     "date": day.isoformat(), "start_utc": ev.get("start_utc"),
                     "venue": ev.get("venue"), "broadcaster": ev.get("broadcaster"),
                     "price": price, "status": ev.get("status") or "scheduled", "bouts": bouts, "c": src["c"]})
    return good


# ---------- matching ----------

def name_bout(name):
    """The two fighters in an event name like "UFC 336: Van vs. Kape", or (None, None)."""
    part = (name or "").split(":")[-1]
    m = re.search(r"(.+?)\s+(?:vs\.?|v\.?)\s+(.+)", part, re.I)
    if not m:
        return None, None
    a, b = m.group(1).strip(" ,-"), re.split(r"\s+[-–|(]", m.group(2))[0].strip(" ,-")
    if not a or not b or "tbd" in (a + b).lower() or "tba" in (a + b).lower():
        return None, None
    return a, b


PLACEHOLDER = re.compile(r"^(tbd|tba|tbc|to be (announced|determined)|opponent)\b", re.I)
KEEP_CAPS = {"UFC", "PFL", "BKFC", "ONE", "RIZIN", "K-1", "MMA", "UK", "US", "USA", "WBA", "WBC",
             "IBF", "WBO", "CA", "FL", "NY", "NV", "TX", "AZ", "PA", "II", "III", "IV", "V", "VI",
             "VII", "VIII", "IX", "X", "XI", "XII"}


def ev_main(ev):
    if ev["bouts"]:
        a, b = ev["bouts"][0]["a"], ev["bouts"][0]["b"]
        if PLACEHOLDER.match(a.strip()) or PLACEHOLDER.match(b.strip()):
            return None, None
        return a, b
    return name_bout(ev.get("name"))


def only_one(group, key):
    """(value, source) when the sites that list `key` all give the same value, else (None, "")."""
    seen = [(d, e.get(key)) for d, e in group if e.get(key)]
    if not seen or any(norm(str(v)) != norm(str(seen[0][1])) for _, v in seen):
        return None, ""
    return seen[0][1], ", ".join(sorted({d for d, _ in seen}))


def tidy(text):
    """Turn SHOUTING page text into normal case, keeping league names and states."""
    if not text or sum(c.isupper() for c in text) < 0.7 * sum(c.isalpha() for c in text):
        return text
    return " ".join("vs" if w.lower().strip(".") == "vs" else w if w.strip(",.:()") in KEEP_CAPS or any(c.isdigit() for c in w)
                    else w.capitalize() for w in text.split()).replace(" - ", ", ")


def event_title(name, main):
    """A readable title. Generic names like "Matchroom Boxing event (A VS B)" become "A vs B"."""
    name = tidy(name or "")
    if not name or re.search(r"\bevent\b\s*\(", name, re.I):
        return f"{tidy(main[0])} vs {tidy(main[1])}"
    name = re.sub(r"\s+VS\.?\s+", " vs ", name)
    if " vs" in name.lower() or " v " in name.lower():
        return name
    return f"{tidy(main[0])} vs {tidy(main[1])}: {name}"


def same_event(x, y):
    """x, y: dicts with date, name and main bout (either a scraped event or a site fight)."""
    dx, dy = dt.date.fromisoformat(x["date"]), dt.date.fromisoformat(y["date"])
    if abs((dx - dy).days) > 1:
        return False
    mx, my = x["main"], y["main"]
    if mx[0] and my[0] and same_bout(mx, my):
        return True
    return bool(x["name"]) and norm(x["name"]) == norm(y["name"])


def as_key(ev):
    return {"date": ev["date"], "name": ev["name"], "main": ev_main(ev)}


def site_key(f):
    return {"date": f["d"], "name": f.get("f", ""), "main": main_event(f)}


def cluster(sightings):
    """Group sightings of the same event from different pages."""
    groups = []
    for dom, ev in sightings:
        for g in groups:
            if same_event(as_key(ev), as_key(g[0][1])):
                g.append((dom, ev))
                break
        else:
            groups.append([(dom, ev)])
    return groups


def agreed(group, get, match=lambda a, b: a == b):
    """A value at least two different websites give, else None."""
    vals = [(dom, get(ev)) for dom, ev in group if get(ev)]
    for i, (d1, v1) in enumerate(vals):
        doms = {d1} | {d2 for d2, v2 in vals[i + 1:] if match(v1, v2)}
        if len(doms) >= 2:
            return v1
    return None


def bout_row(bout):
    weight = bout.get("weight") or ""
    if bout.get("title"):
        weight = (weight + " · title fight").strip(" ·")
    return [bout["a"], "", bout["b"], "", weight]


def weekday(date):
    return dt.date.fromisoformat(date).strftime("%a")


# ---------- deciding ----------

def apply(fights, groups):
    safe = review = 0
    proposed = copy.deepcopy(fights)
    seen_ids = {f["id"] for f in fights}

    def both(fn):
        nonlocal safe
        fn(fights)
        fn(proposed)
        safe += 1

    def risky(note, fn):
        nonlocal review
        fn(proposed)
        review += 1
        review_notes.append(note)

    for group in groups:
        doms = {d for d, _ in group}
        lead = group[0][1]
        idx = next((i for i, f in enumerate(fights) if same_event(as_key(lead), site_key(f))), None)
        names = ", ".join(sorted(doms))

        if idx is None:
            if len(doms) < 2:
                main = ev_main(lead)
                if doms <= {"wikipedia.org"} or not main[0] or lead.get("status") != "scheduled":
                    log(f"- Seen on one site only, waiting for a second: {lead['name']} ({lead['date']}, {names})")
                    continue
                # Listed only by the promotion running it (common in boxing, which has no
                # shared schedule): that is a good source, but it waits for your OK.
                fid = f"{lead['date']}-{slug(main[0])}-{slug(main[1])}"[:80]
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
                rows = [bout_row(b) for b in lead["bouts"]] or [[main[0], "", main[1], "", ""]]
                new = {"id": fid, "c": lead["c"], "d": lead["date"], "day": weekday(lead["date"]),
                       "f": event_title(lead["name"], main), "t": rows[0][4], "v": tidy(lead.get("venue")) or "Venue TBA",
                       "w": lead.get("broadcaster") or "TBA", "auto": 1,
                       **({"wp": lead["price"]} if lead.get("price") else {}),
                       "card": [{"sec": "Main card", "b": rows}]}
                if lead.get("start_utc"):
                    new.update(utc=lead["start_utc"], lab="Main card")
                risky(f"**{lead['name']}** ({lead['date']}): {main[0]} vs {main[1]}, listed only by the "
                      f"promoter ({names}). Approving adds it to the schedule.",
                      lambda fs, new=new: fs.append(copy.deepcopy(new)))
                continue
            date = agreed(group, lambda e: e["date"])
            if not date:
                # Sites a day apart usually differ only by time zone. The promotion's
                # own page knows its event best, so its date wins.
                official = [e["date"] for d, e in group if d != "wikipedia.org"]
                date = official[0] if official else None
            main = agreed(group, ev_main, same_bout)
            if not date or not main:
                log(f"- Two sites mention {lead['name']} but disagree on the date or main event; left out.")
                continue
            status = agreed(group, lambda e: e["status"])
            if status in ("cancelled", "postponed"):
                continue
            bouts = [b for b in lead["bouts"] if confirmed_bout(group, (b["a"], b["b"]))]
            fid = f"{date}-{slug(main[0])}-{slug(main[1])}"[:80]
            if fid in seen_ids:
                continue
            seen_ids.add(fid)
            name = max((e["name"] for _, e in group), key=len)
            if not bouts:
                bouts = max((e["bouts"] for _, e in group), key=len)
                bouts = [b for b in bouts if confirmed_bout(group, (b["a"], b["b"]))] or \
                        [{"a": main[0], "b": main[1], "weight": None, "title": False}]
            new = {"id": fid, "c": lead["c"], "d": date, "day": weekday(date), "f": name,
                   "t": (bouts[0].get("weight") if bouts else None) or "", "v": agreed(group, lambda e: e["venue"], lambda a, b: norm(a)[:12] == norm(b)[:12]) or "Venue TBA",
                   "w": agreed(group, lambda e: e["broadcaster"], lambda a, b: norm(a) == norm(b)) or "TBA",
                   "auto": 1}
            price = agreed(group, lambda e: e.get("price"), lambda a, b: norm(a) == norm(b))
            if price:
                new["wp"] = price
            start = agreed(group, lambda e: e["start_utc"])
            if start:
                new.update(utc=start, lab="Main card")
            if bouts:
                new["card"] = [{"sec": "Main card", "b": [bout_row(b) for b in bouts]}]
            both(lambda fs, new=new: fs.append(copy.deepcopy(new)))
            log(f"- Added {new['f']} on {date} (confirmed by {names}).")
            continue

        fight = fights[idx]
        fid = fight["id"]
        title = fight.get("f", fight["id"])

        status = agreed(group, lambda e: e["status"])
        if status in ("cancelled", "postponed"):
            risky(f"**{title}** is listed as {status} by {names}. Approving removes it from the schedule.",
                  lambda fs, fid=fid: fs.__setitem__(slice(None), [f for f in fs if f["id"] != fid]))
            continue

        date = agreed(group, lambda e: e["date"])
        if date and date != fight["d"]:
            risky(f"**{title}**: date {fight['d']} → {date} ({names}).",
                  lambda fs, fid=fid, d=date: by_id(fs, fid).update(d=d, day=weekday(d)))

        start = agreed(group, lambda e: e["start_utc"])
        if start and start != fight.get("utc"):
            if not fight.get("utc"):
                both(lambda fs, fid=fid, s=start: by_id(fs, fid).update(utc=s, lab=by_id(fs, fid).get("lab") or "Main card"))
                log(f"- {title}: start time {start} (confirmed by {names}).")
            else:
                risky(f"**{title}**: start time {fight['utc']} → {start} ({names}).",
                      lambda fs, fid=fid, s=start: by_id(fs, fid).update(utc=s))
        elif not start and not fight.get("utc"):
            # Often only the promoter posts the time. Show it to you rather than drop it.
            s, who = only_one(group, "start_utc")
            if s:
                risky(f"**{title}**: start time {s}, listed only by {who}. Check it before approving.",
                      lambda fs, fid=fid, s=s: by_id(fs, fid).update(utc=s, lab=by_id(fs, fid).get("lab") or "Main card"))

        for key, field in (("broadcaster", "w"), ("price", "wp"), ("venue", "v")):
            val = agreed(group, lambda e, k=key: e.get(k), lambda a, b: norm(a) == norm(b))
            cur = fight.get(field, "")
            if not val and (not cur or cur in ("TBA", "Venue TBA")):
                one, who = only_one(group, key)
                if one:
                    risky(f"**{title}**: {key} \"{one}\", listed only by {who}.",
                          lambda fs, fid=fid, f=field, v=one: by_id(fs, fid).update({f: v}))
            if val and norm(val) != norm(cur):
                if not cur or cur in ("TBA", "Venue TBA"):
                    both(lambda fs, fid=fid, f=field, v=val: by_id(fs, fid).update({f: v}))
                    log(f"- {title}: {key} → {val} ({names}).")
                elif norm(val) not in norm(cur) and norm(cur) not in norm(val):
                    risky(f"**{title}**: {key} “{cur}” → “{val}” ({names}).",
                          lambda fs, fid=fid, f=field, v=val: by_id(fs, fid).update({f: v}))

        # Bouts: add ones two sites agree on; flag ones that changed or vanished.
        site_bouts = [(b[0], b[2]) for s in fight.get("card", []) for b in s.get("b", [])]
        for b in lead["bouts"]:
            pair = (b["a"], b["b"])
            if any(same_bout(pair, sb) for sb in site_bouts):
                continue
            if not confirmed_bout(group, pair):
                continue
            swapped = [sb for sb in site_bouts if same_fighter_any(sb, pair)]
            if swapped:
                old = swapped[0]
                risky(f"**{title}**: {old[0]} vs {old[1]} is now {pair[0]} vs {pair[1]} ({names}).",
                      lambda fs, fid=fid, o=old, b=b: replace_bout(by_id(fs, fid), o, b))
            else:
                def add(fs, b=b, fid=fight["id"]):
                    target = next(f for f in fs if f["id"] == fid)
                    card = target.setdefault("card", [{"sec": "Main card", "b": []}])
                    card[-1]["b"].append(bout_row(b))
                    site_bouts.append((b["a"], b["b"]))
                both(add)
                log(f"- {title}: added {pair[0]} vs {pair[1]} ({names}).")
        all_seen = [(x["a"], x["b"]) for _, e in group for x in e["bouts"]]
        if len(doms) >= 2 and all(len(e["bouts"]) >= 3 for _, e in group):
            for sb in site_bouts:
                if not any(same_bout(sb, s) for s in all_seen) and not any(same_fighter_any(sb, s) for s in all_seen):
                    risky(f"**{title}**: {sb[0]} vs {sb[1]} is no longer listed by {names}. Approving removes it.",
                          lambda fs, fid=fid, o=sb: remove_bout(by_id(fs, fid), o))
    return proposed, safe, review


def by_id(fs, fid):
    return next((f for f in fs if f["id"] == fid), {"card": []})


def confirmed_bout(group, pair):
    doms = {d for d, e in group if any(same_bout((x["a"], x["b"]), pair) for x in e["bouts"])}
    return len(doms) >= 2


def same_fighter_any(x, y):
    return any(same_fighter(p, q) for p in x for q in y)


def replace_bout(fight, old, bout):
    for s in fight.get("card", []):
        for row in s.get("b", []):
            if same_bout((row[0], row[2]), old):
                row[0], row[2] = bout["a"], bout["b"]
                row[1] = row[3] = ""
                if len(row) > 5:
                    row[5].pop("tape", None)
                return


def remove_bout(fight, old):
    for s in fight.get("card", []):
        s["b"] = [row for row in s.get("b", []) if not same_bout((row[0], row[2]), old)]


# ---------- full fight cards ----------

CARD = """You read one Wikipedia article about a combat sports event on {date} and list every
announced bout on its fight card. Use ONLY this page. For each bout give:
{{"a": fighter, "b": fighter, "weight": weight class or null,
 "title": true only if the page says a title is on the line,
 "sec": "Main card" or "Prelims" or "Early prelims" or "Undercard", as the page groups it,
 "words": the exact words from the page naming both fighters, copied character for character}}
Main event first. Skip bouts the page marks as cancelled or scrapped.
Reply with JSON only: {{"bouts": [ ... ]}}. If the page has no fight card yet, reply {{"bouts": []}}."""

CARD_DAYS = int(os.environ.get("CARD_DAYS_AHEAD", "30"))


def event_page(fight):
    """The Wikipedia article for this event, if one exists: (title, text)."""
    a, b = main_event(fight)
    queries = [fight.get("f", "")] + ([f"{a} vs. {b}"] if a and b else [])
    for q in queries:
        try:
            titles = wiki_search(q, limit=3)
        except requests.RequestException:
            continue
        for t in titles:
            if t.startswith("List of") or " in " in t[:12]:
                continue
            try:
                text = wiki_clean(wiki_wikitext(t))
            except requests.RequestException:
                continue
            n = norm(text)
            if a and b and surname(a) in n and surname(b) in n and fight["d"][:4] in text:
                i = max(n.find("fight card"), 0)
                return t, text[i:i + 20000] if i else text[:20000]
    return None, ""


def swap_opponent(f, target, b, title):
    """If the page pairs a fighter we list with someone new, that's a replacement
    (an injury, a withdrawal). Propose the swap for review instead of adding a second bout."""
    for section in target.get("card", []):
        for row in section.get("b", []):
            for keep, new_name in ((b["a"], b["b"]), (b["b"], b["a"])):
                for i, j in ((0, 2), (2, 0)):
                    if same_fighter(row[i], keep) and not same_fighter(row[j], new_name):
                        old = row[j]
                        row[j], row[j + 1] = new_name, ""
                        if len(row) > 5 and isinstance(row[5], dict):
                            row[5].pop("tape", None)          # the old opponent's stats
                            row[5].pop("c" + ("b" if j == 2 else "a"), None)
                        review_notes.append(
                            f"**{f['f']}**: opponent change: **{old} is out, {new_name} is in** against "
                            f"{row[i]}, per Wikipedia's “{title}”. Check the news before approving; "
                            f"if the site already has the newer name and Wikipedia is behind, remove this from the PR.")
                        return True
    return False


def fill_cards(fights, proposed, cache):
    """Add undercard bouts from each upcoming event's own Wikipedia article.
    One source, so they go to your approval pull request, never straight live."""
    soon = (TODAY + dt.timedelta(days=CARD_DAYS)).isoformat()
    added = 0
    for f in fights:
        if not (TODAY.isoformat() <= f["d"] <= soon):
            continue
        title, text = event_page(f)
        if not text:
            continue
        key = "card:" + title
        fp = fingerprint(text)
        if cache.get(key, {}).get("fp") == fp and not FORCE:
            bouts = cache[key]["bouts"]
        else:
            data = ask_json(CARD.format(date=f["d"]), f"Article: {title}\n\n{text}", max_tokens=12000) or {}
            page, bouts = norm(text), []
            for b in data.get("bouts", []):
                words = norm(b.get("words", ""))
                if b.get("a") and b.get("b") and in_source(words, page) \
                        and surname(b["a"]) in words and surname(b["b"]) in words:
                    bouts.append({k: b.get(k) for k in ("a", "b", "weight", "title", "sec")})
            cache[key] = {"fp": fp, "bouts": bouts, "checked": NOW.isoformat()}
        target = by_id(proposed, f["id"])
        have = [(r[0], r[2]) for s in target.get("card", []) for r in s.get("b", [])]
        new = [b for b in bouts if not any(same_bout((b["a"], b["b"]), h) for h in have)]
        new = [b for b in new if not swap_opponent(f, target, b, title)]
        if not new:
            continue
        for b in new:
            sec = b.get("sec") or "Main card"
            card = target.setdefault("card", [])
            section = next((s for s in card if s["sec"] == sec), None)
            if not section:
                section = {"sec": sec, "b": []}
                card.append(section)
            section["b"].append(bout_row(b))
        added += len(new)
        review_notes.append(f"**{f['f']}**: {len(new)} more bouts from Wikipedia's “{title}” "
                            f"({', '.join(b['a'].split()[-1] + ' vs ' + b['b'].split()[-1] for b in new[:6])}"
                            f"{'…' if len(new) > 6 else ''}).")
    order = {"Main card": 0, "Undercard": 1, "Prelims": 1, "Early prelims": 2}
    for f in proposed:
        if f.get("card"):
            f["card"].sort(key=lambda s: order.get(s["sec"], 1))
    log(f"- Fight cards: {added} more bouts found on event pages.")
    return added


# ---------- main ----------

def fight_week(fights):
    soon = NOW + dt.timedelta(days=7)
    return any(NOW - dt.timedelta(hours=12) <= fight_start(f) <= soon for f in fights)


def main():
    fights = load_json(FIGHTS_PATH, [])
    cache = load_json(CACHE_PATH, {})
    last = cache.get("_last_full_run")
    hours_since = (NOW - dt.datetime.fromisoformat(last)).total_seconds() / 3600 if last else 999
    if not FORCE and hours_since < 20 and not (fight_week(fights) and hours_since >= 0.9):
        print(f"Nothing to do: last full check {hours_since:.1f} hours ago and no fight this week.")
        REPORT_PATH.write_text("")
        return 0

    log(f"# Schedule check, {NOW:%Y-%m-%d %H:%M} UTC\n")
    sightings = []
    for src in SOURCES:
        text, where = source_text(src)
        if not text:
            continue
        fp = fingerprint(text)
        entry = cache.get(src["id"], {})
        if entry.get("fp") == fp and not FORCE:
            events = entry.get("events", [])
        else:
            try:
                events = extract(src, text, where)
            except Exception as e:   # one bad page shouldn't stop the run
                log(f"- Couldn't read events from {where}: {e.__class__.__name__}")
                continue
            cache[src["id"]] = {"fp": fp, "events": events, "checked": NOW.isoformat()}
        events = [e for e in events if TODAY.isoformat() <= e["date"]]
        log(f"- {where}: {len(events)} upcoming events.")
        sightings += [(domain("https://" + where if src.get("wiki") else where), e) for e in events]

    groups = cluster(sightings)
    proposed, safe, review = apply(fights, groups)
    try:
        review += fill_cards(fights, proposed, cache)
    except Exception as e:
        log(f"- Couldn't fill in fight cards: {e.__class__.__name__}")
    fights.sort(key=lambda f: f["d"])
    proposed.sort(key=lambda f: f["d"])
    save_json(FIGHTS_PATH, fights)
    save_json(REVIEW_PATH, proposed)
    cache["_last_full_run"] = NOW.isoformat()
    save_json(CACHE_PATH, cache)
    stamp("schedule")

    log(f"\n{safe} safe changes published, {review} waiting for your approval.")
    if review_notes:
        log("\n## Needs your approval\nMerge this pull request to apply all of these. To skip one, "
            "edit `site/data/fights.json` in the pull request first.\n")
        report.extend(f"- {n}" for n in review_notes)
    log("\nSources: Wikipedia (CC BY-SA) and the promotions' own schedule pages. "
        "Changes only go live by themselves when two different websites agree; "
        "anything from a single source waits in this pull request.")
    REPORT_PATH.write_text("\n".join(report) + "\n")
    with open(AGENT / "schedule_review_count", "w") as fh:
        fh.write(str(review))
    return 0


if __name__ == "__main__":
    sys.exit(main())
