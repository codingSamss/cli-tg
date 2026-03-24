"""Tests for core update dedupe/stale guard."""

from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from src.bot import core as core_module
from src.bot.core import ClaudeCodeBot


@pytest.mark.asyncio
async def test_update_guard_blocks_duplicate_update():
    """Duplicate updates should be blocked by the guard."""
    bot = ClaudeCodeBot(settings=SimpleNamespace(), dependencies={})
    recorded_ids: list[int] = []
    bot._update_offset_store = SimpleNamespace(record=recorded_ids.append)

    update = SimpleNamespace(update_id=2026001)
    context = SimpleNamespace()

    await bot._handle_update_guard(update, context)
    assert recorded_ids == [2026001]

    with pytest.raises(ApplicationHandlerStop):
        await bot._handle_update_guard(update, context)

    assert recorded_ids == [2026001]


@pytest.mark.asyncio
async def test_update_guard_marks_update_activity_timestamp():
    """Any accepted update should refresh polling activity timestamp."""
    bot = ClaudeCodeBot(settings=SimpleNamespace(), dependencies={})
    bot._update_offset_store = SimpleNamespace(record=lambda _update_id: None)
    assert bot._last_update_activity_monotonic == 0.0

    await bot._handle_update_guard(
        SimpleNamespace(update_id=2026002), SimpleNamespace()
    )

    assert bot._last_update_activity_monotonic > 0.0


@pytest.mark.asyncio
async def test_update_guard_blocks_stale_update_before_dedupe():
    """Updates below persisted startup offset should be skipped."""
    bot = ClaudeCodeBot(settings=SimpleNamespace(), dependencies={})
    bot._startup_min_update_id = 300
    recorded_ids: list[int] = []
    bot._update_offset_store = SimpleNamespace(record=recorded_ids.append)

    stale_update = SimpleNamespace(update_id=299)

    with pytest.raises(ApplicationHandlerStop):
        await bot._handle_update_guard(stale_update, SimpleNamespace())

    assert recorded_ids == []


@pytest.mark.asyncio
async def test_update_guard_flags_restart_on_duplicate_streak():
    """Repeated duplicate updates should trigger polling self-recovery flag."""
    bot = ClaudeCodeBot(settings=SimpleNamespace(), dependencies={})
    bot._update_offset_store = SimpleNamespace(record=lambda _update_id: None)

    update = SimpleNamespace(update_id=2027001)

    await bot._handle_update_guard(update, SimpleNamespace())

    threshold = core_module._DUPLICATE_UPDATE_RECOVERY_THRESHOLD
    for _ in range(max(0, threshold - 1)):
        with pytest.raises(ApplicationHandlerStop):
            await bot._handle_update_guard(update, SimpleNamespace())
        assert bot._polling_restart_requested is False

    with pytest.raises(ApplicationHandlerStop):
        await bot._handle_update_guard(update, SimpleNamespace())

    assert bot._polling_restart_requested is True
    assert bot._duplicate_update_id == 2027001
    assert bot._duplicate_update_repeat_count == threshold


@pytest.mark.asyncio
async def test_update_guard_resets_duplicate_streak_after_new_update():
    """A new update id should clear duplicate streak tracking state."""
    bot = ClaudeCodeBot(settings=SimpleNamespace(), dependencies={})
    recorded_ids: list[int] = []
    bot._update_offset_store = SimpleNamespace(record=recorded_ids.append)

    first_update = SimpleNamespace(update_id=2028001)
    second_update = SimpleNamespace(update_id=2028002)

    await bot._handle_update_guard(first_update, SimpleNamespace())

    with pytest.raises(ApplicationHandlerStop):
        await bot._handle_update_guard(first_update, SimpleNamespace())

    assert bot._duplicate_update_id == 2028001
    assert bot._duplicate_update_repeat_count == 1

    await bot._handle_update_guard(second_update, SimpleNamespace())

    assert bot._duplicate_update_id is None
    assert bot._duplicate_update_repeat_count == 0
    assert recorded_ids == [2028001, 2028002]
