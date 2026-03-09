"""Tests for engine-aware Telegram command menu helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.utils.command_menu import (
    build_bot_commands_for_engine,
    sync_chat_command_menu,
)


def test_build_bot_commands_for_claude_hides_codexdiag():
    """Claude menu should include model and hide codex diagnostics."""
    commands = build_bot_commands_for_engine("claude")
    names = [cmd.command for cmd in commands]
    assert "context" in names
    assert "sendpic" in names
    assert "queue" in names
    assert "dequeue" in names
    assert "model" in names
    assert "codexdiag" not in names
    assert "status" not in names


def test_build_bot_commands_for_codex_includes_read_only_model():
    """Codex menu should include model(read-only), status and codex diagnostics."""
    commands = build_bot_commands_for_engine("codex")
    names = [cmd.command for cmd in commands]
    assert "sendpic" in names
    assert "context" not in names
    assert "queue" in names
    assert "dequeue" in names
    assert "codexdiag" in names
    assert "model" in names
    assert "status" in names


@pytest.mark.asyncio
async def test_sync_chat_command_menu_uses_chat_scope():
    """Sync helper should call Telegram set_my_commands with per-chat scope."""
    set_my_commands = AsyncMock()
    bot = SimpleNamespace(set_my_commands=set_my_commands)

    commands = await sync_chat_command_menu(
        bot=bot,
        chat_id=321,
        engine="codex",
    )

    assert commands
    kwargs = set_my_commands.await_args.kwargs
    assert kwargs["scope"].chat_id == 321
    assert any(cmd.command == "codexdiag" for cmd in kwargs["commands"])
