"""Tests for /sendpic command handler."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.handlers.command import send_picture_command


def _scope_key(user_id: int, chat_id: int) -> str:
    return f"{user_id}:{chat_id}:0"


def _build_settings(approved_directory: Path) -> SimpleNamespace:
    return SimpleNamespace(approved_directory=approved_directory)


def _build_update(user_id: int, chat_id: int, chat_type: str = "private"):
    message = SimpleNamespace(message_id=9001, reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=message,
    )
    return update


@pytest.mark.asyncio
async def test_sendpic_command_supports_http_url_source(tmp_path):
    """Command should send remote image URL directly."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 2001
    chat_id = -1002001
    update = _build_update(user_id, chat_id, "supergroup")
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=object()))

    context = SimpleNamespace(
        args=["https://example.com/cat.png", "cute", "cat"],
        bot=bot,
        bot_data={"settings": _build_settings(approved)},
        user_data={"scope_state": {_scope_key(user_id, chat_id): {}}},
    )

    await send_picture_command(update, context)

    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["chat_id"] == chat_id
    assert kwargs["photo"] == "https://example.com/cat.png"
    assert kwargs["caption"] == "cute cat"
    assert kwargs["reply_to_message_id"] == 9001
    assert update.message.reply_text.await_count == 0


@pytest.mark.asyncio
async def test_sendpic_command_supports_local_path_source(tmp_path):
    """Command should send local image file inside approved workspace."""
    approved = tmp_path / "approved"
    approved.mkdir()
    workspace = approved / "workspace"
    workspace.mkdir()
    image_path = workspace / "duck.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 512)

    user_id = 2002
    chat_id = 2002
    update = _build_update(user_id, chat_id)
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=object()))
    context = SimpleNamespace(
        args=["duck.png"],
        bot=bot,
        bot_data={"settings": _build_settings(approved)},
        user_data={
            "scope_state": {
                _scope_key(user_id, chat_id): {
                    "current_directory": workspace,
                }
            }
        },
    )

    await send_picture_command(update, context)

    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["chat_id"] == chat_id
    assert kwargs["filename"] == "duck.png"
    assert hasattr(kwargs["photo"], "read")
    assert kwargs["photo"].closed is True
    assert update.message.reply_text.await_count == 0


@pytest.mark.asyncio
async def test_sendpic_command_rejects_outside_approved_directory(tmp_path):
    """Command should block local paths outside approved directory."""
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_image = outside / "secret.png"
    outside_image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 128)

    user_id = 2003
    chat_id = 2003
    update = _build_update(user_id, chat_id)
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=object()))
    context = SimpleNamespace(
        args=[str(outside_image)],
        bot=bot,
        bot_data={"settings": _build_settings(approved)},
        user_data={"scope_state": {_scope_key(user_id, chat_id): {}}},
    )

    await send_picture_command(update, context)

    assert bot.send_photo.await_count == 0
    rendered = update.message.reply_text.await_args.args[0]
    assert "发送失败" in rendered
    assert "outside approved directory" in rendered


@pytest.mark.asyncio
async def test_sendpic_command_without_args_sends_latest_image(tmp_path):
    """No args should auto-send latest image from current workspace."""
    approved = tmp_path / "approved"
    approved.mkdir()
    workspace = approved / "workspace"
    workspace.mkdir()

    older = workspace / "older.png"
    newer = workspace / "newer.jpg"
    older.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    newer.write_bytes(b"\xff\xd8\xff" + b"\x00" * 64)
    os.utime(older, (older.stat().st_atime, older.stat().st_mtime - 10))

    user_id = 2004
    chat_id = 2004
    update = _build_update(user_id, chat_id)
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=object()))
    context = SimpleNamespace(
        args=[],
        bot=bot,
        bot_data={"settings": _build_settings(approved)},
        user_data={
            "scope_state": {
                _scope_key(user_id, chat_id): {
                    "current_directory": workspace,
                }
            }
        },
    )

    await send_picture_command(update, context)

    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["filename"] == "newer.jpg"
    assert update.message.reply_text.await_count == 0


@pytest.mark.asyncio
async def test_sendpic_command_without_args_shows_help_when_no_image(tmp_path):
    """No args with no image should return usage guidance."""
    approved = tmp_path / "approved"
    approved.mkdir()

    user_id = 2005
    chat_id = 2005
    update = _build_update(user_id, chat_id)
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=object()))
    context = SimpleNamespace(
        args=[],
        bot=bot,
        bot_data={"settings": _build_settings(approved)},
        user_data={"scope_state": {_scope_key(user_id, chat_id): {}}},
    )

    await send_picture_command(update, context)

    assert bot.send_photo.await_count == 0
    rendered = update.message.reply_text.await_args.args[0]
    assert "无参数时会自动发送最近一张图片" in rendered
