"""Shared HTTP fetching with timeouts, retries, and a polite User-Agent.

All outbound scraping requests go through :func:`fetch_url`, which adds a
User-Agent, honors a per-host timeout (government ``*.gov.in`` hosts are slower
and get more time), retries transient failures with exponential backoff via
``tenacity``, and optionally supports conditional GET (ETag / If-Modified-Since)
so scrapers can skip content that has not changed since the last run.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Final, Optional
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

USER_AGENT: Final[str] = (
    "Mozilla/5.0 (compatible; AerospaceNewsBot/1.0; +https://github.com/)"
)

DEFAULT_TIMEOUT: Final[float] = 20.0
GOV_TIMEOUT: Final[float] = 30.0  # .gov.in sites are slow; give them more time

MAX_ATTEMPTS: Final[int] = 3
BACKOFF_MIN_SECONDS: Final[float] = 2.0
BACKOFF_MAX_SECONDS: Final[float] = 10.0


def default_timeout_for(url: str) -> float:
    """Return the default timeout for *url* (30s for Indian government hosts)."""
    host = (urlparse(url).hostname or "").lower()
    if host.endswith(".gov.in") or host.endswith(".nic.in"):
        return GOV_TIMEOUT
    return DEFAULT_TIMEOUT


def is_retryable(exc: BaseException) -> bool:
    """Return True only for transient failures worth retrying.

    Transport errors and 5xx responses are retried; client errors (4xx) and
    anything else re-raise immediately — retrying a 404 or 403 is pointless.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


@retry(
    stop=stop_after_attempt(MAX_ATTEMPTS),
    wait=wait_exponential(min=BACKOFF_MIN_SECONDS, max=BACKOFF_MAX_SECONDS),
    retry=retry_if_exception(is_retryable),
    reraise=True,
)
def fetch_url(
    url: str,
    timeout: Optional[float] = None,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    allow_not_modified: bool = False,
    retries: Optional[int] = None,  # deprecated: retries are handled by tenacity
) -> httpx.Response:
    """GET *url* and return the response, retrying transient failures.

    ``allow_not_modified=True`` returns a ``304 Not Modified`` response as-is
    instead of raising, letting callers skip unchanged content. Raises
    ``httpx.HTTPError`` (after retries) if the request ultimately fails.
    """
    merged_headers = {"User-Agent": USER_AGENT}
    if headers:
        merged_headers.update(headers)

    response = httpx.get(
        url,
        headers=merged_headers,
        params=params,
        timeout=timeout or default_timeout_for(url),
        follow_redirects=True,
    )

    if allow_not_modified and response.status_code == 304:
        return response
    response.raise_for_status()
    return response


def fetch_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> Any:
    """GET *url* and return its parsed JSON body (raising on non-2xx)."""
    response = fetch_url(url, params=params, timeout=timeout)
    return response.json()
