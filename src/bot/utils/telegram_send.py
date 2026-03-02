"""Telegram send helpers with parse/thread/length fallbacks."""

from __future__ import annotations

from typing import Any, Optional

_TELEGRAM_MESSAGE_LIMIT = 4096
_TELEGRAM_SAFE_SPLIT_LIMIT = 3800
_TELEGRAM_DRAFT_MESSAGE_LIMIT = 4096


def is_private_chat_type(chat_type: Optional[str]) -> bool:
    """Whether chat type is private dialog."""
    return str(chat_type or "").strip().lower() == "private"


def is_markdown_parse_error(error: Exception) -> bool:
    """Whether Telegram send failure is caused by entity parsing."""
    error_text = str(error).lower()
    return "can't parse entities" in error_text or "cannot parse entities" in error_text


def is_message_too_long_error(error: Exception) -> bool:
    """Whether Telegram send failure is caused by message length overflow."""
    error_text = str(error).lower()
    return (
        "message is too long" in error_text
        or "text is too long" in error_text
        or "entity is too long" in error_text
    )


def is_thread_not_found_error(error: Exception) -> bool:
    """Whether Telegram rejected the provided topic/thread id."""
    error_text = str(error).lower()
    return "message thread not found" in error_text or "thread not found" in error_text


def split_text_for_telegram(
    text: str, limit: int = _TELEGRAM_SAFE_SPLIT_LIMIT
) -> list[str]:
    """Split long plain text into Telegram-safe chunks."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit

        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            split_at = len(chunk)

        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\n")

    return chunks


def trim_draft_text_for_telegram(
    text: str, limit: int = _TELEGRAM_DRAFT_MESSAGE_LIMIT
) -> str:
    """Trim draft text to Telegram limit while preserving a truncation hint."""
    normalized_text = str(text or "")
    if len(normalized_text) <= limit:
        return normalized_text
    if limit <= 3:
        return normalized_text[:limit]
    return normalized_text[: limit - 3] + "..."


def normalize_message_thread_id(
    message_thread_id: Optional[int],
    *,
    chat_type: Optional[str] = None,
) -> Optional[int]:
    """Normalize thread id with DM/general-topic safety rules."""
    normalized_chat_type = str(chat_type or "").strip().lower()
    if normalized_chat_type == "private":
        return None

    if message_thread_id is None:
        return None

    try:
        thread_id = int(message_thread_id)
    except (TypeError, ValueError):
        return None

    # Telegram forum "General" topic is id=1, should not be explicitly sent.
    if thread_id <= 1:
        return None

    return thread_id


async def send_message_resilient(
    bot: Any,
    *,
    chat_id: int,
    text: str,
    parse_mode: Optional[str] = None,
    reply_markup: Any = None,
    reply_to_message_id: Optional[int] = None,
    message_thread_id: Optional[int] = None,
    chat_type: Optional[str] = None,
) -> Any:
    """Send message with parse fallback, threadless retry and long-text split."""
    private_chat = is_private_chat_type(chat_type)
    normalized_thread_id = normalize_message_thread_id(
        message_thread_id, chat_type=chat_type
    )

    send_kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        send_kwargs["parse_mode"] = parse_mode
    if reply_markup is not None:
        send_kwargs["reply_markup"] = reply_markup
    if (
        not private_chat
        and isinstance(reply_to_message_id, int)
        and reply_to_message_id > 0
    ):
        send_kwargs["reply_to_message_id"] = reply_to_message_id
    if normalized_thread_id is not None:
        send_kwargs["message_thread_id"] = normalized_thread_id

    active_kwargs = dict(send_kwargs)

    try:
        return await bot.send_message(**active_kwargs)
    except Exception as send_error:
        final_error: Exception = send_error

    if "parse_mode" in active_kwargs and is_markdown_parse_error(final_error):
        no_md_kwargs = dict(active_kwargs)
        no_md_kwargs.pop("parse_mode", None)
        try:
            return await bot.send_message(**no_md_kwargs)
        except Exception as no_md_error:
            final_error = no_md_error
            active_kwargs = no_md_kwargs

    if "message_thread_id" in active_kwargs and is_thread_not_found_error(final_error):
        no_thread_kwargs = dict(active_kwargs)
        no_thread_kwargs.pop("message_thread_id", None)
        try:
            return await bot.send_message(**no_thread_kwargs)
        except Exception as no_thread_error:
            final_error = no_thread_error
            active_kwargs = no_thread_kwargs

        if "parse_mode" in active_kwargs and is_markdown_parse_error(final_error):
            no_thread_no_md_kwargs = dict(active_kwargs)
            no_thread_no_md_kwargs.pop("parse_mode", None)
            try:
                return await bot.send_message(**no_thread_no_md_kwargs)
            except Exception as no_thread_no_md_error:
                final_error = no_thread_no_md_error
                active_kwargs = no_thread_no_md_kwargs

    if is_message_too_long_error(final_error) or len(text) > _TELEGRAM_MESSAGE_LIMIT:
        chunks = split_text_for_telegram(text)
        chunk_base_kwargs = dict(active_kwargs)
        chunk_base_kwargs.pop("parse_mode", None)

        last_message = None
        for idx, chunk in enumerate(chunks):
            chunk_kwargs = dict(chunk_base_kwargs)
            chunk_kwargs["text"] = chunk
            if idx > 0:
                chunk_kwargs.pop("reply_markup", None)
            last_message = await bot.send_message(**chunk_kwargs)
        return last_message

    raise final_error


async def send_message_draft_resilient(
    bot: Any,
    *,
    chat_id: int,
    draft_id: int,
    text: str,
    parse_mode: Optional[str] = None,
    entities: Any = None,
    message_thread_id: Optional[int] = None,
    chat_type: Optional[str] = None,
) -> bool:
    """Send stream draft update via raw Bot API with parse fallback."""
    private_chat = is_private_chat_type(chat_type)
    if not private_chat:
        return False

    post_method = getattr(bot, "_post", None)
    if not callable(post_method):
        return False

    normalized_text = str(text or "").strip()
    if not normalized_text:
        return False

    try:
        normalized_draft_id = abs(int(draft_id))
    except (TypeError, ValueError):
        return False
    if normalized_draft_id == 0:
        return False

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "draft_id": normalized_draft_id,
        "text": trim_draft_text_for_telegram(normalized_text),
    }

    normalized_thread_id = normalize_message_thread_id(
        message_thread_id, chat_type=chat_type
    )
    if normalized_thread_id is not None:
        payload["message_thread_id"] = normalized_thread_id
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if entities is not None:
        payload["entities"] = entities

    try:
        await post_method("sendMessageDraft", payload)
        return True
    except Exception as send_error:
        if not parse_mode or not is_markdown_parse_error(send_error):
            raise

    payload_no_md = dict(payload)
    payload_no_md.pop("parse_mode", None)
    payload_no_md.pop("entities", None)
    await post_method("sendMessageDraft", payload_no_md)
    return True
