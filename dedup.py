"""Cross-run item deduplication, run before the DeepSeek step to save cost.

Each scraped item gets a stable hash over its normalized ``(title, url)`` pair.
The aggregator checks every item against a persisted "seen" store and only hands
genuinely new items to DeepSeek, then marks them seen *after* a successful Sheet
write so a failed run can retry cleanly.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Set

from cache_store import CacheStore

SEEN_STORE_PATH = "cache/seen.json"
_SEEN_KEY = "hashes"


def item_hash(title: str, url: str) -> str:
    """Return a stable identity for an item from its title and URL."""
    return hashlib.sha256(f"{title}|{url}".lower().strip().encode()).hexdigest()


class SeenStore:
    """Persisted set of item hashes already written to the log sheet."""

    def __init__(self, path: str = SEEN_STORE_PATH) -> None:
        self._store = CacheStore(path)

    def load(self) -> Set[str]:
        """Return the set of previously seen item hashes."""
        hashes = self._store.get(_SEEN_KEY, [])
        return {h for h in (hashes or []) if isinstance(h, str)}

    def add(self, hashes: Iterable[str]) -> None:
        """Persist *hashes* as seen (idempotent union with existing values)."""
        merged = sorted(self.load() | set(hashes))
        self._store.set(_SEEN_KEY, merged)

    def contains(self, hash_: str) -> bool:
        """Return True if *hash_* has already been seen."""
        return hash_ in self.load()
