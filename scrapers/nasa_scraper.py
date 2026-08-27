"""NASA opportunity ingestion via the public WordPress REST API.

NASA.gov exposes ``/wp-json/wp/v2/posts``, so we query it directly instead of
parsing HTML (the same "direct API + keyword filter" approach used for other
JSON-backed sources). Each configured search term returns matching posts; results
are filtered against :data:`KEYWORDS` and normalized into ``Article`` objects.
"""

from __future__ import annotations

import logging
import re
from typing import List, Set

from models import Article
from scrapers.http_utils import fetch_json

logger = logging.getLogger(__name__)

NASA_POSTS_API = "https://www.nasa.gov/wp-json/wp/v2/posts"
DEFAULT_QUERY = "internship"
_PER_PAGE = 20

# Opportunity signals used to keep only student-relevant content. Titles and
# excerpts are matched case-insensitively against these substrings.
KEYWORDS: tuple = (
    "intern", "internship", "fellowship", "opportunity", "competition",
    "challenge", "hackathon", "contest", "prize", "award", "scholarship",
    "grant", "apply", "application", "student", "stem", "workshop", "webinar",
    "symposium", "conference", "summit", "registration", "deadline",
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace (WordPress returns HTML markup)."""
    text = _HTML_TAG_RE.sub(" ", text or "")
    return " ".join(text.split())


def _matches_keywords(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in KEYWORDS)


def _query_posts(term: str) -> list:
    params = {
        "search": term,
        "per_page": _PER_PAGE,
        "_fields": "date,link,title,excerpt",
    }
    data = fetch_json(NASA_POSTS_API, params=params)
    return data if isinstance(data, list) else []


def fetch_articles(source: dict) -> List[Article]:
    """Query the NASA posts API for a source and return filtered Articles."""
    name = source.get("name") or source.get("id") or "NASA"
    category = source.get("category", "aerospace")
    queries = source.get("query") or [source.get("search") or DEFAULT_QUERY]
    if isinstance(queries, str):
        queries = [queries]

    articles: List[Article] = []
    seen_urls: Set[str] = set()

    for term in queries:
        for item in _query_posts(term):
            title = _strip_html((item.get("title") or {}).get("rendered") or "")
            url = (item.get("link") or "").strip()
            summary = _strip_html((item.get("excerpt") or {}).get("rendered") or "")
            if not title or not url or url in seen_urls:
                continue
            if not _matches_keywords(f"{title} {summary}"):
                continue
            seen_urls.add(url)
            articles.append(
                Article(
                    title=title,
                    url=url,
                    source=name,
                    category=category,
                    published=item.get("date"),
                    description=summary or None,
                )
            )

    if not articles:
        logger.warning("Source '%s': 0 items matched.", name)
    else:
        logger.debug("Fetched %d NASA item(s) for '%s'.", len(articles), name)
    return articles
