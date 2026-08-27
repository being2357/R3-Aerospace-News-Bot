"""Daily aerospace *opportunities* digest orchestrator.

Flow:
    load config -> authenticate Sheets -> scrape sources -> topic pre-filter ->
    URL dedupe -> intra-batch title dedupe -> retry unsent -> classify/summarize
    (DeepSeek, strict 3 sections) -> log delivered -> post to Telegram.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from typing import Dict, List, Set

from dotenv import load_dotenv

from dedup import SeenStore, item_hash
from models import ALLOWED_CATEGORIES, Article
from notifier import telegram_bot
from scrapers import feed_parser, nasa_scraper, web_scraper
from storage.sheets_client import SheetsClient
from summarizer.ai_engine import AIEngine

logger = logging.getLogger("main")

# --------------------------------------------------------------------------
# Topic pre-filtering
# --------------------------------------------------------------------------
# A coarse pass that runs before the strict DeepSeek classifier. An article is
# kept only if its title/description contains at least one "include" signal and
# none of the veto signals. The DeepSeek stage then applies the strict
# three-section classification and discards anything that still does not fit.
INCLUDE_WORDS: Set[str] = {
    "intern", "interns", "internship", "internships", "fellowship", "fellowships",
    "opportunity", "opportunities", "competition", "competitions", "challenge",
    "challenges", "hackathon", "hackathons", "contest", "contests", "prize",
    "prizes", "award", "awards", "scholarship", "scholarships", "grant", "grants",
    "symposium", "symposia", "conference", "conferences", "congress", "workshop",
    "workshops", "summit", "summits", "webinar", "webinars", "seminar", "seminars",
    "deadline", "deadlines", "registration", "register", "apply", "application",
    "applications", "submission", "submissions", "career", "careers", "job", "jobs",
    "hiring", "recruit", "recruitment", "cansat",
}
INCLUDE_PHRASES: tuple = (
    "call for papers", "call for applications", "call for proposals",
    "student award", "student conference", "student competition", "space apps",
    "rocket challenge", "design competition", "essay contest", "innovation challenge",
)
EXCLUDE_WORDS: Set[str] = {
    "exoplanet", "exoplanets", "anniversary", "anniversaries", "podcast",
    "newsletter", "subscribe", "advertise", "advertisement", "sponsorship",
    "cookies", "login", "signup",
}
EXCLUDE_PHRASES: tuple = (
    "privacy policy", "terms of service", "terms and conditions",
    "become a navigator", "booz allen", "photo of the day", "image of the day",
    "this week in", "media kit", "press release", "contact us", "about us",
    "cookie policy",
)
# Exact (normalized) navigation / boilerplate titles that must never pass.
NAV_LINKS: Set[str] = {
    "home", "about", "about us", "contact", "contact us", "privacy", "privacy policy",
    "legal", "terms", "terms of service", "careers", "career", "jobs", "news",
    "events", "media", "press", "login", "sign in", "sign up", "register",
    "subscribe", "become a navigator", "booz allen hamilton", "read more",
    "learn more", "view more", "view all", "see all", "menu", "apply", "apply now",
    "apply here", "register now", "register here", "find out more", "get started",
}
MIN_TITLE_LENGTH = 6

# Title-similarity dedup: drop same-source items whose normalized title-token
# overlap is at or above this ratio (retains the primary announcement).
SIMILARITY_THRESHOLD = 0.6
_STOPWORDS: Set[str] = {
    "a", "an", "and", "the", "of", "to", "for", "in", "on", "at", "with", "by",
    "is", "are", "be", "from", "as", "or", "your", "our", "you", "we", "it",
    "its", "this", "that", "these", "those", "2024", "2025", "2026", "2027",
    "2028", "2029", "2030",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation, collapsing runs of non-alphanumerics."""
    return " ".join(_WORD_RE.findall((text or "").lower()))


def _tokens(text: str) -> Set[str]:
    return set(_normalize(text).split())


def _contains_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def _contains_phrase(text: str, phrase: str) -> bool:
    return _normalize(phrase) in text


def _is_relevant(article: Article) -> bool:
    """Keep only opportunity/competition/event items (coarse pre-filter)."""
    title_norm = _normalize(article.title)
    text = f"{title_norm} {_normalize(article.description or '')}".strip()
    if not text:
        return False
    if len(title_norm) < MIN_TITLE_LENGTH or title_norm in NAV_LINKS:
        return False
    for phrase in EXCLUDE_PHRASES:
        if _contains_phrase(text, phrase):
            return False
    for word in EXCLUDE_WORDS:
        if _contains_word(text, word):
            return False
    for phrase in INCLUDE_PHRASES:
        if _contains_phrase(text, phrase):
            return True
    for word in INCLUDE_WORDS:
        if _contains_word(text, word):
            return True
    return False


def _title_fingerprint(title: str) -> Set[str]:
    return _tokens(title) - _STOPWORDS


def _similar_titles(a: str, b: str) -> bool:
    ta, tb = _title_fingerprint(a), _title_fingerprint(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= SIMILARITY_THRESHOLD


def _dedupe_similar(articles: List[Article]) -> List[Article]:
    """Drop same-source near-duplicates (team profiles, recurring event posts).

    When a near-duplicate is found, retains the more specific (longer) title as
    the primary announcement.
    """
    kept_by_source: Dict[str, List[Article]] = {}
    result: List[Article] = []
    for article in articles:
        kept = kept_by_source.setdefault(article.source, [])
        duplicate = next(
            (k for k in kept if _similar_titles(article.title, k.title)), None
        )
        if duplicate is None:
            kept.append(article)
            result.append(article)
        elif len(article.title) > len(duplicate.title):
            kept[kept.index(duplicate)] = article
            result[result.index(duplicate)] = article
    return result


# --------------------------------------------------------------------------
# Local digest cache
# --------------------------------------------------------------------------
DIGEST_FILE = "latest_digest.txt"
NO_DIGEST_MESSAGE = "No digest available yet"
NO_OPPORTUNITIES_MESSAGE = "No new opportunities, competitions, or events found today."


def get_latest_digest(path: str = DIGEST_FILE) -> str:
    """Return the cached digest text, or a fallback if it does not exist."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read().strip()
        return content or NO_DIGEST_MESSAGE
    except OSError:
        return NO_DIGEST_MESSAGE


def save_digest(text: str, path: str = DIGEST_FILE) -> None:
    """Write the generated digest text to the local cache file."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    logger.info("Saved latest digest to %s", path)


def _notify_no_new_content(token: str, primary_chat_id: str) -> None:
    """Broadcast the friendly "caught up" notice to every subscriber + primary chat."""
    recipients = list(
        dict.fromkeys([*telegram_bot.get_subscribed_chat_ids(), primary_chat_id])
    )
    logger.info("Sending no-new-content notice to %d recipient(s).", len(recipients))
    telegram_bot.send_notice(token, recipients)


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate, classify, and post aerospace opportunities."
    )
    parser.add_argument(
        "--config",
        default="config/sources.json",
        help="Path to the sources JSON config (default: config/sources.json)",
    )
    return parser.parse_args()


def load_sources(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    sources = data.get("sources", [])
    if not sources:
        logger.error("No sources defined in %s", path)
    return sources


def normalize_source(source: dict) -> dict:
    source = dict(source)
    category = source.get("category", "internships")
    if category not in ALLOWED_CATEGORIES:
        logger.warning(
            "Source '%s' has unknown category '%s'; defaulting to 'internships'.",
            source.get("name"), category,
        )
        category = "internships"
    source["category"] = category
    return source


def scrape_source(source: dict) -> List[Article]:
    source_type = source.get("type")
    if source_type == "rss":
        return feed_parser.fetch_articles(source)
    if source_type == "api":
        return nasa_scraper.fetch_articles(source)
    if source_type in ("web", "html_list", "html_multi"):
        return web_scraper.fetch_articles(source)
    logger.warning("Unknown source type '%s'; skipping.", source_type)
    return []


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logger.error("Required environment variable %s is not set.", name)
        sys.exit(1)
    return value


def main() -> None:
    load_dotenv()
    setup_logging()
    args = parse_args()

    sources = [normalize_source(s) for s in load_sources(args.config)]
    if not sources:
        sys.exit(1)

    sheet_id = require_env("GOOGLE_SHEET_ID")
    service_account_key = require_env("GCP_SERVICE_ACCOUNT_KEY")
    telegram_token = require_env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = require_env("TELEGRAM_CHAT_ID")
    deepseek_key = os.getenv("DEEPSEEK_API_KEY")

    # 0. Onboard any pending subscribers (chats that sent /start or added the
    #    bot to a group since the last run) before we scrape, so they are
    #    included in today's dispatch. This also clears Telegram's update queue.
    telegram_bot.catch_up_subscribers(telegram_token)

    # 1. Google Sheets: authenticate and load existing URLs for dedup.
    sheets = SheetsClient(service_account_key, sheet_id)
    sheets.ensure_header()
    existing_urls = sheets.get_existing_urls()
    logger.info("Loaded %d existing URL(s) from the sheet.", len(existing_urls))

    # 2. Scrape every configured source, isolating failures per source.
    scraped: List[Article] = []
    for source in sources:
        try:
            articles = scrape_source(source)
            logger.info(
                "Source '%s': %d article(s) fetched.",
                source.get("name"), len(articles),
            )
            scraped.extend(articles)
        except Exception as exc:
            logger.exception("Failed to scrape '%s': %s", source.get("name"), exc)

    # 3. Topic pre-filter: keep only opportunity/competition/event items.
    before = len(scraped)
    scraped = [a for a in scraped if _is_relevant(a)]
    logger.info("Topic pre-filter kept %d of %d scraped article(s).", len(scraped), before)

    # 4. Dedup: drop URLs already logged to the sheet or seen this run, plus any
    #    item whose (title, url) hash was already processed in a prior run, then
    #    collapse near-duplicate titles per source. Only genuinely new items reach
    #    DeepSeek.
    seen_store = SeenStore()
    seen_hashes = seen_store.load()
    fresh: List[Article] = []
    seen: Set[str] = set()
    for article in scraped:
        if article.url in existing_urls or article.url in seen:
            continue
        if item_hash(article.title, article.url) in seen_hashes:
            continue
        seen.add(article.url)
        fresh.append(article)
    fresh = _dedupe_similar(fresh)
    logger.info("After URL + similarity dedup: %d fresh article(s).", len(fresh))

    # 5. Retry articles previously logged but never successfully sent.
    unsent = sheets.get_unsent_articles()
    to_process = list(fresh)
    for article in unsent:
        if article.url not in seen:
            seen.add(article.url)
            to_process.append(article)
    logger.info("Fresh articles: %d, retry queue: %d.", len(fresh), len(unsent))

    if not to_process:
        logger.info("No new opportunities to send.")
        save_digest(NO_OPPORTUNITIES_MESSAGE)
        _notify_no_new_content(telegram_token, telegram_chat_id)
        return

    # 6. Classify + summarize via DeepSeek (falls back to titles-only).
    engine = AIEngine(deepseek_key)
    digest = engine.build_digest(to_process)
    delivered_urls = {item["url"] for items in digest.values() for item in items}

    # Clear any previously-logged items the strict classifier now discards, so
    # they are not retried forever.
    stale = [a for a in unsent if a.url not in delivered_urls]
    if stale:
        sheets.mark_sent(stale)

    if not delivered_urls:
        logger.info("No articles survived strict classification; nothing to post.")
        save_digest(NO_OPPORTUNITIES_MESSAGE)
        _notify_no_new_content(telegram_token, telegram_chat_id)
        return

    # 7. Cache the digest text, then log the delivered fresh articles and mark
    #    them "seen" only after the Sheet write succeeds, so a failed run retries.
    delivered_fresh = [a for a in fresh if a.url in delivered_urls]
    save_digest(telegram_bot.format_digest(digest))
    sheets.append_articles(delivered_fresh, sent=False)
    seen_store.add([item_hash(a.title, a.url) for a in delivered_fresh])

    # 8. Post to Telegram: the primary channel plus every subscriber. Per-chat
    #    failures are isolated so one blocked user never aborts the whole run.
    subscribers = telegram_bot.get_subscribed_chat_ids()
    recipients = list(dict.fromkeys([*subscribers, telegram_chat_id]))
    logger.info("Sending digest to %d recipient(s).", len(recipients))
    delivered_any = False
    for chat_id in recipients:
        try:
            telegram_bot.send_digest(digest, telegram_token, chat_id)
            delivered_any = True
            logger.info("Sent digest to chat %s.", chat_id)
        except telegram_bot.TelegramForbiddenError as exc:
            logger.warning("%s Removing from subscribers.", exc)
            telegram_bot.remove_subscriber(chat_id)
        except Exception as exc:
            logger.warning("Failed to send digest to chat %s: %s", chat_id, exc)

    if not delivered_any:
        logger.error("Digest was not delivered to any recipient.")
        sys.exit(1)

    delivered = [a for a in to_process if a.url in delivered_urls]
    sheets.mark_sent(delivered)
    logger.info("Marked %d article(s) as sent.", len(delivered))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)
