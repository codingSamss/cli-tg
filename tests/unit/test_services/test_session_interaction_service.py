"""Tests for session interaction service."""

from pathlib import Path
from types import SimpleNamespace

from src.services import SessionInteractionService


def test_build_continue_progress_text_with_existing_session():
    """Existing session should render continue progress text with session id."""
    service = SessionInteractionService()

    text = service.build_continue_progress_text(
        existing_session_id="session-12345678",
        current_dir=Path("/tmp/project"),
        approved_directory=Path("/tmp"),
        prompt=None,
    )

    assert "Continuing Session" in text
    assert "session-..." in text
    assert "project/" in text
    assert "Continuing where you left off" in text


def test_build_continue_progress_text_without_existing_session():
    """Missing active session should render discovery progress text."""
    service = SessionInteractionService()

    text = service.build_continue_progress_text(
        existing_session_id=None,
        current_dir=Path("/tmp/project"),
        approved_directory=Path("/tmp"),
        prompt=None,
    )

    assert "Looking for Recent Session" in text
    assert "Searching for your most recent session" in text


def test_build_new_session_message_for_command_with_previous_session():
    """Command new-session message should include cleared previous session hint."""
    service = SessionInteractionService()

    message = service.build_new_session_message(
        current_dir=Path("/tmp/project"),
        approved_directory=Path("/tmp"),
        previous_session_id="session-old-1234",
        for_callback=False,
    )

    assert "New AI Session" in message.text
    assert "project/" in message.text
    assert "Previous session `session-...` cleared" in message.text
    assert message.keyboard is None


def test_build_new_session_message_for_callback():
    """Callback new-session message should use quick restart copy."""
    service = SessionInteractionService()

    message = service.build_new_session_message(
        current_dir=Path("/tmp/project"),
        approved_directory=Path("/tmp"),
        previous_session_id=None,
        for_callback=True,
    )

    assert "Ready to help you code!" in message.text
    assert message.keyboard is None


def test_build_new_session_message_uses_active_engine_title():
    """New-session title should follow active engine when provided."""
    service = SessionInteractionService()

    message = service.build_new_session_message(
        current_dir=Path("/tmp/project"),
        approved_directory=Path("/tmp"),
        previous_session_id=None,
        for_callback=False,
        active_engine="codex",
    )

    assert "New Codex Session" in message.text


def test_build_end_no_active_message_for_callback():
    """Callback no-active message should include action buttons."""
    service = SessionInteractionService()

    message = service.build_end_no_active_message(for_callback=True)

    assert "No Active Session" in message.text
    assert message.keyboard is not None
    assert message.keyboard[0][0][1] == "action:new_session"
    assert message.keyboard[1][0][1] == "action:context"


def test_build_end_success_message_for_command():
    """Command end success message should include slash-command guidance."""
    service = SessionInteractionService()

    message = service.build_end_success_message(
        current_dir=Path("/tmp/project"),
        approved_directory=Path("/tmp"),
        for_callback=False,
        title="Session Ended",
    )

    assert "Session Ended" in message.text
    assert "Start a new session with `/new`" in message.text
    assert message.keyboard is not None
    assert message.keyboard[0][1][1] == "action:show_projects"


def test_build_continue_not_found_message_for_command():
    """Command variant should include slash-command suggestions."""
    service = SessionInteractionService()

    message = service.build_continue_not_found_message(
        current_dir=Path("/tmp/project"),
        approved_directory=Path("/tmp"),
        for_callback=False,
    )

    assert "No Session Found" in message.text
    assert "Use `/new`" in message.text
    assert "Use `/context`" in message.text
    assert message.keyboard is not None
    assert message.keyboard[0][0][1] == "action:new_session"
    assert message.keyboard[0][1][1] == "action:context"


def test_build_continue_not_found_message_for_callback():
    """Callback variant should keep button-first guidance."""
    service = SessionInteractionService()

    message = service.build_continue_not_found_message(
        current_dir=Path("/tmp/project"),
        approved_directory=Path("/tmp"),
        for_callback=True,
    )

    assert "Use the button below" in message.text
    assert message.keyboard is not None
    assert message.keyboard[0][0][1] == "action:new_session"


def test_build_export_selector_message_contains_expected_buttons():
    """Export selector should include the three formats and cancel action."""
    service = SessionInteractionService()

    message = service.build_export_selector_message("session-abcdefg")

    assert "Export Session" in message.text
    assert "session-..." in message.text
    assert message.keyboard is not None
    assert message.keyboard[0][0][1] == "export:markdown"
    assert message.keyboard[0][1][1] == "export:html"
    assert message.keyboard[1][0][1] == "export:json"
    assert message.keyboard[1][1][1] == "export:cancel"


def test_build_continue_callback_success_text_applies_preview_limit():
    """Success preview should truncate long callback content."""
    content = "A" * 520

    text = SessionInteractionService.build_continue_callback_success_text(content)

    assert "Session Continued" in text
    assert text.endswith("...")
    assert len(text) < 600


def test_build_context_view_spec_for_command_full_mode():
    """Command full mode should disable event summary but keep resumable lookup."""
    service = SessionInteractionService()

    spec = service.build_context_view_spec(for_callback=False, full_mode=True)

    assert spec.loading_text == "⏳ 正在获取会话状态，请稍候..."
    assert spec.loading_parse_mode is None
    assert spec.error_text == "❌ 获取状态失败，请稍后重试。"
    assert spec.include_resumable is True
    assert spec.include_event_summary is False


def test_build_context_view_spec_for_callback():
    """Callback mode should keep refresh copy and event summary enabled."""
    service = SessionInteractionService()

    spec = service.build_context_view_spec(for_callback=True)

    assert "正在刷新状态" in spec.loading_text
    assert spec.loading_parse_mode == "Markdown"
    assert spec.include_resumable is False
    assert spec.include_event_summary is True


def test_build_context_render_result_for_standard_mode():
    """Standard context mode should return markdown single message."""
    service = SessionInteractionService()
    snapshot = SimpleNamespace(lines=["line1", "line2"])

    result = service.build_context_render_result(
        snapshot=snapshot,
        scope_state={},
        approved_directory=Path("/tmp"),
        full_mode=False,
    )

    assert result.primary_text == "line1\nline2"
    assert result.parse_mode == "Markdown"
    assert result.extra_texts == ()


def test_build_context_render_result_for_full_mode_with_chunk_split():
    """Full context mode should return plain-text chunks when content is long."""
    service = SessionInteractionService()
    snapshot = SimpleNamespace(
        lines=["line1", "line2"],
        precise_context={
            "used_tokens": 33_600,
            "total_tokens": 200_000,
            "remaining_tokens": 166_400,
            "used_percent": 16.8,
            "raw_text": (
                "### MCP Tools\n\n"
                "| Tool | Server | Tokens |\n"
                "|------|--------|--------|\n"
                "| mcp__a | notion-local | 1.5k |\n"
                "| mcp__b | notion-local | 800 |\n"
            ),
            "session_id": "session-1",
            "cached": False,
        },
        session_info={
            "project": "/tmp",
            "created": "2026-02-12T10:00:00",
            "last_used": "2026-02-12T10:05:00",
            "cost": 0.1,
            "turns": 1,
            "messages": 1,
            "expired": False,
            "tools_used": [],
            "model_usage": {},
        },
        resumable_payload=None,
    )
    scope_state = {
        "current_directory": Path("/tmp/project"),
        "claude_model": "sonnet",
        "claude_session_id": "session-abcdef123",
    }

    result = service.build_context_render_result(
        snapshot=snapshot,
        scope_state=scope_state,
        approved_directory=Path("/tmp"),
        full_mode=True,
        max_length=120,
    )

    assert result.parse_mode is None
    assert result.primary_text.startswith("Session Context (full)")
    assert result.extra_texts
