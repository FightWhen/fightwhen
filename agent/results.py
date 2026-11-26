"""FightWhen results agent.

On fight nights it checks every 15 minutes, from the card's start until about
nine hours later, and once a day it sweeps the last four days for any bout
still missing a result. For each bout it records the winner, how it ended and
the round, and updates both fighters' records.

Sources: the event's Wikipedia page and the promotions' own results pages from
agent/sources.json (only where robots.txt allows). Never BoxRec, Tapology or ESPN.

The rule, as with the schedule: a result is only published when two different
websites agree on the winner and how the fight ended, and every result must come
with the exact words from the page, checked word for word. Title fights always
wait for your approval, as does anything only one site reports.
"""

import copy
import datetime as dt
import os
import re
import sys

import requests

from common import (AGENT, FIGHTS_PATH, NOW, ask_json, domain, fetch_page, fight_start, in_source,
                    load_json, main_event, norm, same_fighter, save_json, stamp, surname, wiki_clean,
                    wiki_search, wiki_wikitext)

SOURCES = load_json(AGENT / "sources.json", {}).get("results", [])
CACHE_PATH = AGENT / "results_cache.json"
REVIEW_PATH = AGENT / "results_review.json"
REPORT_PATH = AGENT / "results_report.md"
WINDOW_HOURS = 9
SWEEP_DAYS = 4
FORCE = os.environ.get("FORCE_RUN", "").lower() == "true"
TITLE = re.compile(r"title|\bWBA\b|\bWBC\b|\bIBF\b|\bWBO\b|belt|championship|unification|BMF", re.I)

METHODS = {  # what the page may say -> (family, label shown on the site)
    "ko": ("KO", "KO"), "tko": ("KO", "TKO"), "rtd": ("KO", "TKO (corner stoppage)"),
    "ud": ("DEC", "unanimous decision"), "sd": ("DEC", "split decision"), "md": ("DEC", "majority decision"),
    "dec": ("DEC", "decision"), "sub": ("SUB", "submission"), "dq": ("DQ", "disqualification"),
    "draw": ("DRAW", "draw"), "nc": ("NC", "no contest"),
}

report, review_notes = [], []


def log(msg):
    print(msg)
    report.append(msg)


# ---------- which cards need checking ----------

def needs_result(fight):
    return [b for s in fight.get("card", []) for b in s.get("b", [])
            if b[0] and b[2] and not (len(b) > 5 and b[5].get("res"))]


def cards_to_check(fights, cache):
    live, sweep = [], []
    last_sweep = cache.get("_last_sweep")
    do_sweep = FORCE or not last_sweep or (NOW - dt.datetime.fromisoformat(last_sweep)).total_seconds() > 20 * 3600
    for f in fights:
        if not needs_result(f):
            continue
        start = fight_start(f)
        if start - dt.timedelta(minutes=15) <= NOW <= start + dt.timedelta(hours=WINDOW_HOURS):
            live.append(f)
        elif do_sweep and start + dt.timedelta(hours=WINDOW_HOURS) < NOW <= start + dt.timedelta(days=SWEEP_DAYS):
            sweep.append(f)
    return live, sweep, do_sweep


# ---------- reading sources ----------

def wiki_pages(fight):
    a, b = main_event(fight)
    queries = [fight.get("f", "")]
    if a and b:
        queries.append(f"{a} vs {b} {fight['d'][:4]}")
    titles = []
    for q in queries:
        try:
            titles += [t for t in wiki_search(q) if t not in titles]
        except requests.RequestException:
            pass
    year = fight["d"][:4]
    if fight["c"] == "boxing":
        titles.append(f"{year} in boxing")
    out = []
    for t in titles[:4]:
        try:
            text = wiki_clean(wiki_wikitext(t))
        except requests.RequestException:
            continue
        if a and b and surname(a) in norm(text) and surname(b) in norm(text):
            i = norm(text).find(surname(a))
            out.append(("en.wikipedia.org/wiki/" + t.replace(" ", "_"), text[max(0, i - 6000):i + 14000]))
    return out[:2]


def promo_pages(fight):
    out = []
    for src in SOURCES:
        if src["c"] != fight["c"]:
            continue
        text = fetch_page(src["url"], log)
        a, _ = main_event(fight)
        if text and a and surname(a) in norm(text):
            i = norm(text).find(surname(a))
            out.append((src["url"], text[max(0, i - 4000):i + 12000]))
    return out


READ = """You read one web page and report results for the bouts listed below, from the event
on {date}. Use ONLY this page; do not use outside knowledge. Skip any bout the page gives no
result for (for example because it hasn't happened yet).

Bouts:
{bouts}

For each bout with a result, give:
{{"a": fighter name as listed above, "b": the other fighter as listed above,
 "winner": the winner's name as listed above, or "draw" or "no contest",
 "method": one of KO, TKO, RTD, UD, SD, MD, DEC, SUB, DQ, DRAW, NC,
 "round": the round it ended as a number, or null for a decision or if not stated,
 "words": the exact words from the page that give this result, copied character for character}}

Reply with JSON only: {{"results": [ ... ]}}"""


def read_results(fight, where, text, bouts):
    listing = "\n".join(f"- {b[0]} vs {b[2]}" for b in bouts)
    data = ask_json(READ.format(date=fight["d"], bouts=listing), f"Page: {where}\n\n{text}", max_tokens=8000)
    if not data:
        return []
    page, out = norm(text), []
    for r in data.get("results", []):
        bout = next((b for b in bouts if same_fighter(b[0], r.get("a", "")) and same_fighter(b[2], r.get("b", ""))
                     or same_fighter(b[0], r.get("b", "")) and same_fighter(b[2], r.get("a", ""))), None)
        method = METHODS.get(str(r.get("method", "")).lower())
        words = norm(r.get("words", ""))
        if not bout or not method or not in_source(words, page):
            continue
        win = str(r.get("winner", "")).lower()
        if method[0] in ("DRAW", "NC") or win in ("draw", "no contest"):
            side = "d" if method[0] == "DRAW" or win == "draw" else "nc"
        elif same_fighter(bout[0], r["winner"]):
            side = "a"
        elif same_fighter(bout[2], r["winner"]):
            side = "b"
        else:
            continue
        if side in ("a", "b") and surname(bout[0 if side == "a" else 2]) not in words:
            continue   # the quoted words must name the winner
        rnd = r.get("round")
        rnd = int(rnd) if isinstance(rnd, (int, float)) or str(rnd).isdigit() else None
        out.append({"key": (bout[0], bout[2]), "w": side, "family": method[0], "label": method[1], "r": rnd})
    return out


# ---------- deciding ----------

def bump(record, kind):
    m = re.fullmatch(r"(\d+)-(\d+)(?:-(\d+))?(.*)", record or "")
    if not m:
        return record
    w, l, d, rest = int(m[1]), int(m[2]), int(m[3] or 0), m[4]
    w, l, d = (w + 1, l, d) if kind == "W" else (w, l + 1, d) if kind == "L" else (w, l, d + 1)
    return f"{w}-{l}" + (f"-{d}" if d or m[3] else "") + rest


def set_result(fight, key, res):
    for s in fight.get("card", []):
        for row in s.get("b", []):
            if (row[0], row[2]) == key:
                while len(row) < 6:
                    row.append("" if len(row) < 5 else {})
                row[5]["res"] = {k: res[k] for k in ("w", "m", "r") if res.get(k) is not None}
                if res["w"] == "a":
                    row[1], row[3] = bump(row[1], "W"), bump(row[3], "L")
                elif res["w"] == "b":
                    row[1], row[3] = bump(row[1], "L"), bump(row[3], "W")
                elif res["w"] == "d":
                    row[1], row[3] = bump(row[1], "D"), bump(row[3], "D")
                return


def decide(fight, key, readings, title):
    """readings: list of (domain, result). Returns (result, safe) or (None, None)."""
    for i, (d1, r1) in enumerate(readings):
        agree = [(d1, r1)] + [(d, r) for d, r in readings[i + 1:] if r["w"] == r1["w"] and r["family"] == r1["family"]]
        doms = {d for d, _ in agree}
        labels = {r["label"] for _, r in agree}
        rounds = {r["r"] for _, r in agree if r["r"]}
        res = {"w": r1["w"], "m": labels.pop() if len(labels) == 1 else ("decision" if r1["family"] == "DEC" else r1["label"]),
               "r": rounds.pop() if len(rounds) == 1 else None}
        if r1["family"] == "DEC":
            res["r"] = None
        if len(doms) >= 2:
            return res, not title
    if readings:
        return readings[0][1] | {"m": readings[0][1]["label"]}, False
    return None, None


def describe(fight, key, res):
    who = {"a": key[0], "b": key[1]}.get(res["w"])
    how = res["m"] + (f", round {res['r']}" if res.get("r") else "")
    return f"{who} beat {key[1] if res['w'] == 'a' else key[0]} by {how}" if who else f"{key[0]} vs {key[1]}: {how}"


def main():
    fights = load_json(FIGHTS_PATH, [])
    cache = load_json(CACHE_PATH, {})
    live, sweep, swept = cards_to_check(fights, cache)
    if not live and not sweep:
        print("No card is on right now and nothing is missing a result.")
        REPORT_PATH.write_text("")
        return 0

    log(f"# Results check, {NOW:%Y-%m-%d %H:%M} UTC\n")
    proposed = copy.deepcopy(fights)
    safe = review = 0
    for fight in live + sweep:
        bouts = needs_result(fight)
        pages = wiki_pages(fight) + promo_pages(fight)
        readings = {}
        for where, text in pages:
            try:
                for r in read_results(fight, where, text, bouts):
                    readings.setdefault(r["key"], []).append((domain("https://" + where if "://" not in where else where), r))
            except Exception as e:
                log(f"- Couldn't read results from {where}: {e.__class__.__name__}")
        log(f"- {fight['f']}: read {len(pages)} pages, results for {len(readings)} of {len(bouts)} bouts.")
        pfight = next(f for f in proposed if f["id"] == fight["id"])
        for key, rs in readings.items():
            row = next(b for b in bouts if (b[0], b[2]) == key)
            desc = row[4] if len(row) > 4 else ""
            if key == main_event(fight):
                desc += " " + fight.get("t", "") + " " + fight.get("f", "")
            title = bool(TITLE.search(desc))
            res, ok = decide(fight, key, rs, title)
            if not res:
                continue
            if ok:
                set_result(fight, key, res)
                set_result(pfight, key, res)
                safe += 1
                log(f"  - Published: {describe(fight, key, res)} ({', '.join(sorted({d for d, _ in rs}))}).")
            else:
                set_result(pfight, key, res)
                review += 1
                why = "title fight" if title and len({d for d, _ in rs}) >= 2 else "only one site so far"
                review_notes.append(f"**{fight['f']}**: {describe(fight, key, res)} ({why}; {', '.join(sorted({d for d, _ in rs}))}).")

    if swept:
        cache["_last_sweep"] = NOW.isoformat()
    save_json(CACHE_PATH, cache)
    save_json(FIGHTS_PATH, fights)
    save_json(REVIEW_PATH, proposed)
    if safe:
        stamp("results")
    log(f"\n{safe} results published, {review} waiting for your approval.")
    if review_notes:
        log("\n## Needs your approval\nMerge this pull request to publish these. Title fights always wait "
            "for you, and so does any result only one website reports.\n")
        report.extend(f"- {n}" for n in review_notes)
    log("\nSources: Wikipedia (CC BY-SA) and the promotions' own results pages.")
    REPORT_PATH.write_text("\n".join(report) + "\n")
    (AGENT / "results_review_count").write_text(str(review))
    return 0


if __name__ == "__main__":
    sys.exit(main())
