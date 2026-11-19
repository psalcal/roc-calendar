# Redwood City Parks & Rec calendar feed

Builds a subscribable `calendar.ics` from the city's Parks, Recreation & Community
Services calendar once a day, using GitHub Actions. Published with GitHub Pages so
Apple Calendar can subscribe to it.

## How it works

- Every day it reads the calendar page for this month and the next two months. These
  pages give each event's date, start time, title and link.
- It then fetches each event's own .ics export from the city site to get the end
  time, location and description. That's capped at 60 events per run, with 2 seconds
  between requests, and the results are cached in `enrich_cache.json`. The city's CDN
  blocks bursts of requests, so the first full set of details fills in over a few
  days. Until an event has its details, it shows a 1-hour default duration and a link
  to the event page.
- If the city changes an event's start time, the script fetches that event's details
  again.
- If the site can't be reached, the previous feed stays in place.

## Setup (one time, about 10 minutes)

1. Create a free GitHub account if you don't have one.
2. Create a new **public** repository, e.g. `roc-calendar`. Pages is free for public
   repos. The feed only contains public city events.
3. Upload all files from this folder, keeping the `.github/workflows/build.yml` path.
   On the repo page: **Add file → Upload files**, then drag in the folder contents.
   If the web uploader skips the hidden `.github` folder, create the file by hand:
   **Add file → Create new file**, name it `.github/workflows/build.yml`, and paste
   in the contents.
4. **Settings → Actions → General → Workflow permissions**: choose
   "Read and write permissions" and save.
5. **Actions** tab → "Build calendar feed" → **Run workflow**. It takes a few
   minutes. Check that the run log says `Wrote N events`.
6. **Settings → Pages**: Source = "Deploy from a branch", Branch = `main`, folder
   `/ (root)`, then Save. After a minute your feed is at
   `https://YOUR-USERNAME.github.io/roc-calendar/calendar.ics`

## Subscribe in Apple Calendar

- **Mac:** Calendar → File → New Calendar Subscription → paste the URL. Set
  Location to **iCloud** so it syncs to your iPhone, and set Auto-refresh to
  "Every day".
- **iPhone only:** Settings → Apps → Calendar → Calendar Accounts → Add Account →
  Other → Add Subscribed Calendar.

## Options (top of `build_feed.py`)

- `MONTHS_AHEAD`: how far ahead to look (default 2).
- `CALENDARS`: uncomment the City Events line to include the city-wide calendar.
- `MAX_ENRICH_PER_RUN`: per-run cap on detail fetches (default 60).

## Known risks

- If the city redesigns its calendar pages, the script will find 0 events and fail
  without overwriting the feed. GitHub emails you when a scheduled run fails.
- The city's CDN might block GitHub's servers outright. If the first run logs
  "rate-limited" or "Access Denied" even on month pages, this hosting approach
  won't work and the script would need to run from a home computer instead.
