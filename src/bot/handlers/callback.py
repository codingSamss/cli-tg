"""Handle inline keyboard callbacks."""

# mypy: disable-error-code=no-untyped-def

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...claude.facade import ClaudeIntegration
from ...claude.task_registry import TaskRegistry
from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...security.validators import SecurityValidator
from ...services import ApprovalService
from ...services.session_interaction_service import SessionInteractionService
from ...services.session_lifecycle_service import SessionLifecycleService
from ...services.session_service import SessionService
from ..features.session_export import ExportFormat
from ..inbound_task_queue import InboundTaskQueue
from ..utils.cli_engine import (
    ENGINE_CLAUDE,
    ENGINE_CODEX,
    SUPPORTED_CLI_ENGINES,
    get_active_cli_engine,
    get_cli_integration,
    get_engine_capabilities,
    normalize_cli_engine,
    set_active_cli_engine,
)
from ..utils.command_menu import sync_chat_command_menu
from ..utils.recent_projects import build_recent_projects_message, scan_recent_projects
from ..utils.resume_history import ResumeHistoryMessage, load_resume_history_preview
from ..utils.resume_ui import build_resume_project_selector
from ..utils.scope_state import get_scope_state_from_query
from ..utils.telegram_send import (
    is_markdown_parse_error,
    normalize_message_thread_id,
    prepare_telegram_text_and_parse_mode,
    send_message_resilient,
)
from ..utils.ui_adapter import build_reply_markup_from_spec
from .message import (
    _join_progress_lines_for_display,
    _resolve_model_override,
    build_permission_handler,
)

logger = structlog.get_logger()
_PARSE_MODE_UNSET = object()
_CHAT_ACTION_HEARTBEAT_INTERVAL_SECONDS = 4.0


async def _reply_query_message_resilient(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    reply_to_message_id: int | None = None,
) -> Any:
    """Reply to callback message with fallback to resilient send helper."""
    original_text = str(text or "")
    prepared_text, prepared_parse_mode, _ = prepare_telegram_text_and_parse_mode(
        original_text, parse_mode
    )

    message = getattr(query, "message", None)
    if message is None:
        return None

    send_kwargs: dict[str, Any] = {}
    chat_obj = getattr(message, "chat", None)
    chat_type = getattr(chat_obj, "type", None)
    should_quote_reply = str(chat_type or "").strip().lower() != "private"
    if prepared_parse_mode is not None:
        send_kwargs["parse_mode"] = prepared_parse_mode
    if reply_markup is not None:
        send_kwargs["reply_markup"] = reply_markup
    if (
        should_quote_reply
        and isinstance(reply_to_message_id, int)
        and reply_to_message_id > 0
    ):
        send_kwargs["reply_to_message_id"] = reply_to_message_id

    try:
        return await message.reply_text(prepared_text, **send_kwargs)
    except Exception:
        bot = getattr(context, "bot", None)
        chat_id = getattr(message, "chat_id", None)
        if not isinstance(chat_id, int):
            chat_id = getattr(chat_obj, "id", None)
        if bot is None or not isinstance(chat_id, int):
            raise

        return await send_message_resilient(
            bot=bot,
            chat_id=chat_id,
            text=original_text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=getattr(message, "message_thread_id", None),
            chat_type=chat_type,
        )


def _is_noop_edit_error(error: Exception) -> bool:
    """Whether Telegram rejected edit because target text is unchanged."""
    return "message is not modified" in str(error).lower()


async def _edit_query_message_resilient(
    query: Any,
    text: str,
    *,
    parse_mode: str | None | object = _PARSE_MODE_UNSET,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Any:
    """Edit callback message with markdown/no-op fallback."""
    original_text = str(text or "")
    prepared_text = original_text
    prepared_parse_mode: str | None | object = parse_mode
    html_upgraded_from_markdown = False
    if parse_mode is not _PARSE_MODE_UNSET:
        requested_parse_mode: str | None = (
            parse_mode if isinstance(parse_mode, str) else None
        )
        (
            prepared_text,
            prepared_parse_mode,
            html_upgraded_from_markdown,
        ) = prepare_telegram_text_and_parse_mode(original_text, requested_parse_mode)

    edit_kwargs: dict[str, Any] = {}
    if parse_mode is not _PARSE_MODE_UNSET:
        edit_kwargs["parse_mode"] = prepared_parse_mode
    if reply_markup is not None:
        edit_kwargs["reply_markup"] = reply_markup

    try:
        return await query.edit_message_text(prepared_text, **edit_kwargs)
    except Exception as edit_error:
        if _is_noop_edit_error(edit_error):
            return None
        if parse_mode not in (None, _PARSE_MODE_UNSET) and is_markdown_parse_error(
            edit_error
        ):
            if (
                html_upgraded_from_markdown
                and str(parse_mode or "").strip().lower() == "markdown"
            ):
                markdown_kwargs = dict(edit_kwargs)
                markdown_kwargs["parse_mode"] = "Markdown"
                try:
                    return await query.edit_message_text(
                        original_text, **markdown_kwargs
                    )
                except Exception as markdown_error:
                    if _is_noop_edit_error(markdown_error):
                        return None

            fallback_kwargs = dict(edit_kwargs)
            fallback_kwargs.pop("parse_mode", None)
            try:
                return await query.edit_message_text(original_text, **fallback_kwargs)
            except Exception as fallback_error:
                if _is_noop_edit_error(fallback_error):
                    return None
                raise
        raise


async def _send_chat_action_heartbeat(
    *,
    bot: Any,
    chat_id: int,
    action: str,
    stop_event: asyncio.Event,
    interval_seconds: float = _CHAT_ACTION_HEARTBEAT_INTERVAL_SECONDS,
    message_thread_id: int | None = None,
    chat_type: str | None = None,
) -> None:
    """Keep Telegram chat action visible during long-running callback handling."""
    send_chat_action = getattr(bot, "send_chat_action", None)
    if not callable(send_chat_action):
        return

    wait_timeout = max(interval_seconds, 0.1)
    normalized_thread_id = normalize_message_thread_id(
        message_thread_id, chat_type=chat_type
    )
    while not stop_event.is_set():
        try:
            send_kwargs: dict[str, Any] = {"chat_id": chat_id, "action": action}
            if normalized_thread_id is not None:
                send_kwargs["message_thread_id"] = normalized_thread_id
            await send_chat_action(**send_kwargs)
        except Exception as e:
            logger.debug(
                "Failed to send callback chat action heartbeat",
                action=action,
                error=str(e),
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            continue


async def _cancel_task_with_fallback(
    *,
    task_registry: TaskRegistry,
    user_id: int,
    scope_key: str | None,
) -> tuple[bool, bool]:
    """Cancel scoped task first, then fallback to user-level cancellation.

    Returns:
    - cancelled: whether any task was cancelled
    - used_fallback: whether fallback path was used
    """
    cancelled = await task_registry.cancel(user_id, scope_key=scope_key)
    if cancelled:
        return True, False

    if scope_key:
        cancelled_any = await task_registry.cancel(user_id, scope_key=None)
        if cancelled_any:
            return True, True

    return False, False


async def _authorize_callback_user(
    *, user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> tuple[bool, str]:
    """Authorize callback actor before processing privileged actions."""
    auth_manager = context.bot_data.get("auth_manager")
    audit_logger: AuditLogger | None = context.bot_data.get("audit_logger")

    if not auth_manager:
        logger.error(
            "Authentication manager unavailable for callback processing",
            user_id=user_id,
        )
        return False, "🔒 Authentication system unavailable. Please try again later."

    try:
        if auth_manager.is_authenticated(user_id):
            refresh_session = getattr(auth_manager, "refresh_session", None)
            if callable(refresh_session):
                refresh_session(user_id)
            return True, ""
    except Exception as exc:
        logger.warning(
            "Callback authentication pre-check failed",
            user_id=user_id,
            error=str(exc),
        )

    authenticated = False
    try:
        authenticated = await auth_manager.authenticate_user(user_id)
    except Exception as exc:
        logger.error(
            "Callback authentication failed",
            user_id=user_id,
            error=str(exc),
        )

    if audit_logger:
        try:
            await audit_logger.log_auth_attempt(
                user_id=user_id,
                success=authenticated,
                method="callback",
                reason="callback_query",
            )
        except Exception as exc:
            logger.warning(
                "Failed to audit callback authentication attempt",
                user_id=user_id,
                error=str(exc),
            )

    if not authenticated:
        logger.warning("Unauthorized callback blocked", user_id=user_id)
        return False, "🔒 Authentication required. Please contact the administrator."

    return True, ""


def _resume_engine_label(engine: str) -> str:
    """Render resume engine label."""
    return "Codex" if engine == ENGINE_CODEX else "Claude"


def _engine_display_name(engine: str) -> str:
    """Render readable label for engine selector."""
    return "Codex" if engine == ENGINE_CODEX else "Claude"


def _escape_markdown(text: str) -> str:
    """Escape special chars for Telegram Markdown."""
    escaped = text
    for ch in ("\\", "`", "*", "_", "[", "]"):
        escaped = escaped.replace(ch, f"\\{ch}")
    return escaped


def _normalize_preview_text(raw: str, *, max_len: int) -> str:
    """Normalize preview text into one compact line."""
    compact = " ".join(str(raw or "").split())
    if not compact:
        return "无预览"
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3].rstrip() + "..."


def _candidate_event_time(candidate) -> datetime | None:
    """Resolve candidate event time, fallback to file mtime."""
    last_event_at = getattr(candidate, "last_event_at", None)
    if isinstance(last_event_at, datetime):
        return last_event_at

    file_mtime = getattr(candidate, "file_mtime", None)
    if isinstance(file_mtime, datetime):
        return file_mtime
    return None


def _format_relative_time(target: datetime | None) -> str:
    """Format relative age from UTC naive datetime."""
    if target is None:
        return "时间未知"

    now = datetime.utcnow()
    delta_sec = max(0, int((now - target).total_seconds()))
    if delta_sec < 60:
        return "刚刚"
    if delta_sec < 3600:
        return f"{delta_sec // 60}分钟前"
    if delta_sec < 86400:
        return f"{delta_sec // 3600}小时前"
    if delta_sec < 86400 * 7:
        return f"{delta_sec // 86400}天前"
    return target.strftime("%m-%d %H:%M")


def _candidate_preview(candidate, *, max_len: int) -> str:
    """Pick the best preview text for one session candidate."""
    last_user_message = str(getattr(candidate, "last_user_message", "") or "").strip()
    if last_user_message:
        return _normalize_preview_text(last_user_message, max_len=max_len)

    first_message = str(getattr(candidate, "first_message", "") or "").strip()
    return _normalize_preview_text(first_message, max_len=max_len)


def _build_resume_session_button_label(candidate) -> str:
    """Build concise button label for one resumable session."""
    sid_short = str(getattr(candidate, "session_id", "") or "")[:8] or "unknown"
    active = bool(getattr(candidate, "is_probably_active", False))
    status = "🟢" if active else "⚪"
    age = (
        "活跃中" if active else _format_relative_time(_candidate_event_time(candidate))
    )
    preview = _candidate_preview(candidate, max_len=14)
    label = f"{status} {sid_short} · {age} · {preview}"
    if len(label) > 60:
        return label[:57] + "..."
    return label


def _build_resume_session_preview_line(candidate) -> str:
    """Build markdown-safe preview line for session list body."""
    sid_short = str(getattr(candidate, "session_id", "") or "")[:8] or "unknown"
    active = bool(getattr(candidate, "is_probably_active", False))
    status = (
        "活跃中" if active else _format_relative_time(_candidate_event_time(candidate))
    )
    preview = _escape_markdown(_candidate_preview(candidate, max_len=56))
    return f"• `{sid_short}` · {status} · {preview}"


def _build_resume_history_preview_text(
    messages: list[ResumeHistoryMessage],
    *,
    max_len: int = 72,
) -> str:
    """Build markdown-friendly resume history preview block."""
    if not messages:
        return ""

    lines = ["*最近历史预览*"]
    for message in messages:
        role = "你" if message.role == "user" else "助手"
        preview = _escape_markdown(
            _normalize_preview_text(message.content, max_len=max_len)
        )
        lines.append(f"• *{role}*: {preview}")
    return "\n".join(lines)


def _build_engine_selector_keyboard(
    *, active_engine: str, available_engines: set[str]
) -> InlineKeyboardMarkup | None:
    """Build inline keyboard for engine switching callbacks."""
    buttons = []
    for engine in SUPPORTED_CLI_ENGINES:
        if engine not in available_engines:
            continue
        label = _engine_display_name(engine)
        if engine == active_engine:
            label = f"✅ {label}（当前）"
        buttons.append(
            InlineKeyboardButton(
                label,
                callback_data=f"engine:switch:{engine}",
            )
        )

    if not buttons:
        return None

    return InlineKeyboardMarkup([buttons])


def _build_codex_model_keyboard(*, selected_model: str | None) -> InlineKeyboardMarkup:
    """Build inline keyboard for Codex model selection callbacks."""
    selected = str(selected_model or "").strip()
    candidates: list[str] = []
    for candidate in (
        selected,
        "gpt-5.3-codex",
        "gpt-5.1-codex-mini",
        "gpt-5",
    ):
        value = str(candidate or "").strip().replace("`", "")
        if not value or value.lower() in {"default", "current"}:
            continue
        if value not in candidates:
            candidates.append(value)

    rows: list[list[InlineKeyboardButton]] = []
    for value in candidates:
        label = f"✅ {value}" if value == selected else value
        rows.append([InlineKeyboardButton(label, callback_data=f"model:codex:{value}")])

    default_label = "✅ default" if not selected else "default"
    rows.append(
        [InlineKeyboardButton(default_label, callback_data="model:codex:default")]
    )
    return InlineKeyboardMarkup(rows)


def _is_claude_model_name(value: str | None) -> bool:
    """Return whether model id is a Claude alias/name."""
    normalized = str(value or "").strip().lower()
    if not normalized:
        return False
    if normalized in {"sonnet", "opus", "haiku"}:
        return True
    return any(token in normalized for token in ("claude", "sonnet", "opus", "haiku"))


def _get_query_chat_id(query) -> int | None:
    """Extract chat id from callback query message."""
    message_obj = getattr(query, "message", None)
    chat_id = getattr(message_obj, "chat_id", None)
    if not isinstance(chat_id, int):
        chat_id = getattr(getattr(message_obj, "chat", None), "id", None)
    return chat_id if isinstance(chat_id, int) else None


def _get_or_create_resume_token_manager(context: ContextTypes.DEFAULT_TYPE):
    """Get shared resume token manager from bot_data."""
    from ...bot.resume_tokens import ResumeTokenManager

    token_mgr = context.bot_data.get("resume_token_manager")
    if token_mgr is None:
        token_mgr = ResumeTokenManager()
        context.bot_data["resume_token_manager"] = token_mgr
    return token_mgr


def _get_or_create_resume_scanner(
    *, context: ContextTypes.DEFAULT_TYPE, settings: Settings, engine: str
):
    """Get engine-specific desktop session scanner."""
    from ...bot.utils.codex_resume_scanner import CodexSessionScanner
    from ...claude.desktop_scanner import DesktopSessionScanner

    scanner_key = (
        "codex_desktop_scanner" if engine == ENGINE_CODEX else "desktop_scanner"
    )
    scanner = context.bot_data.get(scanner_key)
    if scanner is None:
        scanner = (
            CodexSessionScanner(
                approved_directory=settings.approved_directory,
                cache_ttl_sec=settings.resume_scan_cache_ttl_seconds,
            )
            if engine == ENGINE_CODEX
            else DesktopSessionScanner(
                approved_directory=settings.approved_directory,
                cache_ttl_sec=settings.resume_scan_cache_ttl_seconds,
            )
        )
        context.bot_data[scanner_key] = scanner
    return scanner


def _get_scope_state_for_query(
    query, context: ContextTypes.DEFAULT_TYPE
) -> tuple[str, dict]:
    """Get per-chat/topic scoped state for callback handlers."""
    settings: Settings | None = context.bot_data.get("settings")
    default_directory = settings.approved_directory if settings else Path(".").resolve()
    return get_scope_state_from_query(
        user_data=context.user_data,
        query=query,
        default_directory=default_directory,
    )


def _parse_export_format(export_format: str) -> ExportFormat | None:
    """Parse callback export format string to enum."""
    if not export_format:
        return None

    raw = export_format.strip().lower()
    format_map = {
        ExportFormat.MARKDOWN.value: ExportFormat.MARKDOWN,
        ExportFormat.HTML.value: ExportFormat.HTML,
        ExportFormat.JSON.value: ExportFormat.JSON,
    }
    return format_map.get(raw)


async def _sync_chat_menu_for_engine(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | None,
    engine: str,
) -> None:
    """Refresh per-chat command menu after callback-side engine switch."""
    bot = getattr(context, "bot", None)
    if bot is None:
        return

    try:
        commands = await sync_chat_command_menu(
            bot=bot,
            chat_id=chat_id,
            engine=engine,
        )
        if commands:
            logger.info(
                "Synced callback chat menu",
                chat_id=chat_id,
                engine=engine,
                commands=[cmd.command for cmd in commands],
            )
    except Exception as exc:
        logger.warning(
            "Failed to sync callback chat menu",
            chat_id=chat_id,
            engine=engine,
            error=str(exc),
        )


async def handle_callback_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route callback queries to appropriate handlers."""
    query = update.callback_query
    if query is None:
        logger.warning("Callback handler called without callback_query payload")
        return

    user_id = getattr(getattr(query, "from_user", None), "id", None)
    if not isinstance(user_id, int):
        logger.warning("Callback query missing user id")
        await query.answer("🔒 Authentication required.", show_alert=True)
        return

    callback_data = getattr(query, "data", None)
    if not isinstance(callback_data, str):
        logger.warning(
            "Callback query payload has invalid data type",
            user_id=user_id,
            data_type=type(callback_data).__name__,
        )
        await query.answer("❌ Invalid action payload. Please retry.", show_alert=True)
        return

    data = callback_data.strip()
    if not data:
        logger.warning("Callback query payload is empty", user_id=user_id)
        await query.answer("❌ Invalid action payload. Please retry.", show_alert=True)
        return

    logger.info("Processing callback query", user_id=user_id, callback_data=data)

    authorized, auth_message = await _authorize_callback_user(
        user_id=user_id, context=context
    )
    if not authorized:
        await query.answer(auth_message, show_alert=True)
        return

    try:
        # Parse callback data
        if ":" in data:
            action, param = data.split(":", 1)
        else:
            action, param = data, None

        # Handle cancel callback before the generic answer() call,
        # because cancel needs its own answer text.
        if action == "cancel" and param == "task":
            task_registry: Optional[TaskRegistry] = context.bot_data.get(
                "task_registry"
            )
            if not task_registry:
                await query.answer("Task registry not available.", show_alert=True)
                return
            scope_key, _ = _get_scope_state_for_query(query, context)
            cancelled, used_fallback = await _cancel_task_with_fallback(
                task_registry=task_registry,
                user_id=user_id,
                scope_key=scope_key,
            )
            if cancelled:
                if used_fallback:
                    logger.info(
                        "Cancel button used fallback cancellation scope",
                        user_id=user_id,
                        scope_key=scope_key,
                    )
                await query.answer("Task cancellation requested.")
                # Visible feedback for clients where callback toast is subtle.
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
            else:
                await query.answer("No active task to cancel.", show_alert=True)
                # Stale button cleanup: after restart/crash there may be no
                # in-memory task, but the old progress bubble still shows
                # a cancel button.
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
            audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id, command="cancel_button", args=[], success=cancelled
                )
            return

        # Acknowledge the callback for all other actions
        await query.answer()

        # Route to appropriate handler
        handlers = {
            "cd": handle_cd_callback,
            "action": handle_action_callback,
            "confirm": handle_confirm_callback,
            "quick": handle_quick_action_callback,
            "followup": handle_followup_callback,
            "conversation": handle_conversation_callback,
            "git": handle_git_callback,
            "export": handle_export_callback,
            "permission": handle_permission_callback,
            "thinking": handle_thinking_callback,
            "resume": handle_resume_callback,
            "model": handle_model_callback,
            "engine": handle_engine_callback,
            "provider": handle_provider_callback,
            "queue": handle_queue_callback,
        }

        handler = handlers.get(action)
        if handler:
            await handler(query, param or "", context)
        else:
            await _edit_query_message_resilient(
                query,
                "❌ **Unknown Action**\n\n"
                "This button action is not recognized. "
                "The bot may have been updated since this message was sent.",
            )

    except Exception as e:
        logger.error(
            "Error handling callback query",
            error=str(e),
            user_id=user_id,
            callback_data=data,
        )

        try:
            await _edit_query_message_resilient(
                query,
                "❌ **Error Processing Action**\n\n"
                "An error occurred while processing your request.\n"
                "Please try again or use text commands.",
            )
        except Exception:
            # If we can't edit the message, send a new one
            await _reply_query_message_resilient(
                query,
                context,
                "❌ **Error Processing Action**\n\n"
                "An error occurred while processing your request.",
            )


async def handle_cd_callback(
    query, project_name: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle directory change from inline keyboard."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    security_validator: Optional[SecurityValidator] = context.bot_data.get(
        "security_validator"
    )
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    _, scope_state = _get_scope_state_for_query(query, context)

    try:
        current_dir = scope_state.get("current_directory", settings.approved_directory)

        # Handle special paths
        if project_name == "/":
            new_path = settings.approved_directory
        elif project_name == "..":
            new_path = current_dir.parent
            # Ensure we don't go above approved directory
            if not str(new_path).startswith(str(settings.approved_directory)):
                new_path = settings.approved_directory
        else:
            new_path = settings.approved_directory / project_name

        # Validate path if security validator is available
        if security_validator:
            # Pass the absolute path for validation
            valid, resolved_path, error = security_validator.validate_path(
                str(new_path), settings.approved_directory
            )
            if not valid:
                await _edit_query_message_resilient(
                    query, f"❌ **Access Denied**\n\n{error}"
                )
                return
            if resolved_path is None:
                await _edit_query_message_resilient(
                    query, "❌ **Access Denied**\n\nUnable to resolve directory."
                )
                return
            # Use the validated path
            new_path = resolved_path

        # Check if directory exists
        if not new_path.exists() or not new_path.is_dir():
            await _edit_query_message_resilient(
                query,
                f"❌ **Directory Not Found**\n\n"
                f"The directory `{project_name}` no longer exists or is not accessible.",
            )
            return

        # Update directory and clear session
        old_session_id = scope_state.get("claude_session_id")
        scope_state["current_directory"] = new_path
        scope_state["claude_session_id"] = None
        scope_state["force_new_session"] = True
        if old_session_id:
            permission_manager = context.bot_data.get("permission_manager")
            if permission_manager:
                permission_manager.clear_session(old_session_id)

        # Send confirmation with new directory info
        relative_path = new_path.relative_to(settings.approved_directory)

        # Add navigation buttons
        keyboard = [
            [
                InlineKeyboardButton("📁 List Files", callback_data="action:ls"),
                InlineKeyboardButton(
                    "🆕 New Session", callback_data="action:new_session"
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 Projects", callback_data="action:show_projects"
                ),
                InlineKeyboardButton("📊 Context", callback_data="action:context"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await _edit_query_message_resilient(
            query,
            f"✅ **Directory Changed**\n\n"
            f"📂 Current directory: `{relative_path}/`\n\n"
            f"🔄 Claude session cleared. You can now start coding in this directory!",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

        # Log successful directory change
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id, command="cd", args=[project_name], success=True
            )

    except Exception as e:
        await _edit_query_message_resilient(
            query, f"❌ **Error changing directory**\n\n{str(e)}"
        )

        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id, command="cd", args=[project_name], success=False
            )


async def handle_queue_callback(
    query: Any, param: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle queue callback actions such as removing a queued task."""
    user_id = query.from_user.id
    inbound_queue: Optional[InboundTaskQueue] = context.bot_data.get(
        "inbound_task_queue"
    )
    if not inbound_queue:
        await _edit_query_message_resilient(query, "❌ Queue service unavailable.")
        return

    if not param.startswith("dequeue:"):
        await _edit_query_message_resilient(query, "❌ Invalid queue action.")
        return

    raw_queue_id = param.split(":", 1)[1].strip()
    if not raw_queue_id.isdigit():
        await _edit_query_message_resilient(query, "❌ Invalid queue id.")
        return
    queue_id = int(raw_queue_id)
    if queue_id <= 0:
        await _edit_query_message_resilient(query, "❌ Invalid queue id.")
        return

    scope_key, _ = _get_scope_state_for_query(query, context)
    removed = await inbound_queue.remove(
        user_id=user_id,
        scope_key=scope_key or f"user:{user_id}",
        queue_id=queue_id,
    )

    if removed:
        message_obj = getattr(query, "message", None)
        chat_obj = getattr(message_obj, "chat", None)
        chat_id = getattr(chat_obj, "id", None)
        callback_message_id = getattr(message_obj, "message_id", None)
        source_message_id = getattr(removed, "source_message_id", None)
        target_message_ids: list[int] = []
        for candidate in (source_message_id, callback_message_id):
            if (
                isinstance(candidate, int)
                and candidate > 0
                and candidate not in target_message_ids
            ):
                target_message_ids.append(candidate)

        deleted_message_ids: set[int] = set()
        delete_message = getattr(getattr(context, "bot", None), "delete_message", None)
        if callable(delete_message) and isinstance(chat_id, int):
            for message_id in target_message_ids:
                try:
                    await delete_message(chat_id=chat_id, message_id=message_id)
                    deleted_message_ids.add(message_id)
                except Exception as delete_error:
                    logger.info(
                        "Failed to delete queue-related message after dequeue",
                        user_id=user_id,
                        queue_id=queue_id,
                        chat_id=chat_id,
                        message_id=message_id,
                        error=str(delete_error),
                    )

        callback_deleted = (
            isinstance(callback_message_id, int)
            and callback_message_id in deleted_message_ids
        )
        if not callback_deleted:
            await _edit_query_message_resilient(
                query,
                f"✅ Removed queue item #{queue_id}.",
            )
    else:
        await _edit_query_message_resilient(
            query,
            f"⚠️ Queue item #{queue_id} not found.",
        )

    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    if audit_logger:
        await audit_logger.log_command(
            user_id=user_id,
            command="queue_button_dequeue",
            args=[str(queue_id)],
            success=removed is not None,
        )


async def handle_action_callback(
    query, action_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle general action callbacks."""
    actions = {
        "help": _handle_help_action,
        "show_projects": _handle_show_projects_action,
        "new_session": _handle_new_session_action,
        "continue": _handle_continue_action,
        "end_session": _handle_end_session_action,
        "context": _handle_status_action,
        "refresh_context": _handle_refresh_status_action,
        "recent_cd": _handle_recent_cd_action,
        # Backward compatibility for older callback buttons in history.
        "status": _handle_status_action,
        "ls": _handle_ls_action,
        "start_coding": _handle_start_coding_action,
        "quick_actions": _handle_quick_actions_action,
        "refresh_status": _handle_refresh_status_action,
        "refresh_ls": _handle_refresh_ls_action,
        "export": _handle_export_action,
    }

    handler = actions.get(action_type)
    if handler:
        await handler(query, context)
    else:
        await _edit_query_message_resilient(
            query,
            f"❌ **Unknown Action: {action_type}**\n\n"
            "This action is not implemented yet.",
        )


async def handle_confirm_callback(
    query, confirmation_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle confirmation dialogs."""
    if confirmation_type == "yes":
        await _edit_query_message_resilient(
            query, "✅ **Confirmed**\n\nAction will be processed."
        )
    elif confirmation_type == "no":
        await _edit_query_message_resilient(
            query, "❌ **Cancelled**\n\nAction was cancelled."
        )
    else:
        await _edit_query_message_resilient(
            query, "❓ **Unknown confirmation response**"
        )


# Action handlers


async def _handle_help_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle help action."""
    _, scope_state = _get_scope_state_for_query(query, context)
    active_engine = get_active_cli_engine(scope_state)
    engine_label = "Codex" if active_engine == ENGINE_CODEX else "Claude"
    help_text = (
        "🤖 **Quick Help**\n\n"
        "**Navigation:**\n"
        "• `/ls` - List files\n"
        "• `/cd <dir>` - Change directory\n"
        "• `/projects` - Show projects\n\n"
        "**Sessions:**\n"
        f"• `/new` - New {engine_label} session\n"
        "• `/context` - Session context\n\n"
        "**Tips:**\n"
        f"• Send any text to interact with {engine_label}\n"
        "• Upload files for code review\n"
        "• Use buttons for quick actions\n\n"
        "Use `/help` for detailed help."
    )

    keyboard = [
        [
            InlineKeyboardButton("📖 Full Help", callback_data="action:full_help"),
            InlineKeyboardButton("🏠 Main Menu", callback_data="action:main_menu"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await _edit_query_message_resilient(
        query, help_text, parse_mode="Markdown", reply_markup=reply_markup
    )


async def _handle_show_projects_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle show projects action."""
    settings: Settings = context.bot_data["settings"]

    try:
        # Get directories in approved directory
        projects = []
        for item in sorted(settings.approved_directory.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                projects.append(item.name)

        if not projects:
            await _edit_query_message_resilient(
                query,
                "📁 **No Projects Found**\n\n"
                "No subdirectories found in your approved directory.\n"
                "Create some directories to organize your projects!",
            )
            return

        # Create project buttons
        keyboard = []
        for i in range(0, len(projects), 2):
            row = []
            for j in range(2):
                if i + j < len(projects):
                    project = projects[i + j]
                    row.append(
                        InlineKeyboardButton(
                            f"📁 {project}", callback_data=f"cd:{project}"
                        )
                    )
            keyboard.append(row)

        # Add navigation buttons
        keyboard.append(
            [
                InlineKeyboardButton("🏠 Root", callback_data="cd:/"),
                InlineKeyboardButton(
                    "🔄 Refresh", callback_data="action:show_projects"
                ),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard)
        project_list = "\n".join([f"• `{project}/`" for project in projects])

        await _edit_query_message_resilient(
            query,
            f"📁 **Available Projects**\n\n"
            f"{project_list}\n\n"
            f"Click a project to navigate to it:",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    except Exception as e:
        await _edit_query_message_resilient(
            query, f"❌ Error loading projects: {str(e)}"
        )


async def _handle_new_session_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle new session action."""
    settings: Settings = context.bot_data["settings"]
    _, scope_state = _get_scope_state_for_query(query, context)
    session_lifecycle = context.bot_data.get("session_lifecycle_service") or (
        SessionLifecycleService(
            permission_manager=context.bot_data.get("permission_manager")
        )
    )
    session_interaction = (
        context.bot_data.get("session_interaction_service")
        or SessionInteractionService()
    )
    reset_result = session_lifecycle.start_new_session(scope_state)
    current_dir = scope_state.get("current_directory", settings.approved_directory)
    active_engine = get_active_cli_engine(scope_state)
    session_message = session_interaction.build_new_session_message(
        current_dir=current_dir,
        approved_directory=settings.approved_directory,
        previous_session_id=reset_result.old_session_id,
        for_callback=True,
        active_engine=active_engine,
    )

    await _edit_query_message_resilient(
        query,
        session_message.text,
        parse_mode="Markdown",
        reply_markup=build_reply_markup_from_spec(session_message.keyboard),
    )


async def _handle_end_session_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle end session action."""
    settings: Settings = context.bot_data["settings"]
    _, scope_state = _get_scope_state_for_query(query, context)
    session_lifecycle = context.bot_data.get("session_lifecycle_service") or (
        SessionLifecycleService(
            permission_manager=context.bot_data.get("permission_manager")
        )
    )
    session_interaction = (
        context.bot_data.get("session_interaction_service")
        or SessionInteractionService()
    )
    end_result = session_lifecycle.end_session(scope_state)

    if not end_result.had_active_session:
        no_active_message = session_interaction.build_end_no_active_message(
            for_callback=True
        )
        await _edit_query_message_resilient(
            query,
            no_active_message.text,
            reply_markup=build_reply_markup_from_spec(no_active_message.keyboard),
        )
        return

    # Get current directory for display
    current_dir = scope_state.get("current_directory", settings.approved_directory)
    end_message = session_interaction.build_end_success_message(
        current_dir=current_dir,
        approved_directory=settings.approved_directory,
        for_callback=True,
        title="Session Ended",
    )

    await _edit_query_message_resilient(
        query,
        end_message.text,
        parse_mode="Markdown",
        reply_markup=build_reply_markup_from_spec(end_message.keyboard),
    )


async def _handle_continue_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle continue session action."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = _get_scope_state_for_query(query, context)
    active_engine, cli_integration = get_cli_integration(
        bot_data=context.bot_data,
        scope_state=scope_state,
    )
    session_lifecycle = context.bot_data.get("session_lifecycle_service") or (
        SessionLifecycleService(
            permission_manager=context.bot_data.get("permission_manager")
        )
    )
    session_interaction = (
        context.bot_data.get("session_interaction_service")
        or SessionInteractionService()
    )
    current_dir = scope_state.get("current_directory", settings.approved_directory)
    typing_stop_event = asyncio.Event()
    typing_heartbeat_task: asyncio.Task[None] | None = None

    try:
        if not cli_integration:
            await _edit_query_message_resilient(
                query, session_interaction.get_integration_unavailable_text()
            )
            return

        message_obj = getattr(query, "message", None)
        chat_obj = getattr(message_obj, "chat", None)
        chat_id = getattr(message_obj, "chat_id", None)
        if not isinstance(chat_id, int):
            chat_id = getattr(chat_obj, "id", None)
        if isinstance(chat_id, int):
            typing_heartbeat_task = asyncio.create_task(
                _send_chat_action_heartbeat(
                    bot=context.bot,
                    chat_id=chat_id,
                    action="typing",
                    stop_event=typing_stop_event,
                    message_thread_id=getattr(message_obj, "message_thread_id", None),
                    chat_type=getattr(chat_obj, "type", None),
                )
            )

        # Check if there's an existing session in user context
        claude_session_id = session_lifecycle.get_active_session_id(scope_state)
        permission_handler = build_permission_handler(
            bot=context.bot,
            chat_id=query.message.chat_id,
            settings=settings,
            chat_type=getattr(getattr(query.message, "chat", None), "type", None),
            message_thread_id=getattr(query.message, "message_thread_id", None),
        )

        progress_text = session_interaction.build_continue_progress_text(
            existing_session_id=claude_session_id,
            current_dir=current_dir,
            approved_directory=settings.approved_directory,
            prompt=None,
        )
        await _edit_query_message_resilient(
            query,
            progress_text,
            parse_mode="Markdown",
        )

        continue_result = await session_lifecycle.continue_session(
            user_id=user_id,
            scope_state=scope_state,
            current_dir=current_dir,
            claude_integration=cli_integration,
            prompt=None,
            default_prompt="Please continue where we left off",
            permission_handler=permission_handler,
            use_empty_prompt_when_existing=True,
            allow_none_prompt_when_discover=True,
        )
        claude_response = continue_result.response

        if continue_result.status == "continued" and claude_response:

            # Send Claude's response
            await _reply_query_message_resilient(
                query,
                context,
                session_interaction.build_continue_callback_success_text(
                    claude_response.content
                )
                + f"\n\n`Engine: {active_engine}`",
                parse_mode="Markdown",
            )
        elif continue_result.status == "not_found":
            # No session found to continue
            not_found_message = session_interaction.build_continue_not_found_message(
                current_dir=current_dir,
                approved_directory=settings.approved_directory,
                for_callback=True,
            )
            await _edit_query_message_resilient(
                query,
                not_found_message.text,
                parse_mode="Markdown",
                reply_markup=build_reply_markup_from_spec(not_found_message.keyboard),
            )
        else:
            await _edit_query_message_resilient(
                query, session_interaction.get_integration_unavailable_text()
            )

    except Exception as e:
        logger.error("Error in continue action", error=str(e), user_id=user_id)
        error_message = session_interaction.build_continue_callback_error_message(
            str(e)
        )
        await _edit_query_message_resilient(
            query,
            error_message.text,
            parse_mode="Markdown",
            reply_markup=build_reply_markup_from_spec(error_message.keyboard),
        )
    finally:
        typing_stop_event.set()
        if typing_heartbeat_task and not typing_heartbeat_task.done():
            typing_heartbeat_task.cancel()
            try:
                await typing_heartbeat_task
            except asyncio.CancelledError:
                pass


async def _handle_status_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle status action - synced with /context command logic."""
    settings: Settings = context.bot_data["settings"]
    user_id = int(getattr(getattr(query, "from_user", None), "id", 0) or 0)
    _, scope_state = _get_scope_state_for_query(query, context)
    session_interaction = (
        context.bot_data.get("session_interaction_service")
        or SessionInteractionService()
    )
    view_spec = session_interaction.build_context_view_spec(for_callback=True)
    loading_kwargs: dict[str, Any] = {}
    if view_spec.loading_parse_mode:
        loading_kwargs["parse_mode"] = view_spec.loading_parse_mode
    await _edit_query_message_resilient(
        query,
        view_spec.loading_text,
        **loading_kwargs,
    )

    try:
        active_engine, cli_integration = get_cli_integration(
            bot_data=context.bot_data,
            scope_state=scope_state,
        )
        engine_capabilities = get_engine_capabilities(active_engine)
        session_service = context.bot_data.get("session_service")
        snapshot = await SessionService.build_scope_context_snapshot(
            user_id=user_id,
            scope_state=scope_state,
            approved_directory=settings.approved_directory,
            claude_integration=cli_integration,
            session_service=session_service,
            include_resumable=view_spec.include_resumable,
            include_event_summary=view_spec.include_event_summary,
            allow_precise_context_probe=engine_capabilities.supports_precise_context_probe,
        )
        render_result = session_interaction.build_context_render_result(
            snapshot=snapshot,
            scope_state=scope_state,
            approved_directory=settings.approved_directory,
            full_mode=False,
        )
        await _edit_query_message_resilient(
            query,
            render_result.primary_text,
            parse_mode=render_result.parse_mode,
        )
    except Exception as exc:
        logger.error("Error in context callback", error=str(exc), user_id=user_id)
        await _edit_query_message_resilient(query, view_spec.error_text)


async def handle_model_callback(
    query, param: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle model selection callback from inline keyboard."""
    _, scope_state = _get_scope_state_for_query(query, context)
    active_engine = get_active_cli_engine(scope_state)

    if active_engine == ENGINE_CODEX:
        codex_param = str(param or "").strip()
        if not codex_param.startswith("codex:"):
            await _edit_query_message_resilient(
                query,
                "ℹ️ 当前引擎：`codex`\n"
                "请使用 Codex 模型按钮，或手动执行 `/model <model_name>`。",
                parse_mode="Markdown",
            )
            return

        selected_raw = codex_param.split(":", 1)[1].strip()
        if not selected_raw:
            await _edit_query_message_resilient(
                query,
                "❌ 无效模型参数，请重新执行 `/model` 选择。",
                parse_mode="Markdown",
            )
            return

        normalized = selected_raw.lower()
        if normalized in {"default", "clear", "reset"}:
            scope_state.pop("claude_model", None)
            selected = "default"
        else:
            selected = selected_raw.replace("`", "")
            scope_state["claude_model"] = selected

        await _edit_query_message_resilient(
            query,
            "✅ 已更新 Codex 模型设置。\n"
            f"当前设置：`{selected}`\n\n"
            "你也可以手动输入：`/model <model_name>`",
            parse_mode="Markdown",
            reply_markup=_build_codex_model_keyboard(
                selected_model=str(scope_state.get("claude_model") or "").strip()
            ),
        )
        return

    capabilities = get_engine_capabilities(active_engine)
    if not capabilities.supports_model_selection:
        await _edit_query_message_resilient(
            query,
            "ℹ️ 当前引擎不支持模型选择。\n"
            f"当前引擎：`{active_engine}`\n"
            "请先切换：`/engine claude`",
            parse_mode="Markdown",
        )
        return

    param_norm = str(param or "").strip().lower()
    if param_norm.startswith("codex:"):
        await _edit_query_message_resilient(
            query,
            "ℹ️ 当前引擎：`claude`\n" "请使用 Claude 模型按钮，或手动执行 `/model`。",
            parse_mode="Markdown",
        )
        return
    if param_norm not in {"sonnet", "opus", "haiku", "default"}:
        await _edit_query_message_resilient(
            query,
            "❌ 无效模型参数，请重新执行 `/model` 选择。",
            parse_mode="Markdown",
        )
        return

    if param_norm == "default":
        scope_state.pop("claude_model", None)
        selected = "default"
    else:
        scope_state["claude_model"] = param_norm
        selected = param_norm

    # Claude model switch must start a fresh session. Continuing an existing
    # session may keep the old runtime model and ignore the new selection.
    old_session_id = scope_state.get("claude_session_id")
    scope_state["claude_session_id"] = None
    scope_state["session_started"] = True
    scope_state["force_new_session"] = True
    if old_session_id:
        permission_manager = context.bot_data.get("permission_manager")
        if permission_manager:
            permission_manager.clear_session(old_session_id)

    # Rebuild keyboard with updated selection indicator
    current = scope_state.get("claude_model")
    keyboard = [
        [
            InlineKeyboardButton(
                f"{'> ' if current == 'sonnet' else ''}Sonnet",
                callback_data="model:sonnet",
            ),
            InlineKeyboardButton(
                f"{'> ' if current == 'opus' else ''}Opus",
                callback_data="model:opus",
            ),
            InlineKeyboardButton(
                f"{'> ' if current == 'haiku' else ''}Haiku",
                callback_data="model:haiku",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'> ' if not current else ''}Default",
                callback_data="model:default",
            ),
        ],
    ]

    await _edit_query_message_resilient(
        query,
        "✅ 模型设置已更新。\n"
        f"当前设置：`{selected}`\n\n"
        "下一条消息将从新会话开始，确保模型切换生效。",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_engine_callback(
    query, param: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle engine switch callback from /engine selector."""
    settings: Settings = context.bot_data["settings"]
    _, scope_state = _get_scope_state_for_query(query, context)
    active_engine = get_active_cli_engine(scope_state)
    integrations = context.bot_data.get("cli_integrations") or {}
    available_engines = set(
        normalize_cli_engine(name) for name in integrations.keys() if name
    )
    selector_keyboard = _build_engine_selector_keyboard(
        active_engine=active_engine,
        available_engines=available_engines,
    )

    if not param or not param.startswith("switch:"):
        await _edit_query_message_resilient(
            query,
            "❌ 无效的引擎切换操作，请重新执行 `/engine`。",
            parse_mode="Markdown",
            reply_markup=selector_keyboard,
        )
        return

    requested_engine = normalize_cli_engine(param.split(":", 1)[1])
    if requested_engine not in integrations:
        await _edit_query_message_resilient(
            query,
            f"❌ 引擎 `{requested_engine}` 当前不可用。\n"
            "请检查对应 CLI 是否安装，并在配置中启用。",
            parse_mode="Markdown",
            reply_markup=selector_keyboard,
        )
        return

    if requested_engine == active_engine:
        await _edit_query_message_resilient(
            query,
            f"ℹ️ 当前已经是 `{active_engine}` 引擎。",
            parse_mode="Markdown",
            reply_markup=selector_keyboard,
        )
        return

    old_session_id = scope_state.get("claude_session_id")
    set_active_cli_engine(scope_state, requested_engine)
    scope_state["claude_session_id"] = None
    scope_state["session_started"] = True
    scope_state["force_new_session"] = True
    if requested_engine == ENGINE_CLAUDE:
        selected_model = str(scope_state.get("claude_model") or "").strip()
        if selected_model and not _is_claude_model_name(selected_model):
            scope_state.pop("claude_model", None)
    if old_session_id:
        permission_manager = context.bot_data.get("permission_manager")
        if permission_manager:
            permission_manager.clear_session(old_session_id)

    await _sync_chat_menu_for_engine(
        context=context,
        chat_id=_get_query_chat_id(query),
        engine=requested_engine,
    )
    token_mgr = _get_or_create_resume_token_manager(context)
    scanner = _get_or_create_resume_scanner(
        context=context,
        settings=settings,
        engine=requested_engine,
    )
    projects = await scanner.list_projects()

    if projects:
        current_dir = scope_state.get("current_directory")
        resume_text, resume_keyboard = build_resume_project_selector(
            projects=projects,
            approved_root=settings.approved_directory,
            token_mgr=token_mgr,
            user_id=query.from_user.id,
            current_directory=current_dir,
            show_all=False,
            payload_extra={"engine": requested_engine},
            engine=requested_engine,
        )
        await _edit_query_message_resilient(
            query,
            "✅ **CLI 引擎已切换**\n\n"
            f"从 `{active_engine}` 切换到 `{requested_engine}`。\n"
            "已清空当前会话绑定。请先选目录，再选会话；也可在下一步直接新建会话。\n\n"
            f"{resume_text}",
            parse_mode="Markdown",
            reply_markup=resume_keyboard,
        )
    else:
        await _edit_query_message_resilient(
            query,
            "✅ **CLI 引擎已切换**\n\n"
            f"从 `{active_engine}` 切换到 `{requested_engine}`。\n"
            "未发现可恢复的桌面会话，请直接发送消息开始新会话，"
            "或先 `/cd` 到目标目录后再发送。",
            parse_mode="Markdown",
        )

    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    if audit_logger:
        await audit_logger.log_command(
            user_id=query.from_user.id,
            command="engine_callback",
            args=[requested_engine],
            success=True,
        )


async def handle_provider_callback(
    query, param: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle provider switch callback from /provider selector."""
    from ..utils.cc_switch import CCSwitchManager

    settings: Settings = context.bot_data["settings"]
    user_id = query.from_user.id

    # Permission check
    if settings.allowed_users and user_id not in settings.allowed_users:
        await _edit_query_message_resilient(query, "无权限执行供应商切换。")
        return

    cc_switch: CCSwitchManager | None = context.bot_data.get("cc_switch_manager")
    if not cc_switch or not cc_switch.is_available():
        await _edit_query_message_resilient(query, "cc-switch 不可用。")
        return

    if not param or not param.startswith("switch:"):
        await _edit_query_message_resilient(
            query,
            "无效的供应商切换操作，请重新执行 `/provider`。",
            parse_mode="Markdown",
        )
        return

    provider_id = param.split(":", 1)[1]

    # Check if already current
    current = await cc_switch.get_current_provider("claude")
    if current and current.id == provider_id:
        await _edit_query_message_resilient(
            query,
            f"当前已经是 `{current.name}` 供应商。",
            parse_mode="Markdown",
        )
        return

    # Execute switch
    result = await cc_switch.switch_provider(provider_id, "claude")

    if result.status == "OK":
        # Keep session ID — facade will strip thinking blocks on resume if needed
        _, scope_state = _get_scope_state_for_query(query, context)
        old_session_id = scope_state.get("claude_session_id")
        if old_session_id:
            permission_manager = context.bot_data.get("permission_manager")
            if permission_manager:
                permission_manager.clear_session(old_session_id)

        url_display = result.base_url or "N/A"
        await _edit_query_message_resilient(
            query,
            f"**API 供应商已切换**\n\n"
            f"供应商：`{result.provider_name}`\n"
            f"Base URL：`{url_display}`\n\n"
            "当前会话将自动续接，下一次请求将使用新供应商。",
            parse_mode="Markdown",
        )
    elif result.status == "DEGRADED":
        await _edit_query_message_resilient(
            query,
            f"**供应商切换异常（DEGRADED）**\n\n"
            f"错误：`{result.error}`\n\n"
            "供应商切换功能已禁用，需要手动修复。",
            parse_mode="Markdown",
        )
    else:
        await _edit_query_message_resilient(
            query,
            f"**供应商切换失败**\n\n"
            f"错误：`{result.error}`\n\n"
            "请稍后重试或检查 cc-switch 配置。",
            parse_mode="Markdown",
        )

    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    if audit_logger:
        await audit_logger.log_command(
            user_id=user_id,
            command="provider_callback",
            args=[provider_id],
            success=(result.status == "OK"),
        )


async def _handle_ls_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ls action."""
    settings: Settings = context.bot_data["settings"]
    _, scope_state = _get_scope_state_for_query(query, context)
    current_dir = scope_state.get("current_directory", settings.approved_directory)

    try:
        # List directory contents (similar to /ls command)
        items = []
        directories = []
        files = []

        for item in sorted(current_dir.iterdir()):
            if item.name.startswith("."):
                continue

            # Escape markdown special characters in filenames
            safe_name = _escape_markdown(item.name)

            if item.is_dir():
                directories.append(f"📁 {safe_name}/")
            else:
                try:
                    size = item.stat().st_size
                    size_str = _format_file_size(size)
                    files.append(f"📄 {safe_name} ({size_str})")
                except OSError:
                    files.append(f"📄 {safe_name}")

        items = directories + files
        relative_path = current_dir.relative_to(settings.approved_directory)

        if not items:
            message = f"📂 `{relative_path}/`\n\n_(empty directory)_"
        else:
            message = f"📂 `{relative_path}/`\n\n"
            max_items = 30  # Limit for inline display
            if len(items) > max_items:
                shown_items = items[:max_items]
                message += "\n".join(shown_items)
                message += f"\n\n_... and {len(items) - max_items} more items_"
            else:
                message += "\n".join(items)

        # Add buttons
        keyboard = []
        if current_dir != settings.approved_directory:
            keyboard.append(
                [
                    InlineKeyboardButton("⬆️ Go Up", callback_data="cd:.."),
                    InlineKeyboardButton("🏠 Root", callback_data="cd:/"),
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="action:refresh_ls"),
                InlineKeyboardButton(
                    "📋 Projects", callback_data="action:show_projects"
                ),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard)

        await _edit_query_message_resilient(
            query, message, parse_mode="Markdown", reply_markup=reply_markup
        )

    except Exception as e:
        await _edit_query_message_resilient(
            query, f"❌ Error listing directory: {str(e)}"
        )


async def _handle_start_coding_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle start coding action."""
    await _edit_query_message_resilient(
        query,
        "🚀 **Ready to Code!**\n\n"
        "Send me any message to start coding with Claude:\n\n"
        "**Examples:**\n"
        '• _"Create a Python script that..."_\n'
        '• _"Help me debug this code..."_\n'
        '• _"Explain how this file works..."_\n'
        "• Upload a file for review\n\n"
        "I'm here to help with all your coding needs!",
    )


async def _handle_quick_actions_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle quick actions menu."""
    keyboard = [
        [
            InlineKeyboardButton("🧪 Run Tests", callback_data="quick:test"),
            InlineKeyboardButton("📦 Install Deps", callback_data="quick:install"),
        ],
        [
            InlineKeyboardButton("🎨 Format Code", callback_data="quick:format"),
            InlineKeyboardButton("🔍 Find TODOs", callback_data="quick:find_todos"),
        ],
        [
            InlineKeyboardButton("🔨 Build", callback_data="quick:build"),
            InlineKeyboardButton("🚀 Start Server", callback_data="quick:start"),
        ],
        [
            InlineKeyboardButton("📊 Git Status", callback_data="quick:git_status"),
            InlineKeyboardButton("🔧 Lint Code", callback_data="quick:lint"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="action:new_session")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await _edit_query_message_resilient(
        query,
        "🛠️ **Quick Actions**\n\n"
        "Choose a common development task:\n\n"
        "_Note: These will be fully functional once Claude Code integration is complete._",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )


async def _handle_refresh_status_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle refresh status action."""
    await _handle_status_action(query, context)


async def _handle_recent_cd_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle recent_cd action -- refresh recent projects list for /cd."""
    settings: Settings = context.bot_data["settings"]
    _, scope_state = _get_scope_state_for_query(query, context)
    active_engine = get_active_cli_engine(scope_state)
    current_dir = scope_state.get("current_directory", settings.approved_directory)

    try:
        recent = scan_recent_projects(settings.approved_directory)
        if recent:
            text, markup = build_recent_projects_message(
                recent_projects=recent,
                current_directory=current_dir,
                approved_directory=settings.approved_directory,
                active_engine=active_engine,
            )
            await _edit_query_message_resilient(
                query,
                text,
                parse_mode="Markdown",
                reply_markup=markup,
            )
        else:
            await _edit_query_message_resilient(
                query,
                "No recent projects found.\n\nUse `/cd <path>` to navigate directly.",
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error("Failed to scan recent projects in callback", error=str(e))
        await _edit_query_message_resilient(
            query, f"Failed to load recent projects: {str(e)}"
        )


async def _handle_refresh_ls_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle refresh ls action."""
    await _handle_ls_action(query, context)


async def _handle_export_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle export action."""
    _, scope_state = _get_scope_state_for_query(query, context)
    session_lifecycle = context.bot_data.get("session_lifecycle_service") or (
        SessionLifecycleService(
            permission_manager=context.bot_data.get("permission_manager")
        )
    )
    session_interaction = (
        context.bot_data.get("session_interaction_service")
        or SessionInteractionService()
    )
    features = context.bot_data.get("features")
    session_exporter = features.get_session_export() if features else None

    if not session_exporter:
        await _edit_query_message_resilient(
            query,
            session_interaction.build_export_unavailable_text(for_callback=True),
            parse_mode="Markdown",
        )
        return

    claude_session_id = session_lifecycle.get_active_session_id(scope_state)
    if not claude_session_id:
        await _edit_query_message_resilient(
            query,
            session_interaction.build_export_no_active_session_text(),
            parse_mode="Markdown",
        )
        return

    export_selector = session_interaction.build_export_selector_message(
        claude_session_id
    )

    await _edit_query_message_resilient(
        query,
        export_selector.text,
        parse_mode="Markdown",
        reply_markup=build_reply_markup_from_spec(export_selector.keyboard),
    )


async def handle_quick_action_callback(
    query, action_id: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle quick action callbacks."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = _get_scope_state_for_query(query, context)

    # Get quick actions manager from bot data if available
    quick_actions = context.bot_data.get("quick_actions")

    if not quick_actions:
        await _edit_query_message_resilient(
            query,
            "❌ **Quick Actions Not Available**\n\n"
            "Quick actions feature is not available.",
        )
        return

    # Get Claude integration
    active_engine, cli_integration = get_cli_integration(
        bot_data=context.bot_data,
        scope_state=scope_state,
    )
    if not cli_integration:
        await _edit_query_message_resilient(
            query,
            "❌ **CLI 引擎不可用**\n\n" "当前引擎未正确配置，请联系管理员检查配置。",
        )
        return

    current_dir = scope_state.get("current_directory", settings.approved_directory)
    typing_stop_event = asyncio.Event()
    typing_heartbeat_task: asyncio.Task[None] | None = None

    try:
        # Get the action from the manager
        action = quick_actions.actions.get(action_id)
        if not action:
            await _edit_query_message_resilient(
                query,
                f"❌ **Action Not Found**\n\n"
                f"Quick action '{action_id}' is not available.",
            )
            return

        # Execute the action
        await _edit_query_message_resilient(
            query,
            f"🚀 **Executing {action.icon} {action.name}**\n\n"
            f"Running quick action in directory: `{current_dir.relative_to(settings.approved_directory)}/`\n\n"
            f"Please wait...",
            parse_mode="Markdown",
        )

        message_obj = getattr(query, "message", None)
        chat_obj = getattr(message_obj, "chat", None)
        chat_id = getattr(message_obj, "chat_id", None)
        if not isinstance(chat_id, int):
            chat_id = getattr(chat_obj, "id", None)
        if isinstance(chat_id, int):
            typing_heartbeat_task = asyncio.create_task(
                _send_chat_action_heartbeat(
                    bot=context.bot,
                    chat_id=chat_id,
                    action="typing",
                    stop_event=typing_stop_event,
                    message_thread_id=getattr(message_obj, "message_thread_id", None),
                    chat_type=getattr(chat_obj, "type", None),
                )
            )

        # Run the action through Claude, using scoped session to prevent
        # cross-topic leakage via facade auto-resume.
        session_id = scope_state.get("claude_session_id")
        force_new = scope_state.get("force_new_session", False)
        claude_response = await cli_integration.run_command(
            prompt=action.prompt,
            working_directory=current_dir,
            user_id=user_id,
            session_id=session_id,
            force_new_session=force_new,
            permission_handler=build_permission_handler(
                bot=context.bot,
                chat_id=query.message.chat_id,
                settings=settings,
                chat_type=getattr(getattr(query.message, "chat", None), "type", None),
                message_thread_id=getattr(query.message, "message_thread_id", None),
            ),
            model=_resolve_model_override(scope_state, active_engine, cli_integration),
        )

        if claude_response:
            # Write back session_id and consume flag only on success
            scope_state["claude_session_id"] = claude_response.session_id
            scope_state.pop("force_new_session", None)
            # Format and send the response
            response_text = claude_response.content
            if len(response_text) > 4000:
                response_text = response_text[:4000] + "...\n\n_(Response truncated)_"

            await _reply_query_message_resilient(
                query,
                context,
                f"✅ **{action.icon} {action.name} Complete**\n\n{response_text}\n\n`Engine: {active_engine}`",
                parse_mode="Markdown",
            )
        else:
            await _edit_query_message_resilient(
                query,
                f"❌ **Action Failed**\n\n"
                f"Failed to execute {action.name}. Please try again.",
            )

    except Exception as e:
        logger.error("Quick action execution failed", error=str(e), user_id=user_id)
        await _edit_query_message_resilient(
            query,
            f"❌ **Action Error**\n\n"
            f"An error occurred while executing {action_id}: {str(e)}",
        )
    finally:
        typing_stop_event.set()
        if typing_heartbeat_task and not typing_heartbeat_task.done():
            typing_heartbeat_task.cancel()
            try:
                await typing_heartbeat_task
            except asyncio.CancelledError:
                pass


async def handle_followup_callback(
    query, suggestion_hash: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle follow-up suggestion callbacks."""
    user_id = query.from_user.id

    # Get conversation enhancer from bot data if available
    conversation_enhancer = context.bot_data.get("conversation_enhancer")

    if not conversation_enhancer:
        await _edit_query_message_resilient(
            query,
            "❌ **Follow-up Not Available**\n\n"
            "Conversation enhancement features are not available.",
        )
        return

    try:
        # Get stored suggestions (this would need to be implemented in the enhancer)
        # For now, we'll provide a generic response
        await _edit_query_message_resilient(
            query,
            "💡 **Follow-up Suggestion Selected**\n\n"
            "This follow-up suggestion will be implemented once the conversation "
            "enhancement system is fully integrated with the message handler.\n\n"
            "**Current Status:**\n"
            "• Suggestion received ✅\n"
            "• Integration pending 🔄\n\n"
            "_You can continue the conversation by sending a new message._",
        )

        logger.info(
            "Follow-up suggestion selected",
            user_id=user_id,
            suggestion_hash=suggestion_hash,
        )

    except Exception as e:
        logger.error(
            "Error handling follow-up callback",
            error=str(e),
            user_id=user_id,
            suggestion_hash=suggestion_hash,
        )

        await _edit_query_message_resilient(
            query,
            "❌ **Error Processing Follow-up**\n\n"
            "An error occurred while processing your follow-up suggestion.",
        )


async def handle_conversation_callback(
    query, action_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle conversation control callbacks."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = _get_scope_state_for_query(query, context)

    if action_type == "continue":
        # Remove suggestion buttons and show continue message
        await _edit_query_message_resilient(
            query,
            "✅ **Continuing Conversation**\n\n"
            "Send me your next message to continue coding!\n\n"
            "I'm ready to help with:\n"
            "• Code review and debugging\n"
            "• Feature implementation\n"
            "• Architecture decisions\n"
            "• Testing and optimization\n"
            "• Documentation\n\n"
            "_Just type your request or upload files._",
        )

    elif action_type == "end":
        # End the current session
        conversation_enhancer = context.bot_data.get("conversation_enhancer")
        if conversation_enhancer:
            conversation_enhancer.clear_context(user_id)

        session_lifecycle = context.bot_data.get("session_lifecycle_service") or (
            SessionLifecycleService(
                permission_manager=context.bot_data.get("permission_manager")
            )
        )
        session_interaction = (
            context.bot_data.get("session_interaction_service")
            or SessionInteractionService()
        )
        session_lifecycle.end_session(scope_state)

        current_dir = scope_state.get("current_directory", settings.approved_directory)
        end_message = session_interaction.build_end_success_message(
            current_dir=current_dir,
            approved_directory=settings.approved_directory,
            for_callback=True,
            title="Conversation Ended",
        )

        await _edit_query_message_resilient(
            query,
            end_message.text,
            parse_mode="Markdown",
            reply_markup=build_reply_markup_from_spec(end_message.keyboard),
        )

        logger.info("Conversation ended via callback", user_id=user_id)

    else:
        await _edit_query_message_resilient(
            query,
            f"❌ **Unknown Conversation Action: {action_type}**\n\n"
            "This conversation action is not recognized.",
        )


async def handle_git_callback(
    query, git_action: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle git-related callbacks."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = _get_scope_state_for_query(query, context)
    features = context.bot_data.get("features")

    if not features or not features.is_enabled("git"):
        await _edit_query_message_resilient(
            query,
            "❌ **Git Integration Disabled**\n\n"
            "Git integration feature is not enabled.",
        )
        return

    current_dir = scope_state.get("current_directory", settings.approved_directory)

    try:
        git_integration = features.get_git_integration()
        if not git_integration:
            await _edit_query_message_resilient(
                query,
                "❌ **Git Integration Unavailable**\n\n"
                "Git integration service is not available.",
            )
            return

        if git_action == "status":
            # Refresh git status
            git_status = await git_integration.get_status(current_dir)
            status_message = git_integration.format_status(git_status)

            keyboard = [
                [
                    InlineKeyboardButton("📊 Show Diff", callback_data="git:diff"),
                    InlineKeyboardButton("📜 Show Log", callback_data="git:log"),
                ],
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data="git:status"),
                    InlineKeyboardButton("📁 Files", callback_data="action:ls"),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await _edit_query_message_resilient(
                query, status_message, parse_mode="Markdown", reply_markup=reply_markup
            )

        elif git_action == "diff":
            # Show git diff
            diff_output = await git_integration.get_diff(current_dir)

            if not diff_output.strip():
                diff_message = "📊 **Git Diff**\n\n_No changes to show._"
            else:
                # Clean up diff output for Telegram
                # Remove emoji symbols that interfere with markdown parsing
                clean_diff = (
                    diff_output.replace("➕", "+").replace("➖", "-").replace("📍", "@")
                )

                # Limit diff output
                max_length = 2000
                if len(clean_diff) > max_length:
                    clean_diff = (
                        clean_diff[:max_length] + "\n\n_... output truncated ..._"
                    )

                diff_message = f"📊 **Git Diff**\n\n```\n{clean_diff}\n```"

            keyboard = [
                [
                    InlineKeyboardButton("📜 Show Log", callback_data="git:log"),
                    InlineKeyboardButton("📊 Status", callback_data="git:status"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await _edit_query_message_resilient(
                query, diff_message, parse_mode="Markdown", reply_markup=reply_markup
            )

        elif git_action == "log":
            # Show git log
            commits = await git_integration.get_file_history(current_dir, ".")

            if not commits:
                log_message = "📜 **Git Log**\n\n_No commits found._"
            else:
                log_message = "📜 **Git Log**\n\n"
                for commit in commits[:10]:  # Show last 10 commits
                    short_hash = commit.hash[:7]
                    short_message = commit.message[:60]
                    if len(commit.message) > 60:
                        short_message += "..."
                    log_message += f"• `{short_hash}` {short_message}\n"

            keyboard = [
                [
                    InlineKeyboardButton("📊 Show Diff", callback_data="git:diff"),
                    InlineKeyboardButton("📊 Status", callback_data="git:status"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await _edit_query_message_resilient(
                query, log_message, parse_mode="Markdown", reply_markup=reply_markup
            )

        else:
            await _edit_query_message_resilient(
                query,
                f"❌ **Unknown Git Action: {git_action}**\n\n"
                "This git action is not recognized.",
            )

    except Exception as e:
        logger.error(
            "Error in git callback",
            error=str(e),
            git_action=git_action,
            user_id=user_id,
        )
        await _edit_query_message_resilient(query, f"❌ **Git Error**\n\n{str(e)}")


async def handle_export_callback(
    query, export_format: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle export format selection callbacks."""
    user_id = query.from_user.id
    _, scope_state = _get_scope_state_for_query(query, context)
    features = context.bot_data.get("features")

    if export_format == "cancel":
        await _edit_query_message_resilient(
            query, "📤 **Export Cancelled**\n\n" "Session export has been cancelled."
        )
        return

    parsed_format = _parse_export_format(export_format)
    if not parsed_format:
        await _edit_query_message_resilient(
            query,
            "❌ **Invalid Export Format**\n\n"
            f"Unsupported export format: `{export_format}`",
            parse_mode="Markdown",
        )
        return

    session_exporter = features.get_session_export() if features else None
    if not session_exporter:
        await _edit_query_message_resilient(
            query,
            "❌ **Export Unavailable**\n\n" "Session export service is not available.",
        )
        return

    # Get current session
    claude_session_id = scope_state.get("claude_session_id")
    if not claude_session_id:
        await _edit_query_message_resilient(
            query, "❌ **No Active Session**\n\n" "There's no active session to export."
        )
        return

    try:
        # Show processing message
        await _edit_query_message_resilient(
            query,
            f"📤 **Exporting Session**\n\n"
            f"Generating {parsed_format.value.upper()} export...",
            parse_mode="Markdown",
        )

        # Export session
        exported_session = await session_exporter.export_session(
            user_id=user_id,
            session_id=claude_session_id,
            format=parsed_format,
        )

        # Send the exported file
        from io import BytesIO

        file_bytes = BytesIO(exported_session.content.encode("utf-8"))
        file_bytes.name = exported_session.filename

        await query.message.reply_document(
            document=file_bytes,
            filename=exported_session.filename,
            caption=(
                f"📤 **Session Export Complete**\n\n"
                f"Format: {exported_session.format.value.upper()}\n"
                f"Size: {exported_session.size_bytes:,} bytes\n"
                f"Created: {exported_session.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            parse_mode="Markdown",
        )

        # Update the original message
        await _edit_query_message_resilient(
            query,
            f"✅ **Export Complete**\n\n"
            f"Your session has been exported as {exported_session.filename}.\n"
            f"Check the file above for your complete conversation history.",
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(
            "Export failed", error=str(e), user_id=user_id, format=export_format
        )
        await _edit_query_message_resilient(query, f"❌ **Export Failed**\n\n{str(e)}")


async def handle_permission_callback(
    query, param: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle tool permission button callbacks."""
    user_id = query.from_user.id
    approval_service = context.bot_data.get("approval_service") or ApprovalService()
    permission_manager = context.bot_data.get("permission_manager")
    result = approval_service.resolve_callback(
        param=param,
        user_id=user_id,
        permission_manager=permission_manager,
    )

    try:
        await _edit_query_message_resilient(
            query,
            result.message,
            parse_mode=result.parse_mode,
        )
    except Exception as markdown_error:
        if result.parse_mode:
            logger.warning(
                "Permission callback markdown render failed; fallback to plain text",
                error=str(markdown_error),
                user_id=user_id,
                request_id=result.request_id,
                decision=result.decision,
            )
            await _edit_query_message_resilient(query, result.message)
        else:
            raise

    if not result.ok:
        return

    logger.info(
        "Permission callback handled",
        user_id=user_id,
        request_id=result.request_id,
        decision=result.decision,
    )


async def handle_thinking_callback(
    query, param: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle thinking expand/collapse callbacks."""
    if not param or ":" not in param:
        await _edit_query_message_resilient(query, "Invalid thinking callback data.")
        return

    action, message_id = param.split(":", 1)
    cache_key = f"thinking:{message_id}"
    user_data = context.user_data if isinstance(context.user_data, dict) else {}
    cached = user_data.get(cache_key)

    if not cached:
        await _edit_query_message_resilient(
            query, "Thinking process cache has expired and cannot be expanded."
        )
        return

    def _is_noop_edit_error(error: Exception) -> bool:
        """Whether Telegram rejected edit because target text is unchanged."""
        return "message is not modified" in str(error).lower()

    async def _edit_with_markdown_fallback(
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        """Try Markdown first, then fallback to plain text if entity parsing fails."""
        try:
            await _edit_query_message_resilient(
                query,
                text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            return True
        except Exception as markdown_error:
            if _is_noop_edit_error(markdown_error):
                return True
            logger.warning(
                "Thinking callback markdown render failed; fallback to plain text",
                error=str(markdown_error),
                action=action,
            )
            try:
                await _edit_query_message_resilient(
                    query,
                    text,
                    reply_markup=reply_markup,
                )
                return True
            except Exception as plain_error:
                if _is_noop_edit_error(plain_error):
                    return True
                logger.warning(
                    "Thinking callback plain render failed",
                    error=str(plain_error),
                    action=action,
                )
                return False

    if action == "expand":
        full_text = _join_progress_lines_for_display(cached["lines"])

        # Truncate if exceeds Telegram limit
        if len(full_text) > 3800:
            full_text = _truncate_thinking(cached["lines"], max_chars=3800)

        collapse_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Collapse",
                        callback_data=f"thinking:collapse:{message_id}",
                    )
                ]
            ]
        )
        if await _edit_with_markdown_fallback(
            full_text,
            reply_markup=collapse_keyboard,
        ):
            return

        # Secondary fallback: stricter truncation for edge cases.
        compact_text = _truncate_thinking(cached["lines"], max_chars=2400)
        if await _edit_with_markdown_fallback(
            compact_text,
            reply_markup=collapse_keyboard,
        ):
            return

        await _edit_query_message_resilient(
            query,
            "Unable to expand thinking details right now. "
            "The content may be too long or the message has expired.",
        )

    elif action == "collapse":
        expand_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "View thinking process",
                        callback_data=f"thinking:expand:{message_id}",
                    )
                ]
            ]
        )
        if await _edit_with_markdown_fallback(
            cached["summary"],
            reply_markup=expand_keyboard,
        ):
            return
        await _edit_query_message_resilient(
            query, cached["summary"], reply_markup=expand_keyboard
        )

    else:
        await _edit_query_message_resilient(query, "Unknown thinking action.")


def _truncate_thinking(lines: list[str], max_chars: int = 3800) -> str:
    """Keep recent progress lines from the end, total length under max_chars."""
    result: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) + 2 > max_chars - 50:
            break
        result.insert(0, line)
        total += len(line) + 2

    skipped = len(lines) - len(result)
    if skipped > 0:
        result.insert(0, f"... ({skipped} earlier entries omitted)")

    return _join_progress_lines_for_display(result)


def _format_file_size(size: int) -> str:
    """Format file size in human-readable format."""
    size_value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if size_value < 1024:
            return f"{size_value:.1f}{unit}" if unit != "B" else f"{int(size_value)}B"
        size_value /= 1024
    return f"{size_value:.1f}TB"


async def handle_resume_callback(query, param, context):
    """Handle resume:* callback queries.

    Callback data format: resume:<sub>:<token>
    Sub-actions:
    - p (project), s (session), f (force-confirm)
    - n (start new session in selected project)
    - show_all, show_recent, cancel
    """
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    token_mgr = _get_or_create_resume_token_manager(context)

    # Handle non-token sub-actions first.
    _, scope_state = _get_scope_state_for_query(query, context)
    active_engine = get_active_cli_engine(scope_state)
    show_sub = None
    show_engine = None
    if param in {"show_all", "show_recent"}:
        show_sub = param
    elif param and param.startswith("show_all:"):
        show_sub = "show_all"
        show_engine = normalize_cli_engine(param.split(":", 1)[1])
    elif param and param.startswith("show_recent:"):
        show_sub = "show_recent"
        show_engine = normalize_cli_engine(param.split(":", 1)[1])

    if show_sub is not None:
        target_engine = show_engine or active_engine
        scanner = _get_or_create_resume_scanner(
            context=context,
            settings=settings,
            engine=target_engine,
        )
        await _resume_render_project_list(
            query=query,
            user_id=user_id,
            scanner=scanner,
            token_mgr=token_mgr,
            settings=settings,
            context=context,
            show_all=(show_sub == "show_all"),
            engine=target_engine,
        )
        return

    # Parse tokenized sub-action: "p:<token>" / "s:<token>" / "f:<token>"
    if not param or ":" not in param:
        if param == "cancel":
            await _edit_query_message_resilient(query, "Resume cancelled.")
            return
        await _edit_query_message_resilient(
            query, "Invalid resume action. Please run /resume again."
        )
        return

    sub, token = param.split(":", 1)
    preview = token_mgr.resolve(
        kind=sub,
        user_id=user_id,
        token=token,
        consume=False,
    )
    target_engine = (
        normalize_cli_engine((preview or {}).get("engine"))
        if preview
        else ENGINE_CLAUDE
    )
    scanner = _get_or_create_resume_scanner(
        context=context,
        settings=settings,
        engine=target_engine,
    )

    if sub == "p":
        await _resume_select_project(
            query,
            user_id,
            token,
            token_mgr,
            scanner,
            settings,
            context,
            engine=target_engine,
        )
    elif sub == "s":
        await _resume_select_session(
            query,
            user_id,
            token,
            token_mgr,
            scanner,
            settings,
            context,
            engine=target_engine,
        )
    elif sub == "f":
        await _resume_force_confirm(
            query,
            user_id,
            token,
            token_mgr,
            scanner,
            settings,
            context,
            engine=target_engine,
        )
    elif sub == "n":
        await _resume_start_new_session(
            query,
            user_id,
            token,
            token_mgr,
            settings,
            context,
            engine=target_engine,
        )
    elif sub == "cancel":
        await _edit_query_message_resilient(query, "Resume cancelled.")
    else:
        await _edit_query_message_resilient(
            query, "Unknown resume action. Please run /resume again."
        )


async def _resume_render_project_list(
    *,
    query,
    user_id: int,
    scanner,
    token_mgr,
    settings: Settings,
    context: ContextTypes.DEFAULT_TYPE,
    show_all: bool,
    engine: str,
) -> None:
    """Render resume project selection in recent/all modes."""
    projects = await scanner.list_projects()

    if not projects:
        engine_label = _resume_engine_label(engine)
        await _edit_query_message_resilient(
            query,
            f"No desktop {engine_label} sessions found.\n\n"
            f"Run /resume again after using {engine_label} on desktop.",
            parse_mode="Markdown",
        )
        return

    _, scope_state = _get_scope_state_for_query(query, context)
    current_dir = scope_state.get("current_directory")
    message_text, keyboard = build_resume_project_selector(
        projects=projects,
        approved_root=settings.approved_directory,
        token_mgr=token_mgr,
        user_id=user_id,
        current_directory=current_dir,
        show_all=show_all,
        payload_extra={"engine": engine},
        engine=engine,
    )
    await _edit_query_message_resilient(
        query,
        message_text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def _resume_select_project(
    query,
    user_id,
    token,
    token_mgr,
    scanner,
    settings,
    context,
    engine: str,
):
    """S1: User selected a project, show its sessions."""
    payload = token_mgr.resolve(
        kind="p",
        user_id=user_id,
        token=token,
    )
    if payload is None:
        await _edit_query_message_resilient(
            query, "Token expired or invalid. Please run /resume again."
        )
        return

    from pathlib import Path

    project_cwd = Path(payload["cwd"])
    candidates = await scanner.list_sessions(project_cwd=project_cwd)
    payload_engine_raw = payload.get("engine")
    payload_engine = (
        normalize_cli_engine(payload_engine_raw) if payload_engine_raw else engine
    )

    new_session_token = token_mgr.issue(
        kind="n",
        user_id=user_id,
        payload={
            "cwd": str(project_cwd),
            "engine": payload_engine,
        },
    )

    if not candidates:
        empty_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🆕 Start New Session Here",
                        callback_data=f"resume:n:{new_session_token}",
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="resume:cancel")],
            ]
        )
        await _edit_query_message_resilient(
            query,
            f"No sessions found for project:\n"
            f"`{project_cwd.name}`\n\n"
            f"Start a fresh session in this directory or run /resume again.",
            parse_mode="Markdown",
            reply_markup=empty_markup,
        )
        return

    # Build session selection buttons + preview lines
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    session_preview_lines: list[str] = []
    for c in candidates[:10]:  # limit to 10 sessions
        label = _build_resume_session_button_label(c)
        session_preview_lines.append(_build_resume_session_preview_line(c))

        tok = token_mgr.issue(
            kind="s",
            user_id=user_id,
            payload={
                "cwd": str(project_cwd),
                "session_id": c.session_id,
                "is_active": c.is_probably_active,
                "engine": payload_engine,
            },
        )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"resume:s:{tok}",
                )
            ]
        )

    keyboard_rows.append(
        [
            InlineKeyboardButton(
                "🆕 Start New Session Here",
                callback_data=f"resume:n:{new_session_token}",
            )
        ]
    )
    keyboard_rows.append(
        [
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="resume:cancel",
            )
        ]
    )

    try:
        rel_text = str(project_cwd.relative_to(settings.approved_directory))
    except ValueError:
        rel_text = project_cwd.name

    session_preview_text = "\n".join(session_preview_lines)
    await _edit_query_message_resilient(
        query,
        f"**Sessions in** `{rel_text}`\n\n"
        f"Session previews:\n"
        f"{session_preview_text}\n\n"
        f"Select a session to resume, or tap *Start New Session Here*:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
    )


async def _resume_select_session(
    query,
    user_id,
    token,
    token_mgr,
    scanner,
    settings,
    context,
    engine: str,
):
    """S2: User selected a session. Adopt it or ask for force-confirm."""
    payload = token_mgr.resolve(
        kind="s",
        user_id=user_id,
        token=token,
    )
    if payload is None:
        await _edit_query_message_resilient(
            query, "Token expired or invalid. Please run /resume again."
        )
        return

    from pathlib import Path

    project_cwd = Path(payload["cwd"])
    session_id = payload["session_id"]
    is_active = payload.get("is_active", False)
    payload_engine_raw = payload.get("engine")
    payload_engine = (
        normalize_cli_engine(payload_engine_raw) if payload_engine_raw else engine
    )

    # If session appears active, ask for confirmation
    if is_active:
        tok = token_mgr.issue(
            kind="f",
            user_id=user_id,
            payload={
                "cwd": str(project_cwd),
                "session_id": session_id,
                "engine": payload_engine,
            },
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    "Yes, resume anyway",
                    callback_data=f"resume:f:{tok}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Cancel",
                    callback_data="resume:cancel",
                ),
            ],
        ]
        await _edit_query_message_resilient(
            query,
            f"**Session may be active**\n\n"
            f"Session `{session_id[:8]}...` was modified very recently "
            f"and might still be running on your desktop.\n\n"
            f"Resuming it here could cause conflicts.\n"
            f"Continue anyway?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Not active -> adopt directly
    await _do_adopt_session(
        query,
        user_id,
        project_cwd,
        session_id,
        settings,
        context,
        engine=payload_engine,
        scanner=scanner,
    )


async def _resume_force_confirm(
    query,
    user_id,
    token,
    token_mgr,
    scanner,
    settings,
    context,
    engine: str,
):
    """S3: User confirmed force-resume of an active session."""
    payload = token_mgr.resolve(
        kind="f",
        user_id=user_id,
        token=token,
    )
    if payload is None:
        await _edit_query_message_resilient(
            query, "Token expired or invalid. Please run /resume again."
        )
        return

    from pathlib import Path

    project_cwd = Path(payload["cwd"])
    session_id = payload["session_id"]
    payload_engine_raw = payload.get("engine")
    payload_engine = (
        normalize_cli_engine(payload_engine_raw) if payload_engine_raw else engine
    )

    await _do_adopt_session(
        query,
        user_id,
        project_cwd,
        session_id,
        settings,
        context,
        engine=payload_engine,
        scanner=scanner,
    )


async def _resume_start_new_session(
    query,
    user_id,
    token,
    token_mgr,
    settings,
    context,
    engine: str,
):
    """Start a fresh session in selected project without resuming old sessions."""
    payload = token_mgr.resolve(
        kind="n",
        user_id=user_id,
        token=token,
    )
    if payload is None:
        await _edit_query_message_resilient(
            query, "Token expired or invalid. Please run /resume again."
        )
        return

    project_cwd = Path(payload["cwd"])
    payload_engine_raw = payload.get("engine")
    payload_engine = (
        normalize_cli_engine(payload_engine_raw) if payload_engine_raw else engine
    )

    try:
        resolved = project_cwd.resolve()
        if not resolved.is_relative_to(settings.approved_directory.resolve()):
            await _edit_query_message_resilient(
                query,
                "Path is outside the approved directory. Cannot start a new session.",
            )
            return
        project_cwd = resolved
    except (OSError, ValueError):
        await _edit_query_message_resilient(
            query, "Invalid project path. Please run /resume again."
        )
        return

    _, scope_state = _get_scope_state_for_query(query, context)
    old_session_id = scope_state.get("claude_session_id")

    set_active_cli_engine(scope_state, payload_engine)
    scope_state["current_directory"] = project_cwd
    scope_state["claude_session_id"] = None
    scope_state["session_started"] = True
    scope_state["force_new_session"] = True

    if old_session_id:
        permission_manager = context.bot_data.get("permission_manager")
        if permission_manager:
            permission_manager.clear_session(old_session_id)

    await _sync_chat_menu_for_engine(
        context=context,
        chat_id=_get_query_chat_id(query),
        engine=payload_engine,
    )

    try:
        rel_text = str(project_cwd.relative_to(settings.approved_directory))
    except ValueError:
        rel_text = project_cwd.name

    keyboard = [
        [
            InlineKeyboardButton(
                "Start Coding",
                callback_data="action:start_coding",
            ),
            InlineKeyboardButton(
                "Context",
                callback_data="action:context",
            ),
        ]
    ]

    await _edit_query_message_resilient(
        query,
        f"**New Session Ready**\n\n"
        f"Engine: `{payload_engine}`\n"
        f"Directory: `{rel_text}/`\n\n"
        f"Old session binding was cleared.\n"
        f"Send a message to start a fresh session now.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    if audit_logger:
        await audit_logger.log_command(
            user_id=user_id,
            command="resume_new",
            args=[rel_text],
            success=True,
        )


async def _do_adopt_session(
    query,
    user_id,
    project_cwd,
    session_id,
    settings,
    context,
    engine: str,
    scanner=None,
):
    """S4: Actually adopt the session and switch cwd."""
    # Defensive: verify project_cwd is under approved_directory
    try:
        resolved = project_cwd.resolve()
        if not resolved.is_relative_to(settings.approved_directory.resolve()):
            await _edit_query_message_resilient(
                query, "Path is outside the approved directory. Cannot adopt session."
            )
            return
        project_cwd = resolved
    except (OSError, ValueError):
        await _edit_query_message_resilient(
            query, "Invalid project path. Please run /resume again."
        )
        return

    integrations = context.bot_data.get("cli_integrations") or {}
    cli_integration: ClaudeIntegration | None = integrations.get(
        engine
    ) or integrations.get(ENGINE_CLAUDE)
    if cli_integration is None:
        cli_integration = context.bot_data.get("claude_integration")

    if not cli_integration or not cli_integration.session_manager:
        engine_label = _resume_engine_label(engine)
        await _edit_query_message_resilient(
            query, f"{engine_label} integration not available. Cannot adopt session."
        )
        return

    try:
        engine_label = _resume_engine_label(engine)
        await _edit_query_message_resilient(
            query,
            f"Adopting {engine_label} session `{session_id[:8]}...`\n"
            f"Please wait...",
            parse_mode="Markdown",
        )

        adopted = await cli_integration.session_manager.adopt_external_session(
            user_id=user_id,
            project_path=project_cwd,
            external_session_id=session_id,
        )

        # Switch user's working directory and session
        _, scope_state = _get_scope_state_for_query(query, context)
        set_active_cli_engine(scope_state, engine)
        scope_state["current_directory"] = project_cwd
        scope_state["claude_session_id"] = adopted.session_id
        history_preview: list[ResumeHistoryMessage] = []
        preview_limit_raw = getattr(settings, "resume_history_preview_count", 6)
        try:
            preview_limit = int(preview_limit_raw)
        except (TypeError, ValueError):
            preview_limit = 6
        preview_limit = max(0, min(preview_limit, 20))

        if preview_limit > 0:
            storage = context.bot_data.get("storage")
            candidate_session_ids: list[str] = [adopted.session_id]
            if session_id and session_id != adopted.session_id:
                candidate_session_ids.append(session_id)

            for candidate_session_id in candidate_session_ids:
                try:
                    history_preview = await load_resume_history_preview(
                        session_id=candidate_session_id,
                        user_id=user_id,
                        project_cwd=project_cwd,
                        engine=engine,
                        limit=preview_limit,
                        storage=storage,
                        scanner=scanner,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to load resume history preview",
                        session_id=candidate_session_id,
                        engine=engine,
                        error=str(exc),
                    )
                    history_preview = []
                if history_preview:
                    break
        message_obj = getattr(query, "message", None)
        chat_id = getattr(message_obj, "chat_id", None)
        if not isinstance(chat_id, int):
            chat_id = getattr(getattr(message_obj, "chat", None), "id", None)
        await _sync_chat_menu_for_engine(
            context=context,
            chat_id=chat_id,
            engine=engine,
        )

        try:
            rel = project_cwd.relative_to(settings.approved_directory)
        except ValueError:
            rel = project_cwd.name

        keyboard = [
            [
                InlineKeyboardButton(
                    "Start Coding",
                    callback_data="action:start_coding",
                ),
                InlineKeyboardButton(
                    "Context",
                    callback_data="action:context",
                ),
            ],
        ]
        history_block = ""
        if history_preview:
            history_block = "\n\n" + _build_resume_history_preview_text(history_preview)

        await _edit_query_message_resilient(
            query,
            f"**Session Resumed**\n\n"
            f"Engine: `{engine}`\n"
            f"Session: `{adopted.session_id[:8]}...`\n"
            f"Directory: `{rel}/`"
            f"{history_block}\n\n"
            f"Send a message to continue where you left off.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="resume",
                args=[session_id[:8]],
                success=True,
            )

        logger.info(
            "Desktop session adopted",
            user_id=user_id,
            session_id=session_id,
            project=str(project_cwd),
            engine=engine,
        )

    except Exception as e:
        logger.error(
            "Failed to adopt desktop session",
            error=str(e),
            user_id=user_id,
            session_id=session_id,
        )
        await _edit_query_message_resilient(
            query,
            f"**Failed to Resume Session**\n\n"
            f"Error: `{str(e)}`\n\n"
            f"Please run /resume to try again.",
            parse_mode="Markdown",
        )
