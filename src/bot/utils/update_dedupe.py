"""In-memory dedupe cache for Telegram update ids."""

from __future__ import annotations

import time
from collections import OrderedDict


class UpdateDedupeCache:
    """Track recently processed update IDs with TTL and size bound."""

    def __init__(self, *, ttl_seconds: int = 300, max_size: int = 5000) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_size = max(1, int(max_size))
        self._seen_updates: OrderedDict[int, float] = OrderedDict()

    def check_and_mark(self, update_id: int) -> bool:
        """Return True if update_id already seen, otherwise store and return False."""
        normalized_id = int(update_id)
        now = time.monotonic()
        self._evict_expired(now)

        if normalized_id in self._seen_updates:
            self._seen_updates[normalized_id] = now
            self._seen_updates.move_to_end(normalized_id)
            return True

        self._seen_updates[normalized_id] = now
        self._seen_updates.move_to_end(normalized_id)
        self._evict_overflow()
        return False

    def _evict_expired(self, now: float) -> None:
        cutoff = now - self.ttl_seconds
        while self._seen_updates:
            oldest_id, oldest_timestamp = next(iter(self._seen_updates.items()))
            if oldest_timestamp >= cutoff:
                break
            self._seen_updates.pop(oldest_id, None)

    def _evict_overflow(self) -> None:
        while len(self._seen_updates) > self.max_size:
            self._seen_updates.popitem(last=False)

    def clear(self) -> None:
        """Clear all cached update ids."""
        self._seen_updates.clear()
