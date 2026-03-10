"""Tests for session/event services."""

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.claude.integration import ClaudeResponse
from src.services import EventService, SessionService
from src.storage.facade import Storage


@pytest.fixture
async def storage():
    """Create test storage."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test.db"
        storage = Storage(f"sqlite:///{db_path}")
        await storage.initialize()
        yield storage
        await storage.close()


@pytest.mark.asyncio
async def test_event_service_builds_recent_summary(storage):
    """Event service should summarize recent events by type."""
    await storage.get_or_create_user(22001, "svc_user")
    await storage.create_session(22001, "/test/svc", "svc-session-1")

    response = ClaudeResponse(
        content="处理完成",
        session_id="svc-session-1",
        cost=0.12,
        duration_ms=3200,
        num_turns=2,
        tools_used=[{"name": "Bash", "input": {"command": "pytest -q"}}],
    )
    await storage.save_claude_interaction(
        user_id=22001,
        session_id="svc-session-1",
        prompt="帮我运行测试并总结结果",
        response=response,
    )

    service = EventService(storage)
    summary = await service.get_recent_event_summary("svc-session-1", limit=20)

    assert summary["count"] >= 5
    assert summary["by_type"]["command_exec"] == 1
    assert summary["by_type"]["assistant_text"] == 1
    assert summary["by_type"]["tool_call"] == 1
    assert summary["by_type"]["tool_result"] == 1
    assert summary["latest_at"] is not None
    assert summary["highlights"]


@pytest.mark.asyncio
async def test_session_service_context_event_lines(storage):
    """Session service should render markdown lines for /context."""
    await storage.get_or_create_user(22002, "svc_user2")
    await storage.create_session(22002, "/test/svc2", "svc-session-2")

    response = ClaudeResponse(
        content="已完成最小修复。",
        session_id="svc-session-2",
        cost=0.03,
        duration_ms=1800,
        num_turns=1,
    )
    await storage.save_claude_interaction(
        user_id=22002,
        session_id="svc-session-2",
        prompt="修复这个报错",
        response=response,
    )

    event_service = EventService(storage)
    session_service = SessionService(storage=storage, event_service=event_service)
    lines = await session_service.get_context_event_lines("svc-session-2")

    rendered = "\n".join(lines)
    assert "Recent Session Events" in rendered
    assert "By Type:" in rendered
    assert "`command_exec`" in rendered
    assert "`assistant_text`" in rendered
    assert "Highlights:" not in rendered


@pytest.mark.asyncio
async def test_session_service_context_event_lines_empty(storage):
    """Unknown session should return empty summary lines."""
    event_service = EventService(storage)
    session_service = SessionService(storage=storage, event_service=event_service)

    lines = await session_service.get_context_event_lines("missing-session")
    assert lines == []


@pytest.mark.asyncio
async def test_build_context_snapshot_for_active_session():
    """Unified snapshot should include context/session/event lines."""
    approved = Path("/tmp/project")
    current_dir = approved
    claude_integration = SimpleNamespace(
        get_precise_context_usage=AsyncMock(
            return_value={
                "used_tokens": 1000,
                "total_tokens": 200000,
                "remaining_tokens": 199000,
                "used_percent": 0.5,
                "session_id": "sess-abc",
                "cached": False,
            }
        ),
        get_session_info=AsyncMock(
            return_value={
                "messages": 3,
                "turns": 2,
                "cost": 0.12,
                "model_usage": None,
            }
        ),
    )

    async def _event_lines_provider(session_id: str):
        assert session_id == "sess-abc"
        return ["", "*Recent Session Events*", "Count: 2"]

    snapshot = await SessionService.build_context_snapshot(
        user_id=3001,
        session_id="sess-abc",
        current_dir=current_dir,
        approved_directory=approved,
        current_model="sonnet",
        claude_integration=claude_integration,
        include_resumable=True,
        event_lines_provider=_event_lines_provider,
    )

    rendered = "\n".join(snapshot.lines)
    assert "Session: `sess-abc...`" in rendered
    assert "Messages: 3" in rendered
    assert "Turns: 2" in rendered
    assert "Cost: `$0.1200`" in rendered
    assert "Recent Session Events" in rendered
    assert snapshot.precise_context is not None
    assert snapshot.session_info is not None


@pytest.mark.asyncio
async def test_build_context_snapshot_can_skip_precise_probe():
    """Context snapshot should skip /context probe when capability is disabled."""
    approved = Path("/tmp/project")
    claude_integration = SimpleNamespace(
        get_precise_context_usage=AsyncMock(
            return_value={
                "used_tokens": 1,
                "total_tokens": 2,
                "remaining_tokens": 1,
                "used_percent": 50.0,
            }
        ),
        get_session_info=AsyncMock(
            return_value={
                "messages": 1,
                "turns": 1,
                "cost": 0.01,
                "model_usage": {"input_tokens": 100, "output_tokens": 20},
            }
        ),
    )

    snapshot = await SessionService.build_context_snapshot(
        user_id=3010,
        session_id="sess-no-probe",
        current_dir=approved,
        approved_directory=approved,
        current_model="gpt-5",
        claude_integration=claude_integration,
        allow_precise_context_probe=False,
    )

    claude_integration.get_precise_context_usage.assert_not_awaited()
    claude_integration.get_session_info.assert_awaited_once()
    rendered = "\n".join(snapshot.lines)
    assert "Context (" in rendered


@pytest.mark.asyncio
async def test_build_context_snapshot_codex_without_precise_uses_status_hint():
    """Codex should avoid rendering cumulative usage as current context when probe fails."""
    approved = Path("/tmp/project")
    process_manager = SimpleNamespace(
        _resolve_cli_path=lambda: "/usr/local/bin/codex",
        _detect_cli_kind=lambda _: "codex",
    )
    claude_integration = SimpleNamespace(
        process_manager=process_manager,
        get_precise_context_usage=AsyncMock(return_value=None),
        get_session_info=AsyncMock(
            return_value={
                "messages": 26,
                "turns": 26,
                "cost": 0.0,
                "model_usage": {
                    "input_tokens": 81_313_238,
                    "cached_input_tokens": 75_014_784,
                    "output_tokens": 319_348,
                },
            }
        ),
    )

    snapshot = await SessionService.build_context_snapshot(
        user_id=3011,
        session_id="thread-codex-1",
        current_dir=approved,
        approved_directory=approved,
        current_model="default",
        claude_integration=claude_integration,
        allow_precise_context_probe=True,
    )

    rendered = "\n".join(snapshot.lines)
    assert "Context (/status)" in rendered
    assert "实时额度获取失败。请稍后重试或手动执行 `/status`。" in rendered
    assert "Cost:" not in rendered
    assert "Tokens: `156,647,370`" not in rendered
    claude_integration.get_precise_context_usage.assert_awaited_once()


@pytest.mark.asyncio
async def test_build_context_snapshot_codex_probe_failure_shows_status_hint():
    """Codex /context should show status hint when live probe is unavailable."""
    approved = Path("/tmp/project")
    process_manager = SimpleNamespace(
        _resolve_cli_path=lambda: "/usr/local/bin/codex",
        _detect_cli_kind=lambda _: "codex",
    )
    claude_integration = SimpleNamespace(
        process_manager=process_manager,
        get_precise_context_usage=AsyncMock(return_value=None),
        get_session_info=AsyncMock(
            return_value={
                "messages": 37,
                "turns": 37,
                "cost": 0.0,
                "model_usage": None,
            }
        ),
    )

    snapshot = await SessionService.build_context_snapshot(
        user_id=3012,
        session_id="thread-codex-local",
        current_dir=approved,
        approved_directory=approved,
        current_model="default",
        claude_integration=claude_integration,
        allow_precise_context_probe=True,
    )

    rendered = "\n".join(snapshot.lines)
    assert "Context (/status)" in rendered
    assert "Cost:" not in rendered
    assert "实时额度获取失败。请稍后重试或手动执行 `/status`。" in rendered
    claude_integration.get_precise_context_usage.assert_awaited_once()


@pytest.mark.asyncio
async def test_build_context_snapshot_codex_uses_live_status_usage():
    """Codex /context should use live `/status` usage payload directly."""
    approved = Path("/tmp/project")
    process_manager = SimpleNamespace(
        _resolve_cli_path=lambda: "/usr/local/bin/codex",
        _detect_cli_kind=lambda _: "codex",
    )
    claude_integration = SimpleNamespace(
        process_manager=process_manager,
        get_precise_context_usage=AsyncMock(
            return_value={
                "used_tokens": 40_000,
                "total_tokens": 200_000,
                "remaining_tokens": 160_000,
                "used_percent": 20.0,
                "probe_command": "/status",
                "cached": False,
                "resolved_model": "gpt-5.3-codex",
                "reasoning_effort": "xhigh",
            }
        ),
        get_session_info=AsyncMock(
            return_value={
                "messages": 37,
                "turns": 37,
                "cost": 0.0,
                "model_usage": None,
            }
        ),
    )

    snapshot = await SessionService.build_context_snapshot(
        user_id=3099,
        session_id="thread-codex-live",
        current_dir=approved,
        approved_directory=approved,
        current_model="default",
        claude_integration=claude_integration,
        allow_precise_context_probe=True,
    )

    rendered = "\n".join(snapshot.lines)
    assert "Usage: `40,000` / `200,000` (20.0%)" in rendered
    assert "Model: `gpt-5.3-codex (X High)`" in rendered
    claude_integration.get_precise_context_usage.assert_awaited_once()


@pytest.mark.asyncio
async def test_build_context_snapshot_codex_keeps_user_model_when_probe_unavailable():
    """Codex should keep user-selected model when live probe has no runtime model."""
    approved = Path("/tmp/project")
    process_manager = SimpleNamespace(
        _resolve_cli_path=lambda: "/usr/local/bin/codex",
        _detect_cli_kind=lambda _: "codex",
    )
    claude_integration = SimpleNamespace(
        process_manager=process_manager,
        get_precise_context_usage=AsyncMock(return_value=None),
        get_session_info=AsyncMock(
            return_value={
                "messages": 1,
                "turns": 1,
                "cost": 0.0,
                "model_usage": None,
            }
        ),
    )

    snapshot = await SessionService.build_context_snapshot(
        user_id=3013,
        session_id="thread-codex-stale-model",
        current_dir=approved,
        approved_directory=approved,
        current_model="gpt-5.1-codex-mini",
        claude_integration=claude_integration,
        allow_precise_context_probe=True,
    )

    rendered = "\n".join(snapshot.lines)
    assert "Model: `gpt-5.1-codex-mini`" in rendered


def test_get_cached_codex_snapshot_respects_ttl(monkeypatch):
    """Cached Codex snapshot should obey TTL before expiring."""
    session_id = "rate-limit-cache"
    SessionService._codex_snapshot_cache.clear()
    sample_snapshot = {"used_tokens": 1}
    base_time = time.monotonic()
    SessionService._codex_snapshot_cache[session_id] = (base_time, sample_snapshot)

    first = SessionService.get_cached_codex_snapshot(session_id)
    assert first == sample_snapshot

    monkeypatch.setattr(
        time,
        "monotonic",
        lambda: base_time + SessionService._codex_snapshot_ttl_seconds + 1,
    )
    expired = SessionService.get_cached_codex_snapshot(session_id)
    assert expired is None


def test_resolve_codex_snapshot_falls_back_to_local_probe(monkeypatch):
    """Codex snapshot resolver should read local session data when cache misses."""
    session_id = "rate-limit-probe"
    expected_snapshot = {"used_tokens": 1234, "resolved_model": "gpt-5.4"}
    SessionService._codex_snapshot_cache.clear()
    monkeypatch.setattr(
        SessionService,
        "_probe_codex_session_snapshot",
        staticmethod(lambda sid: expected_snapshot if sid == session_id else None),
    )

    resolved = SessionService.resolve_codex_snapshot(session_id)

    assert resolved == expected_snapshot


def test_cache_codex_snapshot_writes_short_lived_entry():
    """Caching a resolved snapshot should update the in-memory Codex cache."""
    session_id = "rate-limit-cache-write"
    SessionService._codex_snapshot_cache.clear()

    SessionService.cache_codex_snapshot(
        session_id,
        {"used_tokens": 12, "total_tokens": 100, "used_percent": 12.0},
    )

    cached = SessionService.get_cached_codex_snapshot(session_id)
    assert cached is not None
    assert cached["used_tokens"] == 12


def test_parse_codex_rate_limits_extracts_primary_secondary():
    """Codex rate_limits payload should be normalized for status rendering."""
    parsed = SessionService._parse_codex_rate_limits(
        {
            "primary": {
                "used_percent": 11,
                "window_minutes": 300,
                "resets_at": 1_771_060_321,
            },
            "secondary": {
                "used_percent": 42,
                "window_minutes": 10_080,
                "resets_at": 1_771_220_100,
            },
        },
        event_timestamp="2026-02-09T13:54:15.687Z",
    )

    assert parsed is not None
    assert parsed["primary"]["used_percent"] == 11.0
    assert parsed["primary"]["window_minutes"] == 300
    assert parsed["secondary"]["used_percent"] == 42.0
    assert parsed["secondary"]["window_minutes"] == 10_080
    assert parsed["updated_at"] == "2026-02-09T13:54:15.687000Z"


@pytest.mark.asyncio
async def test_build_context_snapshot_for_resumable_session():
    """No active session should show resumable info when available."""
    approved = Path("/tmp/project")
    current_dir = approved
    claude_integration = SimpleNamespace(
        _find_resumable_session=AsyncMock(
            return_value=SimpleNamespace(
                session_id="resume-12345678",
                message_count=18,
            )
        )
    )

    snapshot = await SessionService.build_context_snapshot(
        user_id=3002,
        session_id=None,
        current_dir=current_dir,
        approved_directory=approved,
        current_model=None,
        claude_integration=claude_integration,
        include_resumable=True,
    )

    rendered = "\n".join(snapshot.lines)
    assert "Session: none" in rendered
    assert "Resumable: `resume-1...` (18 msgs)" in rendered
    assert snapshot.resumable_payload is not None


@pytest.mark.asyncio
async def test_build_scope_context_snapshot_uses_scoped_state():
    """Scope snapshot helper should map scope state into unified builder args."""
    approved = Path("/tmp/project")
    claude_integration = SimpleNamespace(
        get_precise_context_usage=AsyncMock(return_value=None),
        get_session_info=AsyncMock(return_value=None),
    )
    provider_owner = SimpleNamespace(
        get_context_event_lines=AsyncMock(return_value=["", "*Recent Session Events*"])
    )
    scope_state = {
        "claude_session_id": "scope-sess-001",
        "current_directory": approved,
        "claude_model": "sonnet",
    }

    snapshot = await SessionService.build_scope_context_snapshot(
        user_id=3003,
        scope_state=scope_state,
        approved_directory=approved,
        claude_integration=claude_integration,
        session_service=provider_owner,
        include_resumable=False,
        include_event_summary=True,
    )

    rendered = "\n".join(snapshot.lines)
    assert "Session: `scope-se...`" in rendered
    assert "Recent Session Events" in rendered
    provider_owner.get_context_event_lines.assert_awaited_once_with("scope-sess-001")


@pytest.mark.asyncio
async def test_build_scope_context_snapshot_skips_event_provider_when_disabled():
    """Event provider should not be called when summary flag is disabled."""
    approved = Path("/tmp/project")
    claude_integration = SimpleNamespace(
        get_precise_context_usage=AsyncMock(return_value=None),
        get_session_info=AsyncMock(return_value=None),
    )
    provider_owner = SimpleNamespace(
        get_context_event_lines=AsyncMock(return_value=["", "*Recent Session Events*"])
    )
    scope_state = {
        "claude_session_id": "scope-sess-002",
        "current_directory": approved,
        "claude_model": "sonnet",
    }

    await SessionService.build_scope_context_snapshot(
        user_id=3004,
        scope_state=scope_state,
        approved_directory=approved,
        claude_integration=claude_integration,
        session_service=provider_owner,
        include_resumable=False,
        include_event_summary=False,
    )

    provider_owner.get_context_event_lines.assert_not_awaited()
