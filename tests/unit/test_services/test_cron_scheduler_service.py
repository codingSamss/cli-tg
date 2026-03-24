"""Tests for model-first cron/reminder intent flow."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from src.services.cron_scheduler_service import (
    CRON_INTENT_CRON,
    CRON_INTENT_ONCE,
    CRON_JOB_TYPE_REMINDER,
    CRON_OPERATION_CREATE,
    CRON_OPERATION_UPDATE,
    CRON_SCHEDULE_CRON,
    CRON_SCHEDULE_ONCE,
    REMINDER_MUTATION_CREATED,
    REMINDER_MUTATION_UPDATED,
    CronSchedulerService,
    CronValidationError,
    ParsedReminderIntent,
    parse_model_reminder_intent,
)


class _DummyScheduler(CronSchedulerService):
    """Minimal scheduler stub for create_natural_language_reminder tests."""

    def __init__(self, intent: ParsedReminderIntent | None) -> None:
        self.settings = cast(Any, SimpleNamespace(cron_nl_min_delay_seconds=60))
        self._timezone = ZoneInfo("Asia/Shanghai")
        self.intent = intent
        self.once_call_args: Optional[dict[str, Any]] = None
        self.cron_call_args: Optional[dict[str, Any]] = None
        self.update_call_args: Optional[dict[str, Any]] = None
        self.infer_call_args: Optional[dict[str, Any]] = None

    async def _infer_reminder_intent(  # type: ignore[override]
        self, **kwargs: Any
    ) -> ParsedReminderIntent | None:
        self.infer_call_args = kwargs
        on_stream = kwargs.get("on_stream")
        if callable(on_stream):
            await on_stream(
                SimpleNamespace(
                    type="progress",
                    content="turn.started",
                    metadata={"subtype": "turn.started"},
                )
            )
        return self.intent

    async def create_one_shot_reminder(  # type: ignore[override]
        self, **kwargs: Any
    ) -> Any:
        self.once_call_args = kwargs
        return SimpleNamespace(
            id=101,
            schedule_type=CRON_SCHEDULE_ONCE,
            payload_text=kwargs["reminder_text"],
            run_at=kwargs["run_at"],
            cron_expr=None,
            next_run_at=kwargs["run_at"],
        )

    async def create_cron_job(self, **kwargs: Any) -> Any:  # type: ignore[override]
        self.cron_call_args = kwargs
        return SimpleNamespace(
            id=102,
            schedule_type=CRON_SCHEDULE_CRON,
            payload_text=kwargs["payload_text"],
            run_at=None,
            cron_expr=kwargs["cron_expr"],
            next_run_at=None,
        )

    async def _apply_natural_language_reminder_update(  # type: ignore[override]
        self, **kwargs: Any
    ) -> Any:
        self.update_call_args = kwargs
        parsed = kwargs["parsed"]
        return SimpleNamespace(
            id=103,
            schedule_type=(
                CRON_SCHEDULE_ONCE
                if parsed.intent == CRON_INTENT_ONCE
                else CRON_SCHEDULE_CRON
            ),
            payload_text=parsed.reminder_text or "沿用内容",
            run_at=parsed.run_at,
            cron_expr=parsed.cron_expr,
            next_run_at=parsed.run_at,
        )


def test_parse_model_reminder_intent_once() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    parsed = parse_model_reminder_intent(
        (
            '{"intent":"once","reminder_text":"喝水",'
            '"run_at":"2026-03-18T10:30:00+08:00","cron_expr":null}'
        ),
        timezone_hint=tz,
    )

    assert parsed is not None
    assert parsed.operation == CRON_OPERATION_CREATE
    assert parsed.intent == CRON_INTENT_ONCE
    assert parsed.reminder_text == "喝水"
    assert parsed.run_at is not None
    assert parsed.run_at.utcoffset() == timedelta(hours=8)


def test_parse_model_reminder_intent_cron_from_wrapped_text() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    parsed = parse_model_reminder_intent(
        (
            "result:\n"
            "```json\n"
            '{"intent":"cron","reminder_text":"写周报","run_at":null,'
            '"cron_expr":"0 18 * * 1-5"}\n'
            "```"
        ),
        timezone_hint=tz,
    )

    assert parsed is not None
    assert parsed.operation == CRON_OPERATION_CREATE
    assert parsed.intent == CRON_INTENT_CRON
    assert parsed.reminder_text == "写周报"
    assert parsed.cron_expr == "0 18 * * 1-5"


def test_parse_model_reminder_intent_update_without_text() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    parsed = parse_model_reminder_intent(
        (
            '{"operation":"update","intent":"once","target_job_id":12,'
            '"reminder_text":null,"run_at":"2026-03-18T12:00:00+08:00","cron_expr":null}'
        ),
        timezone_hint=tz,
    )

    assert parsed is not None
    assert parsed.operation == CRON_OPERATION_UPDATE
    assert parsed.intent == CRON_INTENT_ONCE
    assert parsed.target_job_id == 12
    assert parsed.reminder_text is None
    assert parsed.run_at is not None


def test_parse_model_reminder_intent_invalid_returns_none() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    assert parse_model_reminder_intent("not json", timezone_hint=tz) is None
    assert (
        parse_model_reminder_intent(
            '{"intent":"none","reminder_text":null,"run_at":null,"cron_expr":null}',
            timezone_hint=tz,
        )
        is None
    )


@pytest.mark.asyncio
async def test_create_natural_language_reminder_once_path() -> None:
    run_at = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(minutes=5)
    scheduler = _DummyScheduler(
        ParsedReminderIntent(
            operation=CRON_OPERATION_CREATE,
            intent=CRON_INTENT_ONCE,
            reminder_text="开会",
            run_at=run_at,
        )
    )
    stream_updates: list[str] = []

    async def _capture_stream(update: Any) -> None:
        stream_updates.append(str(getattr(update, "type", "")))

    result = await scheduler.create_natural_language_reminder(
        text="五分钟后提醒我开会",
        user_id=1,
        chat_id=2,
        thread_id=0,
        scope_key="s",
        project_dir=Path("."),
        cli_integration=object(),
        on_stream=_capture_stream,
    )

    assert result is not None
    assert result.action == REMINDER_MUTATION_CREATED
    assert result.job.id == 101
    assert scheduler.infer_call_args is not None
    assert scheduler.infer_call_args["on_stream"] is _capture_stream
    assert stream_updates == ["progress"]
    assert scheduler.once_call_args is not None
    assert scheduler.once_call_args["reminder_text"] == "开会"
    assert scheduler.cron_call_args is None


@pytest.mark.asyncio
async def test_create_natural_language_reminder_once_too_close_raises() -> None:
    run_at = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(seconds=10)
    scheduler = _DummyScheduler(
        ParsedReminderIntent(
            operation=CRON_OPERATION_CREATE,
            intent=CRON_INTENT_ONCE,
            reminder_text="喝水",
            run_at=run_at,
        )
    )

    with pytest.raises(CronValidationError):
        await scheduler.create_natural_language_reminder(
            text="十秒后提醒我喝水",
            user_id=1,
            chat_id=2,
            thread_id=0,
            scope_key="s",
            project_dir=Path("."),
            cli_integration=object(),
            on_stream=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_create_natural_language_reminder_cron_path() -> None:
    scheduler = _DummyScheduler(
        ParsedReminderIntent(
            operation=CRON_OPERATION_CREATE,
            intent=CRON_INTENT_CRON,
            reminder_text="站会",
            cron_expr="0 9 * * 1-5",
        )
    )

    result = await scheduler.create_natural_language_reminder(
        text="工作日早上9点提醒我站会",
        user_id=1,
        chat_id=2,
        thread_id=0,
        scope_key="s",
        project_dir=Path("."),
        cli_integration=object(),
        on_stream=AsyncMock(),
    )

    assert result is not None
    assert result.action == REMINDER_MUTATION_CREATED
    assert result.job.id == 102
    assert scheduler.cron_call_args is not None
    assert scheduler.cron_call_args["job_type"] == CRON_JOB_TYPE_REMINDER
    assert scheduler.cron_call_args["cron_expr"] == "0 9 * * 1-5"
    assert scheduler.cron_call_args["payload_text"] == "站会"
    assert scheduler.once_call_args is None


@pytest.mark.asyncio
async def test_create_natural_language_reminder_update_path() -> None:
    run_at = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(minutes=30)
    scheduler = _DummyScheduler(
        ParsedReminderIntent(
            operation=CRON_OPERATION_UPDATE,
            intent=CRON_INTENT_ONCE,
            reminder_text=None,
            run_at=run_at,
            target_job_id=88,
        )
    )

    result = await scheduler.create_natural_language_reminder(
        text="改成中午吧",
        user_id=1,
        chat_id=2,
        thread_id=0,
        scope_key="s",
        project_dir=Path("."),
        cli_integration=object(),
        on_stream=AsyncMock(),
    )

    assert result is not None
    assert result.action == REMINDER_MUTATION_UPDATED
    assert result.job.id == 103
    assert scheduler.update_call_args is not None
    assert scheduler.update_call_args["parsed"].target_job_id == 88


@pytest.mark.asyncio
async def test_create_natural_language_reminder_none_returns_none() -> None:
    scheduler = _DummyScheduler(None)

    created = await scheduler.create_natural_language_reminder(
        text="帮我总结今天代码",
        user_id=1,
        chat_id=2,
        thread_id=0,
        scope_key="s",
        project_dir=Path("."),
        cli_integration=object(),
        on_stream=AsyncMock(),
    )
    assert created is None
