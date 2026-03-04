"""In-memory inbound message queue for busy-session fallback."""

from __future__ import annotations

import asyncio
import copy
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque, Dict, Optional

QueueExecutor = Callable[[], Awaitable[None]]


@dataclass
class QueuedInboundTask:
    """Queued inbound task metadata."""

    queue_id: int
    user_id: int
    scope_key: str
    kind: str
    preview: str
    executor: QueueExecutor = field(repr=False, compare=False)
    source_message_id: Optional[int] = None
    created_at: datetime = field(default_factory=datetime.now)


class InboundTaskQueue:
    """Thread-safe in-memory FIFO queues keyed by scope."""

    def __init__(self) -> None:
        self._queues: Dict[str, Deque[QueuedInboundTask]] = {}
        self._lock = asyncio.Lock()
        self._next_queue_id = 1

    async def enqueue(
        self,
        *,
        user_id: int,
        scope_key: str,
        kind: str,
        preview: str,
        source_message_id: Optional[int] = None,
        executor: QueueExecutor,
    ) -> tuple[QueuedInboundTask, int]:
        """Push one inbound task and return (item, position_in_scope_queue)."""
        normalized_scope = str(scope_key or "").strip() or f"user:{user_id}"
        normalized_kind = str(kind or "").strip().lower() or "text"
        normalized_preview = str(preview or "").strip() or "(empty)"
        normalized_source_message_id = (
            source_message_id
            if isinstance(source_message_id, int) and source_message_id > 0
            else None
        )

        async with self._lock:
            queue = self._queues.setdefault(normalized_scope, deque())
            queue_id = self._next_queue_id
            self._next_queue_id += 1
            item = QueuedInboundTask(
                queue_id=queue_id,
                user_id=user_id,
                scope_key=normalized_scope,
                kind=normalized_kind,
                preview=normalized_preview,
                source_message_id=normalized_source_message_id,
                executor=executor,
            )
            queue.append(item)
            return copy.copy(item), len(queue)

    async def pop_next(
        self,
        *,
        user_id: int,
        scope_key: str,
    ) -> Optional[QueuedInboundTask]:
        """Pop next queued task for the given scope and user."""
        normalized_scope = str(scope_key or "").strip() or f"user:{user_id}"
        async with self._lock:
            queue = self._queues.get(normalized_scope)
            if not queue:
                return None
            while queue:
                item = queue.popleft()
                if item.user_id == user_id:
                    if not queue:
                        self._queues.pop(normalized_scope, None)
                    return copy.copy(item)
            self._queues.pop(normalized_scope, None)
            return None

    async def list_items(
        self,
        *,
        user_id: int,
        scope_key: str,
    ) -> list[QueuedInboundTask]:
        """List queued tasks for the current user/scope in FIFO order."""
        normalized_scope = str(scope_key or "").strip() or f"user:{user_id}"
        async with self._lock:
            queue = self._queues.get(normalized_scope, deque())
            return [copy.copy(item) for item in queue if item.user_id == user_id]

    async def remove(
        self,
        *,
        user_id: int,
        scope_key: str,
        queue_id: int,
    ) -> Optional[QueuedInboundTask]:
        """Remove one queued task by queue_id for the current user/scope."""
        normalized_scope = str(scope_key or "").strip() or f"user:{user_id}"
        async with self._lock:
            queue = self._queues.get(normalized_scope)
            if not queue:
                return None

            removed: Optional[QueuedInboundTask] = None
            remaining: Deque[QueuedInboundTask] = deque()
            for item in queue:
                if (
                    removed is None
                    and item.user_id == user_id
                    and item.queue_id == queue_id
                ):
                    removed = item
                    continue
                remaining.append(item)

            if remaining:
                self._queues[normalized_scope] = remaining
            else:
                self._queues.pop(normalized_scope, None)

            return copy.copy(removed) if removed else None
