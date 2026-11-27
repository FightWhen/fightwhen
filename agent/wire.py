"""Breaking-news watcher: catches withdrawals and replacements within the hour.

Fights change in the news first (an injury, a replacement on short notice) and on
Wikipedia and promoter pages a day or more later. Every hour this reads the RSS
feeds that combat sports news sites publish for exactly this kind of reuse, picks
out headlines about a change to a fight on our schedule, and puts the change in a
pull request for you to approve. It never changes the live site by itself.
"""
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import unescape

import datetime as dt

from common import (AGENT, FIGHTS_PATH, NOW, TODAY, allowed, ask_json, domain, http, load_json,
                    norm, same_fighter, save_json, stamp, surname)

CACHE_PATH = AGENT / "wire_cache.json"
REVIEW_PATH = AGENT / "wire_review.json"
REPORT_PATH = AGENT / "wire_report.md"

# News sites' home pages. The robot finds each one's feed from the page itself,
# so a moved feed doesn't break it. Add or remove sites here.
SITES = [
    "https://www.boxingscene.com/",
    "https://www.ringmagazine.com/",
    "https://www.badlefthook.com/",
    "https://www.boxingnews24.com/",
    "https://boxingnewsonline.net/",
    "https://www.mmafighting.com/",
    "https://www.bloodyelbow.com/",
    "https://mmajunkie.usatoday.com/",
    "https://www.sherdog.com/",
    "https://www.fightmag.com/",
]
GUESSES = ["rss", "feed", "rss/index.xml", "rss/current", "feed/"]
DAYS_AHEAD = 60          # fights we care about
MAX_AGE_HOURS = 72       # ignore older headlines
MAX_ASKS = 25            # model calls per run, to keep costs small

CHANGE = re.compile(
    r"withdr[ae]w|withdraw|pull(s|ed)? out|out of (the|his|her|their)|replac|steps? in|stepping in|"
    r"short[- ]notice|injur|cancel|postpon|scrapped|called off|off the card|falls? through|fell through|"
    r"new opponent|forced out|ruled out|bout is off|fight is off|medical|failed (a )?drug|visa", re.I)

ASK = """You read one combat sports news item and decide whether it reports a change to a scheduled
fight. Use only the item. Do not use outside knowledge.

Reply with JSON only:
{"change": "replacement" | "withdrawal" | "cancelled" | "postponed" | "none",
 "out": "the fighter who is out, as named in the item, or null",
 "in": "the replacement, as named in the item, or null",
 "against": "the fighter who stays on the card, or null",
 "evidence": "a short span copied character for character from the item that states the change",
 "confirmed": true if the item states it as fact or "sources say", false if it is a rumour, a
              request ("wants to face"), a possibility or a callout}

"replacement" = someone new is named in place of a fighter. "withdrawal" = a fighter is out and no
replacement is named yet. If the item is about anything else (a past fight, a callout, a prediction,
an interview), answer "none"."""

report = []


def log(line):
    print(line)
    report.append(line)


# ---------- feeds ----------

def find_feeds(site, cache):
    known = cache.setdefault("feeds", {})
    if known.get(site):
        return known[site]
    feeds = []
    try:
        if allowed(site):
            r = http.get(site, timeout=20)
            for tag in re.findall(r"<link[^>]+>", r.text[:200000], re.I):
                if re.search(r"application/(rss|atom)\+xml", tag, re.I):
                    m = re.search(r'href=["\']([^"\']+)', tag)
                    if m:
                        href = unescape(m.group(1))
                        feeds.append(href if href.startswith("http") else site.rstrip("/") + "/" + href.lstrip("/"))
        if not feeds:
            for g in GUESSES:
                url = site + g
                if not allowed(url):
                    continue
                r = http.get(url, timeout=20)
                if r.ok and re.search(r"<(rss|feed)\b", r.text[:2000]):
                    feeds.append(url)
                    break
    except Exception as e:                       # one bad site never stops the run
        log(f"- Couldn't find a feed on {site}: {e.__class__.__name__}")
    feeds = [f for f in dict.fromkeys(feeds) if not re.search(r"comment", f, re.I)][:2]
    if feeds:
        known[site] = feeds
    else:
        log(f"- No news feed found on {site}.")
    return feeds


def text_of(el):
    return unescape(re.sub(r"<[^>]+>", " ", "".join(el.itertext()) if el is not None else "")).strip()


def read_feed(url):
    if not allowed(url):
        log(f"- Skipped {url}: the site's robots.txt says no.")
        return []
    r = http.get(url, timeout=20)
    if not r.ok:
        log(f"- {url} answered {r.status_code}.")
        return []
    root = ET.fromstring(r.content)
    items = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        get = {c.tag.split("}")[-1]: c for c in it}
        link = get.get("link")
        href = (link.get("href") if link is not None and link.get("href") else text_of(link))
        when = text_of(get.get("pubDate") or get.get("published") or get.get("updated"))
        try:
            ts = parsedate_to_datetime(when) if "," in when else dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            ts = None
        items.append({"title": text_of(get.get("title")), "link": href, "when": ts,
                      "summary": text_of(get.get("description") or get.get("summary") or get.get("content"))[:1500]})
    return items


# ---------- matching to our fights ----------

def our_fighters(fights):
    """(fight, section index, bout index, name) for every named fighter in the next DAYS_AHEAD days."""
    out = []
    horizon = (TODAY + dt.timedelta(days=DAYS_AHEAD)).isoformat()
    for f in fights:
        if not (TODAY.isoformat() <= f["d"] <= horizon):
            continue
        for si, s in enumerate(f.get("card", [])):
            for bi, b in enumerate(s.get("b", [])):
                for name in (b[0], b[2]):
                    if name and len(surname(name)) >= 4 and norm(name) not in ("tba", "tbd"):
                        out.append((f, si, bi, name))
    return out


def mentions(text, name):
    t = " " + norm(text) + " "
    return f" {surname(name)} " in t


def apply_change(fights, fid, si, bi, ch):
    """Change the bout in `fights` (the copy for review). Returns a note, or '' if nothing to do."""
    f = next(x for x in fights if x["id"] == fid)
    row = f["card"][si]["b"][bi]
    out_i = 0 if same_fighter(row[0], ch["out"]) else 2 if same_fighter(row[2], ch["out"]) else None
    if out_i is None:
        return ""
    stay = row[2 - out_i]
    if ch["change"] == "replacement" and ch.get("in"):
        if same_fighter(row[out_i], ch["in"]):
            return ""
        old = row[out_i]
        row[out_i], row[out_i + 1] = ch["in"], ""
        if len(row) > 5 and isinstance(row[5], dict):
            row[5].pop("tape", None)
            row[5].pop("c" + ("a" if out_i == 0 else "b"), None)
        return f"**{f['f']}**: **{old} is out, {ch['in']} is in** against {stay.rstrip('.')}."
    if ch["change"] == "withdrawal":
        if norm(row[out_i]) in ("tba", "opponent tba"):
            return ""
        old = row[out_i]
        row[out_i], row[out_i + 1] = "TBA", ""
        if len(row) > 5 and isinstance(row[5], dict):
            row[5].pop("tape", None)
        return f"**{f['f']}**: **{old} is out**, opponent for {stay.rstrip('.')} now TBA."
    # Cancelled or postponed: tell you, but leave the card alone. You decide.
    return f"**{f['f']}**: {row[0]} vs {row[2]} reported **{ch['change']}**. Not changed, please check and edit by hand."


# ---------- main ----------

def main():
    fights = load_json(FIGHTS_PATH, [])
    cache = load_json(CACHE_PATH, {})
    seen = set(cache.get("seen", []))
    handled = set(cache.get("handled", []))
    proposed = json.loads(json.dumps(fights))
    roster = our_fighters(fights)
    log(f"# News watch, {NOW:%Y-%m-%d %H:%M} UTC\n")
    log(f"Watching {len({id(r[0]) for r in roster})} upcoming fights, {len(roster)} fighters.\n")

    items = []
    for site in SITES:
        for url in find_feeds(site, cache):
            try:
                got = read_feed(url)
            except Exception as e:
                log(f"- Couldn't read {url}: {e.__class__.__name__}")
                continue
            items += [dict(i, src=domain(url)) for i in got]
            time.sleep(1)
    fresh = [i for i in items if i["link"] and i["link"] not in seen
             and (i["when"] is None or NOW - i["when"] <= dt.timedelta(hours=MAX_AGE_HOURS))]
    log(f"Read {len(items)} headlines, {len(fresh)} new.\n")

    asks, notes, sources = 0, {}, {}
    for item in fresh:
        seen.add(item["link"])
        text = f"{item['title']}\n{item['summary']}"
        if not CHANGE.search(text):
            continue
        hits = [r for r in roster if mentions(text, r[3])]
        if not hits or asks >= MAX_ASKS:
            continue
        asks += 1
        ch = ask_json(ASK, f"Source: {item['src']}\nHeadline: {item['title']}\n\n{item['summary']}", max_tokens=3000)
        if not isinstance(ch, dict) or ch.get("change") in (None, "none") or not ch.get("confirmed"):
            continue
        src = norm(text)
        if not ch.get("out") or not ch.get("evidence") or norm(ch["evidence"]) not in src \
                or not mentions(text, ch["out"]) or (ch.get("in") and not mentions(text, ch["in"])):
            log(f"- Threw out a reading of “{item['title']}”: names or quoted words aren't in the item.")
            continue
        for f, si, bi, name in hits:
            if not same_fighter(name, ch["out"]):
                continue
            row = f["card"][si]["b"][bi]
            other = row[2] if same_fighter(row[0], name) else row[0]
            if ch.get("against") and not same_fighter(other, ch["against"]) and norm(other) != "tba":
                continue                       # same surname, different bout
            key = f"{f['id']}|{surname(ch['out'])}|{surname(ch.get('in') or '')}|{ch['change']}"
            sources.setdefault(key, []).append(f"[{item['src']}]({item['link']})")
            if key in handled or key in notes:
                continue
            note = apply_change(proposed, f["id"], si, bi, ch)
            if note:
                notes[key] = note
                handled.add(key)

    cache["seen"] = sorted(seen)[-3000:]
    cache["handled"] = sorted(handled)[-500:]
    save_json(CACHE_PATH, cache)
    stamp("wire")

    if notes:
        log("## Changes in the news\nMerge this pull request to apply them. Each one links the reports it "
            "came from. Read at least one before approving; to skip one, edit `site/data/fights.json` in this "
            "pull request first.\n")
        for key, note in notes.items():
            log(f"- {note} Reported by: {', '.join(dict.fromkeys(sources[key]))}")
        save_json(REVIEW_PATH, proposed)
    else:
        log("No fight changes in the news.")
    log("\nSources: the news sites' own RSS feeds. Headlines and short summaries only; full articles are never copied.")
    REPORT_PATH.write_text("\n".join(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
