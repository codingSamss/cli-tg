"""Tests for Claude subprocess integration parsing behavior."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.claude.exceptions import ClaudeProcessError
from src.claude.integration import ClaudeProcessManager
from src.config.settings import Settings


def _build_manager(tmp_path: Path, **overrides) -> ClaudeProcessManager:
    config = Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        claude_timeout_seconds=5,
        **overrides,
    )
    return ClaudeProcessManager(config)


def test_parse_result_uses_local_command_stdout_fallback(tmp_path):
    """When result text is empty, parser should use local-command stdout payload."""
    manager = _build_manager(tmp_path)
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 100,
        "num_turns": 1,
        "session_id": "session-1",
        "total_cost_usd": 0.0,
        "result": "",
        "modelUsage": {},
    }
    messages = [
        {
            "type": "user",
            "message": {
                "content": (
                    "<local-command-stdout>\n"
                    "## Context Usage\n"
                    "**Tokens:** 28.8k / 200k (14%)\n"
                    "</local-command-stdout>"
                )
            },
        }
    ]

    response = manager._parse_result(result, messages)

    assert "28.8k / 200k" in response.content


def test_extract_local_command_output_ignores_plain_user_text(tmp_path):
    """Regular user message text should not be treated as local command output."""
    manager = _build_manager(tmp_path)

    extracted = manager._extract_local_command_output("hello world")

    assert extracted == ""


def test_build_command_for_codex_exec_uses_codex_flags(tmp_path, monkeypatch):
    """Codex CLI should use exec/json flags instead of Claude-only options."""
    manager = _build_manager(tmp_path, codex_enable_mcp=False)
    monkeypatch.setattr(
        "src.claude.sdk_integration.find_claude_cli",
        lambda _: "/usr/local/bin/codex",
    )

    cmd = manager._build_command(
        prompt="hello codex",
        session_id=None,
        continue_session=False,
    )

    assert cmd[:4] == [
        "/usr/local/bin/codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
    ]
    assert ["-c", "mcp_servers={}"] == cmd[4:6]
    assert cmd[-1] == "hello codex"
    assert "--output-format" not in cmd
    assert "--allowedTools" not in cmd


def test_build_command_for_codex_exec_keeps_mcp_when_enabled(tmp_path, monkeypatch):
    """Codex CLI should not inject MCP override when explicitly enabled."""
    manager = _build_manager(tmp_path, codex_enable_mcp=True)
    monkeypatch.setattr(
        "src.claude.sdk_integration.find_claude_cli",
        lambda _: "/usr/local/bin/codex",
    )

    cmd = manager._build_command(
        prompt="hello codex",
        session_id=None,
        continue_session=False,
    )

    assert "-c" not in cmd
    assert "mcp_servers={}" not in cmd


def test_build_command_for_codex_exec_includes_image_flags(tmp_path, monkeypatch):
    """Codex CLI should map images[*].file_path to repeated --image options."""
    manager = _build_manager(tmp_path, codex_enable_mcp=False)
    monkeypatch.setattr(
        "src.claude.sdk_integration.find_claude_cli",
        lambda _: "/usr/local/bin/codex",
    )

    cmd = manager._build_command(
        prompt="分析这张图",
        session_id=None,
        continue_session=False,
        images=[
            {"file_path": "/tmp/a.png"},
            {"file_path": "/tmp/b.jpg"},
        ],
    )

    assert cmd == [
        "/usr/local/bin/codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-c",
        "mcp_servers={}",
        "--image",
        "/tmp/a.png",
        "--image",
        "/tmp/b.jpg",
        "分析这张图",
    ]


def test_build_command_for_codex_exec_without_prompt_uses_default(tmp_path, monkeypatch):
    """Codex exec should always include a non-empty prompt argument."""
    manager = _build_manager(tmp_path, codex_enable_mcp=False)
    monkeypatch.setattr(
        "src.claude.sdk_integration.find_claude_cli",
        lambda _: "/usr/local/bin/codex",
    )

    cmd = manager._build_command(
        prompt="  ",
        session_id=None,
        continue_session=False,
    )

    assert cmd == [
        "/usr/local/bin/codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-c",
        "mcp_servers={}",
        "Please continue where we left off",
    ]


def test_build_command_for_codex_exec_with_images_and_blank_prompt_uses_image_default(
    tmp_path, monkeypatch
):
    """Codex image exec should synthesize fallback prompt when caption is blank."""
    manager = _build_manager(tmp_path, codex_enable_mcp=False)
    monkeypatch.setattr(
        "src.claude.sdk_integration.find_claude_cli",
        lambda _: "/usr/local/bin/codex",
    )

    cmd = manager._build_command(
        prompt="  ",
        session_id=None,
        continue_session=False,
        images=[{"file_path": "/tmp/a.png"}],
    )

    assert cmd == [
        "/usr/local/bin/codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-c",
        "mcp_servers={}",
        "--image",
        "/tmp/a.png",
        "Please analyze the attached image(s).",
    ]


def test_build_command_for_codex_resume_uses_resume_subcommand(tmp_path, monkeypatch):
    """Codex continuation should use exec resume with session ID and prompt."""
    manager = _build_manager(tmp_path, codex_enable_mcp=False)
    monkeypatch.setattr(
        "src.claude.sdk_integration.find_claude_cli",
        lambda _: "/usr/local/bin/codex",
    )

    cmd = manager._build_command(
        prompt="继续",
        session_id="thread-123",
        continue_session=True,
        model="gpt-5",
    )

    assert cmd == [
        "/usr/local/bin/codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-c",
        "mcp_servers={}",
        "--model",
        "gpt-5",
        "resume",
        "thread-123",
        "继续",
    ]


def test_build_command_for_codex_resume_with_images_places_flags_after_resume(
    tmp_path, monkeypatch
):
    """Codex resume with images should scope --image flags to resume subcommand."""
    manager = _build_manager(tmp_path, codex_enable_mcp=False)
    monkeypatch.setattr(
        "src.claude.sdk_integration.find_claude_cli",
        lambda _: "/usr/local/bin/codex",
    )

    cmd = manager._build_command(
        prompt="请结合这张图继续分析",
        session_id="thread-123",
        continue_session=True,
        images=[{"file_path": "/tmp/a.png"}],
    )

    assert cmd == [
        "/usr/local/bin/codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-c",
        "mcp_servers={}",
        "resume",
        "thread-123",
        "--image",
        "/tmp/a.png",
        "请结合这张图继续分析",
    ]


def test_build_command_for_codex_resume_without_prompt_uses_default(
    tmp_path, monkeypatch
):
    """Codex resume should always carry a non-empty prompt to satisfy CLI contract."""
    manager = _build_manager(tmp_path, codex_enable_mcp=False)
    monkeypatch.setattr(
        "src.claude.sdk_integration.find_claude_cli",
        lambda _: "/usr/local/bin/codex",
    )

    cmd = manager._build_command(
        prompt="",
        session_id="thread-123",
        continue_session=True,
    )

    assert cmd == [
        "/usr/local/bin/codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-c",
        "mcp_servers={}",
        "resume",
        "thread-123",
        "Please continue where we left off",
    ]


def test_parse_result_supports_codex_turn_completed(tmp_path):
    """Codex turn.completed event should map to unified response fields."""
    manager = _build_manager(tmp_path)
    result = {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 120,
            "cached_input_tokens": 40,
            "output_tokens": 15,
        },
    }
    messages = [
        {"type": "thread.started", "thread_id": "019c-test-thread"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "中间回复"},
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "/bin/zsh -lc pwd",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "最终回复"},
        },
    ]

    response = manager._parse_result(result, messages)

    assert response.session_id == "019c-test-thread"
    assert response.content == "最终回复"
    assert response.model_usage == result["usage"]
    assert response.num_turns == 1
    assert response.tools_used[0]["name"] == "Bash"
    assert response.tools_used[0]["exit_code"] == 0


def test_parse_stream_message_supports_codex_agent_message(tmp_path):
    """Codex item.completed agent_message should stream as assistant update."""
    manager = _build_manager(tmp_path)
    update = manager._parse_stream_message(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "ok"},
        }
    )

    assert update is not None
    assert update.type == "assistant"
    assert update.content == "ok"
    assert update.metadata and update.metadata.get("engine") == "codex"


def test_parse_stream_message_condenses_codex_reasoning(tmp_path):
    """Codex reasoning should be condensed to avoid noisy markdown artifacts."""
    manager = _build_manager(tmp_path)
    update = manager._parse_stream_message(
        {
            "type": "item.completed",
            "item": {
                "type": "reasoning",
                "text": "**Planning code review and tests**\n\n"
                "I will inspect handlers and test files next.",
            },
        }
    )

    assert update is not None
    assert update.type == "progress"
    assert update.content == "Planning code review and tests"
    assert update.metadata and update.metadata.get("engine") == "codex"


def test_parse_stream_message_codex_command_execution_keeps_status_metadata(tmp_path):
    """Codex command execution should preserve structured metadata for renderer."""
    manager = _build_manager(tmp_path)
    update = manager._parse_stream_message(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "status": "in_progress",
                "command": "/bin/zsh -lc 'pwd'",
                "exit_code": None,
            },
        }
    )

    assert update is not None
    assert update.type == "progress"
    assert update.content == "/bin/zsh -lc 'pwd'"
    assert update.metadata and update.metadata.get("status") == "in_progress"
    assert update.metadata and update.metadata.get("engine") == "codex"


def test_parse_result_supports_codex_turn_failed(tmp_path):
    """Codex turn.failed event should map to a unified error response."""
    manager = _build_manager(tmp_path)
    response = manager._parse_result(
        {
            "type": "turn.failed",
            "error": {"message": "Model 'sonnet' is not available"},
            "usage": {"input_tokens": 3},
        },
        [
            {"type": "thread.started", "thread_id": "019c-thread-failed"},
            {"type": "error", "message": "fallback error"},
        ],
    )

    assert response.is_error is True
    assert response.error_type == "turn.failed"
    assert response.session_id == "019c-thread-failed"
    assert "not available" in response.content


def test_parse_stream_message_supports_codex_turn_failed(tmp_path):
    """Codex turn.failed should be streamed as error update."""
    manager = _build_manager(tmp_path)
    update = manager._parse_stream_message(
        {
            "type": "turn.failed",
            "error": {"message": "invalid model"},
        }
    )

    assert update is not None
    assert update.type == "error"
    assert update.content == "invalid model"
    assert update.metadata and update.metadata.get("engine") == "codex"


def test_parse_system_init_includes_model_capabilities(tmp_path):
    """System init should expose model capability fields for downstream UI usage."""
    manager = _build_manager(tmp_path)
    update = manager._parse_system_message(
        {
            "subtype": "init",
            "tools": ["Read"],
            "permissionMode": "default",
            "modelInfo": {
                "supportsEffort": True,
                "supportedEffortLevels": ["low", "medium", "high"],
                "supportsAdaptiveThinking": False,
            },
        }
    )

    assert update.type == "system"
    assert update.metadata is not None
    assert update.metadata.get("subtype") == "init"
    assert update.metadata.get("supportsEffort") is True
    assert update.metadata.get("supports_effort") is True
    assert update.metadata.get("supportedEffortLevels") == ["low", "medium", "high"]
    assert update.metadata.get("supported_effort_levels") == [
        "low",
        "medium",
        "high",
    ]
    assert update.metadata.get("supportsAdaptiveThinking") is False
    assert update.metadata.get("supports_adaptive_thinking") is False
    assert update.metadata.get("permission_mode") == "default"


@pytest.mark.asyncio
async def test_start_process_unsets_claudecode_env(tmp_path, monkeypatch):
    """Subprocess mode should not inherit CLAUDECODE marker."""
    manager = _build_manager(tmp_path)
    monkeypatch.setenv("CLAUDECODE", "nested-session")

    captured_env = {}

    async def _fake_create_subprocess_exec(*args, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return SimpleNamespace()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    await manager._start_process(["echo", "ok"], tmp_path)

    assert "CLAUDECODE" not in captured_env


@pytest.mark.asyncio
async def test_execute_command_emits_codex_init_update(tmp_path, monkeypatch):
    """Codex subprocess should emit init update for progress UI."""
    manager = _build_manager(tmp_path)
    monkeypatch.setattr(
        "src.claude.sdk_integration.find_claude_cli",
        lambda _: "/usr/local/bin/codex",
    )

    async def _fake_start_process(*_args, **_kwargs):
        return SimpleNamespace()

    async def _fake_handle_process_output(*_args, **_kwargs):
        return SimpleNamespace(
            content="ok",
            session_id="sid-1",
            cost=0.0,
            duration_ms=1,
            num_turns=1,
            is_error=False,
            error_type=None,
            tools_used=[],
            model_usage=None,
        )

    monkeypatch.setattr(manager, "_start_process", _fake_start_process)
    monkeypatch.setattr(manager, "_handle_process_output", _fake_handle_process_output)

    updates = []

    async def _stream_callback(update):
        updates.append(update)

    await manager.execute_command(
        prompt="hello",
        working_directory=tmp_path,
        stream_callback=_stream_callback,
        model="gpt-5",
    )

    assert len(updates) == 1
    assert updates[0].type == "system"
    assert updates[0].metadata["subtype"] == "init"
    assert updates[0].metadata["engine"] == "codex"
    assert updates[0].metadata["model"] == "gpt-5"


def test_parse_stream_message_supports_codex_turn_context_model(tmp_path):
    """Codex turn_context should stream resolved runtime model."""
    manager = _build_manager(tmp_path)
    update = manager._parse_stream_message(
        {
            "type": "turn_context",
            "payload": {"model": "gpt-5.2-codex"},
        }
    )

    assert update is not None
    assert update.type == "system"
    assert update.metadata and update.metadata.get("subtype") == "model_resolved"
    assert update.metadata and update.metadata.get("engine") == "codex"
    assert update.metadata and update.metadata.get("model") == "gpt-5.2-codex"


@pytest.mark.asyncio
async def test_handle_process_output_raises_codex_turn_failed_error(
    tmp_path, monkeypatch
):
    """Codex turn.failed should return real error instead of missing-result parsing error."""
    manager = _build_manager(tmp_path)
    lines = [
        '{"type":"thread.started","thread_id":"019c-thread"}',
        '{"type":"turn.failed","error":{"message":"unexpected model"}}',
    ]

    async def _fake_stream(_):
        for line in lines:
            yield line

    process = SimpleNamespace(
        stdout=object(),
        stderr=SimpleNamespace(read=AsyncMock(return_value=b"")),
        wait=AsyncMock(return_value=0),
    )
    monkeypatch.setattr(manager, "_read_stream_bounded", _fake_stream)

    with pytest.raises(ClaudeProcessError) as exc_info:
        await manager._handle_process_output(
            process,
            stream_callback=None,
            cli_kind="codex",
        )

    assert "unexpected model" in str(exc_info.value)


@pytest.mark.asyncio
async def test_handle_process_output_emits_codex_model_from_snapshot_fallback(
    tmp_path, monkeypatch
):
    """Codex stream without turn_context should still emit resolved model from snapshot."""
    manager = _build_manager(tmp_path)
    lines = [
        '{"type":"thread.started","thread_id":"019c-thread"}',
        '{"type":"turn.started"}',
        '{"type":"turn.completed","duration_ms":2,"usage":{"input_tokens":3}}',
    ]

    async def _fake_stream(_):
        for line in lines:
            yield line

    process = SimpleNamespace(
        stdout=object(),
        stderr=SimpleNamespace(read=AsyncMock(return_value=b"")),
        wait=AsyncMock(return_value=0),
    )
    monkeypatch.setattr(manager, "_read_stream_bounded", _fake_stream)
    monkeypatch.setattr(
        manager,
        "_probe_codex_model_from_local_session",
        staticmethod(lambda _sid: "gpt-5.2-codex"),
    )

    updates = []

    async def _stream_callback(update):
        updates.append(update)

    response = await manager._handle_process_output(
        process,
        stream_callback=_stream_callback,
        cli_kind="codex",
    )

    model_updates = [
        u
        for u in updates
        if u.type == "system" and (u.metadata or {}).get("subtype") == "model_resolved"
    ]
    assert model_updates
    assert model_updates[0].metadata.get("model") == "gpt-5.2-codex"
    assert response.session_id == "019c-thread"
