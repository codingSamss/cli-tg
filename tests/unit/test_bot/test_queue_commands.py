"""Tests for /queue and /dequeue command handlers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.handlers.command import dequeue_command, queue_status_command
from src.bot.inbound_task_queue import InboundTaskQueue


async def _noop() -> None:
    return None


def _build_update(user_id: int, chat_id: int):
    message = SimpleNamespace(
        chat_id=chat_id,
        message_id=1001,
        message_thread_id=None,
        reply_text=AsyncMock(),
    )
    return (
        message,
        SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=chat_id),
            effective_message=message,
            message=message,
        ),
    )


@pytest.mark.asyncio
async def test_queue_status_command_lists_running_and_pending_items(tmp_path):
    user_id = 9101
    chat_id = 9201
    scope_key = f"{user_id}:{chat_id}:0"
    inbound_queue = InboundTaskQueue()
    queued_item, _ = await inbound_queue.enqueue(
        user_id=user_id,
        scope_key=scope_key,
        kind="text",
        preview="queued preview",
        executor=_noop,
    )
    task_registry = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(prompt_summary="running prompt"))
    )
    message, update = _build_update(user_id, chat_id)
    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(approved_directory=tmp_path),
            "task_registry": task_registry,
            "inbound_task_queue": inbound_queue,
        },
        user_data={},
    )

    await queue_status_command(update, context)

    task_registry.get.assert_awaited_once_with(user_id, scope_key=scope_key)
    message.reply_text.assert_awaited_once()
    rendered = message.reply_text.await_args.args[0]
    assert "Running: yes" in rendered
    assert f"#{queued_item.queue_id}" in rendered
    assert "Use /dequeue <id>" in rendered


@pytest.mark.asyncio
async def test_dequeue_command_removes_target_queue_item(tmp_path):
    user_id = 9102
    chat_id = 9202
    scope_key = f"{user_id}:{chat_id}:0"
    inbound_queue = InboundTaskQueue()
    queued_item, _ = await inbound_queue.enqueue(
        user_id=user_id,
        scope_key=scope_key,
        kind="text",
        preview="queued preview",
        executor=_noop,
    )
    message, update = _build_update(user_id, chat_id)
    set_message_reaction = AsyncMock(return_value=True)
    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(approved_directory=tmp_path),
            "inbound_task_queue": inbound_queue,
        },
        bot=SimpleNamespace(set_message_reaction=set_message_reaction),
        user_data={},
        args=[str(queued_item.queue_id)],
    )

    await dequeue_command(update, context)

    message.reply_text.assert_not_awaited()
    set_message_reaction.assert_awaited_once_with(
        chat_id=chat_id,
        message_id=message.message_id,
        reaction=["✅"],
        is_big=False,
    )
    remaining = await inbound_queue.list_items(user_id=user_id, scope_key=scope_key)
    assert remaining == []


@pytest.mark.asyncio
async def test_dequeue_command_rejects_invalid_queue_id(tmp_path):
    user_id = 9103
    chat_id = 9203
    message, update = _build_update(user_id, chat_id)
    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(approved_directory=tmp_path),
            "inbound_task_queue": InboundTaskQueue(),
        },
        user_data={},
        args=["oops"],
    )

    await dequeue_command(update, context)

    message.reply_text.assert_awaited_once_with(
        "Invalid queue id. Example: /dequeue 12"
    )
