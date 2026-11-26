"""FightWhen health check.

Runs every 30 minutes. If an agent has stopped working, or the site's data has
gone stale, this check fails, and GitHub emails you about the failed run.

Limits: news agent 4 hours, schedule agent 26 hours (90 minutes in fight week),
fighter stats 30 hours, and on fight nights the results agent 40 minutes.
"""

import datetime as dt
import json
import os
import sys

import requests

from common import FIGHTS_PATH, NOW, STATUS_PATH, fight_start, load_json

REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")


def last_success(workflow):
    r = requests.get(f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow}/runs",
                     params={"status": "success", "per_page": 1},
                     headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
                     timeout=20)
    runs = r.json().get("workflow_runs", []) if r.ok else []
    return dt.datetime.fromisoformat(runs[0]["updated_at"].replace("Z", "+00:00")) if runs else None


def main():
    problems, lines = [], []
    try:
        fights = json.loads(FIGHTS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError) as e:
        fights = []
        problems.append(f"site/data/fights.json can't be read ({e.__class__.__name__}). The schedule on the site is broken.")
    week = any(NOW - dt.timedelta(hours=12) <= fight_start(f) <= NOW + dt.timedelta(days=7) for f in fights)
    tonight = any(fight_start(f) - dt.timedelta(minutes=15) <= NOW <= fight_start(f) + dt.timedelta(hours=9) for f in fights)

    limits = [("agent.yml", "News agent", 4), ("schedule.yml", "Schedule agent", 1.5 if week else 26),
              ("tape.yml", "Fighter stats agent", 30)]
    if tonight:
        limits.append(("results.yml", "Results agent", 0.67))
    for wf, name, hours in limits:
        when = last_success(wf)
        age = (NOW - when).total_seconds() / 3600 if when else None
        lines.append(f"- {name}: last worked {'never' if age is None else f'{age:.1f} hours ago'} (limit {hours:g} h)")
        if age is None or age > hours:
            problems.append(f"{name} hasn't finished a run in {'ever' if age is None else f'{age:.1f} hours'}. "
                            f"Open Actions → {name} and look at the latest run's log.")

    status = load_json(STATUS_PATH, {})
    checked = status.get("schedule")
    if checked:
        age = (NOW - dt.datetime.fromisoformat(checked.replace("Z", "+00:00"))).total_seconds() / 3600
        lines.append(f"- Schedule data last checked {age:.1f} hours ago")
        if age > (2 if week else 30):
            problems.append(f"The schedule hasn't been checked in {age:.1f} hours.")

    summary = "# FightWhen health check\n\n" + "\n".join(lines) + "\n\n" + (
        "## Problems\n" + "\n".join(f"- {p}" for p in problems) if problems else "Everything is up to date.")
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(summary + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
