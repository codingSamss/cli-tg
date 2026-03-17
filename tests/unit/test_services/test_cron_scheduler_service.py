"""Tests for cron scheduler service parsing helpers."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.services.cron_scheduler_service import parse_relative_reminder


def test_parse_relative_reminder_minutes_prefix():
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 3, 18, 9, 0, tzinfo=tz)

    parsed = parse_relative_reminder(
        "5分钟后提醒我拿奶茶",
        now=now,
        timezone_hint=tz,
    )

    assert parsed is not None
    assert parsed.content == "拿奶茶"
    assert parsed.run_at - now == timedelta(minutes=5)


def test_parse_relative_reminder_suffix_hours():
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 3, 18, 9, 0, tzinfo=tz)

    parsed = parse_relative_reminder(
        "提醒我开周会 2小时后",
        now=now,
        timezone_hint=tz,
    )

    assert parsed is not None
    assert parsed.content == "开周会"
    assert parsed.run_at - now == timedelta(hours=2)


def test_parse_relative_reminder_chinese_number():
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 3, 18, 9, 0, tzinfo=tz)

    parsed = parse_relative_reminder(
        "十分钟后提醒我喝水",
        now=now,
        timezone_hint=tz,
    )

    assert parsed is not None
    assert parsed.content == "喝水"
    assert parsed.run_at - now == timedelta(minutes=10)


def test_parse_relative_reminder_non_match_returns_none():
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 3, 18, 9, 0, tzinfo=tz)

    parsed = parse_relative_reminder(
        "请帮我总结今天的日志",
        now=now,
        timezone_hint=tz,
    )

    assert parsed is None
