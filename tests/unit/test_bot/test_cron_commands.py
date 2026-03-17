"""Tests for /cron command handler."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.handlers.command import cron_command
from src.services.cron_scheduler_service import (
    CRON_JOB_TYPE_REMINDER,
    CronSchedulerService,
)


class _DummyCronScheduler(CronSchedulerService):
    def __init__(self) -> None:  # pragma: no cover - simple test helper
        pass

    async def list_user_jobs(self, *, user_id: int):
        return []

    async def create_cron_job(
        self,
        *,
        user_id: int,
        chat_id: int,
        thread_id: int,
        scope_key: str,
        project_dir,
        job_type: str,
        cron_expr: str,
        payload_text: str,
        engine=None,
    ):
        self.create_args = {
            "user_id": user_id,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "scope_key": scope_key,
            "project_dir": str(project_dir),
            "job_type": job_type,
            "cron_expr": cron_expr,
            "payload_text": payload_text,
            "engine": engine,
        }
        return SimpleNamespace(
            id=12,
            job_type=job_type,
            cron_expr=cron_expr,
            next_run_at=datetime(2026, 3, 19, 1, 0, 0),
        )

    async def pause_job(self, *, user_id: int, job_id: int) -> bool:
        self.paused = (user_id, job_id)
        return True

    async def resume_job(self, *, user_id: int, job_id: int) -> bool:
        return True

    async def delete_job(self, *, user_id: int, job_id: int) -> bool:
        return True


def _build_update(user_id: int, chat_id: int):
    message = SimpleNamespace(
        chat_id=chat_id,
        message_id=1234,
        message_thread_id=None,
        reply_text=AsyncMock(),
    )
    return message, SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message,
        message=message,
    )


@pytest.mark.asyncio
async def test_cron_list_empty(tmp_path):
    user_id = 801
    chat_id = 901
    message, update = _build_update(user_id, chat_id)
    scheduler = _DummyCronScheduler()
    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(approved_directory=tmp_path),
            "cron_scheduler_service": scheduler,
        },
        user_data={},
        args=["list"],
    )

    await cron_command(update, context)

    message.reply_text.assert_awaited_once()
    rendered = message.reply_text.await_args.args[0]
    assert "No cron jobs" in rendered


@pytest.mark.asyncio
async def test_cron_add_reminder_parses_and_calls_scheduler(tmp_path):
    user_id = 802
    chat_id = 902
    message, update = _build_update(user_id, chat_id)
    scheduler = _DummyCronScheduler()
    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(approved_directory=tmp_path),
            "cron_scheduler_service": scheduler,
        },
        user_data={},
        args=["add", "reminder", "0", "9", "*", "*", "1-5", "standup", "reminder"],
    )

    await cron_command(update, context)

    assert scheduler.create_args["user_id"] == user_id
    assert scheduler.create_args["job_type"] == CRON_JOB_TYPE_REMINDER
    assert scheduler.create_args["cron_expr"] == "0 9 * * 1-5"
    assert scheduler.create_args["payload_text"] == "standup reminder"
    message.reply_text.assert_awaited_once()
    rendered = message.reply_text.await_args.args[0]
    assert "Cron job created" in rendered


@pytest.mark.asyncio
async def test_cron_pause_uses_message_reaction(tmp_path):
    user_id = 803
    chat_id = 903
    message, update = _build_update(user_id, chat_id)
    set_message_reaction = AsyncMock(return_value=True)
    scheduler = _DummyCronScheduler()
    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(approved_directory=tmp_path),
            "cron_scheduler_service": scheduler,
        },
        bot=SimpleNamespace(set_message_reaction=set_message_reaction),
        user_data={},
        args=["pause", "18"],
    )

    await cron_command(update, context)

    assert scheduler.paused == (user_id, 18)
    set_message_reaction.assert_awaited_once()
