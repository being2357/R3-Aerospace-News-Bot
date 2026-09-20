# R3 Aerospace Opportunities Digest — Documentation

Comprehensive system documentation for the aerospace opportunities digest bot:
how it is architected, how subscribers are persisted and dispatched to, how the
GitHub Actions CI auto-commits state, and how to troubleshoot common failures.

## Table of contents

1. [Overview](#overview)
2. [System architecture](#system-architecture)
3. [Daily pipeline flow](#daily-pipeline-flow)
4. [Subscriber handling (Google Sheet CSV)](#subscriber-handling-google-sheet-csv)
5. [Multi-recipient dispatch (`main.py`)](#multi-recipient-dispatch-mainpy)
6. [GitHub Actions CI workflow](#github-actions-ci-workflow)
7. [Environment variables](#environment-variables)
8. [Troubleshooting](#troubleshooting)

---

## Overview

The bot discovers aerospace-adjacent opportunities for students and young
professionals — internships, competitions/hackathons, and conferences/events —
from RSS feeds and web pages, classifies and summarizes them with DeepSeek, logs
them to Google Sheets, and pushes a categorized digest to Telegram. Two Python
entry points share one codebase:

| Entry point | Command | Role |
|---|---|---|
| Daily pipeline | `python main.py` | Onboard pending subscribers → scrape → filter → classify → summarize → **broadcast** to all subscribers + primary chat |
| Command bot | `python -m notifier.telegram_bot` | Long-poll Telegram, answer `/start` `/latest` `/help`, persist new subscribers |

A GitHub Actions cron runs the daily pipeline and commits the resulting state
files back to the repository.

## System architecture

```
config.py ──► scrapers/feed_parser.py   (RSS/Atom)
              scrapers/web_scraper.py    (HTML, async)
              scrapers/nasa_scraper.py   (JSON API)
              scrapers/http_utils.py     (shared httpx + tenacity)
              cache_store.py  dedup.py   (etag + seen state)

main.py ─► summarizer/ai_engine.py (DeepSeek) ─► storage/sheets_client.py
       └─► notifier/telegram_bot.py ◄── latest_digest.txt, GOOGLE_SHEET_CSV_URL
```

| Path | Purpose |
|---|---|
| `config.py` | Internal source registry (RSS, HTML, JSON API targets; category hint) |
| `scrapers/feed_parser.py` | RSS/Atom ingestion with conditional GET (ETag/Last-Modified) |
| `scrapers/web_scraper.py` | Concurrent HTML fallback scraper (`httpx.AsyncClient` + `asyncio.gather`) |
| `scrapers/nasa_scraper.py` | NASA JSON ingestion via the WordPress REST API |
| `scrapers/http_utils.py` | Shared fetch: User-Agent, timeouts, tenacity retries, conditional GET |
| `cache_store.py` | JSON-file key/value store for cross-run state (`cache/`) |
| `dedup.py` | `item_hash` + persisted "seen" store (dedup before DeepSeek) |
| `storage/sheets_client.py` | Google Sheets auth + URL dedup + sent-flag logging |
| `summarizer/ai_engine.py` | DeepSeek classification + summarization |
| `notifier/telegram_bot.py` | Telegram HTML delivery + command bot + subscriber onboarding/persistence |
| `main.py` | Orchestrator + topic filter + dedup + digest cache + multi-recipient send |
| `data/subscribers.json` | Command-bot subscriber chat IDs (added on `/start`, pruned on block) |
| `latest_digest.txt` | Cached digest text served by `/latest` and `/start` |
| `cache/` | Persisted ETag/Last-Modified + seen-hash state (committed + actions/cache) |
| `.github/workflows/daily_digest.yml` | Daily cron + state-file auto-commit |
| `models.py` | Shared `Article` dataclass + category/section constants |

## Daily pipeline flow

`main.py` runs these stages in order:

0. **Onboard subscribers** — a one-shot `getUpdates` pass (`catch_up_subscribers`)
   that subscribes any chat that sent `/start` or added the bot to a group since
   the last run, sends each a confirmation, and advances the offset to clear
   Telegram's update queue.
1. **Load config** — read the `SOURCES` list from `config.py`, normalize each
   source's category.
2. **Authenticate Sheets** — decode `GCP_SERVICE_ACCOUNT_KEY`, open the sheet,
   ensure the header row, and load existing URLs for dedup.
3. **Scrape** — fetch every source (RSS, JSON API, or HTML). HTML sources fetch
   concurrently (`asyncio.gather`), and a multi-URL source's pages are gathered in
   parallel; failures are isolated per source (and per URL).
4. **Topic pre-filter** — keep only opportunity/competition/event items via
   include/exclude keyword and phrase lists.
5. **Dedupe** — drop URLs already in the sheet or seen this run, drop any item
   whose `(title, url)` hash is already in the persisted "seen" store, then
   collapse near-duplicate titles per source. Only genuinely new items reach
   DeepSeek; hashes are marked "seen" only after a successful Sheets write.
6. **Retry queue** — re-include articles previously logged but never sent.
7. **Classify + summarize** — DeepSeek strictly assigns each article to one of
   three sections and discards the rest.
8. **Cache + log** — write `latest_digest.txt`, append fresh articles to Sheets
   as "unsent".
9. **Broadcast** — send the digest to every subscriber from the published Sheet
   CSV plus the primary chat (see
   [Multi-recipient dispatch](#multi-recipient-dispatch-mainpy)).
10. **Mark sent** — flag delivered articles in Sheets so they are not retried.

If no new opportunities survive filtering, the pipeline writes a "no
opportunities" cache, then broadcasts a friendly "caught up" notice to every
subscriber plus the primary chat, and exits successfully.

## Subscriber handling (Google Sheet CSV)

The chats that receive every daily digest come from a published Google Sheet,
read as a CSV on each run. `TELEGRAM_CHAT_ID` is always appended as a fallback
primary recipient.

### Source of truth

`main.py` resolves broadcast recipients via
`telegram_bot.fetch_subscribers_from_csv(GOOGLE_SHEET_CSV_URL)`, which:

1. Downloads the published CSV over HTTP (`urllib.request`, 30s timeout).
2. Parses it with the standard `csv` module.
3. Reads the `chat_id` column (matched by name, so it may appear anywhere).
4. Strips quotes, apostrophes, and surrounding whitespace from each value.
5. Returns the distinct chat IDs, in order, deduplicated.

Any failure — blank URL, network error, non-CSV body, or missing column — returns
an empty list, so the broadcast falls back to `TELEGRAM_CHAT_ID`.

### Sheet format

The subscriber Sheet needs a header row with a `chat_id` column:

```
chat_id,note
5636898169,Alice
-1001234567890,Announcements channel
```

Group and channel IDs are negative. Publish the sheet to the web as CSV
(**File → Share → Publish to web → Comma-separated values**) and set
`GOOGLE_SHEET_CSV_URL` to the resulting URL.

### Helper API (in `notifier/telegram_bot.py`)

| Function | Purpose |
|---|---|
| `fetch_subscribers_from_csv(csv_url)` | Download + parse the published CSV; return stripped, deduplicated `chat_id` values (empty list on failure) |

### Command-bot subscribers (`data/subscribers.json`)

Separate from the CSV broadcast list, the command bot still persists `/start`
sign-ups to `data/subscribers.json` so it can answer commands and remember who
interacted with it. The pipeline's startup pass (`catch_up_subscribers`) records
pending `/start`/group-join chats there too, but the daily broadcast recipients
are resolved from the CSV above.

Because the published CSV is read-only, a Telegram `403` (blocked user) prunes
only `data/subscribers.json`; a blocked CSV subscriber is skipped with a warning
and must be removed from the Sheet manually.

## Multi-recipient dispatch (`main.py`)

The broadcast stage resolves recipients and sends with per-chat isolation:

```text
subscribers  = telegram_bot.fetch_subscribers_from_csv(GOOGLE_SHEET_CSV_URL)
recipients   = dedupe(subscribers + [TELEGRAM_CHAT_ID])
```

- Subscribers are read from the published Google Sheet CSV (the `chat_id`
  column).
- `TELEGRAM_CHAT_ID` is **always** included as the primary/fallback channel,
  even if no CSV subscribers exist or the CSV fails to load.
- Duplicates are removed (a subscriber whose ID equals `TELEGRAM_CHAT_ID` is not
  sent twice).

For each recipient, the script:

1. Calls `send_digest(...)` (format → split → send each chunk).
2. On `TelegramForbiddenError` (HTTP 403 / `error_code: 403`) → logs a warning
   and calls `remove_subscriber(chat_id)` to prune the blocked user from the
   local subscriber file (CSV subscribers are read-only and must be removed from
   the Sheet manually).
3. On any other error → logs a warning and continues to the next recipient
   (one transient failure never aborts the run).

After the loop:

- If **at least one** recipient received the digest, the delivered articles are
  marked "sent" in Sheets.
- If **zero** recipients received it, the run exits non-zero so the articles stay
  "unsent" and are retried on the next run.

## GitHub Actions CI workflow

`.github/workflows/daily_digest.yml` runs the pipeline and commits state back.

### Triggers

- **Schedule** — `0 18 * * *` (18:00 UTC daily).
- **Manual** — `workflow_dispatch` from the Actions tab.

### Job permissions

`permissions: contents: write` — required for the auto-commit/push.

### Steps

1. **Checkout** — `actions/checkout@v4`.
2. **Set up Python** — `actions/setup-python@v5`, Python 3.11.
3. **Install dependencies** — `pip install -r requirements.txt`.
4. **Restore scraper cache** — `actions/cache@v4` restores `cache/` (keyed
   `aero-cache-${{ github.run_id }}`, prefix `aero-cache-`) so ETag/Last-Modified
   and seen-hash state carry over between runs; a new entry is saved on success.
5. **Run daily digest** — `python main.py` with secrets in the environment.
6. **Commit and push state files** — stages `latest_digest.txt` and `cache/`,
   commits as `github-actions[bot]`, and pushes:

   ```bash
   git add -A -- latest_digest.txt cache/
   if git diff --cached --quiet; then
     echo 'No state changes to commit.'
   else
     git commit -m 'Update digest and subscriber state'
     git push
   fi
   ```

   The `git diff --cached --quiet` guard makes "nothing to commit" a clean no-op.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `GCP_SERVICE_ACCOUNT_KEY` | Yes | Base64-encoded Google service-account JSON |
| `GOOGLE_SHEET_ID` | Yes | Target output-log sheet ID |
| `GOOGLE_SHEET_CSV_URL` | No | Published CSV URL of the subscriber Sheet (with a `chat_id` column) |
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | Primary channel always included in the broadcast (fallback if the CSV is empty/fails) |
| `DEEPSEEK_API_KEY` | No* | DeepSeek key (falls back to titles-only if missing) |
| `DEEPSEEK_MODEL` | No | Model override (default `deepseek-chat`) |
| `LOG_LEVEL` | No | Log verbosity (default `INFO`) |

\* Without `DEEPSEEK_API_KEY`, the pipeline degrades to a titles-and-links-only
digest grouped by each source's category hint.

## Troubleshooting

### Scheduled workflow disabled after ~60 days

**Symptom:** the workflow stops running on schedule; the Actions tab shows the
workflow as "disabled" with a note about repository inactivity.

**Cause:** GitHub automatically disables scheduled workflows when the repository
has had no activity (no commits) for 60 days.

**Fix:**

1. Push any commit to the default branch — the schedule re-activates. Even a
   trivial commit (e.g. bump a comment in the workflow or DOCS.md) is enough.
2. Alternatively, re-enable the workflow from the Actions tab and run it manually
   via **Run workflow**.

The `workflow_dispatch` trigger keeps manual runs available even while the
schedule is disabled.

### `403 Forbidden` — bot blocked by a user

**Symptom:** a daily run logs `Telegram refused the message to <id> (403: user
blocked the bot)` and "Removing from subscribers."

**Cause:** the subscriber blocked the bot (or the bot was removed from a group).
Telegram returns HTTP 403 / `error_code: 403`.

**Behavior (automatic):** the digest script prunes that `chat_id` from the local
`data/subscribers.json`, keeps sending to everyone else, and the workflow
commits the removal. If the blocked `chat_id` came from the published CSV, the
script only logs a warning and skips it — remove that `chat_id` from the Sheet
to stop retrying it.

To resubscribe, the user must message the bot with `/start` again (and be
re-added to the subscriber Sheet if that is the broadcast source).

### `git push` rejected — non-fast-forward (rebase)

**Symptom:** a push fails with:

```
! [rejected]        main -> main (non-fast-forward)
error: failed to push some refs
```

**Cause:** the local `main` is behind the remote — typically because the CI run
committed a digest/state update after your last `git pull`.

**Fix — local bot host:**

```bash
git pull --rebase origin main
# resolve any conflict in data/subscribers.json if prompted, then:
git push
```

**Fix — CI workflow:** the job checks out a fresh clone, so its push usually
fast-forwards cleanly. If it still rejects (a concurrent commit landed mid-run),
re-run the workflow — it re-checks out the latest `main`. For extra robustness,
add a `git pull --rebase origin main` before the `git push` in the commit step.

### Other common issues

| Symptom | Likely cause / fix |
|---|---|
| `Could not open the Google Sheet` | Share the sheet (Editor) with the service-account email |
| `Failed to decode GCP_SERVICE_ACCOUNT_KEY` | Value isn't valid Base64 of a service-account JSON |
| `/latest` returns "No digest available yet" | `latest_digest.txt` not generated — run `python main.py` |
| `getUpdates` error | Wrong `TELEGRAM_BOT_TOKEN` |
| `Failed to fetch subscriber CSV` warning | `GOOGLE_SHEET_CSV_URL` missing or not published; the broadcast falls back to `TELEGRAM_CHAT_ID` |
| A "You're all caught up" notice, no digest | No new opportunities — the catch-up notice is the expected output |

---

## Keeping documentation in sync

`README.md` (user-facing) and `DOCS.md` (technical) must both reflect current
behavior. After any change to code, config, or the workflow, update both in the
same change set.
