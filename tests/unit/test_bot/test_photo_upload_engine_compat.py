"""Tests for photo upload engine compatibility guardrails."""

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.handlers import message as message_handler
from src.bot.handlers.message import _integration_supports_image_analysis, handle_photo
from src.bot.utils.cli_engine import ENGINE_STATE_KEY
from src.config.settings import Settings


def _scope_key(user_id: int) -> str:
    return f"{user_id}:{user_id}:0"


class _FakeFeatures:
    """Minimal features adapter exposing image handler."""

    def __init__(self, image_handler):
        self._image_handler = image_handler

    def get_image_handler(self):
        return self._image_handler


class _FakeStreamUpdate:
    """Minimal stream update object used by image flow tests."""

    def __init__(
        self,
        *,
        update_type: str,
        content: str = "",
        metadata: dict | None = None,
        tool_calls: list | None = None,
    ) -> None:
        self.type = update_type
        self.content = content
        self.metadata = metadata or {}
        self.tool_calls = tool_calls or []
        self.progress = None

    def get_progress_percentage(self):
        return None

    def is_error(self) -> bool:
        return False

    def get_error_message(self) -> str:
        return ""


def _build_update_and_progress(user_id: int):
    progress_msg = SimpleNamespace(edit_text=AsyncMock(), message_id=9001)
    message = SimpleNamespace(
        reply_text=AsyncMock(return_value=progress_msg),
        message_id=1001,
        caption=None,
        photo=[],
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=message,
    )
    return update, progress_msg


def _mk_integration(*, use_sdk: bool):
    return SimpleNamespace(
        config=SimpleNamespace(use_sdk=use_sdk),
        sdk_manager=object() if use_sdk else None,
    )


def test_integration_supports_image_analysis_for_codex_subprocess():
    """Codex subprocess capability should be recognized as image-enabled."""
    integration = SimpleNamespace(
        config=SimpleNamespace(use_sdk=False),
        sdk_manager=None,
        process_manager=SimpleNamespace(supports_image_inputs=lambda images=None: True),
    )

    assert _integration_supports_image_analysis(integration) is True


@pytest.mark.asyncio
async def test_photo_upload_prompts_switch_to_claude_when_codex_active(tmp_path):
    """Codex active without image capability should guide user to /engine claude."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 3101
    update, progress_msg = _build_update_and_progress(user_id)

    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(approved_directory=approved),
            "features": _FakeFeatures(image_handler=object()),
            "cli_integrations": {
                "codex": _mk_integration(use_sdk=False),
                "claude": _mk_integration(use_sdk=True),
            },
        },
        user_data={
            "scope_state": {
                _scope_key(user_id): {
                    ENGINE_STATE_KEY: "codex",
                    "current_directory": approved,
                }
            }
        },
    )

    await handle_photo(update, context)

    rendered = progress_msg.edit_text.await_args.args[0]
    assert "当前引擎不支持图片分析" in rendered
    assert "/engine claude" in rendered
    assert "当前引擎：`codex`" in rendered


@pytest.mark.asyncio
async def test_photo_upload_reports_sdk_required_when_no_engine_supports_images(
    tmp_path,
):
    """When no integration supports images, should return SDK mode guidance."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 3102
    update, progress_msg = _build_update_and_progress(user_id)

    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(approved_directory=approved),
            "features": _FakeFeatures(image_handler=object()),
            "cli_integrations": {
                "claude": _mk_integration(use_sdk=False),
            },
        },
        user_data={
            "scope_state": {
                _scope_key(user_id): {
                    ENGINE_STATE_KEY: "claude",
                    "current_directory": approved,
                }
            }
        },
    )

    await handle_photo(update, context)

    rendered = progress_msg.edit_text.await_args.args[0]
    assert "图片分析需要 SDK 模式" in rendered
    assert "USE_SDK" in rendered


@pytest.mark.asyncio
async def test_photo_upload_codex_passes_cli_image_file_and_cleans_up(tmp_path):
    """Codex image flow should attach local file path and cleanup temp file."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 3201

    settings = Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=approved,
        use_sdk=False,
    )

    progress_msg = SimpleNamespace(
        edit_text=AsyncMock(),
        delete=AsyncMock(),
        message_id=9101,
    )
    message = SimpleNamespace(
        reply_text=AsyncMock(return_value=progress_msg),
        message_id=1001,
        caption="请分析",
        photo=[SimpleNamespace()],
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=message,
    )

    image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256
    processed_image = SimpleNamespace(
        prompt="请分析",
        base64_data=base64.b64encode(image_bytes).decode("utf-8"),
        metadata={"format": "png"},
    )
    image_handler = SimpleNamespace(
        process_image=AsyncMock(return_value=processed_image)
    )

    async def _run_command_side_effect(**kwargs):
        on_stream = kwargs.get("on_stream")
        if on_stream:
            await on_stream(
                _FakeStreamUpdate(
                    update_type="system",
                    metadata={"subtype": "init", "tools": [], "engine": "codex"},
                )
            )
            await on_stream(
                _FakeStreamUpdate(
                    update_type="progress",
                    metadata={"engine": "codex", "subtype": "turn.started"},
                )
            )
            await on_stream(
                _FakeStreamUpdate(
                    update_type="assistant",
                    content="正在读取图片...",
                    metadata={"engine": "codex"},
                )
            )
            await on_stream(
                _FakeStreamUpdate(
                    update_type="progress",
                    metadata={"engine": "codex", "subtype": "turn.started"},
                )
            )
            await on_stream(
                _FakeStreamUpdate(
                    update_type="system",
                    metadata={"subtype": "model_resolved", "model": "gpt-5.3-codex"},
                )
            )
        return SimpleNamespace(
            session_id="codex-session-1",
            content="图片分析完成",
        )

    run_command = AsyncMock(side_effect=_run_command_side_effect)
    codex_integration = SimpleNamespace(
        config=SimpleNamespace(use_sdk=False),
        sdk_manager=None,
        process_manager=SimpleNamespace(supports_image_inputs=lambda images=None: True),
        run_command=run_command,
    )

    context = SimpleNamespace(
        bot=SimpleNamespace(),
        bot_data={
            "settings": settings,
            "features": _FakeFeatures(image_handler=image_handler),
            "cli_integrations": {"codex": codex_integration},
        },
        user_data={
            "scope_state": {
                _scope_key(user_id): {
                    ENGINE_STATE_KEY: "codex",
                    "current_directory": approved,
                }
            }
        },
    )
    await handle_photo(update, context)

    kwargs = run_command.await_args.kwargs
    image_payload = kwargs["images"][0]
    image_path = Path(image_payload["file_path"])
    edited_texts = [call.args[0] for call in progress_msg.edit_text.await_args_list]

    assert image_payload["media_type"] == "image/png"
    assert image_path.suffix == ".png"
    assert image_path.exists() is False
    assert any("Starting Codex" in text for text in edited_texts)
    assert any("Codex is working" in text for text in edited_texts)
    assert not any(text.count("Codex is working") > 1 for text in edited_texts)
    assert not any("Session context" in text for text in edited_texts)


@pytest.mark.asyncio
async def test_photo_upload_claude_stream_progress_matches_text_flow(tmp_path):
    """Claude image flow should also show stream-thinking style progress lines."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 3301

    settings = Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=approved,
        use_sdk=True,
    )

    progress_msg = SimpleNamespace(
        edit_text=AsyncMock(),
        delete=AsyncMock(),
        message_id=9201,
    )
    message = SimpleNamespace(
        reply_text=AsyncMock(return_value=progress_msg),
        message_id=1001,
        caption="请分析",
        photo=[SimpleNamespace()],
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=message,
    )

    image_bytes = b"\xff\xd8\xff" + b"\x00" * 256
    processed_image = SimpleNamespace(
        prompt="请分析",
        base64_data=base64.b64encode(image_bytes).decode("utf-8"),
        metadata={"format": "jpeg"},
    )
    image_handler = SimpleNamespace(
        process_image=AsyncMock(return_value=processed_image)
    )

    async def _run_command_side_effect(**kwargs):
        on_stream = kwargs.get("on_stream")
        if on_stream:
            await on_stream(
                _FakeStreamUpdate(
                    update_type="system",
                    metadata={"subtype": "init", "tools": ["Read"], "engine": "claude"},
                )
            )
            await on_stream(
                _FakeStreamUpdate(
                    update_type="progress",
                    metadata={"subtype": "turn.started", "engine": "claude"},
                )
            )
            await on_stream(
                _FakeStreamUpdate(
                    update_type="system",
                    metadata={"subtype": "model_resolved", "model": "claude-opus-4.1"},
                )
            )
        return SimpleNamespace(session_id="claude-session-1", content="图片分析完成")

    claude_integration = SimpleNamespace(
        config=SimpleNamespace(use_sdk=True),
        sdk_manager=object(),
        run_command=AsyncMock(side_effect=_run_command_side_effect),
    )

    context = SimpleNamespace(
        bot=SimpleNamespace(),
        bot_data={
            "settings": settings,
            "features": _FakeFeatures(image_handler=image_handler),
            "cli_integrations": {"claude": claude_integration},
        },
        user_data={
            "scope_state": {
                _scope_key(user_id): {
                    ENGINE_STATE_KEY: "claude",
                    "current_directory": approved,
                }
            }
        },
    )

    await handle_photo(update, context)

    edited_texts = [call.args[0] for call in progress_msg.edit_text.await_args_list]
    assert any("Starting Claude" in text for text in edited_texts)
    assert any("🟧 `Claude CLI`" in text for text in edited_texts)


@pytest.mark.asyncio
async def test_photo_upload_private_chat_drafts_only_final_response(
    tmp_path, monkeypatch
):
    """Private image flow should use draft only for final response delivery."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 3302

    settings = Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=approved,
        use_sdk=True,
    )

    progress_msg = SimpleNamespace(
        edit_text=AsyncMock(),
        delete=AsyncMock(),
        message_id=9301,
    )
    message = SimpleNamespace(
        reply_text=AsyncMock(return_value=progress_msg),
        message_id=1002,
        caption="请分析截图",
        photo=[SimpleNamespace()],
        message_thread_id=None,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id, type="private"),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=message,
    )

    image_bytes = b"\xff\xd8\xff" + b"\x00" * 256
    processed_image = SimpleNamespace(
        prompt="请分析截图",
        base64_data=base64.b64encode(image_bytes).decode("utf-8"),
        metadata={"format": "jpeg"},
    )
    image_handler = SimpleNamespace(
        process_image=AsyncMock(return_value=processed_image)
    )

    async def _run_command_side_effect(**kwargs):
        on_stream = kwargs.get("on_stream")
        if on_stream:
            await on_stream(
                _FakeStreamUpdate(
                    update_type="assistant",
                    content="中间进度",
                    metadata={"engine": "codex"},
                )
            )
        return SimpleNamespace(session_id="codex-session-2", content="最终结论")

    codex_integration = SimpleNamespace(
        config=SimpleNamespace(use_sdk=True),
        sdk_manager=object(),
        run_command=AsyncMock(side_effect=_run_command_side_effect),
    )

    draft_sender = AsyncMock(return_value=True)
    monkeypatch.setattr(message_handler, "send_message_draft_resilient", draft_sender)

    context = SimpleNamespace(
        bot=SimpleNamespace(),
        bot_data={
            "settings": settings,
            "features": _FakeFeatures(image_handler=image_handler),
            "cli_integrations": {"codex": codex_integration},
        },
        user_data={
            "scope_state": {
                _scope_key(user_id): {
                    ENGINE_STATE_KEY: "codex",
                    "current_directory": approved,
                }
            }
        },
    )

    await handle_photo(update, context)

    assert draft_sender.await_count == 1
    assert draft_sender.await_args.kwargs["chat_id"] == user_id
    assert draft_sender.await_args.kwargs["chat_type"] == "private"
    assert "最终结论" in draft_sender.await_args.kwargs["text"]
