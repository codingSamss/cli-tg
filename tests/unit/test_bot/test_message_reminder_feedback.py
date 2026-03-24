from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.handlers.message import (
    _looks_like_reminder_adjustment,
    _looks_like_reminder_request,
    handle_text_message,
)
from src.services.cron_scheduler_service import CronSchedulerService


class _ReminderSchedulerStub(CronSchedulerService):
    def __init__(
        self, reminder_result: object | None, delay_seconds: float = 0.05
    ) -> None:
        self._reminder_result = reminder_result
        self._delay_seconds = delay_seconds
        self.called = False
        self.has_active = False
        self.on_stream = None

    async def create_natural_language_reminder(
        self,
        *,
        text: str,
        user_id: int,
        chat_id: int,
        thread_id: int,
        scope_key: str,
        project_dir,
        cli_integration,
        on_stream=None,
    ) -> object:
        self.called = True
        self.on_stream = on_stream
        await asyncio.sleep(self._delay_seconds)
        return self._reminder_result

    async def has_active_reminder(self, *, user_id: int, scope_key: str) -> bool:
        return self.has_active

    async def count_user_pending_reminders(self, *, user_id: int) -> int:
        return 1


def _build_status_message(chat_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        edit_text=AsyncMock(),
        delete=AsyncMock(),
        chat=SimpleNamespace(id=chat_id, type="private"),
        chat_id=chat_id,
        message_thread_id=None,
        message_id=456,
    )


def _build_claude_integration() -> SimpleNamespace:
    return SimpleNamespace(run_command=AsyncMock())


def _build_bot() -> SimpleNamespace:
    return SimpleNamespace(set_message_reaction=AsyncMock())


@pytest.mark.asyncio
async def test_handle_text_message_sends_confirmation_when_reminder_created(
    tmp_path,
) -> None:
    scheduler = _ReminderSchedulerStub(
        SimpleNamespace(
            action="created",
            job=SimpleNamespace(
                id=1,
                schedule_type="once",
                run_at=datetime(2026, 3, 18, 2, 19, 26),
                payload_text="睡觉",
            ),
        ),
        delay_seconds=0.08,
    )
    send_action = AsyncMock()
    progress_message = _build_status_message(chat_id=901)
    reply_text = AsyncMock(return_value=progress_message)
    message = SimpleNamespace(
        text="10分钟后提醒我睡觉",
        message_id=123,
        message_thread_id=None,
        reply_text=reply_text,
        chat=SimpleNamespace(id=901, type="private", send_action=send_action),
        chat_id=901,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=801),
        effective_chat=SimpleNamespace(id=901, type="private"),
        effective_message=message,
        message=message,
    )
    context = SimpleNamespace(
        bot=_build_bot(),
        bot_data={
            "settings": SimpleNamespace(approved_directory=tmp_path),
            "cron_scheduler_service": scheduler,
            "claude_integration": _build_claude_integration(),
        },
        user_data={},
    )

    await handle_text_message(update, context)

    assert scheduler.called is True
    assert scheduler.on_stream is not None
    assert send_action.await_count >= 1
    reply_text.assert_awaited_once()
    progress_message.edit_text.assert_awaited_once()
    rendered = progress_message.edit_text.await_args.args[0]
    assert "已设置提醒" in rendered
    assert "当前待提醒：1 条" in rendered
    assert "睡觉" in rendered


@pytest.mark.asyncio
async def test_handle_text_message_can_update_reminder_on_followup_phrase(
    tmp_path,
) -> None:
    scheduler = _ReminderSchedulerStub(
        SimpleNamespace(
            action="updated",
            job=SimpleNamespace(
                id=1,
                schedule_type="once",
                run_at=datetime(2026, 3, 20, 4, 0, 0),
                payload_text="买花给小宝",
            ),
        ),
        delay_seconds=0.08,
    )
    scheduler.has_active = True
    send_action = AsyncMock()
    progress_message = _build_status_message(chat_id=904)
    reply_text = AsyncMock(return_value=progress_message)
    message = SimpleNamespace(
        text="五点太晚了，中午吧",
        message_id=126,
        message_thread_id=None,
        reply_text=reply_text,
        chat=SimpleNamespace(id=904, type="private", send_action=send_action),
        chat_id=904,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=804),
        effective_chat=SimpleNamespace(id=904, type="private"),
        effective_message=message,
        message=message,
    )
    context = SimpleNamespace(
        bot=_build_bot(),
        bot_data={
            "settings": SimpleNamespace(approved_directory=tmp_path),
            "cron_scheduler_service": scheduler,
            "claude_integration": _build_claude_integration(),
        },
        user_data={},
    )

    await handle_text_message(update, context)

    assert scheduler.called is True
    assert scheduler.on_stream is not None
    assert send_action.await_count >= 1
    reply_text.assert_awaited_once()
    progress_message.edit_text.assert_awaited_once()
    rendered = progress_message.edit_text.await_args.args[0]
    assert "已更新提醒" in rendered
    assert "买花给小宝" in rendered


@pytest.mark.asyncio
async def test_handle_text_message_skips_pre_progress_for_reminder_like_non_reminder_path(
    tmp_path, monkeypatch
) -> None:
    scheduler = _ReminderSchedulerStub(None, delay_seconds=0.08)
    send_action = AsyncMock()
    progress_message = _build_status_message(chat_id=902)
    reply_text = AsyncMock(return_value=progress_message)
    message = SimpleNamespace(
        text="10分钟后提醒我看看这个文案是否需要优化",
        message_id=124,
        message_thread_id=None,
        reply_text=reply_text,
        chat=SimpleNamespace(id=902, type="private", send_action=send_action),
        chat_id=902,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=802),
        effective_chat=SimpleNamespace(id=902, type="private"),
        effective_message=message,
        message=message,
    )
    context = SimpleNamespace(
        bot=_build_bot(),
        bot_data={
            "settings": SimpleNamespace(approved_directory=tmp_path),
            "cron_scheduler_service": scheduler,
            "task_registry": SimpleNamespace(is_busy=AsyncMock(return_value=True)),
            "claude_integration": _build_claude_integration(),
        },
        user_data={},
    )
    monkeypatch.setattr(
        "src.bot.handlers.message._enqueue_busy_text_task",
        AsyncMock(return_value=None),
    )

    await handle_text_message(update, context)

    assert scheduler.called is True
    assert scheduler.on_stream is not None
    assert send_action.await_count >= 1
    assert reply_text.await_count == 2
    progress_message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_text_message_skips_reminder_inference_for_plain_text(
    tmp_path, monkeypatch
) -> None:
    scheduler = _ReminderSchedulerStub(None, delay_seconds=0.08)
    send_action = AsyncMock()
    reply_text = AsyncMock()
    message = SimpleNamespace(
        text="帮我分析一下这个 Python 报错",
        message_id=125,
        message_thread_id=None,
        reply_text=reply_text,
        chat=SimpleNamespace(id=903, type="private", send_action=send_action),
        chat_id=903,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=803),
        effective_chat=SimpleNamespace(id=903, type="private"),
        effective_message=message,
        message=message,
    )
    context = SimpleNamespace(
        bot=_build_bot(),
        bot_data={
            "settings": SimpleNamespace(approved_directory=tmp_path),
            "cron_scheduler_service": scheduler,
            "task_registry": SimpleNamespace(is_busy=AsyncMock(return_value=True)),
            "claude_integration": _build_claude_integration(),
        },
        user_data={},
    )
    monkeypatch.setattr(
        "src.bot.handlers.message._enqueue_busy_text_task",
        AsyncMock(return_value=None),
    )

    await handle_text_message(update, context)

    assert scheduler.called is False
    assert send_action.await_count == 0
    reply_text.assert_awaited_once()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10分钟后提醒我喝水", True),
        ("remind me in 2 hours to stand up", True),
        ("帮我分析一下这个 Python 报错", False),
    ],
)
def test_looks_like_reminder_request(text: str, expected: bool) -> None:
    assert _looks_like_reminder_request(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("五点太晚了，中午吧", True),
        ("改成下午两点", True),
        ("帮我分析一下这个 Python 报错", False),
    ],
)
def test_looks_like_reminder_adjustment(text: str, expected: bool) -> None:
    assert _looks_like_reminder_adjustment(text) is expected
