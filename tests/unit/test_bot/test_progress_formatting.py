"""Tests for streaming progress text formatting."""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock

import pytest

from src.bot.handlers.message import (
    _append_progress_line_with_merge,
    _build_collapsed_thinking_summary,
    _build_context_tag,
    _build_session_context_summary,
    _extract_model_from_model_usage,
    _format_error_message,
    _format_progress_update,
    _get_stream_merge_key,
    _is_high_priority_stream_update,
    _is_markdown_parse_error,
    _is_noop_edit_error,
    _reply_text_resilient,
    _resolve_collapsed_fallback_model,
    _send_private_final_response_draft,
    _split_text_for_telegram,
    _with_engine_badge,
)
from src.bot.utils.cli_engine import ENGINE_CLAUDE, ENGINE_CODEX


@dataclass
class _FakeUpdate:
    type: str
    metadata: Optional[dict] = None
    content: Optional[str] = None
    tool_calls: Optional[list] = None
    progress: Optional[dict] = None
    error_info: Optional[dict] = None

    def is_error(self) -> bool:
        return False

    def get_error_message(self):
        return self.content or ""

    def get_progress_percentage(self):
        if self.progress:
            return self.progress.get("percentage")
        return None


@pytest.mark.asyncio
async def test_init_progress_text_does_not_show_stale_model_name():
    """Init line should stay generic and not claim a specific model."""
    update = _FakeUpdate(
        type="system",
        metadata={
            "subtype": "init",
            "tools": ["Read", "Write"],
            "model": "claude-3-5-sonnet-20241022",
        },
    )
    text = await _format_progress_update(update)
    assert text == "🚀 *Starting Claude*"


@pytest.mark.asyncio
async def test_model_resolved_progress_text_uses_using_model_label():
    """Resolved model line should explicitly show the actual model in use."""
    update = _FakeUpdate(
        type="system",
        metadata={
            "subtype": "model_resolved",
            "model": "claude-opus-4-1",
        },
    )
    text = await _format_progress_update(update)
    assert text == "🧠 *Using model:* claude-opus-4-1"


@pytest.mark.asyncio
async def test_assistant_progress_text_uses_codex_label_when_metadata_present():
    """Assistant streaming line should show Codex label for codex metadata."""
    update = _FakeUpdate(
        type="assistant",
        content="partial response",
        metadata={"engine": "codex"},
    )

    text = await _format_progress_update(update)

    assert text is not None
    assert text.startswith("🤖 *Codex is working...*")


@pytest.mark.asyncio
async def test_progress_turn_started_renders_codex_working_line():
    """Codex turn.started should render a concise working status line."""
    update = _FakeUpdate(
        type="progress",
        content="Codex turn started",
        metadata={"subtype": "turn.started", "engine": "codex"},
    )

    text = await _format_progress_update(update)

    assert text == "🤖 *Codex is working...*"


@pytest.mark.asyncio
async def test_progress_turn_started_renders_claude_working_line():
    """Claude turn.started should render the same style working status line."""
    update = _FakeUpdate(
        type="progress",
        content="Claude turn started",
        metadata={"subtype": "turn.started", "engine": "claude"},
    )

    text = await _format_progress_update(update)

    assert text == "🤖 *Claude is working...*"


@pytest.mark.asyncio
async def test_progress_command_execution_renders_compact_running_line():
    """Codex command execution updates should render compact command status."""
    update = _FakeUpdate(
        type="progress",
        content="/bin/zsh -lc 'cd /tmp && ls'",
        metadata={
            "item_type": "command_execution",
            "status": "in_progress",
            "command": "/bin/zsh -lc 'cd /tmp && ls'",
            "engine": "codex",
        },
    )

    text = await _format_progress_update(update)

    assert text is not None
    assert text.startswith("🔧 *Running command*")
    assert "/bin/zsh -lc" in text


@pytest.mark.asyncio
async def test_progress_command_execution_renders_completion_exit_code():
    """Completed command update should include exit code in rendered line."""
    update = _FakeUpdate(
        type="progress",
        content="/bin/zsh -lc 'pwd'",
        metadata={
            "item_type": "command_execution",
            "status": "completed",
            "command": "/bin/zsh -lc 'pwd'",
            "exit_code": 0,
            "engine": "codex",
        },
    )

    text = await _format_progress_update(update)

    assert text is not None
    assert text.startswith("✅ *Command completed*")
    assert "exit 0" in text


def test_get_stream_merge_key_for_mergeable_events():
    """Progress and assistant plain content should be mergeable."""
    assistant_update = _FakeUpdate(
        type="assistant",
        content="partial",
        tool_calls=None,
    )
    progress_update = _FakeUpdate(type="progress", content="working")
    tool_update = _FakeUpdate(
        type="assistant",
        content=None,
        tool_calls=[{"name": "Read"}],
    )

    assert _get_stream_merge_key(assistant_update) == "assistant_content"
    assert _get_stream_merge_key(progress_update) == "progress"
    assert _get_stream_merge_key(tool_update) is None


def test_append_progress_line_with_merge_merges_only_consecutive_same_key():
    """Same merge key should replace previous line; other keys should append."""
    lines: list[str] = []
    merge_keys: list[Optional[str]] = []

    _append_progress_line_with_merge(
        progress_lines=lines,
        progress_merge_keys=merge_keys,
        progress_text="🤖 first",
        merge_key="assistant_content",
    )
    _append_progress_line_with_merge(
        progress_lines=lines,
        progress_merge_keys=merge_keys,
        progress_text="🤖 second",
        merge_key="assistant_content",
    )
    _append_progress_line_with_merge(
        progress_lines=lines,
        progress_merge_keys=merge_keys,
        progress_text="🔄 step 1",
        merge_key="progress",
    )
    _append_progress_line_with_merge(
        progress_lines=lines,
        progress_merge_keys=merge_keys,
        progress_text="🔄 step 2",
        merge_key="progress",
    )
    _append_progress_line_with_merge(
        progress_lines=lines,
        progress_merge_keys=merge_keys,
        progress_text="✅ done",
        merge_key=None,
    )
    _append_progress_line_with_merge(
        progress_lines=lines,
        progress_merge_keys=merge_keys,
        progress_text="✅ done again",
        merge_key=None,
    )

    assert lines == ["🤖 second", "🔄 step 2", "✅ done", "✅ done again"]
    assert merge_keys == ["assistant_content", "progress", None, None]


def test_append_progress_line_with_merge_skips_exact_consecutive_duplicates():
    """Exact duplicates should be skipped to reduce noisy edits."""
    lines: list[str] = []
    merge_keys: list[Optional[str]] = []

    _append_progress_line_with_merge(
        progress_lines=lines,
        progress_merge_keys=merge_keys,
        progress_text="🔧 Read: `a.py`",
        merge_key=None,
    )
    _append_progress_line_with_merge(
        progress_lines=lines,
        progress_merge_keys=merge_keys,
        progress_text="🔧 Read: `a.py`",
        merge_key=None,
    )

    assert lines == ["🔧 Read: `a.py`"]
    assert merge_keys == [None]


def test_high_priority_stream_update_detection():
    """High-priority updates should bypass debounce for snappier feedback."""
    error_update = _FakeUpdate(type="error", content="boom")
    tool_result_update = _FakeUpdate(type="tool_result", content="done")
    tool_call_update = _FakeUpdate(
        type="assistant",
        tool_calls=[{"name": "Read", "input": {"file_path": "x.py"}}],
    )
    system_init = _FakeUpdate(type="system", metadata={"subtype": "init"})
    system_model = _FakeUpdate(type="system", metadata={"subtype": "model_resolved"})
    plain_progress = _FakeUpdate(type="progress", content="working")

    assert _is_high_priority_stream_update(error_update) is True
    assert _is_high_priority_stream_update(tool_result_update) is True
    assert _is_high_priority_stream_update(tool_call_update) is True
    assert _is_high_priority_stream_update(system_init) is True
    assert _is_high_priority_stream_update(system_model) is True
    assert _is_high_priority_stream_update(plain_progress) is False


def test_noop_edit_error_detection():
    """Should detect Telegram 'message is not modified' edit rejection."""
    assert _is_noop_edit_error(Exception("Message is not modified")) is True
    assert (
        _is_noop_edit_error(Exception("Bad Request: message is not modified")) is True
    )
    assert _is_noop_edit_error(Exception("network timeout")) is False


@pytest.mark.asyncio
async def test_send_private_final_response_draft_only_for_private_chat(monkeypatch):
    """Final-response draft preview should be private-chat only."""
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr("src.bot.handlers.message.send_message_draft_resilient", sender)

    sent_private = await _send_private_final_response_draft(
        bot=SimpleNamespace(),
        chat_id=12345,
        chat_type="private",
        message_thread_id=None,
        draft_id=777,
        text="最终结论",
        parse_mode="Markdown",
    )
    sent_group = await _send_private_final_response_draft(
        bot=SimpleNamespace(),
        chat_id=-1001,
        chat_type="supergroup",
        message_thread_id=None,
        draft_id=888,
        text="群聊结论",
        parse_mode="Markdown",
    )

    assert sent_private is True
    assert sent_group is False
    assert sender.await_count == 1
    assert sender.await_args.kwargs["chat_id"] == 12345
    assert sender.await_args.kwargs["draft_id"] == 777


def test_markdown_parse_error_detection():
    """Markdown parsing errors should be detected for fallback retry."""
    assert _is_markdown_parse_error(Exception("Bad Request: can't parse entities"))
    assert _is_markdown_parse_error(Exception("cannot parse entities")) is True
    assert _is_markdown_parse_error(Exception("Message is too long")) is False


def test_split_text_for_telegram_splits_long_text():
    """Long text should be split into safe chunks under Telegram limit."""
    text = "a" * 8000
    chunks = _split_text_for_telegram(text, limit=3900)
    assert len(chunks) == 3
    assert sum(len(chunk) for chunk in chunks) == len(text)
    assert all(len(chunk) <= 3900 for chunk in chunks)


@pytest.mark.asyncio
async def test_reply_text_resilient_retries_without_markdown_parse_mode():
    """Markdown parse failure should fallback to plain text send."""
    message = type("FakeMessage", (), {})()
    message.reply_text = AsyncMock(
        side_effect=[Exception("Bad Request: can't parse entities"), object()]
    )

    await _reply_text_resilient(
        message, "codex_core::rollout::list", parse_mode="Markdown"
    )

    assert message.reply_text.await_count == 2
    assert message.reply_text.await_args_list[0].kwargs["parse_mode"] == "Markdown"
    assert "parse_mode" not in message.reply_text.await_args_list[1].kwargs


@pytest.mark.asyncio
async def test_reply_text_resilient_splits_when_message_too_long():
    """Too-long errors should fallback to chunked plain text sending."""
    message = type("FakeMessage", (), {})()

    async def _reply_text_side_effect(text: str, **kwargs):
        if len(text) > 4096:
            raise Exception("Bad Request: message is too long")
        return object()

    message.reply_text = AsyncMock(side_effect=_reply_text_side_effect)
    text = "x" * 9000

    await _reply_text_resilient(message, text, parse_mode=None)

    assert message.reply_text.await_count == 4
    first_call = message.reply_text.await_args_list[0]
    assert len(first_call.args[0]) == 9000
    split_calls = message.reply_text.await_args_list[1:]
    assert all(len(call.args[0]) <= 3900 for call in split_calls)


@pytest.mark.asyncio
async def test_reply_text_resilient_uses_bot_send_path_when_available():
    """When bot/chat context is available, helper should use resilient send wrapper."""
    bot = type("FakeBot", (), {})()
    bot.send_message = AsyncMock(return_value=object())
    message = type("FakeMessage", (), {})()
    message.chat_id = -100123
    message.message_thread_id = 42

    await _reply_text_resilient(
        message,
        "hello",
        parse_mode="Markdown",
        reply_to_message_id=77,
        bot=bot,
        chat_type="supergroup",
    )

    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == -100123
    assert kwargs["message_thread_id"] == 42
    assert kwargs["reply_to_message_id"] == 77
    assert kwargs["parse_mode"] == "Markdown"


@pytest.mark.asyncio
async def test_reply_text_resilient_bot_path_retries_without_thread():
    """Bot send path should retry without thread when topic id is invalid."""
    bot = type("FakeBot", (), {})()
    bot.send_message = AsyncMock(
        side_effect=[Exception("Bad Request: message thread not found"), object()]
    )
    message = type("FakeMessage", (), {})()
    message.chat_id = -100123
    message.message_thread_id = 42

    await _reply_text_resilient(
        message,
        "hello",
        parse_mode="Markdown",
        bot=bot,
        chat_type="supergroup",
    )

    assert bot.send_message.await_count == 2
    first_call_kwargs = bot.send_message.await_args_list[0].kwargs
    second_call_kwargs = bot.send_message.await_args_list[1].kwargs
    assert first_call_kwargs["message_thread_id"] == 42
    assert "message_thread_id" not in second_call_kwargs


def test_format_error_message_uses_codex_label_for_generic_errors():
    """Codex generic error should not render Claude-branded header."""
    text = _format_error_message("mcp backend crashed", engine=ENGINE_CODEX)
    assert "Codex CLI Error" in text
    assert "Claude Code Error" not in text


def test_format_error_message_uses_status_command_for_codex_hints():
    """Codex error hints should point to /status instead of /context."""
    text = _format_error_message("rate limit reached", engine=ENGINE_CODEX)
    assert "/status" in text
    assert "/context" not in text


def test_build_context_tag_renders_codex_badge():
    """Context tag should include Codex badge for Codex engine responses."""
    tag = _build_context_tag(
        scope_state={"current_directory": Path("/tmp/demo-project")},
        approved_directory=Path("/tmp"),
        active_engine=ENGINE_CODEX,
        session_id="session-codex-123456",
    )

    assert "⬜ `Codex CLI`" in tag
    assert "`demo-project`" in tag


def test_build_context_tag_renders_claude_badge():
    """Context tag should include Claude badge for Claude engine responses."""
    tag = _build_context_tag(
        scope_state={"current_directory": Path("/tmp/claude-project")},
        approved_directory=Path("/tmp"),
        active_engine=ENGINE_CLAUDE,
        session_id="session-claude-123456",
    )

    assert "🟧 `Claude CLI`" in tag
    assert "`claude-project`" in tag


def test_build_context_tag_shows_rate_limit_summary():
    """Context tag should append rate limit info when provided."""
    summary = (
        "5h window: 87.5% remaining\n"
        "7d window: 63.0% remaining\n"
        "(updated 2026-02-09T13:54:15Z)"
    )
    tag = _build_context_tag(
        scope_state={"current_directory": Path("/tmp/demo-project")},
        approved_directory=Path("/tmp"),
        active_engine=ENGINE_CODEX,
        session_id="session-codex-123456",
        rate_limit_summary=summary,
    )

    lines = tag.splitlines()
    assert "🔋 5h window: 87.5% remaining" in lines
    assert "   7d window: 63.0% remaining" in lines
    assert "   (updated 2026-02-09T13:54:15Z)" in lines


def test_build_context_tag_shows_session_context_summary():
    """Context tag should include session usage summary on a dedicated line."""
    tag = _build_context_tag(
        scope_state={"current_directory": Path("/tmp/demo-project")},
        approved_directory=Path("/tmp"),
        active_engine=ENGINE_CODEX,
        session_id="session-codex-123456",
        session_context_summary="🔋 Session context: `71.8%` remaining",
        rate_limit_summary="5h window: 87.5% remaining",
    )

    lines = tag.splitlines()
    assert len(lines) == 3
    assert lines[1].startswith("🔋 Session context")
    assert lines[2].startswith("🔋")


def test_build_session_context_summary_prefers_explicit_remaining_tokens():
    """Session context summary should derive remaining percent from token fields."""
    summary = _build_session_context_summary(
        {
            "used_percent": 28.2,
            "total_tokens": 258_400,
            "remaining_tokens": 185_549,
        }
    )

    assert summary is not None
    assert "`71.8%` remaining" in summary
    assert "used" not in summary


def test_build_collapsed_thinking_summary_keeps_model_and_context():
    """Collapsed thinking summary should keep model line and append context info."""
    collapsed = _build_collapsed_thinking_summary(
        all_progress_lines=[
            "🚀 *Starting Codex*",
            "🧠 *Using model:* o4-mini",
            "🔧 Read: `src/main.py`",
        ],
        context_tag=(
            "⬜ `Codex CLI` | `cli-tg` | `019c6252`\n"
            "🔋 Session context: `86.2%` remaining\n"
            "🔋 5h window: 97.0% remaining\n"
            "   7d window: 99.0% remaining"
        ),
    )

    lines = collapsed.splitlines()
    assert lines[0] == "⬜ `Codex CLI` | `cli-tg` | `019c6252`"
    assert "🔋 Session context: `86.2%` remaining" in lines
    assert "🧠 *Using model:* o4-mini" in lines
    assert "🔋 5h window: 97.0% remaining" not in collapsed
    assert "💭 Thinking done" not in collapsed


def test_build_collapsed_thinking_summary_falls_back_when_no_model_line():
    """Collapsed thinking summary should still render compact context without model."""
    collapsed = _build_collapsed_thinking_summary(
        all_progress_lines=["🔧 Read: `src/main.py`"],
        context_tag=(
            "🟧 `Claude CLI` | `cli-tg` | `019c6252`\n" "🔋 5h window: 87.5% remaining"
        ),
    )

    assert "🧠 *Using model:*" not in collapsed
    assert "🟧 `Claude CLI` | `cli-tg` | `019c6252`" in collapsed
    assert "🔋 5h window: 87.5% remaining" not in collapsed
    assert "💭 Thinking done" not in collapsed


def test_build_collapsed_thinking_summary_uses_fallback_model_when_missing():
    """Collapsed summary should use provided fallback model when stream has no model line."""
    collapsed = _build_collapsed_thinking_summary(
        all_progress_lines=["🔧 Read: `src/main.py`"],
        context_tag="⬜ `Codex CLI` | `cli-tg` | `019c6252`",
        fallback_model="gpt-5.3-codex",
    )

    lines = collapsed.splitlines()
    assert lines[0] == "⬜ `Codex CLI` | `cli-tg` | `019c6252`"
    assert "🧠 *Using model:* gpt-5.3-codex" in lines


def test_extract_model_from_model_usage_supports_nested_and_flat_payloads():
    """Model extraction should work for both flat and nested usage payload shapes."""
    flat = {"resolvedModel": "claude-opus-4-1", "inputTokens": 100}
    nested = {"gpt-5.3-codex": {"inputTokens": 100}}

    assert _extract_model_from_model_usage(flat) == "claude-opus-4-1"
    assert _extract_model_from_model_usage(nested) == "gpt-5.3-codex"


def test_resolve_collapsed_fallback_model_supports_codex_and_claude_modes():
    """Fallback model resolver should keep specific models for both engines."""
    codex_model = _resolve_collapsed_fallback_model(
        active_engine=ENGINE_CODEX,
        scope_state={},
        claude_response=None,
        codex_snapshot={"resolved_model": "gpt-5.3-codex"},
    )
    claude_model = _resolve_collapsed_fallback_model(
        active_engine=ENGINE_CLAUDE,
        scope_state={"claude_model": "claude-opus-4-1"},
        claude_response=None,
        codex_snapshot=None,
    )

    assert codex_model == "gpt-5.3-codex"
    assert claude_model == "claude-opus-4-1"


def test_with_engine_badge_prefixes_codex_bubble():
    """Engine badge helper should prepend codex marker to bubble text."""
    text = _with_engine_badge("正在处理你的请求...", ENGINE_CODEX)
    assert text.startswith("⬜ `Codex CLI`")
    assert "正在处理你的请求..." in text


def test_with_engine_badge_handles_empty_body():
    """Engine badge helper should still return badge when body is empty."""
    text = _with_engine_badge("", ENGINE_CLAUDE)
    assert text == "🟧 `Claude CLI`"


def test_with_engine_badge_falls_back_to_claude_for_unknown_engine():
    """Unknown engine values should fallback to Claude with orange badge."""
    text = _with_engine_badge("running...", "groq")
    assert text.startswith("🟧 `Claude CLI`")
