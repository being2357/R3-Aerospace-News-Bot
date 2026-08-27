"""RSS/Atom ingestion using ``feedparser``.

Feeds are fetched over HTTP with conditional GET (ETag / Last-Modified persisted
in ``cache/feed_state.json``) so an unchanged feed short-circuits to an empty
result without re-parsing. The network call is isolated in a narrowed
``except httpx.HTTPError`` block with a direct ``feedparser.parse(url)`` fallback,
so unrelated parsing bugs are never swallowed.
"""

from __future__ import annotations

import logging
import re
from typing import List

import feedparser
import httpx

from cache_store import CacheStore
from models import Article
from scrapers.http_utils import fetch_url

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
FEED_STATE_PATH = "cache/feed_state.json"


def _strip_html(text: str) -> str:
    """Remove HTML tags from a feed summary and collapse whitespace."""
    text = _HTML_TAG_RE.sub(" ", text or "")
    return " ".join(text.split())


def fetch_articles(source: dict) -> List[Article]:
    """Fetch and parse a single RSS/Atom source into normalized Articles."""
    url = source["url"]
    name = source.get("name") or source.get("id") or url
    category = source.get("category", "aerospace")

    store = CacheStore(FEED_STATE_PATH)
    state = store.get(url, {}) or {}
    conditional_headers = {}
    if state.get("etag"):
        conditional_headers["If-None-Match"] = state["etag"]
    if state.get("modified"):
        conditional_headers["If-Modified-Since"] = state["modified"]

    # Only the network fetch is wrapped here; parsing stays outside the try so
    # a bug in the logic below can never be misreported as a feed failure.
    try:
        response = fetch_url(
            url, headers=conditional_headers, allow_not_modified=True
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "Feed %s fetch failed (%s); falling back to direct feedparser.parse.",
            url, exc,
        )
        feed = feedparser.parse(url)
    else:
        if response.status_code == 304:
            logger.info("Feed %s unchanged (304); skipping.", url)
            return []

        # Persist the new validators for the next run's conditional GET.
        new_state = {}
        if response.headers.get("etag"):
            new_state["etag"] = response.headers["etag"]
        if response.headers.get("last-modified"):
            new_state["modified"] = response.headers["last-modified"]
        if new_state:
            store.set(url, new_state)

        feed = feedparser.parse(response.content)

    # feedparser reports malformed feeds via the "bozo" flag; log and continue.
    if getattr(feed, "bozo", False):
        logger.warning(
            "Feed %s may be malformed: %s",
            url, getattr(feed, "bozo_exception", "unknown"),
        )

    articles: List[Article] = []
    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        published = entry.get("published") or entry.get("updated") or None
        description = _strip_html(
            entry.get("summary") or entry.get("description") or ""
        )
        articles.append(
            Article(
                title=title,
                url=link,
                source=name,
                category=category,
                published=published,
                description=description or None,
            )
        )

    logger.debug("Parsed %d entry/entries from %s", len(articles), url)
    return articles
