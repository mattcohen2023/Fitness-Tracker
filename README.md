# Fitness Tracker

A personal AI fitness coach that automates weekly lift programming using real biometric data. Built in Python, integrated with the Whoop API and Notion.

## Architecture

```
┌─────────────────┐     daily cron      ┌──────────────────────┐
│   Whoop API v2  │ ──────────────────► │  Whoop Recovery Log  │
│  (biometrics)   │   src/sync_whoop.py │  (Notion database)   │
└─────────────────┘                     └──────────────────────┘
                                                   │
                                                   │  reads both DBs
┌─────────────────┐                     ┌──────────▼───────────┐
│    Lift Log     │                     │   Weekly Check-in    │
│ (Notion DB,     │ ──────────────────► │  (Claude Cowork,     │
│  manual entry)  │                     │   Sunday mornings)   │
└─────────────────┘                     └──────────────────────┘
                                                   │
                                         ┌─────────▼────────────┐
                                         │  Weekly Program Page │
                                         │  (Notion, generated) │
                                         └──────────────────────┘
```

**This repository covers piece 1:** the daily Whoop → Notion sync. The weekly check-in and lift log are future work (see [Roadmap](#roadmap)).

## Project Structure

```
Fitness-Tracker/
├── src/
│   ├── whoop_client.py    # Whoop API v2 wrapper + OAuth lifecycle
│   ├── notion_client.py   # Notion SDK wrapper with upsert logic
│   └── sync_whoop.py      # Orchestration: fetch → merge → write
├── tests/
│   └── test_sync.py       # 39 unit tests (no live API calls)
├── .env.example           # Required environment variables
├── PRIVACY.md             # Whoop developer app privacy policy
└── requirements.txt
```

## Setup

### 1. Clone and install

```bash
git clone <your-repo-url>
cd Fitness-Tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Register a Whoop developer app

1. Go to [developer.whoop.com](https://developer.whoop.com) and create an account
2. Create a new application — set the redirect URI to `http://localhost:8080/callback`
3. Copy your **Client ID** and **Client Secret**

### 3. Create a Notion integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) and create a new integration
2. Copy the **Integration Secret** (your API key)
3. In Notion, open your **Whoop Recovery Log** database → `...` menu → **Connections** → connect your integration
4. Copy the database ID from the URL: `notion.so/<workspace>/<DATABASE_ID>?v=...`

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```bash
# Notion
NOTION_API_KEY=secret_...
WHOOP_RECOVERY_DB_ID=your-database-id-here

# Whoop OAuth
WHOOP_CLIENT_ID=your-client-id
WHOOP_CLIENT_SECRET=your-client-secret
WHOOP_REDIRECT_URI=http://localhost:8080/callback
```

### 5. Notion database schema

Create a database named **Whoop Recovery Log** with these properties:

| Property | Type |
|---|---|
| Day | Title |
| Date | Date |
| Recovery Score | Number |
| HRV | Number |
| Resting Heart Rate | Number |
| Sleep Score | Number |
| Sleep Duration | Number |
| Sleep Debt | Number |
| Day Strain | Number |
| Workout Strain | Number |
| Workout Duration | Number |
| Active Calories | Number |
| Total Calories | Number |
| Respiratory Rate | Number |
| Sync Timestamp | Date |
| Notes | Text |

> If you rename any column, update the matching key in `notion_client.PROPERTY_NAMES`.

## Usage

### Sync yesterday (default)

```bash
python -m src.sync_whoop
```

### Sync a specific date or range

```bash
python -m src.sync_whoop --start 2026-01-01
python -m src.sync_whoop --start 2026-01-01 --end 2026-01-07
```

### Verbose / debug logging

```bash
python -m src.sync_whoop --verbose
```

### Run the test suite

```bash
python -m pytest tests/ -v
```

## How OAuth works

Whoop uses OAuth 2.0. The first time you run the sync, a one-time browser flow runs automatically:

1. Your browser opens to the Whoop authorization page
2. You click **Authorize**
3. Whoop redirects to `localhost:8080/callback` — the script catches this with a temporary local server
4. The authorization code is exchanged for an access token + refresh token
5. Both tokens are saved to `.whoop_credentials.json` (gitignored)

On every subsequent run the script silently refreshes the access token using the saved refresh token. You should only ever need to authorize in the browser once.

> **Security note:** `.whoop_credentials.json` contains your OAuth tokens. It is gitignored by default. Never commit it.

## How the sync works

For each date in the requested range, the script:

1. Fetches **cycles**, **recovery**, **sleep**, and **workouts** from the Whoop API
2. Anchors each record to a calendar date using the cycle's start timestamp (cycles are the canonical "day" unit in Whoop's data model)
3. Merges all four data types into a single `DailyRecord` per day
4. Upserts each record to Notion — creating a new row if none exists, updating in-place if one does. Running the same range twice is always safe.

**Rest days:** `Workout Strain` and `Workout Duration` are left blank (not set to 0) so they're visually distinct from a recorded 0-strain workout.

**Multi-workout days:** strain scores and durations are summed across all sessions.

**No-data days** (Whoop not worn, or data not yet processed): a placeholder row is written with only `Date` and `Sync Timestamp` filled in — but only if no row already exists for that date. This protects real data from being overwritten by a backfill.

## Scheduling

### GitHub Actions (recommended)

Create `.github/workflows/sync.yml`:

```yaml
name: Daily Whoop Sync

on:
  schedule:
    - cron: '0 10 * * *'   # 10:00 UTC daily (6am ET)
  workflow_dispatch:        # allow manual runs from GitHub UI

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python -m src.sync_whoop
        env:
          NOTION_API_KEY:        ${{ secrets.NOTION_API_KEY }}
          WHOOP_RECOVERY_DB_ID:  ${{ secrets.WHOOP_RECOVERY_DB_ID }}
          WHOOP_CLIENT_ID:       ${{ secrets.WHOOP_CLIENT_ID }}
          WHOOP_CLIENT_SECRET:   ${{ secrets.WHOOP_CLIENT_SECRET }}
          WHOOP_REDIRECT_URI:    ${{ secrets.WHOOP_REDIRECT_URI }}
```

> **One extra step for GitHub Actions:** because the OAuth browser flow can't run in CI, you need to pre-seed the refresh token. Run the sync locally once to generate `.whoop_credentials.json`, then add its contents as a GitHub secret named `WHOOP_CREDENTIALS` and update the workflow to write it to disk before running:
>
> ```yaml
> - run: echo '${{ secrets.WHOOP_CREDENTIALS }}' > .whoop_credentials.json
> - run: python -m src.sync_whoop
> ```

### Local cron (macOS)

```bash
# Open crontab
crontab -e

# Add this line to run daily at 6am
0 6 * * * cd /path/to/Fitness-Tracker && .venv/bin/python -m src.sync_whoop >> logs/sync.log 2>&1
```

## API notes

A few Whoop API v2 quirks discovered during development, documented here for anyone reading this later:

- **Sleep and workout endpoints** are at `/v2/activity/sleep` and `/v2/activity/workout` — not `/v2/sleep` and `/v2/workout` as you might expect from the cycle and recovery pattern
- **Total sleep time** is not a direct field. It must be summed from `total_light_sleep_time_milli + total_slow_wave_sleep_time_milli + total_rem_sleep_time_milli` in the `stage_summary` object
- **Sleep debt** is `sleep_needed.need_from_sleep_debt_milli`, not `sleep_debt_milli`
- **Calories** are returned as kilojoules and converted to kcal (`× 0.239006`)
- **notion-client SDK** is pinned to `<3.0.0` because v3 removed `databases.query()` and introduced breaking API version changes

## Roadmap

- [ ] **Lift Log sync** — a separate Notion database for manually logging every exercise, set, rep, and weight
- [ ] **Weekly check-in** — a Claude Cowork scheduled task (Sundays) that reads both databases, runs a recovery-aware check-in conversation, and generates next week's lifting program as a Notion page
- [ ] **GitHub Actions deployment** — automated daily sync without needing a local machine running
- [ ] **Backfill script** — utility to sync historical Whoop data in bulk

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `requests` | ≥2.31 | Whoop API HTTP calls |
| `notion-client` | ≥2.2.1, <3.0 | Official Notion SDK |
| `python-dotenv` | ≥1.0 | `.env` file loading |
| `pytest` | dev only | Test runner |
