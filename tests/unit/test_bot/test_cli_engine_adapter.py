"""Tests for CLI engine adapter flow."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers.callback import (
    _do_adopt_session,
    _resume_select_project,
    handle_engine_callback,
    handle_model_callback,
    handle_quick_action_callback,
    handle_resume_callback,
)
from src.bot.handlers.command import (
    codex_diag_command,
    help_command,
    model_command,
    resume_command,
    start_command,
    switch_engine,
)
from src.bot.resume_tokens import ResumeTokenManager
from src.bot.utils.cli_engine import ENGINE_CODEX, ENGINE_STATE_KEY, get_cli_integration


def _scope_key(user_id: int, chat_id: int) -> str:
    """Build scoped key used by scope_state helpers."""
    return f"{user_id}:{chat_id}:0"


def _build_settings(approved_directory: Path) -> SimpleNamespace:
    """Build minimal settings stub."""
    return SimpleNamespace(
        approved_directory=approved_directory,
        use_sdk=False,
        resume_scan_cache_ttl_seconds=30,
    )


@pytest.mark.asyncio
async def test_switch_engine_updates_scope_state_and_clears_old_session(tmp_path):
    """Switching engine should reset session binding and force new session."""
    approved = tmp_path / "approved"
    approved.mkdir()
    project = approved / "proj-switch-1"
    project.mkdir()
    user_id = 1001
    scope_key = _scope_key(user_id, user_id)
    permission_manager = SimpleNamespace(clear_session=MagicMock())
    codex_scanner = SimpleNamespace(list_projects=AsyncMock(return_value=[project]))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        args=["codex"],
        bot_data={
            "settings": _build_settings(approved),
            "cli_integrations": {"claude": object(), "codex": object()},
            "permission_manager": permission_manager,
            "codex_desktop_scanner": codex_scanner,
            "resume_token_manager": ResumeTokenManager(),
        },
        user_data={
            "scope_state": {
                scope_key: {
                    "claude_session_id": "session-old-1",
                    ENGINE_STATE_KEY: "claude",
                }
            }
        },
    )

    await switch_engine(update, context)

    scope_state = context.user_data["scope_state"][scope_key]
    assert scope_state[ENGINE_STATE_KEY] == ENGINE_CODEX
    assert scope_state["claude_session_id"] is None
    assert scope_state["force_new_session"] is True
    permission_manager.clear_session.assert_called_once_with("session-old-1")
    rendered = update.message.reply_text.await_args.args[0]
    assert "CLI 引擎已切换" in rendered
    assert "`codex`" in rendered


@pytest.mark.asyncio
async def test_switch_engine_to_claude_clears_non_claude_model(tmp_path):
    """Switching back to claude should drop stale codex model override."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 1012
    scope_key = _scope_key(user_id, user_id)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        args=["claude"],
        bot_data={
            "settings": _build_settings(approved),
            "cli_integrations": {"claude": object(), "codex": object()},
            "desktop_scanner": SimpleNamespace(
                list_projects=AsyncMock(return_value=[])
            ),
        },
        user_data={
            "scope_state": {
                scope_key: {
                    ENGINE_STATE_KEY: "codex",
                    "claude_model": "gpt-5.3-codex",
                }
            }
        },
    )

    await switch_engine(update, context)

    scope_state = context.user_data["scope_state"][scope_key]
    assert scope_state[ENGINE_STATE_KEY] == "claude"
    assert "claude_model" not in scope_state


@pytest.mark.asyncio
async def test_switch_engine_rejects_unavailable_target(tmp_path):
    """Switching to unavailable engine should keep current state unchanged."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 1002
    scope_key = _scope_key(user_id, user_id)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        args=["codex"],
        bot_data={
            "settings": _build_settings(approved),
            "cli_integrations": {"claude": object()},
        },
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "claude"}}},
    )

    await switch_engine(update, context)

    scope_state = context.user_data["scope_state"][scope_key]
    assert scope_state[ENGINE_STATE_KEY] == "claude"
    rendered = update.message.reply_text.await_args.args[0]
    assert "当前不可用" in rendered


@pytest.mark.asyncio
async def test_switch_engine_syncs_chat_menu_by_target_engine(tmp_path):
    """Switching engine should refresh per-chat command menu visibility."""
    approved = tmp_path / "approved"
    approved.mkdir()
    project = approved / "proj-switch-2"
    project.mkdir()
    user_id = 1003
    scope_key = _scope_key(user_id, user_id)
    set_my_commands = AsyncMock()
    codex_scanner = SimpleNamespace(list_projects=AsyncMock(return_value=[project]))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(set_my_commands=set_my_commands),
        args=["codex"],
        bot_data={
            "settings": _build_settings(approved),
            "cli_integrations": {"claude": object(), "codex": object()},
            "codex_desktop_scanner": codex_scanner,
            "resume_token_manager": ResumeTokenManager(),
        },
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "claude"}}},
    )

    await switch_engine(update, context)

    call_kwargs = set_my_commands.await_args.kwargs
    command_names = [cmd.command for cmd in call_kwargs["commands"]]
    assert "codexdiag" in command_names
    assert "model" in command_names
    assert "status" in command_names
    assert "context" not in command_names


@pytest.mark.asyncio
async def test_switch_engine_with_args_enters_resume_project_selector(tmp_path):
    """Manual `/engine codex` should continue into project/session selector flow."""
    approved = tmp_path / "approved"
    approved.mkdir()
    project = approved / "proj-switch-3"
    project.mkdir()
    user_id = 1099
    scope_key = _scope_key(user_id, user_id)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(set_my_commands=AsyncMock()),
        args=["codex"],
        bot_data={
            "settings": _build_settings(approved),
            "cli_integrations": {"claude": object(), "codex": object()},
            "codex_desktop_scanner": SimpleNamespace(
                list_projects=AsyncMock(return_value=[project])
            ),
            "resume_token_manager": ResumeTokenManager(),
        },
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "claude"}}},
    )

    await switch_engine(update, context)

    rendered = update.message.reply_text.await_args.args[0]
    assert "CLI 引擎已切换" in rendered
    assert "Resume Desktop Session" in rendered
    reply_markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    callback_ids = [
        btn.callback_data for row in reply_markup.inline_keyboard for btn in row
    ]
    assert any(callback.startswith("resume:p:") for callback in callback_ids)


@pytest.mark.asyncio
async def test_switch_engine_without_args_shows_selector_keyboard(tmp_path):
    """`/engine` without args should show clickable selector keyboard."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 1009
    scope_key = _scope_key(user_id, user_id)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(set_my_commands=AsyncMock()),
        args=[],
        bot_data={
            "settings": _build_settings(approved),
            "cli_integrations": {"claude": object(), "codex": object()},
        },
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "claude"}}},
    )

    await switch_engine(update, context)

    kwargs = update.message.reply_text.await_args.kwargs
    keyboard = kwargs["reply_markup"].inline_keyboard
    callback_ids = [btn.callback_data for row in keyboard for btn in row]
    assert "engine:switch:claude" in callback_ids
    assert "engine:switch:codex" in callback_ids
    rendered = update.message.reply_text.await_args.args[0]
    assert "点击下方按钮即可切换" in rendered


@pytest.mark.asyncio
async def test_help_command_uses_context_profile_for_claude(tmp_path):
    """Help should expose Claude-facing commands when engine is claude."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 1007
    scope_key = _scope_key(user_id, user_id)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        bot_data={"settings": _build_settings(approved)},
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "claude"}}},
    )

    await help_command(update, context)

    rendered = update.message.reply_text.await_args.args[0]
    assert "• `/context [full]` - Show session context and usage" in rendered
    assert "• `/status [full]` - Show session status and usage" not in rendered
    assert "• `/model` - View or switch Claude model" in rendered
    assert "**Diagnostics:**" not in rendered


@pytest.mark.asyncio
async def test_start_command_uses_status_profile_for_codex(tmp_path):
    """Start should expose Codex-facing status command and button label."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 1008
    scope_key = _scope_key(user_id, user_id)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, first_name="Tester"),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(set_my_commands=AsyncMock()),
        bot_data={"settings": _build_settings(approved)},
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "codex"}}},
    )

    await start_command(update, context)

    rendered = update.message.reply_text.await_args.args[0]
    assert "• `/status [full]` - Show session status and usage" in rendered
    assert "• `/context [full]` - Show session context and usage" not in rendered
    assert "📊 Use `/status` to check your usage limits." in rendered
    reply_markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    button_labels = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "📊 Check Status" in button_labels


@pytest.mark.asyncio
async def test_model_command_shows_switch_hint_for_codex_engine(tmp_path):
    """Codex /model without args should show current model and switch hint."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 1004
    scope_key = _scope_key(user_id, user_id)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        args=[],
        bot_data={"settings": _build_settings(approved)},
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "codex"}}},
    )

    await model_command(update, context)

    rendered = update.message.reply_text.await_args.args[0]
    assert "当前引擎：`codex`" in rendered
    assert "当前模型：`default`" in rendered
    assert "/model <model_name>" in rendered
    assert "/model default" in rendered
    keyboard = update.message.reply_text.await_args.kwargs[
        "reply_markup"
    ].inline_keyboard
    callback_ids = [btn.callback_data for row in keyboard for btn in row]
    assert "model:codex:gpt-5.3-codex" in callback_ids
    assert "model:codex:default" in callback_ids


@pytest.mark.asyncio
async def test_model_command_clears_stale_codex_model_for_claude_engine(tmp_path):
    """Claude /model should sanitize stale codex model values."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 10043
    scope_key = _scope_key(user_id, user_id)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        args=[],
        bot_data={"settings": _build_settings(approved)},
        user_data={
            "scope_state": {
                scope_key: {
                    ENGINE_STATE_KEY: "claude",
                    "claude_model": "gpt-5.3-codex",
                }
            }
        },
    )

    await model_command(update, context)

    scope_state = context.user_data["scope_state"][scope_key]
    assert "claude_model" not in scope_state
    rendered = update.message.reply_text.await_args.args[0]
    assert "Current model: `default`" in rendered


@pytest.mark.asyncio
async def test_model_command_codex_can_set_and_reset_model(tmp_path):
    """Codex /model should support setting explicit model and resetting default."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 10042
    scope_key = _scope_key(user_id, user_id)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        args=["gpt-5.3-codex"],
        bot_data={"settings": _build_settings(approved)},
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "codex"}}},
    )

    await model_command(update, context)

    scope_state = context.user_data["scope_state"][scope_key]
    assert scope_state["claude_model"] == "gpt-5.3-codex"
    rendered = update.message.reply_text.await_args.args[0]
    assert "已更新 Codex 模型设置" in rendered
    assert "当前设置：`gpt-5.3-codex`" in rendered

    context.args = ["default"]
    await model_command(update, context)
    assert "claude_model" not in scope_state


@pytest.mark.asyncio
async def test_model_command_codex_without_explicit_model_shows_default(tmp_path):
    """Codex /model should show default when user has not selected a model."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 10041
    scope_key = _scope_key(user_id, user_id)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        args=[],
        bot_data={"settings": _build_settings(approved)},
        user_data={
            "scope_state": {
                scope_key: {
                    ENGINE_STATE_KEY: "codex",
                    "claude_session_id": "session-codex-1",
                }
            }
        },
    )
    await model_command(update, context)

    rendered = update.message.reply_text.await_args.args[0]
    assert "当前模型：`default`" in rendered


@pytest.mark.asyncio
async def test_codex_diag_command_rejects_when_engine_not_codex(tmp_path):
    """Codex diagnostic command should be hidden behind codex engine."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 1005
    scope_key = _scope_key(user_id, user_id)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        args=[],
        bot_data={"settings": _build_settings(approved)},
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "claude"}}},
    )

    await codex_diag_command(update, context)

    rendered = update.message.reply_text.await_args.args[0]
    assert "不支持 `/codexdiag`" in rendered
    assert "/engine codex" in rendered


@pytest.mark.asyncio
async def test_model_callback_rejects_when_engine_does_not_support(tmp_path):
    """Codex engine should reject stale Claude model buttons."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 1006
    scope_key = _scope_key(user_id, user_id)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=user_id), message_thread_id=None
        ),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(
        bot_data={"settings": _build_settings(approved)},
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "codex"}}},
    )

    await handle_model_callback(query, "sonnet", context)

    rendered = query.edit_message_text.await_args.args[0]
    assert "当前引擎：`codex`" in rendered
    assert "请使用 Codex 模型按钮" in rendered


@pytest.mark.asyncio
async def test_model_callback_rejects_codex_param_when_engine_is_claude(tmp_path):
    """Claude model callback should reject codex-prefixed values."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 10062
    scope_key = _scope_key(user_id, user_id)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=user_id), message_thread_id=None
        ),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(
        bot_data={"settings": _build_settings(approved)},
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "claude"}}},
    )

    await handle_model_callback(query, "codex:gpt-5.3-codex", context)

    scope_state = context.user_data["scope_state"][scope_key]
    assert "claude_model" not in scope_state
    rendered = query.edit_message_text.await_args.args[0]
    assert "当前引擎：`claude`" in rendered
    assert "请使用 Claude 模型按钮" in rendered


@pytest.mark.asyncio
async def test_model_callback_sets_model_for_codex_engine(tmp_path):
    """Codex model callback should persist model and refresh keyboard."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 10061
    scope_key = _scope_key(user_id, user_id)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=user_id), message_thread_id=None
        ),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(
        bot_data={"settings": _build_settings(approved)},
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "codex"}}},
    )

    await handle_model_callback(query, "codex:gpt-5.3-codex", context)

    scope_state = context.user_data["scope_state"][scope_key]
    assert scope_state["claude_model"] == "gpt-5.3-codex"
    rendered = query.edit_message_text.await_args.args[0]
    assert "已更新 Codex 模型设置" in rendered
    assert "当前设置：`gpt-5.3-codex`" in rendered
    keyboard = query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
    callback_ids = [btn.callback_data for row in keyboard for btn in row]
    assert "model:codex:default" in callback_ids


@pytest.mark.asyncio
async def test_model_callback_for_claude_clears_session_and_forces_new(tmp_path):
    """Claude model callback should reset active session to apply new model."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 10063
    scope_key = _scope_key(user_id, user_id)
    permission_manager = SimpleNamespace(clear_session=MagicMock())
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=user_id), message_thread_id=None
        ),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(
        bot_data={
            "settings": _build_settings(approved),
            "permission_manager": permission_manager,
        },
        user_data={
            "scope_state": {
                scope_key: {
                    ENGINE_STATE_KEY: "claude",
                    "claude_session_id": "session-claude-old-1",
                }
            }
        },
    )

    await handle_model_callback(query, "opus", context)

    scope_state = context.user_data["scope_state"][scope_key]
    assert scope_state["claude_model"] == "opus"
    assert scope_state["claude_session_id"] is None
    assert scope_state["force_new_session"] is True
    permission_manager.clear_session.assert_called_once_with("session-claude-old-1")
    rendered = query.edit_message_text.await_args.args[0]
    assert "模型设置已更新" in rendered
    assert "下一条消息将从新会话开始" in rendered


@pytest.mark.asyncio
async def test_engine_callback_switches_engine_and_clears_session(tmp_path):
    """Engine callback should switch active engine and clear prior session."""
    approved = tmp_path / "approved"
    approved.mkdir()
    project = approved / "proj-a"
    project.mkdir()
    user_id = 1010
    chat_id = 1011
    scope_key = _scope_key(user_id, chat_id)
    permission_manager = SimpleNamespace(clear_session=MagicMock())
    codex_scanner = SimpleNamespace(list_projects=AsyncMock(return_value=[project]))
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), chat_id=chat_id),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(set_my_commands=AsyncMock()),
        bot_data={
            "settings": _build_settings(approved),
            "cli_integrations": {"claude": object(), "codex": object()},
            "permission_manager": permission_manager,
            "resume_token_manager": ResumeTokenManager(),
            "codex_desktop_scanner": codex_scanner,
        },
        user_data={
            "scope_state": {
                scope_key: {
                    ENGINE_STATE_KEY: "claude",
                    "claude_session_id": "session-old-2",
                }
            }
        },
    )

    await handle_engine_callback(query, "switch:codex", context)

    scope_state = context.user_data["scope_state"][scope_key]
    assert scope_state[ENGINE_STATE_KEY] == "codex"
    assert scope_state["claude_session_id"] is None
    assert scope_state["force_new_session"] is True
    permission_manager.clear_session.assert_called_once_with("session-old-2")
    rendered = query.edit_message_text.await_args.args[0]
    assert "CLI 引擎已切换" in rendered
    assert "Resume Desktop Session" in rendered
    reply_markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    callback_ids = [
        btn.callback_data for row in reply_markup.inline_keyboard for btn in row
    ]
    assert any(callback.startswith("resume:p:") for callback in callback_ids)


@pytest.mark.asyncio
async def test_resume_project_selection_includes_start_new_session_option(tmp_path):
    """Selecting a resume project should provide a direct 'start new session' option."""
    approved = tmp_path / "approved"
    approved.mkdir()
    project = approved / "proj-b"
    project.mkdir()
    user_id = 1014
    chat_id = 1015
    scope_key = _scope_key(user_id, chat_id)
    token_mgr = ResumeTokenManager()
    project_token = token_mgr.issue(
        kind="p",
        user_id=user_id,
        payload={"cwd": str(project), "engine": "codex"},
    )
    codex_scanner = SimpleNamespace(
        list_sessions=AsyncMock(
            return_value=[
                SimpleNamespace(
                    session_id="019c-test-session-1",
                    first_message="hello",
                    is_probably_active=False,
                )
            ]
        )
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), chat_id=chat_id),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(
        bot_data={
            "settings": _build_settings(approved),
            "resume_token_manager": token_mgr,
            "codex_desktop_scanner": codex_scanner,
        },
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "codex"}}},
    )

    await handle_resume_callback(query, f"p:{project_token}", context)

    rendered = query.edit_message_text.await_args.args[0]
    assert "Session previews" in rendered
    assert "Start New Session Here" in rendered
    reply_markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    callback_ids = [
        btn.callback_data for row in reply_markup.inline_keyboard for btn in row
    ]
    assert any(callback.startswith("resume:s:") for callback in callback_ids)
    assert any(callback.startswith("resume:n:") for callback in callback_ids)


@pytest.mark.asyncio
async def test_resume_select_project_prefers_payload_engine_for_session_tokens(
    tmp_path,
):
    """Session tokens should preserve engine from project token payload."""
    approved = tmp_path / "approved"
    approved.mkdir()
    project = approved / "proj-b2"
    project.mkdir()
    user_id = 10141
    chat_id = 10151
    token_mgr = ResumeTokenManager()
    project_token = token_mgr.issue(
        kind="p",
        user_id=user_id,
        payload={"cwd": str(project), "engine": "codex"},
    )
    scanner = SimpleNamespace(
        list_sessions=AsyncMock(
            return_value=[
                SimpleNamespace(
                    session_id="019c-test-session-2",
                    first_message="hello",
                    is_probably_active=False,
                )
            ]
        )
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), chat_id=chat_id),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(
        bot_data={"settings": _build_settings(approved)},
        user_data={},
    )

    await _resume_select_project(
        query=query,
        user_id=user_id,
        token=project_token,
        token_mgr=token_mgr,
        scanner=scanner,
        settings=_build_settings(approved),
        context=context,
        engine="claude",
    )

    reply_markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    callback_ids = [
        btn.callback_data for row in reply_markup.inline_keyboard for btn in row
    ]
    session_callbacks = [cid for cid in callback_ids if cid.startswith("resume:s:")]
    assert session_callbacks
    session_token = session_callbacks[0].split("resume:s:", 1)[1]
    session_payload = token_mgr.resolve(
        kind="s",
        user_id=user_id,
        token=session_token,
        consume=False,
    )
    assert session_payload["engine"] == "codex"


@pytest.mark.asyncio
async def test_resume_new_session_callback_sets_directory_and_resets_session(tmp_path):
    """`resume:n:*` should move to directory and prepare a fresh session immediately."""
    approved = tmp_path / "approved"
    approved.mkdir()
    project = approved / "proj-c"
    project.mkdir()
    user_id = 1016
    chat_id = 1017
    scope_key = _scope_key(user_id, chat_id)
    permission_manager = SimpleNamespace(clear_session=MagicMock())
    token_mgr = ResumeTokenManager()
    new_token = token_mgr.issue(
        kind="n",
        user_id=user_id,
        payload={"cwd": str(project), "engine": "codex"},
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), chat_id=chat_id),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(set_my_commands=AsyncMock()),
        bot_data={
            "settings": _build_settings(approved),
            "resume_token_manager": token_mgr,
            "permission_manager": permission_manager,
            "cli_integrations": {"claude": object(), "codex": object()},
        },
        user_data={
            "scope_state": {
                scope_key: {
                    ENGINE_STATE_KEY: "claude",
                    "claude_session_id": "session-old-3",
                }
            }
        },
    )

    await handle_resume_callback(query, f"n:{new_token}", context)

    scope_state = context.user_data["scope_state"][scope_key]
    assert scope_state[ENGINE_STATE_KEY] == "codex"
    assert scope_state["current_directory"] == project.resolve()
    assert scope_state["claude_session_id"] is None
    assert scope_state["force_new_session"] is True
    permission_manager.clear_session.assert_called_once_with("session-old-3")
    rendered = query.edit_message_text.await_args.args[0]
    assert "New Session Ready" in rendered


@pytest.mark.asyncio
async def test_engine_callback_rejects_unavailable_target(tmp_path):
    """Engine callback should reject target not available in integrations."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 1012
    chat_id = 1013
    scope_key = _scope_key(user_id, chat_id)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), chat_id=chat_id),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(set_my_commands=AsyncMock()),
        bot_data={
            "settings": _build_settings(approved),
            "cli_integrations": {"claude": object()},
        },
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "claude"}}},
    )

    await handle_engine_callback(query, "switch:codex", context)

    scope_state = context.user_data["scope_state"][scope_key]
    assert scope_state[ENGINE_STATE_KEY] == "claude"
    rendered = query.edit_message_text.await_args.args[0]
    assert "当前不可用" in rendered


def test_get_cli_integration_respects_active_engine_with_fallback():
    """Adapter resolver should pick active engine then fallback to claude."""
    claude_integration = object()
    codex_integration = object()
    engine, selected = get_cli_integration(
        bot_data={
            "cli_integrations": {
                "claude": claude_integration,
                "codex": codex_integration,
            }
        },
        scope_state={ENGINE_STATE_KEY: "codex"},
    )
    assert engine == "codex"
    assert selected is codex_integration

    fallback_engine, fallback_selected = get_cli_integration(
        bot_data={"cli_integrations": {"claude": claude_integration}},
        scope_state={ENGINE_STATE_KEY: "codex"},
    )
    assert fallback_engine == "codex"
    assert fallback_selected is claude_integration


@pytest.mark.asyncio
async def test_quick_action_callback_runs_with_active_engine_integration(tmp_path):
    """Quick action callback should route execution to active engine integration."""
    approved = tmp_path / "approved"
    approved.mkdir()

    user_id = 2001
    chat_id = 3001
    scope_key = _scope_key(user_id, chat_id)
    codex_integration = SimpleNamespace(
        run_command=AsyncMock(
            return_value=SimpleNamespace(
                content="执行完成",
                session_id="session-codex-1",
            )
        )
    )
    claude_integration = SimpleNamespace(run_command=AsyncMock())
    action = SimpleNamespace(icon="🧪", name="Run Tests", prompt="pytest")
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            chat_id=chat_id,
            reply_text=AsyncMock(),
        ),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        bot_data={
            "settings": _build_settings(approved),
            "quick_actions": SimpleNamespace(actions={"test": action}),
            "cli_integrations": {
                "claude": claude_integration,
                "codex": codex_integration,
            },
        },
        user_data={
            "scope_state": {
                scope_key: {
                    "current_directory": approved,
                    "claude_session_id": "session-old",
                    "force_new_session": True,
                    "claude_model": "gpt-5.3-codex",
                    ENGINE_STATE_KEY: "codex",
                }
            }
        },
    )

    await handle_quick_action_callback(query, "test", context)

    codex_integration.run_command.assert_awaited_once()
    claude_integration.run_command.assert_not_awaited()

    scope_state = context.user_data["scope_state"][scope_key]
    assert scope_state["claude_session_id"] == "session-codex-1"
    assert "force_new_session" not in scope_state

    kwargs = codex_integration.run_command.await_args.kwargs
    assert kwargs["model"] == "gpt-5.3-codex"

    rendered = query.message.reply_text.await_args.args[0]
    assert "Engine: codex" in rendered


@pytest.mark.asyncio
async def test_quick_action_callback_sanitizes_model_when_fallbacks_to_claude(
    tmp_path,
):
    """Codex active with Claude fallback should not pass codex-only model id."""
    approved = tmp_path / "approved"
    approved.mkdir()

    user_id = 2002
    chat_id = 3002
    scope_key = _scope_key(user_id, chat_id)
    claude_integration = SimpleNamespace(
        process_manager=SimpleNamespace(
            _resolve_cli_path=lambda: "/usr/local/bin/claude",
            _detect_cli_kind=lambda _path: "claude",
        ),
        run_command=AsyncMock(
            return_value=SimpleNamespace(
                content="执行完成",
                session_id="session-claude-fallback-1",
            )
        ),
    )
    action = SimpleNamespace(icon="🧪", name="Run Tests", prompt="pytest")
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            chat_id=chat_id,
            reply_text=AsyncMock(),
        ),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        bot_data={
            "settings": _build_settings(approved),
            "quick_actions": SimpleNamespace(actions={"test": action}),
            "cli_integrations": {
                "claude": claude_integration,
            },
        },
        user_data={
            "scope_state": {
                scope_key: {
                    "current_directory": approved,
                    "claude_session_id": "session-old",
                    "force_new_session": True,
                    "claude_model": "gpt-5.3-codex",
                    ENGINE_STATE_KEY: "codex",
                }
            }
        },
    )

    await handle_quick_action_callback(query, "test", context)

    claude_integration.run_command.assert_awaited_once()
    kwargs = claude_integration.run_command.await_args.kwargs
    assert kwargs["model"] is None


@pytest.mark.asyncio
async def test_resume_command_uses_codex_scanner_when_engine_is_codex(tmp_path):
    """Resume command should route scanner by active engine."""
    approved = tmp_path / "approved"
    approved.mkdir()
    user_id = 3001
    scope_key = _scope_key(user_id, user_id)
    codex_scanner = SimpleNamespace(list_projects=AsyncMock(return_value=[]))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_thread_id=None),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        args=[],
        bot_data={
            "settings": _build_settings(approved),
            "codex_desktop_scanner": codex_scanner,
        },
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "codex"}}},
    )

    await resume_command(update, context)

    codex_scanner.list_projects.assert_awaited_once()
    rendered = update.message.reply_text.await_args.args[0]
    assert "Codex" in rendered


@pytest.mark.asyncio
async def test_do_adopt_session_uses_engine_specific_integration(tmp_path):
    """Resume adopt should use selected engine integration and switch scope engine."""
    approved = tmp_path / "approved"
    approved.mkdir()
    project = approved / "proj1"
    user_id = 4001
    chat_id = 5001
    scope_key = _scope_key(user_id, chat_id)

    adopted = SimpleNamespace(session_id="codex-session-1")
    codex_integration = SimpleNamespace(
        session_manager=SimpleNamespace(
            adopt_external_session=AsyncMock(return_value=adopted)
        )
    )
    claude_integration = SimpleNamespace(
        session_manager=SimpleNamespace(adopt_external_session=AsyncMock())
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id), message_thread_id=None
        ),
        edit_message_text=AsyncMock(),
    )
    settings = _build_settings(approved)
    context = SimpleNamespace(
        bot_data={
            "settings": settings,
            "cli_integrations": {
                "claude": claude_integration,
                "codex": codex_integration,
            },
        },
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "claude"}}},
    )

    await _do_adopt_session(
        query=query,
        user_id=user_id,
        project_cwd=project,
        session_id="019c-test-codex-session",
        settings=settings,
        context=context,
        engine="codex",
    )

    codex_integration.session_manager.adopt_external_session.assert_awaited_once()
    claude_integration.session_manager.adopt_external_session.assert_not_awaited()
    scope_state = context.user_data["scope_state"][scope_key]
    assert scope_state[ENGINE_STATE_KEY] == "codex"
    assert scope_state["claude_session_id"] == "codex-session-1"


@pytest.mark.asyncio
async def test_do_adopt_session_renders_history_preview_after_resume(tmp_path):
    """Resume success message should include recent history preview when available."""
    approved = tmp_path / "approved"
    approved.mkdir()
    project = approved / "proj2"
    project.mkdir()
    user_id = 4101
    chat_id = 5101
    scope_key = _scope_key(user_id, chat_id)
    session_id = "resume-preview-session"

    session_file = tmp_path / "session-preview.jsonl"
    session_file.write_text(
        "\n".join(
            [
                '{"type":"session_meta","payload":{"id":"resume-preview-session","cwd":"%s"}}'
                % str(project).replace("\\", "\\\\"),
                '{"type":"event_msg","payload":{"type":"user_message","message":"first user"}}',
                '{"type":"event_msg","payload":{"type":"assistant_message","message":"first assistant"}}',
                '{"type":"event_msg","payload":{"type":"user_message","message":"second user"}}',
                '{"type":"event_msg","payload":{"type":"assistant_message","message":"second assistant"}}',
            ]
        ),
        encoding="utf-8",
    )

    adopted = SimpleNamespace(session_id=session_id)
    codex_integration = SimpleNamespace(
        session_manager=SimpleNamespace(
            adopt_external_session=AsyncMock(return_value=adopted)
        )
    )
    scanner = SimpleNamespace(
        list_sessions=AsyncMock(
            return_value=[
                SimpleNamespace(session_id=session_id, source_file=session_file),
            ]
        )
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id), message_thread_id=None
        ),
        edit_message_text=AsyncMock(),
    )
    settings = _build_settings(approved)
    settings.resume_history_preview_count = 4
    context = SimpleNamespace(
        bot_data={
            "settings": settings,
            "cli_integrations": {
                "claude": SimpleNamespace(
                    session_manager=SimpleNamespace(adopt_external_session=AsyncMock())
                ),
                "codex": codex_integration,
            },
        },
        user_data={"scope_state": {scope_key: {ENGINE_STATE_KEY: "claude"}}},
    )

    await _do_adopt_session(
        query=query,
        user_id=user_id,
        project_cwd=project,
        session_id=session_id,
        settings=settings,
        context=context,
        engine="codex",
        scanner=scanner,
    )

    rendered = query.edit_message_text.await_args.args[0]
    assert "Session Resumed" in rendered
    assert "最近历史预览" in rendered
    assert "*你*:" in rendered
    assert "*助手*:" in rendered
    assert "second user" in rendered
