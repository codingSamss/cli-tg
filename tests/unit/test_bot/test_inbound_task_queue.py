"""Tests for inbound task queue behavior."""

from __future__ import annotations

import pytest

from src.bot.inbound_task_queue import InboundTaskQueue


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_enqueue_list_and_pop_preserve_fifo_order() -> None:
    queue = InboundTaskQueue()
    scope_key = "1001:-100:0"

    first, first_pos = await queue.enqueue(
        user_id=1001,
        scope_key=scope_key,
        kind="text",
        preview="first request",
        executor=_noop,
    )
    second, second_pos = await queue.enqueue(
        user_id=1001,
        scope_key=scope_key,
        kind="text",
        preview="second request",
        executor=_noop,
    )

    assert first_pos == 1
    assert second_pos == 2

    listed = await queue.list_items(user_id=1001, scope_key=scope_key)
    assert [item.queue_id for item in listed] == [first.queue_id, second.queue_id]

    popped_one = await queue.pop_next(user_id=1001, scope_key=scope_key)
    popped_two = await queue.pop_next(user_id=1001, scope_key=scope_key)
    popped_none = await queue.pop_next(user_id=1001, scope_key=scope_key)

    assert popped_one is not None and popped_one.queue_id == first.queue_id
    assert popped_two is not None and popped_two.queue_id == second.queue_id
    assert popped_none is None


@pytest.mark.asyncio
async def test_remove_specific_queue_item_by_id() -> None:
    queue = InboundTaskQueue()
    scope_key = "2001:-200:0"
    first, _ = await queue.enqueue(
        user_id=2001,
        scope_key=scope_key,
        kind="text",
        preview="alpha",
        executor=_noop,
    )
    second, _ = await queue.enqueue(
        user_id=2001,
        scope_key=scope_key,
        kind="text",
        preview="beta",
        executor=_noop,
    )

    removed = await queue.remove(
        user_id=2001,
        scope_key=scope_key,
        queue_id=first.queue_id,
    )
    remaining = await queue.list_items(user_id=2001, scope_key=scope_key)

    assert removed is not None
    assert removed.queue_id == first.queue_id
    assert [item.queue_id for item in remaining] == [second.queue_id]


@pytest.mark.asyncio
async def test_scope_and_user_isolation() -> None:
    queue = InboundTaskQueue()
    await queue.enqueue(
        user_id=3001,
        scope_key="3001:-1:0",
        kind="text",
        preview="u1",
        executor=_noop,
    )
    await queue.enqueue(
        user_id=3002,
        scope_key="3002:-1:0",
        kind="text",
        preview="u2",
        executor=_noop,
    )

    user_1_items = await queue.list_items(user_id=3001, scope_key="3001:-1:0")
    user_2_items = await queue.list_items(user_id=3002, scope_key="3002:-1:0")
    cross_scope_items = await queue.list_items(user_id=3001, scope_key="3002:-1:0")

    assert len(user_1_items) == 1
    assert len(user_2_items) == 1
    assert cross_scope_items == []
