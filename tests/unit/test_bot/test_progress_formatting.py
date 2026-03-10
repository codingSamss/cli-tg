"""Tests for streaming progress text formatting."""

import time
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
    _RequestTimingDiagnostics,
    _build_session_context_summary,
    _extract_model_from_model_usage,
    _format_error_message,
    _format_progress_update,
    _get_stream_merge_key,
    _is_high_priority_stream_update,
    _is_markdown_parse_error,
    _is_noop_edit_error,
    _is_turn_started_update,
    _join_progress_lines_for_display,
    _reply_text_resilient,
    _resolve_collapsed_fallback_model,
    _resolve_codex_context_snapshot,
    _send_private_final_response_draft,
    _split_text_for_telegram,
    _with_engine_badge,
    handle_photo,
    handle_text_message,
)
from src.bot.inbound_task_queue import InboundTaskQueue
from src.bot.utils.cli_engine import ENGINE_CLAUDE, ENGINE_CODEX
from src.services.session_service import SessionService


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
    """Assistant streaming line should render compact content preview only."""
    update = _FakeUpdate(
        type="assistant",
        content="partial response",
        metadata={"engine": "codex"},
    )

    text = await _format_progress_update(update)

    assert text is not None
    assert text == "🤔 partial response"


@pytest.mark.asyncio
async def test_progress_turn_started_renders_codex_working_line():
    """Codex turn.started should render a single working line."""
    update = _FakeUpdate(
        type="progress",
        content="Codex turn started",
        metadata={"subtype": "turn.started", "engine": "codex"},
    )

    text = await _format_progress_update(update)

    assert text == "🤖 *Codex is working...*"


@pytest.mark.asyncio
async def test_progress_turn_started_renders_claude_working_line():
    """Claude turn.started should render a single working line."""
    update = _FakeUpdate(
        type="progress",
        content="Claude turn started",
        metadata={"subtype": "turn.started", "engine": "claude"},
    )

    text = await _format_progress_update(update)

    assert text == "🤖 *Claude is working...*"


def test_is_turn_started_update_detection():
    """Only progress turn.started should be identified as start markers."""
    started = _FakeUpdate(type="progress", metadata={"subtype": "turn.started"})
    progress = _FakeUpdate(type="progress", metadata={"subtype": "step"})
    assistant = _FakeUpdate(type="assistant", metadata={"subtype": "turn.started"})

    assert _is_turn_started_update(started) is True
    assert _is_turn_started_update(progress) is False
    assert _is_turn_started_update(assistant) is False


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


@pytest.mark.asyncio
async def test_handle_text_message_fallback_does_not_crash_when_progress_init_fails(
    tmp_path, monkeypatch
):
    """Early progress-message failure should not trigger secondary UnboundLocalError."""
    approved = tmp_path / "approved"
    approved.mkdir()

    message = SimpleNamespace(
        text="trigger fallback",
        message_id=11,
        message_thread_id=None,
        chat=SimpleNamespace(send_action=AsyncMock()),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=10001),
        message=message,
        effective_chat=SimpleNamespace(id=10001, type="private"),
        effective_message=message,
    )
    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(
                approved_directory=approved,
                stream_render_debounce_ms=0,
                stream_render_min_edit_interval_ms=0,
                status_reactions_enabled=False,
            )
        },
        user_data={},
        bot=SimpleNamespace(),
    )

    reply_calls = {"count": 0}

    async def _fake_reply(*args, **kwargs):
        reply_calls["count"] += 1
        if reply_calls["count"] == 1:
            raise RuntimeError("progress message init failed")
        return SimpleNamespace()

    monkeypatch.setattr(
        "src.bot.handlers.message.get_cli_integration",
        lambda **_: (ENGINE_CODEX, SimpleNamespace()),
    )
    monkeypatch.setattr("src.bot.handlers.message._reply_text_resilient", _fake_reply)
    set_reaction = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "src.bot.handlers.message._set_message_reaction_safe", set_reaction
    )

    await handle_text_message(update, context)

    assert reply_calls["count"] >= 2
    set_reaction.assert_awaited()


@pytest.mark.asyncio
async def test_handle_text_message_keeps_assistant_intro_line_for_system_updates(
    tmp_path, monkeypatch
):
    """First stream update as system/init should keep assistant intro narration."""
    approved = tmp_path / "approved"
    approved.mkdir()

    message = SimpleNamespace(
        text="check consistency",
        message_id=41,
        message_thread_id=None,
        chat=SimpleNamespace(send_action=AsyncMock()),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=10041),
        message=message,
        effective_chat=SimpleNamespace(id=-10041, type="supergroup"),
        effective_message=message,
    )
    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(
                approved_directory=approved,
                stream_render_debounce_ms=0,
                stream_render_min_edit_interval_ms=0,
                status_reactions_enabled=False,
            )
        },
        user_data={},
        bot=SimpleNamespace(),
    )

    progress_msg = SimpleNamespace(
        message_id=90041,
        edit_text=AsyncMock(),
        edit_reply_markup=AsyncMock(),
        delete=AsyncMock(),
    )

    async def _fake_reply(_message, _text, *args, **kwargs):
        if _fake_reply.calls == 0:
            _fake_reply.calls += 1
            return progress_msg
        _fake_reply.calls += 1
        return SimpleNamespace(
            message_id=90041 + _fake_reply.calls,
            edit_text=AsyncMock(),
            edit_reply_markup=AsyncMock(),
            delete=AsyncMock(),
        )

    _fake_reply.calls = 0

    async def _fake_run_command(**kwargs):
        on_stream = kwargs.get("on_stream")
        if on_stream:
            await on_stream(
                _FakeUpdate(
                    type="system",
                    metadata={"subtype": "init", "engine": "codex"},
                )
            )
        return SimpleNamespace(
            content="final answer",
            session_id="sid-fast-41",
            cost=0.0,
            duration_ms=1,
            num_turns=1,
            is_error=False,
            error_type=None,
            tools_used=[],
            model_usage={},
        )

    monkeypatch.setattr(
        "src.bot.handlers.message.get_cli_integration",
        lambda **_: (ENGINE_CODEX, SimpleNamespace(run_command=_fake_run_command)),
    )
    monkeypatch.setattr("src.bot.handlers.message._reply_text_resilient", _fake_reply)

    await handle_text_message(update, context)

    edited_texts = [str(call.args[0]) for call in progress_msg.edit_text.await_args_list]
    assert any(
        "🤔 正在处理你的请求..." in text and "🚀 *Starting Codex*" in text
        for text in edited_texts
    )


@pytest.mark.asyncio
async def test_handle_text_message_busy_state_queues_request(tmp_path, monkeypatch):
    """Busy text request should be queued with a removable queue id."""
    approved = tmp_path / "approved"
    approved.mkdir()

    message = SimpleNamespace(
        text="please queue me",
        message_id=21,
        message_thread_id=None,
        chat=SimpleNamespace(send_action=AsyncMock()),
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=11001),
        message=message,
        effective_chat=SimpleNamespace(id=11001, type="private"),
        effective_message=message,
    )
    task_registry = SimpleNamespace(is_busy=AsyncMock(return_value=True))
    inbound_queue = InboundTaskQueue()
    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(
                approved_directory=approved,
                stream_render_debounce_ms=0,
                stream_render_min_edit_interval_ms=0,
                status_reactions_enabled=False,
            ),
            "task_registry": task_registry,
            "inbound_task_queue": inbound_queue,
        },
        user_data={},
        bot=SimpleNamespace(),
    )

    sent_texts: list[str] = []
    sent_kwargs: list[dict] = []

    async def _fake_reply(_message, text, *args, **kwargs):
        sent_texts.append(str(text))
        sent_kwargs.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("src.bot.handlers.message._reply_text_resilient", _fake_reply)

    await handle_text_message(update, context)

    queued_items = await inbound_queue.list_items(
        user_id=11001, scope_key="11001:11001:0"
    )
    assert len(queued_items) == 1
    assert queued_items[0].kind == "text"
    assert sent_texts
    assert "queued as #" in sent_texts[0]
    reply_markup = sent_kwargs[0].get("reply_markup")
    assert reply_markup is not None
    assert reply_markup.inline_keyboard[0][0].callback_data == "queue:dequeue:1"


def test_request_timing_diagnostics_tracks_stream_and_send_metrics(monkeypatch):
    """Request timing diagnostics should separate stream/tool/TG send stages."""
    monotonic_values = iter([101.0, 102.5])
    monkeypatch.setattr(
        "src.bot.handlers.message.time.monotonic",
        lambda: next(monotonic_values),
    )

    diagnostics = _RequestTimingDiagnostics(request_started_monotonic=100.0)

    diagnostics.mark_stream_update(
        _FakeUpdate(type="assistant", content="partial answer", tool_calls=None)
    )
    diagnostics.mark_stream_update(
        _FakeUpdate(
            type="progress",
            metadata={"item_type": "command_execution", "status": "in_progress"},
        )
    )
    diagnostics.record_progress_edit(120)
    diagnostics.record_progress_refresh(340)
    diagnostics.record_final_reply(560)
    diagnostics.record_final_draft(80)

    fields = diagnostics.to_log_fields(total_wall_ms=9000)

    assert fields["first_stream_update_ms"] == 1000
    assert fields["first_assistant_text_ms"] == 1000
    assert fields["first_tool_activity_ms"] == 2500
    assert fields["stream_update_count"] == 2
    assert fields["command_progress_count"] == 1
    assert fields["tool_activity_count"] == 1
    assert fields["tg_progress_edit_total_ms"] == 120
    assert fields["tg_progress_refresh_total_ms"] == 340
    assert fields["final_reply_total_ms"] == 560
    assert fields["final_draft_total_ms"] == 80


@pytest.mark.asyncio
async def test_handle_photo_busy_state_queues_request(tmp_path, monkeypatch):
    """Busy photo request should be queued with queue id."""
    approved = tmp_path / "approved"
    approved.mkdir()

    photo_obj = SimpleNamespace(file_id="photo-1")
    message = SimpleNamespace(
        photo=[photo_obj],
        caption="check this screenshot",
        media_group_id=None,
        message_id=31,
        message_thread_id=None,
        chat=SimpleNamespace(send_action=AsyncMock()),
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=12001),
        message=message,
        effective_chat=SimpleNamespace(id=12001, type="private"),
        effective_message=message,
    )
    task_registry = SimpleNamespace(is_busy=AsyncMock(return_value=True))
    inbound_queue = InboundTaskQueue()
    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(
                approved_directory=approved,
                stream_render_debounce_ms=0,
                stream_render_min_edit_interval_ms=0,
                status_reactions_enabled=False,
            ),
            "features": SimpleNamespace(get_image_handler=lambda: object()),
            "task_registry": task_registry,
            "inbound_task_queue": inbound_queue,
        },
        user_data={},
        bot=SimpleNamespace(),
    )

    sent_texts: list[str] = []
    sent_kwargs: list[dict] = []

    async def _fake_reply(_message, text, *args, **kwargs):
        sent_texts.append(str(text))
        sent_kwargs.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("src.bot.handlers.message._reply_text_resilient", _fake_reply)

    await handle_photo(update, context)

    queued_items = await inbound_queue.list_items(
        user_id=12001, scope_key="12001:12001:0"
    )
    assert len(queued_items) == 1
    assert queued_items[0].kind == "photo"
    assert sent_texts
    assert "queued as #" in sent_texts[0]
    reply_markup = sent_kwargs[0].get("reply_markup")
    assert reply_markup is not None
    assert reply_markup.inline_keyboard[0][0].callback_data == "queue:dequeue:1"


def test_join_progress_lines_for_display_adds_spacing_around_command_blocks():
    """Command blocks should keep one blank line before and after for readability."""
    rendered = _join_progress_lines_for_display(
        [
            "🤔 preparing checks",
            "🔧 *Running command*\n\n`/bin/zsh -lc 'git status --short --branch'`",
            "✅ *Command completed* \\(exit 0\\)\n\n`/bin/zsh -lc 'git status --short --branch'`",
            "🤔 moving to next step",
        ]
    )

    assert "🤔 preparing checks\n\n🔧 *Running command*" in rendered
    assert (
        "`/bin/zsh -lc 'git status --short --branch'`\n\n✅ *Command completed*"
        in rendered
    )
    assert (
        "`/bin/zsh -lc 'git status --short --branch'`\n\n🤔 moving to next step"
        in rendered
    )


def test_join_progress_lines_for_display_keeps_non_command_lines_compact():
    """Non-command and non-assistant lines should preserve compact spacing."""
    rendered = _join_progress_lines_for_display(
        ["🤖 *Codex is working...*", "🔄 *step 1*", "✅ *done*"]
    )

    assert rendered == "🤖 *Codex is working...*\n🔄 *step 1*\n✅ *done*"


def test_join_progress_lines_for_display_adds_spacing_around_assistant_blocks():
    """Assistant narration lines should be separated by one blank line."""
    rendered = _join_progress_lines_for_display(
        [
            "🔄 Checking Markdown compatibility",
            "🤔 我先检查 parse_mode 配置",
            "🔄 Investigating Markdown parsing fallback",
            "🤔 已定位到兜底逻辑",
        ]
    )

    assert (
        rendered == "🔄 Checking Markdown compatibility\n\n"
        "🤔 我先检查 parse_mode 配置\n\n"
        "🔄 Investigating Markdown parsing fallback\n\n"
        "🤔 已定位到兜底逻辑"
    )


def test_get_stream_merge_key_for_mergeable_events():
    """Assistant and non-command progress updates should be mergeable."""
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


def test_get_stream_merge_key_for_command_execution_uses_command_identity():
    """Command execution updates should merge by command identity."""
    command_update = _FakeUpdate(
        type="progress",
        content="/bin/zsh -lc 'make lint'",
        metadata={
            "item_type": "command_execution",
            "status": "in_progress",
            "command": "/bin/zsh -lc 'make lint'",
        },
    )

    key = _get_stream_merge_key(command_update)
    assert key is not None
    assert key.startswith("command_execution:")
    assert "make lint" in key


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


def test_append_progress_line_with_merge_collapses_command_status_updates():
    """Consecutive status updates of the same command should keep latest state only."""
    lines: list[str] = []
    merge_keys: list[Optional[str]] = []
    command_key = "command_execution:/bin/zsh -lc 'make lint'"

    _append_progress_line_with_merge(
        progress_lines=lines,
        progress_merge_keys=merge_keys,
        progress_text="🔧 *Running command*\n\n`/bin/zsh -lc 'make lint'`",
        merge_key=command_key,
    )
    _append_progress_line_with_merge(
        progress_lines=lines,
        progress_merge_keys=merge_keys,
        progress_text="✅ *Command completed* \\(exit 0\\)\n\n`/bin/zsh -lc 'make lint'`",
        merge_key=command_key,
    )

    assert lines == [
        "✅ *Command completed* \\(exit 0\\)\n\n`/bin/zsh -lc 'make lint'`"
    ]
    assert merge_keys == [command_key]


def test_append_progress_line_with_merge_collapses_interleaved_command_updates():
    """Interleaved command updates should still keep one latest line per command."""
    lines: list[str] = []
    merge_keys: list[Optional[str]] = []
    key_a = "command_execution:/bin/zsh -lc 'rg -n foo src'"
    key_b = "command_execution:/bin/zsh -lc 'sed -n 1,200p app.py'"

    _append_progress_line_with_merge(
        progress_lines=lines,
        progress_merge_keys=merge_keys,
        progress_text="🔧 *Running command*\n\n`/bin/zsh -lc 'rg -n foo src'`",
        merge_key=key_a,
    )
    _append_progress_line_with_merge(
        progress_lines=lines,
        progress_merge_keys=merge_keys,
        progress_text="🔧 *Running command*\n\n`/bin/zsh -lc 'sed -n 1,200p app.py'`",
        merge_key=key_b,
    )
    _append_progress_line_with_merge(
        progress_lines=lines,
        progress_merge_keys=merge_keys,
        progress_text="✅ *Command completed* \\(exit 0\\)\n\n`/bin/zsh -lc 'rg -n foo src'`",
        merge_key=key_a,
    )
    _append_progress_line_with_merge(
        progress_lines=lines,
        progress_merge_keys=merge_keys,
        progress_text=(
            "✅ *Command completed* \\(exit 0\\)\n\n"
            "`/bin/zsh -lc 'sed -n 1,200p app.py'`"
        ),
        merge_key=key_b,
    )

    assert lines == [
        "✅ *Command completed* \\(exit 0\\)\n\n`/bin/zsh -lc 'rg -n foo src'`",
        "✅ *Command completed* \\(exit 0\\)\n\n`/bin/zsh -lc 'sed -n 1,200p app.py'`",
    ]
    assert merge_keys == [key_a, key_b]


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


def test_resolve_codex_context_snapshot_reads_cached_session_usage():
    """Collapsed/status tags should reuse the latest cached Codex snapshot."""
    session_id = "session-codex-cached"
    SessionService._codex_snapshot_cache[session_id] = (
        time.monotonic(),
        {
            "used_percent": 13.8,
            "total_tokens": 200_000,
            "remaining_tokens": 172_400,
            "resolved_model": "gpt-5.4",
            "rate_limits": {
                "primary": {"used_percent": 12.5, "window_minutes": 300},
                "secondary": {"used_percent": 37.0, "window_minutes": 10_080},
            },
        },
    )

    try:
        snapshot, session_summary, rate_limit_summary = (
            _resolve_codex_context_snapshot(
                active_engine=ENGINE_CODEX,
                session_id=session_id,
            )
        )
    finally:
        SessionService._codex_snapshot_cache.pop(session_id, None)

    assert snapshot is not None
    assert snapshot["resolved_model"] == "gpt-5.4"
    assert session_summary == "🔋 Session context: `86.2%` remaining"
    assert rate_limit_summary is not None
    assert "5h window: 87.5% remaining" in rate_limit_summary
    assert "7d window: 63.0% remaining" in rate_limit_summary


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
