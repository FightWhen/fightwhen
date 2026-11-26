# FightWhen

Combat sports schedules and straight-from-the-fighter news. The website lives in `site/`, and a news agent in `agent/` writes new stories from YouTube press conferences and interviews.

## How it works

1. Every 15 minutes GitHub checks whether it's a fight night (from 1 hour before a card until 5 hours after). On fight nights the agent works every run. Other days it works every 3 hours.
2. It collects the newest uploads from every channel in `agent/channels.json` (boxing media, promoters, trainers and MMA outlets). During fight week it also searches all of YouTube for fresh videos about the fighters on that week's big cards.
3. It only looks closer at videos whose title or description names someone on `agent/fighters.json` or the schedule, or uses words like "press conference", "injury" or "calls out".
4. For those, it reads the transcript. A fast, low-cost model (Claude Haiku) screens it first. Only videos where a fighter, trainer or promoter says something new go on to the writer model (Claude Sonnet), which writes the story in the site's style.
5. **Every quote is checked against the transcript word for word.** A story with a quote that doesn't match is thrown away.
6. New stories arrive as a pull request called "New stories from the news agent". Merge it from the GitHub app and the site updates in about a minute. Drafts you don't merge expire after two days.
7. If a video says something that changes the schedule (a new opponent, an injury), the pull request flags it with ⚠ so you can update `site/data/fights.json`.

## One-time setup (about 30 minutes)

**1. Make the repository.** Create a free GitHub account, then a new **public** repository called `undercard`. (Public keeps GitHub Actions free; your API keys stay secret either way.) Upload everything in this folder, including the hidden `.github` folder. GitHub Desktop is the easiest way to do that.

**2. Get a YouTube API key (free).** Go to console.cloud.google.com, create a project, search for "YouTube Data API v3" and click Enable. Then Credentials → Create credentials → API key. Copy it.

**3. Get a Claude API key.** Go to console.anthropic.com → API keys → Create key. Add about $10 of credit under Billing.

**4. Add both keys to GitHub.** In your repository: Settings → Secrets and variables → Actions → New repository secret.
- `YOUTUBE_API_KEY` = your YouTube key
- `ANTHROPIC_API_KEY` = your Claude key

**5. Let the agent open pull requests.** Settings → Actions → General → Workflow permissions: choose "Read and write permissions" and tick "Allow GitHub Actions to create and approve pull requests". Save.

**6. Turn on the website.** Settings → Pages → Source: "GitHub Actions". Your site will be at `https://<your-username>.github.io/undercard/`.

**7. First run.** Actions tab → News agent → Run workflow. When it finishes, open the run to see what it found, then check Pull requests for drafts.

## If no transcripts come through

With this many channels you will probably need this. YouTube often blocks transcript requests from cloud servers. If the run log shows `no transcript ... RequestBlocked` or `IpBlocked` for every video, sign up for a Webshare "Residential" proxy plan (a few dollars a month) and add two more secrets: `PROXY_USERNAME` and `PROXY_PASSWORD`. Nothing else changes.

## Everyday use

- **Approve news:** merge the pull request. To drop one story, open the pull request, edit `site/data/stories.json` in the browser, delete that story and commit, then merge.
- **Update the schedule:** edit `site/data/fights.json` on GitHub. Each fight has a date (`d`), a start time in UTC if known (`utc`), a category (`c`) and an `id` stories link to. Set `"big": 1` on the fight you want in the top card. To show the full card when someone taps a fight, add `"card"`: a list of sections like `{"sec": "Main card", "t": "2026-12-13T02:00:00Z", "b": [["Fighter A", "10-0", "Fighter B", "8-2", "Lightweight"]]}`. `t` (the section's start time in UTC) and the records are optional.
- **Add or remove channels:** edit `agent/channels.json`. A name is enough, like `{"name": "iFL TV"}`; the agent finds the channel and writes the match in the run log so you can check it. If it picks the wrong one, add the channel's @handle from its YouTube page.
- **Add fighters, trainers or promoters to watch:** edit `agent/fighters.json`.
- **Publish without approving each story:** Settings → Secrets and variables → Actions → Variables → New variable `AUTO_PUBLISH` = `true`. Only do this once you trust what it writes.
- **Your own domain:** buy one (about $12 a year), then Settings → Pages → Custom domain.

## Costs

- GitHub hosting and Actions: free on a public repository.
- YouTube Data API: free (the agent uses a tiny part of the daily quota).
- Claude API: roughly $25–60 a month for all agents (Haiku screening is pennies; most of the cost is Sonnet writing the stories that pass).
- Transcript proxy: about $5–10 a month at this volume.
- YouTube quota: the free daily quota is 10,000 units. A normal day uses well under that; a fight night with fighter searches uses roughly 4,000–7,000. If you ever hit the limit, Google grants increases for free through a form in the Cloud Console.

## Schedule agent, results agent and health check

These keep the schedule and results current without you typing them in.

**The rule for both:** nothing goes live from one website alone. A change is made only when
**two different websites agree**, and every fact must come with the exact words it was read
from, checked against the page. The sources are in `agent/sources.json`: Wikipedia and the
promotions' own schedule and results pages, read only where robots.txt allows. BoxRec,
Tapology and ESPN are never read.

**Schedule agent** (`agent/schedule.py`, `.github/workflows/schedule.yml`)
- Does a full check once a day, and every hour during a week with a fight on.
- Goes live by itself (safe): new events, new bouts, start times, venues and broadcasters,
  as long as the site had nothing there yet (blank or TBA).
- Waits for you in a pull request called **"Schedule changes that need your OK"** (risky):
  date changes, new opponents, bouts that disappeared, cancellations, and anything that
  would overwrite a value already on the site.
- Anything only one website mentions is listed in the run log and never applied.

**Results agent** (`agent/results.py`, `.github/workflows/results.yml`)
- Every 15 minutes from a card's start until about nine hours later, plus a daily sweep
  of the last four days for any bout still missing a result.
- Records the winner, how it ended and the round, and updates both fighters' records.
- Title fights, and any result only one website reports, wait in a pull request called
  **"Fight results that need your OK"**.

**Health check** (`agent/health.py`, `.github/workflows/health.yml`)
- Every 30 minutes it checks that each agent has worked recently: news within 4 hours,
  schedule within a day (90 minutes in fight week), fighter stats within 30 hours, and
  results within 40 minutes while a card is on. If not, the run fails and **GitHub emails
  you**. Make sure Settings → Notifications → Actions → "Notify me for failed workflows"
  is turned on in your GitHub account.
- The site's footer shows "Last checked" with the time of the latest update.

**Settings**
- To review safe schedule and results changes too, add the variable
  `AUTO_PUBLISH_SAFE` = `false` (Settings → Secrets and variables → Actions → Variables).
- If a page in `agent/sources.json` keeps answering 404 in the run log, fix its address or
  delete its line. Pages often move.
- Start times late in the night are estimates even on official sites, so the site keeps
  labeling ring walks "approx."

Extra cost: roughly $5–15 a month in Claude usage. Pages that haven't changed since the
last check are not read again, which keeps it low.

## Fighter stats agent (tale of the tape)

`agent/tape.py` runs once a day (`.github/workflows/tape.yml`). For every bout in the
next 5 weeks it fills in age, height, reach, stance and country flag from:

- Wikidata and Wikipedia (free to reuse with credit)
- the promotion's own athlete page (UFC, ONE, BKFC, PFL, PBC), only where robots.txt allows

It never reads BoxRec or Tapology, whose terms forbid automated copying.

Every number must come with the exact words it was read from, and those words are checked
against the source. When sources disagree the box stays blank and the pull request lists
the fighter. Settle it by adding the right value to `agent/tape_overrides.json`:

```json
{ "sergei-pavlovich": { "stance": "Orthodox", "reach_in": 84 } }
```

Values you type into `fights.json` yourself are never overwritten; only ages are recalculated
from birth dates. It uses the same Claude API key as the news agent and costs a few cents a day.
