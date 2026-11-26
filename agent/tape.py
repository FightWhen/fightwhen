"""FightWhen tale-of-the-tape agent.

For every bout in the next few weeks, fills in each fighter's age, height,
reach, stance and country from sources we are allowed to use:

  1. Wikidata (free to reuse): birth date, height, country.
  2. Wikipedia (free to reuse with credit): the fighter's infobox.
  3. The promotion's own athlete page (UFC, ONE, BKFC, PBC, PFL), only
     where the site's robots.txt allows it. One request per fighter per run.

It never reads BoxRec or Tapology: their terms forbid automated copying.

Claude (the cheap model) reads each source and pulls out the numbers. Every
number has to come with the exact words it was read from, and those words are
checked against the source text, so nothing can be invented. If two sources
disagree, the field stays blank and the bout is listed in the report for you
to settle by hand. Values you typed yourself are never overwritten.

Changes go to site/data/fights.json through a pull request you approve.
"""

import datetime as dt
import json
import os
import pathlib
import re
import sys
import time
import unicodedata
import urllib.robotparser
from html import unescape

import anthropic
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIGHTS_PATH = ROOT / "site" / "data" / "fights.json"
AGENT = ROOT / "agent"
CACHE_PATH = AGENT / "tape_cache.json"      # what each source said, with dates
OVERRIDES_PATH = AGENT / "tape_overrides.json"  # your hand-settled values win
REPORT_PATH = AGENT / "tape_report.md"

CONFIG = json.loads((AGENT / "channels.json").read_text())
MODEL = CONFIG.get("screen_model", "claude-haiku-5-5")
DAYS_AHEAD = int(os.environ.get("TAPE_DAYS_AHEAD", "35"))
MAX_FIGHTERS = int(os.environ.get("TAPE_MAX_FIGHTERS", "60"))
REFRESH_DAYS = 30   # look a fighter up again after this many days
UA = "FightWhenBot/1.0 (+https://fightwhen.com/about; stats for fight cards)"
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date()

FIELDS = ["dob", "height_in", "reach_in", "stance", "country"]
STANCES = {"orthodox": "Orthodox", "southpaw": "Southpaw", "switch": "Switch"}

# Promotion athlete pages, by schedule category. {slug} is the fighter's name
# in lower case with dashes. Only used when robots.txt allows it.
PROMO_PAGES = {
    "ufc": ["https://www.ufc.com/athlete/{slug}"],
    "one": ["https://www.onefc.com/athletes/{slug}/"],
    "bkfc": ["https://www.bkfc.com/fighters/{slug}"],
    "pfl": ["https://pflmma.com/fighter/{slug}"],
    "boxing": ["https://www.premierboxingchampions.com/{slug}"],
}

report = []
http = requests.Session()
http.headers["User-Agent"] = UA
claude = anthropic.Anthropic() if os.environ.get("ANTHROPIC_API_KEY") else None


def log(msg):
    print(msg)
    report.append(msg)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n")


def slug(name):
    name = name.translate(str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ß": "ss", "æ": "ae"}))
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[’'.]", "", s.lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def norm(text):
    text = unicodedata.normalize("NFKC", text).replace("’", "'").replace("″", '"').replace("′", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


# ---------- sources ----------

_robots = {}


def allowed(url):
    base = re.match(r"https?://[^/]+", url).group(0)
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = http.get(base + "/robots.txt", timeout=15)
            rp.parse(r.text.splitlines() if r.ok else [])
        except requests.RequestException:
            rp.parse(["User-agent: *", "Disallow: /"])   # can't check, so don't fetch
        _robots[base] = rp
    return _robots[base].can_fetch(UA, url)


def page_text(html):
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|td|th|h\d|dt|dd)>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def wikidata(name, sport_words):
    """Find the fighter on Wikidata. Returns (facts, wikipedia_title) or (None, None)."""
    api = "https://www.wikidata.org/w/api.php"
    r = http.get(api, params={"action": "wbsearchentities", "search": name, "language": "en",
                              "type": "item", "limit": 7, "format": "json"}, timeout=20)
    for hit in r.json().get("search", []):
        desc = (hit.get("description") or "").lower()
        if not any(w in desc for w in sport_words):
            continue
        ent = http.get(api, params={"action": "wbgetentities", "ids": hit["id"], "format": "json",
                                    "props": "claims|sitelinks"}, timeout=20).json()["entities"][hit["id"]]
        claims = ent.get("claims", {})
        facts = {}

        def first(prop):
            for c in claims.get(prop, []):
                if c.get("rank") != "deprecated" and c["mainsnak"].get("datavalue"):
                    return c["mainsnak"]["datavalue"]["value"]

        born = first("P569")
        if born and born.get("precision", 0) >= 11:
            facts["dob"] = born["time"][1:11]
        height = first("P2048")
        if height:
            amount, unit = float(height["amount"]), height.get("unit", "")
            if unit.endswith("Q174728"):        # centimetre
                facts["height_in"] = round(amount / 2.54 * 2) / 2
            elif unit.endswith("Q11573"):       # metre
                facts["height_in"] = round(amount * 100 / 2.54 * 2) / 2
            elif unit.endswith("Q218593"):      # inch
                facts["height_in"] = amount
        countries = [c["mainsnak"]["datavalue"]["value"]["id"] for c in claims.get("P27", [])
                     if c.get("rank") != "deprecated" and c["mainsnak"].get("datavalue")]
        if len(countries) == 1:                 # dual nationals get settled by hand
            cent = http.get(api, params={"action": "wbgetentities", "ids": countries[0],
                                         "props": "claims", "format": "json"}, timeout=20).json()
            iso = cent["entities"][countries[0]]["claims"].get("P298", [])
            if iso:
                facts["country"] = iso[0]["mainsnak"]["datavalue"]["value"]
        title = ent.get("sitelinks", {}).get("enwiki", {}).get("title")
        return facts, title
    return None, None


def wikipedia_infobox(title):
    r = http.get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main",
        "titles": title, "format": "json", "formatversion": 2}, timeout=20)
    pages = r.json().get("query", {}).get("pages", [])
    if not pages or "revisions" not in pages[0]:
        return ""
    text = pages[0]["revisions"][0]["slots"]["main"]["content"]
    m = re.search(r"\{\{\s*Infobox", text, re.I)
    if not m:
        return ""
    depth, i = 0, m.start()          # take the whole infobox, nested templates included
    while i < len(text) - 1:
        pair = text[i:i + 2]
        if pair == "{{":
            depth, i = depth + 1, i + 2
            continue
        if pair == "}}":
            depth, i = depth - 1, i + 2
            if depth == 0:
                break
            continue
        i += 1
    return text[m.start():i][:6000]


def promo_page(category, name):
    for pattern in PROMO_PAGES.get(category, []):
        url = pattern.format(slug=slug(name))
        if not allowed(url):
            log(f"- Skipped {url}: the site's robots.txt says no.")
            continue
        try:
            r = http.get(url, timeout=20)
        except requests.RequestException:
            continue
        if r.ok:
            text = page_text(r.text)
            if norm(name.split()[-1]) in norm(text):
                return url, text[:8000]
        time.sleep(1)
    return None, ""


# ---------- reading a source ----------

READ_PROMPT = """You read one source about a combat sports fighter and pull out their physical stats.
Only report what this source states about THIS fighter. Do not use outside knowledge, do not
estimate, do not convert a value you are unsure of. For each value, copy the exact words from
the source it came from into "evidence" (a short span, copied character for character).

Reply with JSON only:
{"dob": {"value": "YYYY-MM-DD", "evidence": "..."} or null,
 "height_in": {"value": number of inches, "evidence": "..."} or null,
 "reach_in": {"value": number of inches, "evidence": "..."} or null,
 "stance": {"value": "Orthodox" | "Southpaw" | "Switch", "evidence": "..."} or null,
 "country": {"value": "ISO 3166 alpha-3 code of the country they represent", "evidence": "..."} or null}
Convert centimetres to inches (divide by 2.54, round to the nearest half inch)."""


def read_source(name, source_text):
    if not claude or not source_text:
        return {}
    msg = claude.messages.create(
        model=MODEL, max_tokens=4000, system=READ_PROMPT,
        messages=[{"role": "user", "content": f"Fighter: {name}\n\nSource:\n{source_text}"}])
    raw = "".join(b.text for b in msg.content if b.type == "text")  # skip thinking blocks
    try:
        data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except ValueError:
        return {}
    out, src = {}, norm(source_text)
    for field in FIELDS:
        item = data.get(field)
        if not isinstance(item, dict) or item.get("value") in (None, ""):
            continue
        ev = norm(str(item.get("evidence", "")))
        if len(ev) < 2 or ev not in src:
            log(f"- Threw out {field} for {name}: the quoted words aren't in the source.")
            continue
        value = item["value"]
        if field in ("height_in", "reach_in"):
            try:
                value = round(float(value) * 2) / 2
            except (TypeError, ValueError):
                continue
            if not (48 <= value <= 96):
                continue
        if field == "stance":
            value = STANCES.get(str(value).lower())
            if not value:
                continue
        if field == "dob" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
            continue
        if field == "country" and not re.fullmatch(r"[A-Z]{3}", str(value)):
            continue
        out[field] = value
    return out


# ---------- combining sources ----------

def agree(field, a, b):
    if field in ("height_in", "reach_in"):
        return abs(a - b) <= 0.5
    return a == b


def combine(name, readings):
    """readings: list of (source_label, {field: value}). Returns (values, conflicts)."""
    values, conflicts = {}, []
    for field in FIELDS:
        seen = [(label, r[field]) for label, r in readings if field in r]
        if not seen:
            continue
        if all(agree(field, seen[0][1], v) for _, v in seen[1:]):
            values[field] = seen[0][1]
        else:
            conflicts.append(f"{field.replace('_in', '')}: " + ", ".join(f"{v} ({l})" for l, v in seen))
    return values, conflicts


def feet(inches):
    if inches is None:
        return ""
    ft, rest = divmod(inches, 12)
    rest = f"{rest:g}".replace(".5", "½")
    return f"{int(ft)}′{rest}″"


def age_on(dob, day):
    b = dt.date.fromisoformat(dob)
    return str(day.year - b.year - ((day.month, day.day) < (b.month, b.day)))


# ---------- main ----------

SPORT_WORDS = {
    "boxing": ["boxer"],
    "misfits": ["boxer", "influencer", "youtuber", "personality", "fighter", "football"],
    "bkfc": ["bare-knuckle", "bare knuckle", "fighter", "mixed martial", "boxer"],
}
DEFAULT_WORDS = ["mixed martial", "fighter", "kickboxer", "muay thai", "grappler", "jiu-jitsu", "boxer"]


def main():
    fights = load_json(FIGHTS_PATH, [])
    cache = load_json(CACHE_PATH, {})
    overrides = load_json(OVERRIDES_PATH, {})
    horizon = TODAY + dt.timedelta(days=DAYS_AHEAD)
    looked_up, changed, to_settle = 0, 0, []

    log(f"# Fighter stats run, {TODAY.isoformat()}\n")
    for fight in fights:
        day = dt.date.fromisoformat(fight["d"])
        if not (TODAY <= day <= horizon):
            continue
        for section in fight.get("card", []):
            for bout in section.get("b", []):
                while len(bout) < 6:
                    bout.append("" if len(bout) < 5 else {})
                extra = bout[5]
                tape = extra.setdefault("tape", {"a": ["", "", "", ""], "b": ["", "", "", ""]})
                for side, idx in (("a", 0), ("b", 2)):
                    name = bout[idx].strip()
                    if not name or name.lower() in ("tba", "tbd", "opponent tbd"):
                        continue
                    key = slug(name)
                    entry = cache.get(key, {})
                    stale = not entry or (TODAY - dt.date.fromisoformat(entry["checked"])).days >= REFRESH_DAYS
                    if stale and looked_up < MAX_FIGHTERS:
                        looked_up += 1
                        readings = []
                        try:
                            facts, title = wikidata(name, SPORT_WORDS.get(fight["c"], DEFAULT_WORDS))
                            if facts:
                                readings.append(("Wikidata", facts))
                            if title:
                                readings.append((f"Wikipedia: {title}", read_source(name, wikipedia_infobox(title))))
                            url, text = promo_page(fight["c"], name)
                            if url:
                                readings.append((url, read_source(name, text)))
                        except (requests.RequestException, anthropic.APIError, KeyError) as e:
                            log(f"- Couldn't look up {name}: {e}")
                            continue
                        values, conflicts = combine(name, [r for r in readings if r[1]])
                        entry = {"checked": TODAY.isoformat(), "values": values, "conflicts": conflicts,
                                 "sources": [label for label, r in readings if r]}
                        cache[key] = entry
                        time.sleep(1)

                    vals = {**entry.get("values", {}), **overrides.get(key, {})}
                    if entry.get("conflicts") and not overrides.get(key):
                        to_settle.append(f"- **{name}** ({fight['f']}): " + "; ".join(entry["conflicts"]))

                    new = [age_on(vals["dob"], day) if vals.get("dob") else "",
                           feet(vals.get("height_in")),
                           f"{vals['reach_in']:g}″" if vals.get("reach_in") else "",
                           vals.get("stance", "")]
                    current = tape.setdefault(side, ["", "", "", ""])
                    for i in range(4):
                        # Ages are always recomputed from the birth date; other boxes
                        # are only filled when empty, so your typed values stay.
                        if new[i] and (not current[i] or (i == 0 and vals.get("dob"))) and current[i] != new[i]:
                            current[i] = new[i]
                            changed += 1
                    if vals.get("country") and not extra.get("c" + side):
                        extra["c" + side] = vals["country"]
                        changed += 1

                if not any(tape["a"]) and not any(tape["b"]):
                    del extra["tape"]

    save_json(CACHE_PATH, cache)
    save_json(FIGHTS_PATH, fights)

    log(f"Looked up {looked_up} fighters and filled {changed} boxes.\n")
    if to_settle:
        log("## Sources disagree, please settle by hand")
        log("Add the right values to `agent/tape_overrides.json`, keyed by the fighter's slug, e.g.\n"
            '`"sergei-pavlovich": {"stance": "Orthodox"}`. Those boxes stay blank until you do.\n')
        report.extend(sorted(set(to_settle)))
    log("\nSources: Wikidata and Wikipedia (CC BY-SA) and the promotions' own athlete pages. "
        "BoxRec and Tapology are never read.")
    REPORT_PATH.write_text("\n".join(report) + "\n")


if __name__ == "__main__":
    sys.exit(main())
