"""Telegram command menu helpers with engine-aware visibility."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

from telegram import BotCommand, BotCommandScopeChat

from .cli_engine import ENGINE_CLAUDE, command_visible_for_engine, normalize_cli_engine


@dataclass(frozen=True)
class MenuCommandSpec:
    """Static menu command metadata."""

    command: str
    description: str


COMMAND_MENU_SPECS: tuple[MenuCommandSpec, ...] = (
    MenuCommandSpec("new", "Clear context and start fresh session"),
    MenuCommandSpec("resume", "Resume a desktop session"),
    MenuCommandSpec("context", "Show session context (Claude)"),
    MenuCommandSpec("status", "Show session status (Codex)"),
    MenuCommandSpec("engine", "Switch CLI engine (claude/codex)"),
    MenuCommandSpec("cancel", "Cancel the current running task"),
    MenuCommandSpec("queue", "Show queued tasks"),
    MenuCommandSpec("dequeue", "Remove a queued task by ID"),
    MenuCommandSpec("model", "View or set model"),
    MenuCommandSpec("codexdiag", "Diagnose Codex MCP status (Codex)"),
    MenuCommandSpec("projects", "Show all projects"),
    MenuCommandSpec("cd", "Change directory (resumes project session)"),
    MenuCommandSpec("ls", "List files in current directory"),
    MenuCommandSpec("sendpic", "Send image by path or URL"),
    MenuCommandSpec("git", "Git repository commands"),
    MenuCommandSpec("export", "Export current session"),
    MenuCommandSpec("provider", "Switch API provider (cc-switch)"),
    MenuCommandSpec("help", "Show available commands"),
)


def build_bot_commands_for_engine(engine: str | None) -> List[BotCommand]:
    """Build command menu based on active engine capabilities."""
    normalized = normalize_cli_engine(engine)
    return [
        BotCommand(spec.command, spec.description)
        for spec in COMMAND_MENU_SPECS
        if command_visible_for_engine(spec.command, normalized)
    ]


async def sync_chat_command_menu(
    *,
    bot: Any,
    chat_id: int | None,
    engine: str | None,
) -> List[BotCommand]:
    """Apply per-chat command menu for the active engine."""
    if bot is None or not isinstance(chat_id, int) or chat_id <= 0:
        return []

    normalized = normalize_cli_engine(engine or ENGINE_CLAUDE)
    commands = build_bot_commands_for_engine(normalized)
    await bot.set_my_commands(
        commands=commands,
        scope=BotCommandScopeChat(chat_id=chat_id),
    )
    return commands
