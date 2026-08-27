"""Tiny JSON-file-backed key/value store for cross-run scraper state.

Used to persist HTTP conditional-GET headers (ETag / Last-Modified) per feed or
source URL, and the dedup "seen" hashes, so nightly runs can skip unchanged
content and avoid re-sending already-processed items.

Writes are atomic (``.tmp`` + ``os.replace``) and the parent directory is created
on demand, so the store works from a clean checkout and in CI.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class CacheStore:
    """A persistent JSON object with ``get``/``set`` accessors.

    The whole store is loaded into memory once and rewritten on each ``set``.
    This is fine for the small state files used here (a handful of URLs/hashes).
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: Dict[str, Any] = self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self) -> None:
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self._path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self._path)

    # -- API ---------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value stored under *key*, or *default* if absent."""
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Store *value* under *key* and persist immediately."""
        self._data[key] = value
        self._write()

    def all(self) -> Dict[str, Any]:
        """Return a copy of the full store."""
        return dict(self._data)
