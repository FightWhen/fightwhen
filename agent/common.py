"""Shared helpers for the FightWhen schedule, results and health agents."""

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import time
import unicodedata
import urllib.robotparser
from html import unescape

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
SITE_DATA = ROOT / "site" / "data"
FIGHTS_PATH = SITE_DATA / "fights.json"
STATUS_PATH = SITE_DATA / "status.json"

UA = "FightWhenBot/1.0 (+https://fightwhen.com/about; fight schedules and results)"
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date()

CONFIG = json.loads((AGENT / "channels.json").read_text())
FAST_MODEL = os.environ.get("CLAUDE_FAST_MODEL") or CONFIG.get("screen_model", "claude-haiku-5-5")

http = requests.Session()
http.headers["User-Agent"] = UA

_claude = None


def claude():
    """The Claude client, or None when no API key is set (dry runs and tests)."""
    global _claude
    if _claude is None and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        _claude = anthropic.Anthropic()
    return _claude


def load_json(path, default):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, value):
    pathlib.Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n")


def slug(name):
    name = name.translate(str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ß": "ss", "æ": "ae"}))
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[’'.]", "", s.lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def norm(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    text = text.replace("’", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


def surname(name):
    parts = [p for p in re.split(r"\s+", norm(name)) if p not in ("jr", "jr.", "sr", "ii", "iii")]
    return parts[-1] if parts else ""


def same_fighter(a, b):
    """Loose match: same surname, and first initials agree when both are given."""
    na, nb = norm(a).split(), norm(b).split()
    if not na or not nb or surname(a) != surname(b):
        return False
    return len(na) == 1 or len(nb) == 1 or na[0][0] == nb[0][0]


def same_bout(x, y):
    """x, y: (name_a, name_b) in any order."""
    return (same_fighter(x[0], y[0]) and same_fighter(x[1], y[1])) or \
           (same_fighter(x[0], y[1]) and same_fighter(x[1], y[0]))


def domain(url):
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    return ".".join(host.split(".")[-2:])


# ---------- fetching, politely ----------

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
    html = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|td|th|h\d|dt|dd|section|article)>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def fetch_page(url, log):
    """Plain text of a web page, or '' if robots.txt says no or it fails."""
    if not allowed(url):
        log(f"- Skipped {url}: the site's robots.txt says no.")
        return ""
    try:
        r = http.get(url, timeout=25)
    except requests.RequestException as e:
        log(f"- Couldn't open {url}: {e.__class__.__name__}")
        return ""
    time.sleep(1)
    if not r.ok:
        log(f"- {url} answered {r.status_code}.")
        return ""
    return page_text(r.text)


def wiki_wikitext(title):
    r = http.get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main",
        "titles": title, "redirects": 1, "format": "json", "formatversion": 2}, timeout=25)
    pages = r.json().get("query", {}).get("pages", [])
    if not pages or "revisions" not in pages[0]:
        return ""
    return pages[0]["revisions"][0]["slots"]["main"]["content"]


def wiki_search(query, limit=3):
    r = http.get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "list": "search", "srsearch": query, "srlimit": limit,
        "format": "json"}, timeout=25)
    return [h["title"] for h in r.json().get("query", {}).get("search", [])]


def wiki_clean(text):
    """Make wikitext readable for the model: drop refs and comments, keep tables."""
    text = re.sub(r"(?s)<!--.*?-->", "", text)
    text = re.sub(r"(?s)<ref[^>/]*/>|<ref[^>]*>.*?</ref>", "", text)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\{\{(?:flagicon|flag|nowrap|small)\|([^}|]*)[^}]*\}\}", r"\1", text, flags=re.I)
    return re.sub(r"'{2,}", "", text)


def fingerprint(text):
    return hashlib.sha1(text.encode()).hexdigest()[:16]


# ---------- asking the model, with evidence that is checked ----------

def ask_json(system, user, max_tokens=4000, model=None):
    c = claude()
    if not c:
        return None
    msg = c.messages.create(model=model or FAST_MODEL, max_tokens=max_tokens, system=system,
                            messages=[{"role": "user", "content": user}])
    raw = "".join(b.text for b in msg.content if b.type == "text")  # skip thinking blocks
    start = min([i for i in (raw.find("{"), raw.find("[")) if i >= 0], default=-1)
    if start < 0:
        return None
    end = max(raw.rfind("}"), raw.rfind("]"))
    try:
        return json.loads(raw[start:end + 1])
    except ValueError:
        return None


def in_source(evidence, source_norm):
    ev = norm(evidence)
    return len(ev) >= 4 and ev in source_norm


# ---------- schedule helpers ----------

def fight_start(fight):
    """Best guess at when a card starts, in UTC."""
    if fight.get("utc"):
        return dt.datetime.fromisoformat(fight["utc"].replace("Z", "+00:00"))
    times = [s["t"] for s in fight.get("card", []) if s.get("t")]
    if times:
        return dt.datetime.fromisoformat(min(times).replace("Z", "+00:00"))
    d = dt.date.fromisoformat(fight["d"])
    return dt.datetime(d.year, d.month, d.day, 23, 0, tzinfo=dt.timezone.utc)  # ~6 PM US Central


def main_event(fight):
    for sec in fight.get("card", []):
        for b in sec.get("b", []):
            return b[0], b[2]
    m = re.match(r"(?:.*?:\s*)?(.+?)\s+vs\.?\s+(.+)$", fight.get("f", ""), re.I)
    return (m.group(1), m.group(2)) if m else (None, None)


def stamp(key):
    status = load_json(STATUS_PATH, {})
    status[key] = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    save_json(STATUS_PATH, status)
