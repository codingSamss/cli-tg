"""Tests for Telegram send helper behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.utils.telegram_send import (
    send_message_draft_resilient,
    send_message_resilient,
    trim_draft_text_for_telegram,
)


@pytest.mark.asyncio
async def test_send_message_resilient_private_chat_drops_reply_to_message_id():
    """Private chats should not include quote replies by default."""
    bot = SimpleNamespace(send_message=AsyncMock(return_value=object()))

    await send_message_resilient(
        bot=bot,
        chat_id=12345,
        text="hello",
        reply_to_message_id=777,
        chat_type="private",
    )

    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert kwargs["text"] == "hello"
    assert "reply_to_message_id" not in kwargs


@pytest.mark.asyncio
async def test_send_message_resilient_group_chat_keeps_reply_to_message_id():
    """Group chats should keep explicit reply target."""
    bot = SimpleNamespace(send_message=AsyncMock(return_value=object()))

    await send_message_resilient(
        bot=bot,
        chat_id=-100123,
        text="hello",
        reply_to_message_id=777,
        chat_type="supergroup",
    )

    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == -100123
    assert kwargs["reply_to_message_id"] == 777


@pytest.mark.asyncio
async def test_send_message_draft_resilient_private_chat_uses_raw_api():
    """Private chat draft streaming should call sendMessageDraft via raw API."""
    bot = SimpleNamespace(_post=AsyncMock(return_value=True))

    sent = await send_message_draft_resilient(
        bot=bot,
        chat_id=12345,
        draft_id=777,
        text="hello draft",
        chat_type="private",
    )

    assert sent is True
    endpoint, payload = bot._post.await_args.args
    assert endpoint == "sendMessageDraft"
    assert payload["chat_id"] == 12345
    assert payload["draft_id"] == 777
    assert payload["text"] == "hello draft"


@pytest.mark.asyncio
async def test_send_message_draft_resilient_group_chat_is_noop():
    """Draft streaming is private-chat only."""
    bot = SimpleNamespace(_post=AsyncMock(return_value=True))

    sent = await send_message_draft_resilient(
        bot=bot,
        chat_id=-100123,
        draft_id=1,
        text="hello",
        chat_type="supergroup",
    )

    assert sent is False
    assert bot._post.await_count == 0


@pytest.mark.asyncio
async def test_send_message_draft_resilient_parse_mode_fallback():
    """Markdown parse failures should retry draft update without parse mode."""
    bot = SimpleNamespace(
        _post=AsyncMock(side_effect=[Exception("can't parse entities"), True])
    )

    sent = await send_message_draft_resilient(
        bot=bot,
        chat_id=12345,
        draft_id=2,
        text="*broken",
        parse_mode="Markdown",
        chat_type="private",
    )

    assert sent is True
    first_payload = bot._post.await_args_list[0].args[1]
    second_payload = bot._post.await_args_list[1].args[1]
    assert first_payload["parse_mode"] == "Markdown"
    assert "parse_mode" not in second_payload


def test_trim_draft_text_for_telegram_adds_ellipsis():
    """Draft text should be trimmed to Telegram limit with visible suffix."""
    text = "a" * 5000
    trimmed = trim_draft_text_for_telegram(text)

    assert len(trimmed) == 4096
    assert trimmed.endswith("...")
