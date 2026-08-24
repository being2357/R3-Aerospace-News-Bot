# R3 Aerospace Opportunities Digest

A production-ready Python bot that discovers **aerospace-adjacent opportunities
for students and young professionals** — internships, competitions/hackathons,
and conferences/events — from RSS feeds and web pages, classifies and
summarizes them with the DeepSeek API, logs them to Google Sheets, and serves a
categorized digest through a Telegram command bot. A GitHub Actions cron keeps
the digest fresh daily.

## What it does

- **Targeted ingestion** — RSS/Atom via `feedparser`, plus an HTML fallback via
  `httpx` + `BeautifulSoup`, pointed at careers, opportunity, and event pages
  (NASA internships, ESA Academy, CanSat, Space Apps, AIAA student conferences).
- **Two-stage relevance filtering** — a keyword pre-filter in `main.py` drops
  navigation links, boilerplate, and general space news; DeepSeek then strictly
  classifies each item and **discards anything that doesn't fit**.
- **Strict three-section output**:
  - 🎓 Internships & Student Opportunities
  - 🏆 Competitions & Hackathons
  - 📅 Conferences & Upcoming Events
- **Deduplication** — URL dedup against Google Sheets (Column D) plus same-source
  title-similarity dedup so recurring posts (team profiles, event reminders)
  collapse into the primary announcement.
- **AI summaries** — DeepSeek (OpenAI-compatible SDK) writes two-sentence
  summaries per article.
- **Telegram command bot** — `/start`, `/latest`, and `/help`, backed by a local
  `latest_digest.txt` cache. Every chat that sends `/start` is persisted to
  `data/subscribers.json` for the daily broadcast.
- **Daily automation** — a GitHub Actions cron (18:00 UTC) runs the pipeline,
  posts to every subscriber plus the primary `TELEGRAM_CHAT_ID`, prunes users who
  have blocked the bot, and commits the digest + subscriber list back to the
  repository.

## Architecture

```
config/sources.json ──► scrapers/feed_parser.py   ─┐
                        scrapers/web_scraper.py     ├─► main.py ─► summarizer/ai_engine.py
                        scrapers/http_utils.py      │      │                 │
                                                   │      │                 ▼
                        storage/sheets_client.py ◄──┴── latest_digest.txt ─ notifier/telegram_bot.py
                                                                                 ▲
                                                   data/subscribers.json ────────┘
```

| Path | Purpose |
|---|---|
| `config/sources.json` | Source registry (RSS + web targets, category hint) |
| `scrapers/feed_parser.py` | RSS/Atom ingestion (captures title + summary) |
| `scrapers/web_scraper.py` | HTML fallback scraper |
| `scrapers/http_utils.py` | Shared fetch with timeout/retry/User-Agent |
| `storage/sheets_client.py` | Google Sheets auth + dedup + logging |
| `summarizer/ai_engine.py` | DeepSeek classification + summarization |
| `notifier/telegram_bot.py` | Telegram HTML delivery + command bot + subscriber persistence |
| `main.py` | Orchestrator + topic filter + dedup + digest cache + multi-recipient send |
| `data/subscribers.json` | Persisted subscriber chat IDs (added on `/start`, pruned on block) |
| `.github/workflows/daily_digest.yml` | Daily cron + state-file auto-commit |
| `models.py` | Shared `Article` dataclass + category/section constants |

## How it runs

There are two entry points:

1. **Daily pipeline** — `python main.py` scrapes sources, filters, classifies,
   summarizes, logs to Sheets, then posts the digest to every chat in
   `data/subscribers.json` plus the primary `TELEGRAM_CHAT_ID`, and writes
   `latest_digest.txt`.
2. **Command bot** — `python -m notifier.telegram_bot` long-polls Telegram and
   answers commands from the `latest_digest.txt` cache, saving each `/start` chat
   to `data/subscribers.json`.

The GitHub Actions cron runs the daily pipeline and commits `latest_digest.txt`
and `data/subscribers.json` back to the repository, so both the digest and the
subscriber list are versioned (and available after a `git pull` for a bot running
elsewhere).

## Prerequisites

- Python 3.10+
- A Google Cloud project with a service account (Sheets + Drive APIs enabled)
- A Google Sheet
- A Telegram bot (via [@BotFather](https://t.me/BotFather))
- A [DeepSeek API key](https://platform.deepseek.com/)

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Then fill in the values (see `.env.example`):

| Variable | Purpose |
|---|---|
| `GCP_SERVICE_ACCOUNT_KEY` | Base64-encoded Google service-account JSON |
| `GOOGLE_SHEET_ID` | The `<ID>` from `spreadsheets/d/<ID>/edit` |
| `DEEPSEEK_API_KEY` | DeepSeek API key |
| `DEEPSEEK_MODEL` | Optional model override (default `deepseek-chat`) |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Chat/channel that receives the daily post |

### 3. Google Sheets + service account

1. Enable the **Sheets API** and **Drive API** in Google Cloud Console.
2. Create a **service account** and download its JSON key.
3. Create a Google Sheet and share it (Editor) with the service-account email
   (the `client_email` field inside the JSON key).
4. Set `GOOGLE_SHEET_ID` to the Sheet ID.
5. Base64-encode the JSON key into `GCP_SERVICE_ACCOUNT_KEY`:

   **Linux/macOS:**
   ```bash
   base64 -w 0 service-account.json
   ```

   **Windows (PowerShell):**
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("service-account.json"))
   ```

The bot writes the **first sheet** with these columns (the header row is created
automatically on first run):

```
A: Timestamp   B: Source   C: Title   D: URL   E: Category   F: Sent Flag
```

`Category` holds one of `internships`, `competitions`, or `conferences`.

### 4. Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and set
   `TELEGRAM_BOT_TOKEN`.
2. Set `TELEGRAM_CHAT_ID` to the primary chat/channel that always receives the
   daily post. For a private chat, message your bot and read the ID from the
   `getUpdates` endpoint, or add the bot to a channel and use the channel ID
   (e.g. `@mychannel`).
3. Anyone who messages the bot with `/start` is automatically added to
   `data/subscribers.json` and receives the digest on every run, alongside the
   primary `TELEGRAM_CHAT_ID`.

### 5. DeepSeek

Set `DEEPSEEK_API_KEY` (and optionally `DEEPSEEK_MODEL` to override the default
`deepseek-chat`, e.g. `deepseek-v4-flash`).

### 6. Configure sources

Edit `config/sources.json`. Each entry supports:

```json
{
  "id": "esa_academy",
  "name": "ESA Academy (Student Opportunities)",
  "type": "web",
  "url": "https://www.esa.int/Education/ESA_Academy",
  "category": "internships",
  "css_selectors": { "link": "a[href]", "title": "h2" }
}
```

- `type` — `"rss"` or `"web"`.
- `category` — a *hint* (`internships`, `competitions`, or `conferences`). The
  AI re-classifies every article, so this only biases the fallback path.
- `css_selectors` — for `"web"` sources only. `link` (required) matches `<a>`
  elements; `title` (optional) selects the title within the anchor.

> **Note:** web selectors are site-specific and can break if a site redesigns.
> If a `web` source yields nothing, inspect the page and update `css_selectors`.

## Running locally

Daily pipeline:

```bash
python main.py
# or with a custom config:
python main.py --config path/to/sources.json
```

Command bot (run from the project root, with dependencies installed — it imports
`main` to reach the cache helper):

```bash
python -m notifier.telegram_bot
```

Commands:

| Command | Response |
|---|---|
| `/start` | Subscribes the chat + confirmation + the latest digest |
| `/latest` | The latest digest |
| `/help` | List of available commands |

If there are no new opportunities, the cache is set to
"No new opportunities, competitions, or events found today." and no Telegram
post is sent. Set `LOG_LEVEL=DEBUG` for verbose output.

## GitHub Actions

1. Push the repository to GitHub.
2. Add these secrets under **Settings → Secrets and variables → Actions**:
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DEEPSEEK_API_KEY`,
   `GCP_SERVICE_ACCOUNT_KEY`, `GOOGLE_SHEET_ID`.
3. The workflow runs daily at **18:00 UTC** (and on manual dispatch), then
   commits `latest_digest.txt` and `data/subscribers.json` back to the repository.
   The job declares `permissions: contents: write`, which the auto-commit
   requires.

> GitHub Actions schedules are approximate and can be delayed by minutes; the
> cron time is always interpreted in UTC.

## Error handling

- **Network failures** — every outbound request uses timeouts and retries with
  backoff; one failing source never crashes the whole run.
- **Empty/invalid feeds** — logged and skipped.
- **DeepSeek failure** — falls back to a titles-and-links-only digest grouped by
  each source's category hint.
- **Telegram send failure** — per-recipient failures are logged and skipped; if
  the digest reaches no recipient at all, the run exits non-zero (visible in
  Actions) and the affected articles stay "unsent" so they are retried.
- **Blocked user** — a `403 Forbidden` from Telegram removes that chat from
  `data/subscribers.json` (committed by the workflow) so it is not retried.
- **Auth failures** — raised with a clear message (e.g. "share the sheet with
  the service-account email").

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Could not open the Google Sheet` | Sheet not shared with the service-account email, or wrong `GOOGLE_SHEET_ID`. |
| `Failed to decode GCP_SERVICE_ACCOUNT_KEY` | The value isn't valid Base64 of a service-account JSON. |
| `DEEPSEEK_API_KEY is not set` | Missing secret / `.env` entry. |
| `/latest` returns "No digest available yet" | `latest_digest.txt` not generated yet — run `python main.py`. |
| `getUpdates` error when running the bot | Wrong `TELEGRAM_BOT_TOKEN`. |
| No message, no error | No new opportunities — this is the expected silent exit. |
