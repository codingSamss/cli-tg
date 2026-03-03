"""Tests for polling self-heal and watchdog behavior."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.bot import core as core_module
from src.bot.core import ClaudeCodeBot


def test_polling_error_callback_flags_restart_after_threshold() -> None:
    """Repeated polling network errors should flag self-recovery."""
    bot = ClaudeCodeBot(settings=SimpleNamespace(), dependencies={})

    for _ in range(core_module._POLLING_RECOVERY_ERROR_THRESHOLD):
        bot._polling_error_callback(RuntimeError("network failure"))

    assert bot._polling_restart_requested is True


@pytest.mark.asyncio
async def test_restart_polling_stops_then_starts_updater() -> None:
    """Polling restart should stop current updater and start a new polling loop."""
    updater = SimpleNamespace(
        running=True,
        stop=AsyncMock(),
        start_polling=AsyncMock(),
    )
    bot = ClaudeCodeBot(
        settings=SimpleNamespace(webhook_url=None),
        dependencies={},
    )
    bot.app = SimpleNamespace(updater=updater)
    bot._polling_restart_requested = True
    bot._polling_error_count = 9
    bot._duplicate_update_id = 123
    bot._duplicate_update_repeat_count = 4
    bot._update_dedupe_cache = SimpleNamespace(clear=Mock())

    restarted = await bot._restart_polling(reason="unit_test")

    assert restarted is True
    updater.stop.assert_awaited_once()
    updater.start_polling.assert_awaited_once()
    kwargs = updater.start_polling.await_args.kwargs
    assert kwargs["drop_pending_updates"] is False
    assert kwargs["bootstrap_retries"] == 10
    assert kwargs["error_callback"] == bot._polling_error_callback
    assert bot._polling_restart_requested is False
    assert bot._polling_error_count == 0
    bot._update_dedupe_cache.clear.assert_called_once()
    assert bot._duplicate_update_id is None
    assert bot._duplicate_update_repeat_count == 0


@pytest.mark.asyncio
async def test_restart_polling_respects_restart_cooldown() -> None:
    """Restart attempts inside cooldown window should be skipped."""
    updater = SimpleNamespace(
        running=False,
        stop=AsyncMock(),
        start_polling=AsyncMock(),
    )
    bot = ClaudeCodeBot(
        settings=SimpleNamespace(webhook_url=None),
        dependencies={},
    )
    bot.app = SimpleNamespace(updater=updater)
    bot._last_polling_restart_monotonic = asyncio.get_running_loop().time()

    restarted = await bot._restart_polling(reason="cooldown")

    assert restarted is False
    updater.stop.assert_not_awaited()
    updater.start_polling.assert_not_awaited()


@pytest.mark.asyncio
async def test_watchdog_restarts_when_updater_not_running() -> None:
    """Watchdog should prefer updater-state recovery path."""
    bot = ClaudeCodeBot(
        settings=SimpleNamespace(webhook_url=None),
        dependencies={},
    )
    bot.app = SimpleNamespace(updater=SimpleNamespace(running=False))
    bot._restart_polling = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await bot._polling_watchdog_tick()

    bot._restart_polling.assert_awaited_once_with(reason="updater_not_running")


@pytest.mark.asyncio
async def test_watchdog_restarts_when_error_flag_set() -> None:
    """Watchdog should restart polling when error threshold requested recovery."""
    bot = ClaudeCodeBot(
        settings=SimpleNamespace(webhook_url=None),
        dependencies={},
    )
    bot.app = SimpleNamespace(updater=SimpleNamespace(running=True))
    bot._polling_restart_requested = True
    bot._restart_polling = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await bot._polling_watchdog_tick()

    bot._restart_polling.assert_awaited_once_with(reason="network_error_threshold")
