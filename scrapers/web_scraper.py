"""HTML scraping fallback for sources without an RSS/Atom or JSON API.

Only sites with no public JSON endpoint remain here (see ``config/sources.json``
notes): ESA education pages, Space Apps, AIAA, CanSatCompetition, IIST, and the
static Indian government sites (ISRO centres, SAC, DRDO, RAC). NASA was migrated
to ``scrapers/nasa_scraper.py`` (WordPress REST API).

Fetches run concurrently with ``httpx.AsyncClient`` + ``asyncio.gather`` (one
URL failing does not kill the batch), honour per-URL conditional GET (ETag /
Last-Modified persisted in ``cache/html_state.json``), and reuse the shared
timeout/retry policy from ``scrapers.http_utils``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from cache_store import CacheStore
from models import Article
from scrapers import http_utils

logger = logging.getLogger(__name__)

HTML_STATE_PATH = "cache/html_state.json"

# Page text fed to DeepSeek for selector-less "best-effort" sources is capped to
# keep classifier payloads bounded.
_MAX_PAGE_TEXT = 1500


@retry(
    stop=stop_after_attempt(http_utils.MAX_ATTEMPTS),
    wait=wait_exponential(
        min=http_utils.BACKOFF_MIN_SECONDS, max=http_utils.BACKOFF_MAX_SECONDS
    ),
    retry=retry_if_exception(http_utils.is_retryable),
    reraise=True,
)
async def _fetch_html(
    url: str, headers: Optional[Dict[str, str]] = None
) -> httpx.Response:
    """GET *url* with the shared UA/timeout and tenacity retries; returns 304 as-is."""
    merged = {"User-Agent": http_utils.USER_AGENT}
    if headers:
        merged.update(headers)
    async with httpx.AsyncClient(
        timeout=http_utils.default_timeout_for(url),
        follow_redirects=True,
        headers=merged,
    ) as client:
        response = await client.get(url)
    if response.status_code == 304:
        return response
    response.raise_for_status()
    return response


def _selectors(source: dict) -> dict:
    """Accept either the legacy ``css_selectors`` or the newer ``selectors`` key."""
    return source.get("css_selectors") or source.get("selectors") or {}


def _title_text(scope, title_selector: Optional[str], anchor) -> str:
    """Pull a title from *scope* (or the anchor), never returning an empty anchor."""
    if title_selector:
        node = scope.select_one(title_selector)
        if node is not None:
            text = node.get_text(" ", strip=True)
            if text:
                return text
    return anchor.get_text(" ", strip=True)


def _emit(
    scope, anchor, base_url: str, title_selector: Optional[str]
) -> Optional[Tuple[str, str]]:
    """Return ``(absolute_url, title)`` for one anchor, or None if unusable."""
    href = anchor.get("href")
    if not href:
        return None
    title = _title_text(scope, title_selector, anchor)
    if not title:
        return None
    return urljoin(base_url, href), title


def _extract_anchors(
    soup, selectors: dict, url: str, name: str, category: str
) -> List[Article]:
    """Extract Articles from one parsed page using the configured selectors."""
    item_selector = selectors.get("item")
    link_selector = selectors.get("link")
    title_selector = selectors.get("title")

    articles: List[Article] = []
    seen_urls: Set[str] = set()

    if item_selector and link_selector:
        # Container pattern (e.g. DRDO: item = div.views-row, link = a).
        for container in soup.select(item_selector):
            anchor = container.select_one(link_selector)
            if anchor is None and container.name == "a":
                anchor = container
            if anchor is None:
                continue
            result = _emit(container, anchor, url, title_selector)
            if result is None:
                continue
            absolute_url, title = result
            if absolute_url in seen_urls:
                continue
            seen_urls.add(absolute_url)
            articles.append(
                Article(title=title, url=absolute_url, source=name, category=category)
            )
        return articles

    # Anchor pattern: each match of `item`/`link` is itself an <a>.
    anchor_selector = link_selector or item_selector or "a[href]"
    for anchor in soup.select(anchor_selector):
        result = _emit(anchor, anchor, url, title_selector)
        if result is None:
            continue
        absolute_url, title = result
        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)
        articles.append(
            Article(title=title, url=absolute_url, source=name, category=category)
        )
    return articles


def _extract_page_item(soup, url: str, name: str, category: str) -> Optional[Article]:
    """Best-effort single item for selector-less pages: <title> + body text.

    Used for the structurally inconsistent ISRO centre pages, where there is no
    reliable selector — we hand the raw (truncated) page text to DeepSeek instead.
    """
    title_node = soup.find("title")
    title = title_node.get_text(" ", strip=True) if title_node else ""
    if not title:
        return None
    description = " ".join(soup.get_text(" ", strip=True).split())[:_MAX_PAGE_TEXT]
    return Article(
        title=title,
        url=url,
        source=name,
        category=category,
        description=description or None,
    )


async def _scrape_url(
    url: str,
    selectors: dict,
    name: str,
    category: str,
    store: CacheStore,
) -> Tuple[List[Article], Dict[str, str]]:
    """Fetch and parse one URL, returning (articles, new conditional-GET state)."""
    state = store.get(url, {}) or {}
    conditional_headers: Dict[str, str] = {}
    if state.get("etag"):
        conditional_headers["If-None-Match"] = state["etag"]
    if state.get("modified"):
        conditional_headers["If-Modified-Since"] = state["modified"]

    # Only the network call is guarded here; parsing stays outside the try.
    try:
        response = await _fetch_html(url, headers=conditional_headers)
    except httpx.HTTPError as exc:
        logger.warning("Scrape of %s failed: %s", url, exc)
        return [], {}

    if response.status_code == 304:
        logger.info("Page %s unchanged (304); skipping.", url)
        return [], {}

    new_state: Dict[str, str] = {}
    if response.headers.get("etag"):
        new_state["etag"] = response.headers["etag"]
    if response.headers.get("last-modified"):
        new_state["modified"] = response.headers["last-modified"]

    soup = BeautifulSoup(response.text, "html.parser")
    if selectors:
        articles = _extract_anchors(soup, selectors, url, name, category)
    else:
        item = _extract_page_item(soup, url, name, category)
        articles = [item] if item else []

    return articles, new_state


def _source_urls(source: dict) -> List[str]:
    if source.get("urls"):
        return list(source["urls"])
    if source.get("url"):
        return [source["url"]]
    return []


async def fetch_articles_async(source: dict) -> List[Article]:
    """Scrape a single source (one or many URLs) concurrently into Articles."""
    name = source.get("name") or source.get("id") or ""
    category = source.get("category", "aerospace")
    selectors = _selectors(source)
    urls = _source_urls(source)

    if not urls:
        logger.warning("Source '%s' has no url/urls; skipping.", name)
        return []

    store = CacheStore(HTML_STATE_PATH)

    async def scrape_one(
        url: str,
    ) -> Tuple[str, List[Article], Dict[str, str]]:
        # Per-URL network failures are isolated inside _scrape_url so one bad
        # URL never kills the whole batch.
        fetched, new_state = await _scrape_url(
            url, selectors, name, category, store
        )
        return url, fetched, new_state

    results = await asyncio.gather(*(scrape_one(u) for u in urls))

    articles: List[Article] = []
    for url, fetched, new_state in results:
        articles.extend(fetched)
        if new_state:
            # Persist sequentially (single-threaded) to avoid racing writes.
            store.set(url, new_state)

    if not articles:
        logger.warning("Source '%s': 0 items matched.", name)
    else:
        logger.debug("Scraped %d article(s) from '%s'.", len(articles), name)
    return articles


def fetch_articles(source: dict) -> List[Article]:
    """Synchronous wrapper around :func:`fetch_articles_async`."""
    return asyncio.run(fetch_articles_async(source))
