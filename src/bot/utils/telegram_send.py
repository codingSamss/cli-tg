"""Telegram send helpers with parse/thread/length fallbacks."""

from __future__ import annotations

import html
import os
import re
from typing import Any, Optional

_TELEGRAM_MESSAGE_LIMIT = 4096
_TELEGRAM_SAFE_SPLIT_LIMIT = 3800
_TELEGRAM_DRAFT_MESSAGE_LIMIT = 4096
_TELEGRAM_PARSE_MODE_ENV = "TELEGRAM_PARSE_MODE"
_FENCED_CODE_BLOCK_PATTERN = re.compile(
    r"```(?:[a-zA-Z0-9_.+#-]+)?\n([\s\S]*?)```", re.MULTILINE
)
_INLINE_CODE_PATTERN = re.compile(r"`([^`\n]+)`")
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_MARKDOWN_HEADING_PATTERN = re.compile(r"(?m)^\s*#{1,6}\s+(.+)$")
_MARKDOWN_BOLD_DOUBLE_PATTERN = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_MARKDOWN_BOLD_SINGLE_PATTERN = re.compile(r"\*(?=\S)(.+?)(?<=\S)\*")
_MARKDOWN_ITALIC_PATTERN = re.compile(r"_(?=\S)(.+?)(?<=\S)_")
_MARKDOWN_STRIKE_PATTERN = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_MARKDOWN_ESCAPED_CONTROL_PATTERN = re.compile(r"\\([_*[\]()~`>#+=|{}.!-])")


def is_private_chat_type(chat_type: Optional[str]) -> bool:
    """Whether chat type is private dialog."""
    return str(chat_type or "").strip().lower() == "private"


def _normalize_parse_mode(parse_mode: Optional[str]) -> Optional[str]:
    """Normalize parse mode alias casing."""
    if parse_mode is None:
        return None
    normalized = str(parse_mode).strip()
    if not normalized:
        return None
    lowered = normalized.lower()
    if lowered == "markdown":
        return "Markdown"
    if lowered == "markdownv2":
        return "MarkdownV2"
    if lowered == "html":
        return "HTML"
    return normalized


def get_telegram_parse_mode_preference() -> str:
    """Get preferred Telegram parse mode from environment."""
    env_mode = _normalize_parse_mode(os.getenv(_TELEGRAM_PARSE_MODE_ENV))
    return "HTML" if env_mode == "HTML" else "Markdown"


def _render_markdown_to_telegram_html(text: str) -> str:
    """Best-effort convert Telegram legacy Markdown subset to Telegram HTML."""
    normalized = str(text or "")
    placeholders: dict[str, str] = {}

    def _store(fragment: str) -> str:
        key = f"@@TGHTML{len(placeholders)}@@"
        placeholders[key] = fragment
        return key

    def _replace_fenced_code(match: re.Match[str]) -> str:
        code = match.group(1) or ""
        return _store(f"<pre><code>{html.escape(code, quote=False)}</code></pre>")

    def _replace_inline_code(match: re.Match[str]) -> str:
        code = match.group(1) or ""
        return _store(f"<code>{html.escape(code, quote=False)}</code>")

    def _replace_link(match: re.Match[str]) -> str:
        label = html.escape(match.group(1) or "", quote=False)
        url = html.escape(match.group(2) or "", quote=True)
        return _store(f'<a href="{url}">{label}</a>')

    rendered = _FENCED_CODE_BLOCK_PATTERN.sub(_replace_fenced_code, normalized)
    rendered = _INLINE_CODE_PATTERN.sub(_replace_inline_code, rendered)
    rendered = _MARKDOWN_LINK_PATTERN.sub(_replace_link, rendered)
    rendered = html.escape(rendered, quote=False)

    # Remove legacy Markdown escape slashes that are no longer needed in HTML mode.
    rendered = _MARKDOWN_ESCAPED_CONTROL_PATTERN.sub(r"\1", rendered)

    rendered = _MARKDOWN_HEADING_PATTERN.sub(
        lambda m: f"<b>{m.group(1).strip()}</b>", rendered
    )
    rendered = _MARKDOWN_BOLD_DOUBLE_PATTERN.sub(r"<b>\1</b>", rendered)
    rendered = _MARKDOWN_BOLD_SINGLE_PATTERN.sub(r"<b>\1</b>", rendered)
    rendered = _MARKDOWN_ITALIC_PATTERN.sub(r"<i>\1</i>", rendered)
    rendered = _MARKDOWN_STRIKE_PATTERN.sub(r"<s>\1</s>", rendered)

    for key, fragment in placeholders.items():
        rendered = rendered.replace(key, fragment)

    return rendered


def prepare_telegram_text_and_parse_mode(
    text: str,
    parse_mode: Optional[str],
) -> tuple[str, Optional[str], bool]:
    """Resolve preferred parse mode and transformed text.

    Returns:
        (prepared_text, prepared_parse_mode, html_upgraded_from_markdown)
    """
    normalized_text = str(text or "")
    normalized_mode = _normalize_parse_mode(parse_mode)
    if normalized_mode != "Markdown":
        return normalized_text, normalized_mode, False
    if get_telegram_parse_mode_preference() != "HTML":
        return normalized_text, normalized_mode, False
    return _render_markdown_to_telegram_html(normalized_text), "HTML", True


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
    original_text = str(text or "")
    original_parse_mode = _normalize_parse_mode(parse_mode)
    prepared_text, prepared_parse_mode, html_upgraded_from_markdown = (
        prepare_telegram_text_and_parse_mode(original_text, original_parse_mode)
    )

    private_chat = is_private_chat_type(chat_type)
    normalized_thread_id = normalize_message_thread_id(
        message_thread_id, chat_type=chat_type
    )

    send_kwargs: dict[str, Any] = {"chat_id": chat_id, "text": prepared_text}
    if prepared_parse_mode:
        send_kwargs["parse_mode"] = prepared_parse_mode
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
        if html_upgraded_from_markdown and original_parse_mode == "Markdown":
            markdown_kwargs = dict(active_kwargs)
            markdown_kwargs["text"] = original_text
            markdown_kwargs["parse_mode"] = "Markdown"
            try:
                return await bot.send_message(**markdown_kwargs)
            except Exception as markdown_error:
                final_error = markdown_error
                active_kwargs = markdown_kwargs

        no_md_kwargs = dict(active_kwargs)
        no_md_kwargs.pop("parse_mode", None)
        try:
            no_md_kwargs["text"] = original_text
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
            if html_upgraded_from_markdown and original_parse_mode == "Markdown":
                no_thread_md_kwargs = dict(active_kwargs)
                no_thread_md_kwargs["text"] = original_text
                no_thread_md_kwargs["parse_mode"] = "Markdown"
                try:
                    return await bot.send_message(**no_thread_md_kwargs)
                except Exception as no_thread_md_error:
                    final_error = no_thread_md_error
                    active_kwargs = no_thread_md_kwargs

            no_thread_no_md_kwargs = dict(active_kwargs)
            no_thread_no_md_kwargs.pop("parse_mode", None)
            try:
                no_thread_no_md_kwargs["text"] = original_text
                return await bot.send_message(**no_thread_no_md_kwargs)
            except Exception as no_thread_no_md_error:
                final_error = no_thread_no_md_error
                active_kwargs = no_thread_no_md_kwargs

    if (
        is_message_too_long_error(final_error)
        or len(original_text) > _TELEGRAM_MESSAGE_LIMIT
    ):
        chunks = split_text_for_telegram(original_text)
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

    original_text = str(text or "").strip()
    if not original_text:
        return False

    try:
        normalized_draft_id = abs(int(draft_id))
    except (TypeError, ValueError):
        return False
    if normalized_draft_id == 0:
        return False

    original_parse_mode = _normalize_parse_mode(parse_mode)
    prepared_text, prepared_parse_mode, html_upgraded_from_markdown = (
        prepare_telegram_text_and_parse_mode(original_text, original_parse_mode)
    )

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "draft_id": normalized_draft_id,
        "text": trim_draft_text_for_telegram(prepared_text),
    }

    normalized_thread_id = normalize_message_thread_id(
        message_thread_id, chat_type=chat_type
    )
    if normalized_thread_id is not None:
        payload["message_thread_id"] = normalized_thread_id
    if prepared_parse_mode:
        payload["parse_mode"] = prepared_parse_mode
    if entities is not None:
        payload["entities"] = entities

    try:
        await post_method("sendMessageDraft", payload)
        return True
    except Exception as send_error:
        if not prepared_parse_mode or not is_markdown_parse_error(send_error):
            raise

    if html_upgraded_from_markdown and original_parse_mode == "Markdown":
        payload_markdown = dict(payload)
        payload_markdown["text"] = trim_draft_text_for_telegram(original_text)
        payload_markdown["parse_mode"] = "Markdown"
        payload_markdown.pop("entities", None)
        try:
            await post_method("sendMessageDraft", payload_markdown)
            return True
        except Exception as markdown_error:
            if not is_markdown_parse_error(markdown_error):
                raise

    payload_no_md = dict(payload)
    payload_no_md.pop("parse_mode", None)
    payload_no_md.pop("entities", None)
    payload_no_md["text"] = trim_draft_text_for_telegram(original_text)
    await post_method("sendMessageDraft", payload_no_md)
    return True
