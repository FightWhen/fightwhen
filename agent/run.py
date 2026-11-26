"""FightWhen news agent.

Checks the YouTube channels in channels.json for new videos, reads their
transcripts, and asks Claude to write a short news story when a video has
something fans care about. Every exact quote is checked against the
transcript before a story is kept. New stories go into site/data/stories.json
through a pull request you approve (or straight to the site if AUTO_PUBLISH
is "true").
"""

import datetime as dt
import json
import os
import pathlib
import re
import sys
import unicodedata

import anthropic
import requests
from youtube_transcript_api import YouTubeTranscriptApi

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "site" / "data"
AGENT = ROOT / "agent"
STATE_PATH = AGENT / "state.json"
REPORT_PATH = AGENT / "last_run.md"

CONFIG = json.loads((AGENT / "channels.json").read_text())
PROMPT = (AGENT / "prompt.md").read_text()
WRITE_MODEL = os.environ.get("CLAUDE_MODEL") or CONFIG.get("write_model", "claude-sonnet-5-5")
SCREEN_MODEL = CONFIG.get("screen_model", "claude-haiku-5-5")
FIGHTERS = json.loads((AGENT / "fighters.json").read_text())
YT_KEY = os.environ.get("YOUTUBE_API_KEY", "")
NOW = dt.datetime.now(dt.timezone.utc)

CATEGORIES = ["boxing", "ufc", "pfl", "one", "bkfc", "misfits", "k1", "rizin"]
MOODS = ["Bold prediction", "Bad intentions", "Mind games", "Hungry", "Defiant",
         "Unfinished business", "Calm before the storm", "Hype", "Card change",
         "Result", "Bad blood", "Respect", "Injury", "Callout"]

report = []


def redact(text):
    """Never let a key reach the log or the public pull request."""
    text = re.sub(r"(key|token|password)=[^&\s]+", r"\1=***", str(text), flags=re.I)
    for secret in (os.environ.get(n) for n in ("YOUTUBE_API_KEY", "ANTHROPIC_API_KEY", "PROXY_PASSWORD")):
        if secret:
            text = text.replace(secret, "***")
    return text


def log(msg):
    msg = redact(msg)
    print(msg)
    report.append(msg)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n")


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------- when to run ----------

def fight_night(fights):
    """True from 1 hour before a timed card until 5 hours after it, and all
    day on the date of a card with no announced time."""
    for f in fights:
        if f.get("utc"):
            start = parse_time(f["utc"])
            if start - dt.timedelta(hours=1) <= NOW <= start + dt.timedelta(hours=5):
                return True
        elif f.get("d") == NOW.date().isoformat():
            return True
    return False


def should_run(state, fights):
    if os.environ.get("FORCE_RUN") == "true":
        return True
    if fight_night(fights):
        return True
    last = state.get("last_full_run")
    if not last:
        return True
    hours = CONFIG.get("normal_hours_between_runs", 3)
    return NOW - parse_time(last) >= dt.timedelta(hours=hours)


# ---------- YouTube ----------

def channel_id(channel, state):
    """Find a channel's id from its @handle, or by searching its name, once."""
    cache = state.setdefault("channel_ids", {})
    key = channel.get("handle") or channel["name"]
    if key in cache:
        return cache[key]
    found = None
    if channel.get("handle"):
        r = requests.get("https://www.googleapis.com/youtube/v3/channels",
                         params={"part": "id", "forHandle": channel["handle"], "key": YT_KEY}, timeout=30)
        r.raise_for_status()
        items = r.json().get("items", [])
        found = items[0]["id"] if items else None
    if not found:
        r = requests.get("https://www.googleapis.com/youtube/v3/search",
                         params={"part": "snippet", "type": "channel", "q": channel["name"],
                                 "maxResults": 1, "key": YT_KEY}, timeout=30)
        r.raise_for_status()
        items = r.json().get("items", [])
        if items:
            found = items[0]["snippet"]["channelId"]
            log(f"Matched '{channel['name']}' to YouTube channel '{items[0]['snippet']['channelTitle']}'")
    if not found:
        raise ValueError(f"no YouTube channel found for {key}")
    cache[key] = found
    return found


def new_videos(channel, state):
    """Latest uploads from one channel that we haven't processed yet."""
    uploads = "UU" + channel_id(channel, state)[2:]
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/playlistItems",
        params={"part": "snippet,contentDetails", "playlistId": uploads,
                "maxResults": 15, "key": YT_KEY},
        timeout=30,
    )
    r.raise_for_status()
    cutoff = NOW - dt.timedelta(days=CONFIG.get("max_video_age_days", 3))
    out = []
    for item in r.json().get("items", []):
        vid = item["contentDetails"]["videoId"]
        published = parse_time(item["contentDetails"].get("videoPublishedAt")
                               or item["snippet"]["publishedAt"])
        if vid in state["seen"] or published < cutoff:
            continue
        out.append({
            "id": vid,
            "title": item["snippet"]["title"],
            "description": item["snippet"].get("description", "")[:1500],
            "published": published.isoformat().replace("+00:00", "Z"),
            "channel": channel["name"],
        })
    return out


def fight_week_names(fights):
    """Fighters in headline bouts happening in the next 7 days."""
    names = []
    for f in fights:
        day = dt.date.fromisoformat(f["d"])
        if 0 <= (day - NOW.date()).days <= 7 and (f.get("big") or f.get("card")):
            name = re.sub(r"^[A-Za-z0-9 -]+: ", "", f["f"])
            names += [s.strip() for s in re.split(r"\s+vs\.?\s+", name) if s.strip()]
    return names


def search_videos(name, state):
    """Newest YouTube videos about one fighter from the last day (costs more quota)."""
    r = requests.get("https://www.googleapis.com/youtube/v3/search",
                     params={"part": "snippet", "type": "video", "order": "date", "q": f'"{name}"',
                             "publishedAfter": (NOW - dt.timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                             "maxResults": CONFIG.get("search_results_per_fighter", 8), "key": YT_KEY}, timeout=30)
    r.raise_for_status()
    out = []
    for item in r.json().get("items", []):
        vid = item["id"]["videoId"]
        if vid in state["seen"]:
            continue
        sn = item["snippet"]
        out.append({"id": vid, "title": sn["title"], "description": sn.get("description", "")[:1500],
                    "published": sn["publishedAt"], "channel": sn["channelTitle"]})
    return out


def watch_words(fights):
    """Names and phrases worth paying for a transcript."""
    words = set(w.lower() for w in CONFIG.get("extra_keywords", []))
    for n in FIGHTERS:
        words.add(n.lower())
        last = n.split()[-1].lower()
        if len(last) >= 5 and last not in CONFIG.get("too_common_names", []):
            words.add(last)
    for f in fights:
        name = re.sub(r"^[A-Za-z0-9 -]+: ", "", f["f"])
        for side in re.split(r"\s+vs\.?\s+", name):
            if side.strip():
                words.add(side.strip().lower())
    return words


def looks_relevant(video, words):
    text = (video["title"] + " " + video["description"]).lower()
    return any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", text) for w in words)


def transcript(video_id):
    """Timestamped transcript text, or None if YouTube has none."""
    kwargs = {}
    user, pw = os.environ.get("PROXY_USERNAME"), os.environ.get("PROXY_PASSWORD")
    if user and pw:
        from youtube_transcript_api.proxies import WebshareProxyConfig
        kwargs["proxy_config"] = WebshareProxyConfig(proxy_username=user, proxy_password=pw)
    try:
        api = YouTubeTranscriptApi(**kwargs)
        fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
        lines = [(s.start, s.text) for s in fetched]
    except Exception as exc:  # no captions, blocked, private video, etc.
        log(f"  no transcript for {video_id}: {type(exc).__name__}")
        return None
    return "\n".join(f"[{int(t)}s] {txt}" for t, txt in lines)


# ---------- quote checking ----------

def norm(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower().replace("gonna", "going to").replace("wanna", "want to")
    text = re.sub(r"\[\d+s\]", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def quotes_in(text):
    return re.findall(r"[“\"]([^”\"]+)[”\"]", text)


def verify(story, transcript_text):
    """Every quoted phrase must appear word for word in the transcript."""
    src = norm(transcript_text)
    phrases = [story.get("q", "")]
    for p in story.get("body", []):
        phrases += quotes_in(p)
    bad = [p for p in phrases if p and norm(p) not in src]
    return bad


# ---------- Claude ----------

SCREEN_PROMPT = """You screen YouTube videos for a combat sports news site. Given a video's title, channel and the start of its transcript, decide if a fighter, trainer or promoter says something new that fans would care about: a prediction, callout, trash talk, injury, opponent or date change, result, reaction, retirement or signing. Say no to highlight reels, full fights, fan reaction or betting videos, and to channels just repeating news without a fighter or insider speaking. Reply with JSON only: {"keep": true or false, "who": "main speaker or empty", "why": "a few words"}"""


def json_answer(msg):
    """The JSON object in the model's answer, ignoring thinking blocks and code fences."""
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise json.JSONDecodeError("no JSON object in answer", raw, 0)
    return json.loads(raw[start:end + 1])


def screen(video, text):
    """Cheap first look so only real news reaches the writer model."""
    msg = anthropic.Anthropic().messages.create(
        model=SCREEN_MODEL, max_tokens=2000, system=SCREEN_PROMPT,
        messages=[{"role": "user", "content": f"Title: {video['title']}\nChannel: {video['channel']}\nDescription: {video['description'][:500]}\n\nTranscript start:\n{text[:12000]}"}])
    try:
        return json_answer(msg)
    except json.JSONDecodeError:
        return {"keep": False, "why": "unreadable screen answer"}


def write_story(video, text, fights, recent):
    fight_list = "\n".join(
        f'- id "{f["id"]}": {f["f"]} ({f["d"]}, {f["c"]})' for f in fights)
    recent_list = "\n".join(f"- {s['h']}" for s in recent[:25])
    user = f"""VIDEO
Title: {video['title']}
Channel: {video['channel']}
Published: {video['published']}
Description: {video['description']}

UPCOMING FIGHTS (use one of these ids for "fid", or null)
{fight_list}

HEADLINES ALREADY ON THE SITE (do not repeat these stories)
{recent_list}

TRANSCRIPT (timestamps in seconds)
{text[:120000]}"""
    msg = anthropic.Anthropic().messages.create(
        model=WRITE_MODEL,
        max_tokens=12000,
        system=PROMPT,
        messages=[{"role": "user", "content": user}],
    )
    return json_answer(msg)


def retry_later(state, video, tries=2):
    """A model hiccup shouldn't lose a good video: look at it again next run."""
    retries = state.setdefault("retries", {})
    retries[video["id"]] = retries.get(video["id"], 0) + 1
    if retries[video["id"]] <= tries and video["id"] in state["seen"]:
        state["seen"].remove(video["id"])


def slug(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:60]


def clean(story, video, fights):
    ids = {f["id"] for f in fights}
    start = int(story.pop("quote_start_seconds", 0) or 0)
    url = f"https://www.youtube.com/watch?v={video['id']}"
    story["id"] = slug(story["h"]) + "-" + video["id"][:6].lower()
    story["fid"] = story.get("fid") if story.get("fid") in ids else None
    story["c"] = story.get("c") if story.get("c") in CATEGORIES else "boxing"
    story["mood"] = story.get("mood") if story.get("mood") in MOODS else "Hype"
    story["ev"] = (story.get("ev") or "").upper()[:28]
    story["src"] = video["channel"]
    story["url"] = url + (f"&t={start}s" if start else "")
    story["clip"] = url
    story["vid"] = video["id"]   # the site shows this video's thumbnail, linked to YouTube
    story["published"] = NOW.isoformat().replace("+00:00", "Z")
    for key in ("tags", "body"):
        story[key] = [str(x) for x in story.get(key, [])][:6]
    for key in ("h", "sum", "q", "who", "ctx"):
        story[key] = str(story.get(key) or "")
    return story


# ---------- main ----------

def main():
    if not YT_KEY or not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set YOUTUBE_API_KEY and ANTHROPIC_API_KEY.")

    fights = load_json(DATA / "fights.json", [])
    published = load_json(DATA / "stories.json", [])
    state = load_json(STATE_PATH, {"seen": [], "pending": []})
    state.setdefault("pending", [])

    # Drafts you already merged are now on the site, so stop carrying them.
    live_ids = {s["id"] for s in published}
    # Drafts you never approved expire after two days.
    keep_after = NOW - dt.timedelta(hours=CONFIG.get("draft_expiry_hours", 48))
    state["pending"] = [s for s in state["pending"]
                        if s["id"] not in live_ids and parse_time(s["published"]) >= keep_after]

    if not should_run(state, fights):
        print("Quiet period, nothing to do.")
        return

    mode = "fight-night" if fight_night(fights) else "normal"
    log(f"Run at {NOW:%Y-%m-%d %H:%M} UTC ({mode} mode)")
    words = watch_words(fights)
    new = []

    candidates = []
    for channel in CONFIG["channels"]:
        try:
            candidates += new_videos(channel, state)
        except (requests.HTTPError, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", "")
            log(f"Could not read {channel['name']}: {exc.__class__.__name__} {status}".rstrip())

    # Fight week: also search YouTube for the fighters on this week's big cards.
    last_search = state.get("last_search")
    search_every = dt.timedelta(hours=CONFIG.get("search_every_hours", 3))
    if CONFIG.get("search_fight_week", True) and (not last_search or NOW - parse_time(last_search) >= search_every):
        names = fight_week_names(fights)[: CONFIG.get("max_fighters_to_search", 8)]
        for name in names:
            try:
                candidates += search_videos(name, state)
            except requests.HTTPError as exc:
                log(f"Search for {name} failed: {exc}")
        if names:
            state["last_search"] = NOW.isoformat().replace("+00:00", "Z")
            log(f"Searched YouTube for: {', '.join(names)}")

    unique = {}
    for v in candidates:
        unique.setdefault(v["id"], v)
    limit = CONFIG.get("max_transcripts_per_run", 40)
    checked = 0

    for video in unique.values():
        state["seen"].append(video["id"])
        if not looks_relevant(video, words):
            continue
        if checked >= limit:
            state["seen"].pop()  # leave it for the next run
            continue
        checked += 1
        log(f"- {video['channel']}: {video['title']}")
        text = transcript(video["id"])
        if not text:
            continue
        try:
            first = screen(video, text)
        except Exception as exc:
            log(f"  screen step failed: {exc}")
            retry_later(state, video)
            continue
        if first.get("why") == "unreadable screen answer":
            log("  screen answer unreadable, will try again next run")
            retry_later(state, video)
            continue
        if not first.get("keep"):
            log(f"  passed on it: {first.get('why', '')}")
            continue
        try:
            result = write_story(video, text, fights, new + state["pending"] + published)
        except Exception as exc:
            log(f"  writer step failed: {exc}")
            retry_later(state, video)
            continue
        if not result.get("newsworthy"):
            log(f"  skipped: {result.get('reason', 'not newsworthy')}")
            continue
        story = result["story"]
        bad = verify(story, text)
        if bad:
            log(f"  rejected, quote not found in transcript: {bad[0][:80]}")
            continue
        story = clean(story, video, fights)
        new.append(story)
        log(f"  NEW STORY: {story['h']}")
        if result.get("schedule_alert"):
            log(f"  ⚠ Schedule check: {result['schedule_alert']}")

    state["seen"] = state["seen"][-3000:]
    state["pending"] = new + state["pending"]
    state["last_full_run"] = NOW.isoformat().replace("+00:00", "Z")
    save_json(STATE_PATH, state)

    if os.environ.get("AUTO_PUBLISH") == "true":
        save_json(DATA / "stories.json", new + published)
        state["pending"] = []
        save_json(STATE_PATH, state)
    else:
        # The pull request shows the site with every draft not yet approved.
        save_json(DATA / "stories.json", state["pending"] + published)

    REPORT_PATH.write_text(
        "New drafts from the news agent. Merge this pull request to publish "
        "them. Drafts you don't merge expire after two days.\n\n" + "\n".join(report) + "\n")
    log(f"{len(new)} new stories, {len(state['pending'])} waiting for approval.")


if __name__ == "__main__":
    main()
