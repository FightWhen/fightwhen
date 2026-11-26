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
                    save_json, slug, stamp, surname, wiki_clean, wiki_wikitext)

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
        if not bouts and not ev.get("name"):
            continue
        good.append({"name": ev.get("name") or f"{bouts[0]['a']} vs {bouts[0]['b']}",
                     "date": day.isoformat(), "start_utc": ev.get("start_utc"),
                     "venue": ev.get("venue"), "broadcaster": ev.get("broadcaster"),
                     "status": ev.get("status") or "scheduled", "bouts": bouts, "c": src["c"]})
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


def ev_main(ev):
    if ev["bouts"]:
        return ev["bouts"][0]["a"], ev["bouts"][0]["b"]
    return name_bout(ev.get("name"))


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
                log(f"- Seen on one site only, waiting for a second: {lead['name']} ({lead['date']}, {names})")
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

        for key, field in (("broadcaster", "w"), ("venue", "v")):
            val = agreed(group, lambda e, k=key: e[k], lambda a, b: norm(a) == norm(b))
            cur = fight.get(field, "")
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
        "A change is only made when two different websites agree.")
    REPORT_PATH.write_text("\n".join(report) + "\n")
    with open(AGENT / "schedule_review_count", "w") as fh:
        fh.write(str(review))
    return 0


if __name__ == "__main__":
    sys.exit(main())
