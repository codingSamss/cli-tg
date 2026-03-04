"""Tests for queue inline callback actions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from src.bot.handlers.callback import handle_callback_query
from src.bot.inbound_task_queue import InboundTaskQueue


class _FakeAuthManager:
    """Simple auth manager stub for callback guard."""

    def is_authenticated(self, user_id: int) -> bool:
        return True

    def refresh_session(self, user_id: int) -> bool:
        return True


async def _noop() -> None:
    return None


def _build_query(
    user_id: int, chat_id: int, data: str, *, message_id: int | None = None
):
    """Build callback query stub."""
    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        message_thread_id=None,
    )
    if isinstance(message_id, int):
        message.message_id = message_id

    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=message,
    )


@pytest.mark.asyncio
async def test_queue_dequeue_callback_removes_item(tmp_path):
    user_id = 8701
    chat_id = 9701
    scope_key = f"{user_id}:{chat_id}:0"
    inbound_queue = InboundTaskQueue()
    queued_item, _ = await inbound_queue.enqueue(
        user_id=user_id,
        scope_key=scope_key,
        kind="text",
        preview="queued",
        executor=_noop,
    )
    query = _build_query(
        user_id=user_id,
        chat_id=chat_id,
        data=f"queue:dequeue:{queued_item.queue_id}",
    )
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(approved_directory=tmp_path),
            "auth_manager": _FakeAuthManager(),
            "inbound_task_queue": inbound_queue,
        },
        user_data={},
    )

    await handle_callback_query(update, context)

    query.answer.assert_awaited_once_with()
    query.edit_message_text.assert_awaited_once_with(
        f"✅ Removed queue item #{queued_item.queue_id}."
    )
    remaining = await inbound_queue.list_items(user_id=user_id, scope_key=scope_key)
    assert remaining == []


@pytest.mark.asyncio
async def test_queue_dequeue_callback_handles_missing_item(tmp_path):
    user_id = 8702
    chat_id = 9702
    query = _build_query(
        user_id=user_id,
        chat_id=chat_id,
        data="queue:dequeue:9999",
    )
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(approved_directory=tmp_path),
            "auth_manager": _FakeAuthManager(),
            "inbound_task_queue": InboundTaskQueue(),
        },
        user_data={},
    )

    await handle_callback_query(update, context)

    query.answer.assert_awaited_once_with()
    query.edit_message_text.assert_awaited_once_with("⚠️ Queue item #9999 not found.")


@pytest.mark.asyncio
async def test_queue_dequeue_callback_deletes_queue_and_source_bubbles_when_possible(
    tmp_path,
):
    user_id = 8703
    chat_id = 9703
    scope_key = f"{user_id}:{chat_id}:0"
    inbound_queue = InboundTaskQueue()
    queued_item, _ = await inbound_queue.enqueue(
        user_id=user_id,
        scope_key=scope_key,
        kind="text",
        preview="queued",
        source_message_id=12345,
        executor=_noop,
    )
    query = _build_query(
        user_id=user_id,
        chat_id=chat_id,
        data=f"queue:dequeue:{queued_item.queue_id}",
        message_id=67890,
    )
    update = SimpleNamespace(callback_query=query)
    delete_message = AsyncMock()
    context = SimpleNamespace(
        bot=SimpleNamespace(delete_message=delete_message),
        bot_data={
            "settings": SimpleNamespace(approved_directory=tmp_path),
            "auth_manager": _FakeAuthManager(),
            "inbound_task_queue": inbound_queue,
        },
        user_data={},
    )

    await handle_callback_query(update, context)

    query.answer.assert_awaited_once_with()
    query.edit_message_text.assert_not_awaited()
    assert delete_message.await_args_list == [
        call(chat_id=chat_id, message_id=12345),
        call(chat_id=chat_id, message_id=67890),
    ]
    remaining = await inbound_queue.list_items(user_id=user_id, scope_key=scope_key)
    assert remaining == []
