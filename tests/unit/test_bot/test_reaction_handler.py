"""Tests for Telegram message reaction handling."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.handlers.message import (
    _compose_prompt_with_reaction_feedback,
    _MessageStatusReactionController,
    _resolve_status_reaction_tool_emoji,
    _set_message_reaction_safe,
    handle_message_reaction,
    handle_reaction_update_fallback,
)


def _build_reaction_update(
    *,
    old_reaction,
    new_reaction,
    user_id: int = 1001,
    chat_id: int = -1009988,
    update_id: int = 10001,
):
    """Build a lightweight reaction update stub."""
    return SimpleNamespace(
        update_id=update_id,
        message_reaction=SimpleNamespace(
            old_reaction=old_reaction,
            new_reaction=new_reaction,
            user=SimpleNamespace(
                id=user_id,
                username="alice",
                first_name="Alice",
                last_name=None,
            ),
            actor_chat=None,
            chat=SimpleNamespace(id=chat_id, type="supergroup"),
            message_id=88,
        ),
    )


def _build_reaction_count_update(
    *,
    reactions,
    chat_id: int = -1009988,
    chat_type: str = "supergroup",
    message_id: int = 188,
    update_id: int = 20001,
):
    """Build a lightweight reaction-count update stub."""
    return SimpleNamespace(
        update_id=update_id,
        message_reaction=None,
        message_reaction_count=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type=chat_type),
            message_id=message_id,
            reactions=reactions,
        ),
    )


@pytest.mark.asyncio
async def test_reaction_handler_logs_audit_for_added_reaction():
    """Reaction handler should emit session audit event for valid updates."""
    update = _build_reaction_update(
        old_reaction=[SimpleNamespace(type="emoji", emoji="👍")],
        new_reaction=[
            SimpleNamespace(type="emoji", emoji="👍"),
            SimpleNamespace(type="emoji", emoji="🔥"),
        ],
    )
    audit_logger = SimpleNamespace(log_session_event=AsyncMock())
    auth_manager = SimpleNamespace(is_authenticated=lambda _uid: True)
    context = SimpleNamespace(
        bot_data={
            "audit_logger": audit_logger,
            "auth_manager": auth_manager,
            "settings": SimpleNamespace(approved_directory=Path("/tmp")),
        },
        application=SimpleNamespace(user_data={}),
    )

    await handle_message_reaction(update, context)

    audit_logger.log_session_event.assert_awaited_once()
    kwargs = audit_logger.log_session_event.await_args.kwargs
    assert kwargs["user_id"] == 1001
    assert kwargs["action"] == "telegram_reaction"
    assert kwargs["details"]["chat_id"] == -1009988
    assert kwargs["details"]["message_id"] == 88
    assert kwargs["details"]["added_reactions"] == ["emoji:🔥"]
    assert kwargs["details"]["removed_reactions"] == []


@pytest.mark.asyncio
async def test_reaction_handler_skips_unauthenticated_actor():
    """Reaction updates from unauthenticated actors should be ignored."""
    update = _build_reaction_update(
        old_reaction=[],
        new_reaction=[SimpleNamespace(type="emoji", emoji="👀")],
    )
    audit_logger = SimpleNamespace(log_session_event=AsyncMock())
    auth_manager = SimpleNamespace(is_authenticated=lambda _uid: False)
    context = SimpleNamespace(
        bot_data={
            "audit_logger": audit_logger,
            "auth_manager": auth_manager,
            "settings": SimpleNamespace(approved_directory=Path("/tmp")),
        },
        application=SimpleNamespace(user_data={}),
    )

    await handle_message_reaction(update, context)

    audit_logger.log_session_event.assert_not_called()


@pytest.mark.asyncio
async def test_reaction_handler_ignores_noop_reaction_delta():
    """No-op reaction updates (old==new) should not write audit events."""
    current = [SimpleNamespace(type="emoji", emoji="✅")]
    update = _build_reaction_update(old_reaction=current, new_reaction=current)
    audit_logger = SimpleNamespace(log_session_event=AsyncMock())
    auth_manager = SimpleNamespace(is_authenticated=lambda _uid: True)
    context = SimpleNamespace(
        bot_data={
            "audit_logger": audit_logger,
            "auth_manager": auth_manager,
            "settings": SimpleNamespace(approved_directory=Path("/tmp")),
        },
        application=SimpleNamespace(user_data={}),
    )

    await handle_message_reaction(update, context)

    audit_logger.log_session_event.assert_not_called()


@pytest.mark.asyncio
async def test_reaction_handler_stores_pending_feedback_for_scope(tmp_path: Path):
    """Negative reaction should be stored as pending scope feedback."""
    update = _build_reaction_update(
        old_reaction=[],
        new_reaction=[SimpleNamespace(type="emoji", emoji="👎")],
        user_id=42001,
        chat_id=-10042,
    )
    audit_logger = SimpleNamespace(log_session_event=AsyncMock())
    auth_manager = SimpleNamespace(is_authenticated=lambda _uid: True)
    app_user_data: dict = {}
    context = SimpleNamespace(
        bot_data={
            "audit_logger": audit_logger,
            "auth_manager": auth_manager,
            "settings": SimpleNamespace(approved_directory=tmp_path),
        },
        application=SimpleNamespace(user_data=app_user_data),
    )

    await handle_message_reaction(update, context)

    scope_state = app_user_data[42001]["scope_state"]["42001:-10042:0"]
    feedback = scope_state["pending_reaction_feedback"]
    assert feedback["signal"] == "negative"
    assert feedback["emoji"] == "👎"
    assert feedback["chat_id"] == -10042
    assert feedback["thread_id"] == 0


def test_compose_prompt_with_reaction_feedback_injects_hint():
    """Prompt composer should prepend reaction guidance."""
    prompt = "请帮我继续修复这个 bug"
    enriched = _compose_prompt_with_reaction_feedback(
        prompt,
        {"signal": "negative", "emoji": "👎"},
    )
    assert enriched != prompt
    assert "不满意" in enriched
    assert prompt in enriched


@pytest.mark.asyncio
async def test_set_message_reaction_safe_sets_emoji():
    """Reaction helper should call Telegram API with normalized emoji payload."""
    bot = SimpleNamespace(set_message_reaction=AsyncMock(return_value=True))

    ok = await _set_message_reaction_safe(
        bot,
        chat_id=-1001,
        message_id=77,
        emoji="👍",
    )

    assert ok is True
    bot.set_message_reaction.assert_awaited_once()
    kwargs = bot.set_message_reaction.await_args.kwargs
    assert kwargs["chat_id"] == -1001
    assert kwargs["message_id"] == 77
    assert kwargs["reaction"] == ["👍"]


@pytest.mark.asyncio
async def test_set_message_reaction_safe_clears_reaction_when_emoji_missing():
    """Passing None emoji should clear bot reaction with empty payload."""
    bot = SimpleNamespace(set_message_reaction=AsyncMock(return_value=True))

    ok = await _set_message_reaction_safe(
        bot,
        chat_id=-1001,
        message_id=78,
        emoji=None,
    )

    assert ok is True
    kwargs = bot.set_message_reaction.await_args.kwargs
    assert kwargs["reaction"] == []


@pytest.mark.asyncio
async def test_set_message_reaction_safe_returns_false_on_api_error():
    """Any Telegram API error should be swallowed and reported as False."""
    bot = SimpleNamespace(
        set_message_reaction=AsyncMock(side_effect=RuntimeError("bad"))
    )

    ok = await _set_message_reaction_safe(
        bot,
        chat_id=-1001,
        message_id=79,
        emoji="👎",
    )

    assert ok is False


def test_resolve_status_reaction_tool_emoji_with_known_categories():
    """Tool reaction should map web/coding/unknown names to different emojis."""
    assert _resolve_status_reaction_tool_emoji("WebFetch") == "⚡"
    assert _resolve_status_reaction_tool_emoji("Bash") == "👨‍💻"
    assert _resolve_status_reaction_tool_emoji("SomeCustomTool") == "🔥"


@pytest.mark.asyncio
async def test_status_reaction_controller_progression():
    """Controller should advance queued -> tool -> done with debounced middle stage."""
    bot = SimpleNamespace(set_message_reaction=AsyncMock(return_value=True))
    controller = _MessageStatusReactionController(
        enabled=True,
        bot=bot,
        chat_id=-1001,
        message_id=101,
        debounce_ms=5,
        stall_soft_ms=1000,
        stall_hard_ms=2000,
    )

    await controller.set_queued()
    await controller.set_thinking()
    await controller.set_tool("WebFetch")
    await asyncio.sleep(0.03)
    await controller.set_done()
    await controller.shutdown()

    reactions = [
        call.kwargs["reaction"] for call in bot.set_message_reaction.await_args_list
    ]
    assert reactions[0] == ["👀"]
    assert ["⚡"] in reactions
    assert reactions[-1] == ["👍"]


@pytest.mark.asyncio
async def test_status_reaction_controller_emits_stall_reactions():
    """Controller should emit soft/hard stall emojis after inactivity."""
    bot = SimpleNamespace(set_message_reaction=AsyncMock(return_value=True))
    controller = _MessageStatusReactionController(
        enabled=True,
        bot=bot,
        chat_id=-1001,
        message_id=102,
        debounce_ms=0,
        stall_soft_ms=20,
        stall_hard_ms=40,
    )

    await controller.set_queued()
    await controller.set_thinking()
    await asyncio.sleep(0.08)
    await controller.set_done()
    await controller.shutdown()

    reactions = [
        call.kwargs["reaction"] for call in bot.set_message_reaction.await_args_list
    ]
    assert ["🥱"] in reactions
    assert ["😨"] in reactions


@pytest.mark.asyncio
async def test_reaction_count_update_stores_feedback_for_single_allowed_user(
    tmp_path: Path,
):
    """Anonymous count update should fallback to single allowed user id."""
    update = _build_reaction_count_update(
        reactions=[
            SimpleNamespace(
                type=SimpleNamespace(type="emoji", emoji="👎"), total_count=1
            )
        ],
        chat_id=-10042,
        chat_type="supergroup",
        message_id=2048,
    )
    audit_logger = SimpleNamespace(log_session_event=AsyncMock())
    auth_manager = SimpleNamespace(is_authenticated=lambda _uid: True)
    app_user_data: dict = {}
    context = SimpleNamespace(
        bot_data={
            "audit_logger": audit_logger,
            "auth_manager": auth_manager,
            "settings": SimpleNamespace(
                approved_directory=tmp_path,
                allowed_users=[42001],
            ),
        },
        application=SimpleNamespace(user_data=app_user_data),
    )

    await handle_message_reaction(update, context)

    scope_state = app_user_data[42001]["scope_state"]["42001:-10042:0"]
    feedback = scope_state["pending_reaction_feedback"]
    assert feedback["signal"] == "negative"
    assert feedback["emoji"] == "👎"
    assert feedback["chat_id"] == -10042
    assert feedback["thread_id"] == 0


@pytest.mark.asyncio
async def test_reaction_count_update_noop_when_counts_unchanged(tmp_path: Path):
    """Repeated same count payload should not duplicate audit or feedback writes."""
    update = _build_reaction_count_update(
        reactions=[
            SimpleNamespace(
                type=SimpleNamespace(type="emoji", emoji="👍"), total_count=1
            )
        ],
        chat_id=-10042,
        chat_type="supergroup",
        message_id=9001,
    )
    audit_logger = SimpleNamespace(log_session_event=AsyncMock())
    auth_manager = SimpleNamespace(is_authenticated=lambda _uid: True)
    app_user_data: dict = {}
    context = SimpleNamespace(
        bot_data={
            "audit_logger": audit_logger,
            "auth_manager": auth_manager,
            "settings": SimpleNamespace(
                approved_directory=tmp_path,
                allowed_users=[42001],
            ),
        },
        application=SimpleNamespace(user_data=app_user_data),
    )

    await handle_message_reaction(update, context)
    await handle_message_reaction(update, context)

    # First update stores + logs. Second same-count update should be no-op.
    assert audit_logger.log_session_event.await_count == 1


@pytest.mark.asyncio
async def test_reaction_fallback_ignores_non_reaction_updates(tmp_path: Path):
    """Fallback should skip generic updates without reaction payloads."""
    update = SimpleNamespace(
        update_id=987654, message_reaction=None, message_reaction_count=None
    )
    audit_logger = SimpleNamespace(log_session_event=AsyncMock())
    context = SimpleNamespace(
        bot_data={
            "audit_logger": audit_logger,
            "auth_manager": SimpleNamespace(is_authenticated=lambda _uid: True),
            "settings": SimpleNamespace(
                approved_directory=tmp_path, allowed_users=[42001]
            ),
        },
        application=SimpleNamespace(user_data={}),
    )

    await handle_reaction_update_fallback(update, context)

    audit_logger.log_session_event.assert_not_called()


@pytest.mark.asyncio
async def test_reaction_update_deduplicated_between_handlers(tmp_path: Path):
    """Generic fallback + specialized handler should process same update only once."""
    update = _build_reaction_update(
        old_reaction=[],
        new_reaction=[SimpleNamespace(type="emoji", emoji="🔥")],
        user_id=42001,
        chat_id=-10042,
        update_id=335577,
    )
    audit_logger = SimpleNamespace(log_session_event=AsyncMock())
    context = SimpleNamespace(
        bot_data={
            "audit_logger": audit_logger,
            "auth_manager": SimpleNamespace(is_authenticated=lambda _uid: True),
            "settings": SimpleNamespace(
                approved_directory=tmp_path, allowed_users=[42001]
            ),
        },
        application=SimpleNamespace(user_data={}),
    )

    await handle_reaction_update_fallback(update, context)
    await handle_message_reaction(update, context)

    assert audit_logger.log_session_event.await_count == 1
