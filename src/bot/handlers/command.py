"""Command handlers for bot operations."""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...claude.task_registry import TaskRegistry
from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...security.validators import SecurityValidator
from ...services.cron_scheduler_service import (
    CRON_JOB_TYPE_AI_PROMPT,
    CRON_JOB_TYPE_REMINDER,
    CronSchedulerService,
    CronValidationError,
)
from ...services.session_interaction_service import SessionInteractionService
from ...services.session_lifecycle_service import SessionLifecycleService
from ...services.session_service import SessionService
from ..inbound_task_queue import InboundTaskQueue
from ..utils.cli_engine import (
    ENGINE_CLAUDE,
    ENGINE_CODEX,
    SUPPORTED_CLI_ENGINES,
    get_active_cli_engine,
    get_cli_integration,
    get_engine_capabilities,
    get_engine_primary_status_command,
    normalize_cli_engine,
    set_active_cli_engine,
)
from ..utils.command_menu import sync_chat_command_menu
from ..utils.recent_projects import build_recent_projects_message, scan_recent_projects
from ..utils.resume_ui import build_resume_project_selector
from ..utils.scope_state import get_scope_state_from_update
from ..utils.telegram_send import (
    is_markdown_parse_error,
    is_thread_not_found_error,
    normalize_message_thread_id,
    prepare_telegram_text_and_parse_mode,
    send_message_resilient,
)
from ..utils.ui_adapter import build_reply_markup_from_spec
from .message import build_permission_handler

logger = structlog.get_logger()
_PARSE_MODE_UNSET = object()
_CHAT_ACTION_HEARTBEAT_INTERVAL_SECONDS = 4.0
_SENDPIC_ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _require_effective_user(update: Update) -> Any:
    """Return effective user or raise for unsupported updates."""
    user = update.effective_user
    if user is None:
        raise ValueError("Missing effective_user in update")
    return user


def _require_effective_chat(update: Update) -> Any:
    """Return effective chat or raise for unsupported updates."""
    chat = update.effective_chat
    if chat is None:
        raise ValueError("Missing effective_chat in update")
    return chat


async def _reply_update_message_resilient(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    reply_to_message_id: int | None = None,
) -> Any:
    """Reply to update message with fallback to resilient send helper."""
    original_text = str(text or "")
    prepared_text, prepared_parse_mode, _ = prepare_telegram_text_and_parse_mode(
        original_text, parse_mode
    )

    message = getattr(update, "message", None)
    if message is None:
        return None

    send_kwargs: dict[str, Any] = {}
    chat_type = getattr(update.effective_chat, "type", None)
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
        chat = getattr(update, "effective_chat", None)
        chat_id = getattr(chat, "id", None)
        if bot is None or not isinstance(chat_id, int):
            raise

        return await send_message_resilient(
            bot=bot,
            chat_id=chat_id,
            text=original_text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=getattr(
                update.effective_message, "message_thread_id", None
            ),
            chat_type=chat_type,
        )


async def _set_update_message_reaction_safe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    emoji: str,
) -> bool:
    """Best-effort command feedback via reaction to avoid extra chat bubbles."""
    bot = getattr(context, "bot", None)
    message = getattr(update, "message", None)
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(message, "message_id", None)
    if (
        bot is None
        or not hasattr(bot, "set_message_reaction")
        or not isinstance(chat_id, int)
        or not isinstance(message_id, int)
    ):
        return False

    normalized_emoji = str(emoji or "").strip()
    reaction_payload: list[str] = [normalized_emoji] if normalized_emoji else []
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=reaction_payload,
            is_big=False,
        )
        return True
    except Exception as error:
        logger.debug(
            "Failed to set command reaction feedback",
            chat_id=chat_id,
            message_id=message_id,
            emoji=normalized_emoji or None,
            error=str(error),
        )
        return False


def _is_noop_edit_error(error: Exception) -> bool:
    """Whether Telegram rejected edit because target text is unchanged."""
    return "message is not modified" in str(error).lower()


async def _edit_message_resilient(
    message: Any,
    text: str,
    *,
    parse_mode: str | None | object = _PARSE_MODE_UNSET,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Any:
    """Edit message with markdown/no-op fallback."""
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
        return await message.edit_text(prepared_text, **edit_kwargs)
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
                    return await message.edit_text(original_text, **markdown_kwargs)
                except Exception as markdown_error:
                    if _is_noop_edit_error(markdown_error):
                        return None

            fallback_kwargs = dict(edit_kwargs)
            fallback_kwargs.pop("parse_mode", None)
            try:
                return await message.edit_text(original_text, **fallback_kwargs)
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
    """Keep Telegram chat action visible during long-running command handling."""
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
                "Failed to send command chat action heartbeat",
                action=action,
                error=str(e),
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            continue


def _is_http_photo_source(raw_source: str) -> bool:
    """Whether source is an http/https URL."""
    parsed = urlparse(str(raw_source or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _resolve_sendpic_local_path(
    *,
    source: str,
    current_directory: Path,
    approved_directory: Path,
    security_validator: Optional[SecurityValidator],
) -> tuple[Optional[Path], Optional[str]]:
    """Resolve and validate local image path for /sendpic."""
    normalized_source = str(source or "").strip()
    if not normalized_source:
        return None, "图片路径不能为空。"

    resolved_path: Optional[Path] = None
    if security_validator:
        valid, candidate_path, error = security_validator.validate_path(
            normalized_source, current_directory
        )
        if not valid or candidate_path is None:
            return None, error or "图片路径不合法。"
        resolved_path = candidate_path
    else:
        candidate = Path(normalized_source).expanduser()
        if not candidate.is_absolute():
            candidate = (current_directory / candidate).resolve()
        else:
            candidate = candidate.resolve()
        try:
            candidate.relative_to(approved_directory.resolve())
        except ValueError:
            return None, "Access denied: path outside approved directory"
        resolved_path = candidate

    if resolved_path is None:
        return None, "无法解析图片路径。"

    if not resolved_path.exists() or not resolved_path.is_file():
        return None, f"图片文件不存在：`{resolved_path}`"

    suffix = resolved_path.suffix.lower()
    if suffix not in _SENDPIC_ALLOWED_SUFFIXES:
        allowed = ", ".join(sorted(_SENDPIC_ALLOWED_SUFFIXES))
        return None, f"仅支持图片扩展名：{allowed}"

    return resolved_path, None


def _find_latest_sendpic_image(
    *,
    current_directory: Path,
    approved_directory: Path,
) -> Optional[Path]:
    """Find latest image under current workspace (and .claude-images if present)."""
    approved_root = approved_directory.resolve()
    candidate_roots: list[Path] = [
        current_directory.resolve(),
        approved_root / ".claude-images",
    ]

    best_path: Optional[Path] = None
    best_mtime: float = -1.0
    scanned_roots: set[str] = set()

    for root in candidate_roots:
        root_key = str(root)
        if root_key in scanned_roots:
            continue
        scanned_roots.add(root_key)

        if not root.exists() or not root.is_dir():
            continue
        if not root.is_relative_to(approved_root):
            continue

        for image_path in root.rglob("*"):
            if not image_path.is_file():
                continue
            if image_path.suffix.lower() not in _SENDPIC_ALLOWED_SUFFIXES:
                continue
            try:
                stat = image_path.stat()
            except OSError:
                continue
            if stat.st_mtime > best_mtime:
                best_mtime = stat.st_mtime
                best_path = image_path

    return best_path


async def _send_photo_resilient(
    *,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    photo: Any,
    caption: Optional[str] = None,
    filename: Optional[str] = None,
) -> Any:
    """Send photo with forum-thread fallback."""
    chat = _require_effective_chat(update)
    chat_id = chat.id
    bot = getattr(context, "bot", None)
    if bot is None:
        raise RuntimeError("Telegram bot instance is unavailable")

    chat_type = getattr(chat, "type", None)
    should_quote_reply = str(chat_type or "").strip().lower() != "private"
    reply_to_message_id = getattr(getattr(update, "message", None), "message_id", None)
    normalized_thread_id = normalize_message_thread_id(
        getattr(update.effective_message, "message_thread_id", None),
        chat_type=chat_type,
    )

    send_kwargs: dict[str, Any] = {"chat_id": chat_id, "photo": photo}
    if caption:
        send_kwargs["caption"] = caption
    if filename:
        send_kwargs["filename"] = filename
    if (
        should_quote_reply
        and isinstance(reply_to_message_id, int)
        and reply_to_message_id > 0
    ):
        send_kwargs["reply_to_message_id"] = reply_to_message_id
    if normalized_thread_id is not None:
        send_kwargs["message_thread_id"] = normalized_thread_id

    try:
        return await bot.send_photo(**send_kwargs)
    except Exception as send_error:
        if "message_thread_id" in send_kwargs and is_thread_not_found_error(send_error):
            retry_kwargs = dict(send_kwargs)
            retry_kwargs.pop("message_thread_id", None)
            return await bot.send_photo(**retry_kwargs)
        raise


def _get_or_create_resume_token_manager(context: ContextTypes.DEFAULT_TYPE) -> Any:
    """Get shared resume token manager from bot_data."""
    from ...bot.resume_tokens import ResumeTokenManager

    token_mgr = context.bot_data.get("resume_token_manager")
    if token_mgr is None:
        token_mgr = ResumeTokenManager()
        context.bot_data["resume_token_manager"] = token_mgr
    return token_mgr


def _get_or_create_resume_scanner(
    *, context: ContextTypes.DEFAULT_TYPE, settings: Settings, engine: str
) -> Any:
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


def _engine_display_name(engine: str) -> str:
    """Human-readable engine name."""
    return "Codex" if engine == ENGINE_CODEX else "Claude"


def _normalize_reasoning_effort_label(raw: str) -> str:
    """Normalize reasoning effort label for user-facing text."""
    mapping = {
        "high": "High",
        "medium": "Medium",
        "low": "Low",
        "xhigh": "X High",
        "x-high": "X High",
    }
    normalized = str(raw or "").strip().lower()
    if not normalized:
        return ""
    return mapping.get(normalized, normalized.title())


def _is_claude_model_name(value: str | None) -> bool:
    """Return whether model id is a Claude alias/name."""
    normalized = str(value or "").strip().lower()
    if not normalized:
        return False
    if normalized in {"sonnet", "opus", "haiku"}:
        return True
    return any(token in normalized for token in ("claude", "sonnet", "opus", "haiku"))


def _build_engine_selector_keyboard(
    *, active_engine: str, available_engines: set[str]
) -> InlineKeyboardMarkup | None:
    """Build inline keyboard for engine switching."""
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


def _build_codex_model_keyboard(
    *, selected_model: str | None, resolved_model: str | None = None
) -> InlineKeyboardMarkup:
    """Build inline keyboard for Codex model selection."""
    selected = str(selected_model or "").strip()
    candidates: list[str] = []
    for candidate in (
        resolved_model,
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


def _is_context_full_mode(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Whether `/context` should render full detail output."""
    args = getattr(context, "args", None) or []
    normalized = [str(arg).strip().lower() for arg in args if str(arg).strip()]
    if not normalized:
        return False

    return normalized[0] in {"full", "all", "verbose", "detail"}


def _split_status_text(text: str, max_length: int = 3900) -> list[str]:
    """Split long status text into Telegram-safe chunks."""
    return SessionInteractionService.split_context_full_text(
        text=text,
        max_length=max_length,
    )


def _build_status_full_payload(
    *,
    relative_path: Path,
    current_model: str | None,
    claude_session_id: str | None,
    precise_context: dict | None,
    info: dict | None,
    resumable_payload: dict | None,
) -> dict:
    """Build full status payload."""
    return SessionInteractionService.build_context_full_payload(
        relative_path=relative_path,
        current_model=current_model,
        claude_session_id=claude_session_id,
        precise_context=precise_context,
        info=info,
        resumable_payload=resumable_payload,
    )


def _render_status_full_text(payload: dict) -> str:
    """Render readable full status plus raw payload JSON."""
    return SessionInteractionService.render_context_full_text(payload)


async def _sync_chat_menu_for_engine(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | None,
    engine: str,
) -> None:
    """Refresh per-chat command menu after engine resolution/switch."""
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
                "Synced chat command menu",
                chat_id=chat_id,
                engine=engine,
                commands=[cmd.command for cmd in commands],
            )
    except Exception as exc:
        logger.warning(
            "Failed to sync chat command menu",
            chat_id=chat_id,
            engine=engine,
            error=str(exc),
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    user = _require_effective_user(update)
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    active_engine = get_active_cli_engine(scope_state)

    await _sync_chat_menu_for_engine(
        context=context,
        chat_id=getattr(update.effective_chat, "id", None),
        engine=active_engine,
    )
    status_command = get_engine_primary_status_command(active_engine)
    if status_command == "status":
        status_line = "• `/status [full]` - Show session status and usage"
        status_hint = "📊 Use `/status` to check your usage limits."
        diagnostics_line = "• `/codexdiag` - Diagnose codex MCP status\n"
        status_button = "📊 Check Status"
        status_button_action = "action:status"
    else:
        status_line = "• `/context [full]` - Show session context and usage"
        status_hint = "📊 Use `/context` to check your usage limits."
        diagnostics_line = ""
        status_button = "📊 Check Context"
        status_button_action = "action:context"

    welcome_message = (
        f"👋 Welcome to CLITG, {user.first_name}!\n\n"
        f"🤖 I help you access CLI coding agents remotely through Telegram.\n\n"
        f"**Available Commands:**\n"
        f"• `/help` - Show detailed help\n"
        f"• `/new` - Start a new session\n"
        f"• `/ls` - List files in current directory\n"
        f"• `/cd <dir>` - Change directory\n"
        f"• `/projects` - Show available projects\n"
        f"{status_line}\n"
        f"• `/engine [claude|codex]` - Switch CLI engine\n"
        f"• `/cancel` - Cancel current running task\n"
        f"• `/queue` - Show queued tasks\n"
        f"• `/dequeue <id>` - Remove one queued task\n"
        f"• `/cron ...` - Manage reminders and scheduled jobs\n"
        f"• `/sendpic [path|url] [caption]` - Send latest/specified image\n"
        f"• `/git` - Git repository commands\n"
        f"{diagnostics_line}\n"
        f"**Quick Start:**\n"
        f"1. Use `/projects` to see available projects\n"
        f"2. Use `/cd <project>` to navigate to a project\n"
        f"3. Send any message to start coding with your active CLI engine!\n\n"
        f"🔒 Your access is secured and all actions are logged.\n"
        f"{status_hint}"
    )

    # Add quick action buttons
    keyboard = [
        [
            InlineKeyboardButton(
                "📁 Show Projects", callback_data="action:show_projects"
            ),
            InlineKeyboardButton("❓ Get Help", callback_data="action:help"),
        ],
        [
            InlineKeyboardButton("🆕 New Session", callback_data="action:new_session"),
            InlineKeyboardButton(status_button, callback_data=status_button_action),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await _reply_update_message_resilient(
        update,
        context,
        welcome_message,
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )

    # Log command
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    if audit_logger:
        await audit_logger.log_command(
            user_id=user.id, command="start", args=[], success=True
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    active_engine = get_active_cli_engine(scope_state)
    capabilities = get_engine_capabilities(active_engine)
    status_command = get_engine_primary_status_command(active_engine)
    if status_command == "status":
        status_line = "• `/status [full]` - Show session status and usage"
        status_alias_line = "• Compatibility alias: `/context [full]`"
        diagnostics_text = (
            "**Diagnostics:**\n"
            "• `/codexdiag` - Diagnose latest codex MCP call in current directory\n"
            "• `/codexdiag root` - Diagnose codex MCP call under approved root\n"
            "• `/codexdiag <session_id>` - Diagnose a specific CLI session\n\n"
        )
        status_hint_line = "• Check `/status` to monitor your usage"
    else:
        status_line = "• `/context [full]` - Show session context and usage"
        status_alias_line = "• Compatibility alias: `/status [full]`"
        diagnostics_text = ""
        status_hint_line = "• Check `/context` to monitor your usage"
    if active_engine == ENGINE_CODEX:
        model_line = "• `/model [name|default]` - View or set Codex model\n"
    elif capabilities.supports_model_selection:
        model_line = "• `/model` - View or switch Claude model\n"
    else:
        model_line = "• `/model` - View current model\n"

    help_text = (
        "🤖 **CLITG Help**\n\n"
        "**Navigation Commands:**\n"
        "• `/ls` - List files and directories\n"
        "• `/cd <directory>` - Change to directory\n"
        "• `/projects` - Show available projects\n\n"
        "**Session Commands:**\n"
        "• `/new` - Clear context and start a fresh session\n"
        f"{status_line}\n"
        f"{status_alias_line}\n"
        "• `/engine [claude|codex]` - Switch active CLI engine\n"
        "• `/provider` - Switch API provider (cc-switch)\n"
        "• `/cancel` - Cancel current running task\n"
        "• `/queue` - Show queued tasks\n"
        "• `/dequeue <id>` - Remove one queued task\n"
        "• `/cron ...` - Manage reminders and scheduled jobs\n"
        "• `/sendpic [path|url] [caption]` - Send latest/specified image\n"
        f"{model_line}"
        "• `/export` - Export session history\n"
        "• `/git` - Git repository information\n\n"
        f"{diagnostics_text}"
        "**Session Behavior:**\n"
        "• Sessions are automatically maintained per project directory\n"
        "• Switching directories with `/cd` resumes the session for that project\n"
        "• Use `/new` to explicitly clear session context\n"
        "• Sessions persist across bot restarts\n\n"
        "**Usage Examples:**\n"
        "• `cd myproject` - Enter project directory\n"
        "• `ls` - See what's in current directory\n"
        "• `Create a simple Python script` - Ask your current engine to code\n"
        "• Send a file to have your current engine review it\n\n"
        "**File Operations:**\n"
        "• Send text files (.py, .js, .md, etc.) for review\n"
        "• CLI engine can read, modify, and create files\n"
        "• All file operations are within your approved directory\n\n"
        "**Security Features:**\n"
        "• 🔒 Path traversal protection\n"
        "• ⏱️ Rate limiting to prevent abuse\n"
        "• 📊 Usage tracking and limits\n"
        "• 🛡️ Input validation and sanitization\n\n"
        "**Tips:**\n"
        "• Use specific, clear requests for best results\n"
        f"{status_hint_line}\n"
        "• File uploads are automatically processed by active engine\n\n"
        "Need more help? Contact your administrator."
    )

    await _reply_update_message_resilient(
        update, context, help_text, parse_mode="Markdown"
    )


async def send_picture_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /sendpic command to send image by local path or URL."""
    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    security_validator: Optional[SecurityValidator] = context.bot_data.get(
        "security_validator"
    )
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")

    current_dir_raw = scope_state.get("current_directory", settings.approved_directory)
    current_directory = (
        Path(current_dir_raw)
        if isinstance(current_dir_raw, (str, Path))
        else settings.approved_directory
    )
    args = [str(arg).strip() for arg in (context.args or []) if str(arg).strip()]
    source = args[0] if args else ""
    caption = " ".join(args[1:]).strip() if len(args) > 1 else ""
    normalized_caption = caption or None

    try:
        if not args:
            latest_image = _find_latest_sendpic_image(
                current_directory=current_directory,
                approved_directory=settings.approved_directory,
            )
            if latest_image is None:
                await _reply_update_message_resilient(
                    update,
                    context,
                    "**用法：** `/sendpic [path_or_url] [caption]`\n\n"
                    "无参数时会自动发送最近一张图片。\n"
                    "未找到可发送图片时，可显式指定路径或 URL。\n\n"
                    "**示例：**\n"
                    "• `/sendpic`（自动发送最近图片）\n"
                    "• `/sendpic assets/logo.png`\n"
                    "• `/sendpic https://example.com/demo.jpg Demo image`",
                    parse_mode="Markdown",
                )
                return

            with latest_image.open("rb") as image_file:
                await _send_photo_resilient(
                    update=update,
                    context=context,
                    photo=image_file,
                    caption=normalized_caption,
                    filename=latest_image.name,
                )
        elif _is_http_photo_source(source):
            await _send_photo_resilient(
                update=update,
                context=context,
                photo=source,
                caption=normalized_caption,
            )
        else:
            resolved_path, path_error = _resolve_sendpic_local_path(
                source=source,
                current_directory=current_directory,
                approved_directory=settings.approved_directory,
                security_validator=security_validator,
            )
            if resolved_path is None:
                await _reply_update_message_resilient(
                    update,
                    context,
                    f"❌ **发送失败**\n\n{path_error or '无效图片路径。'}",
                    parse_mode="Markdown",
                )
                if audit_logger:
                    await audit_logger.log_command(
                        user_id=user_id,
                        command="sendpic",
                        args=context.args or [],
                        success=False,
                    )
                return

            with resolved_path.open("rb") as image_file:
                await _send_photo_resilient(
                    update=update,
                    context=context,
                    photo=image_file,
                    caption=normalized_caption,
                    filename=resolved_path.name,
                )

        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="sendpic",
                args=context.args or [],
                success=True,
            )
    except Exception as error:
        logger.error(
            "sendpic command failed",
            user_id=user_id,
            source=source,
            error=str(error),
        )
        await _reply_update_message_resilient(
            update,
            context,
            "❌ **发送图片失败**\n\n" "请检查路径/URL是否可访问，或稍后重试。",
            parse_mode="Markdown",
        )
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="sendpic",
                args=context.args or [],
                success=False,
            )


async def switch_engine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /engine command to switch active CLI adapter."""
    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")

    active_engine = get_active_cli_engine(scope_state)
    args = [
        str(arg).strip().lower() for arg in (context.args or []) if str(arg).strip()
    ]
    if not args:
        integrations = context.bot_data.get("cli_integrations") or {}
        available_engines = set(
            normalize_cli_engine(name) for name in integrations.keys() if name
        )
        await _sync_chat_menu_for_engine(
            context=context,
            chat_id=getattr(update.effective_chat, "id", None),
            engine=active_engine,
        )
        supported = ", ".join(
            engine for engine in SUPPORTED_CLI_ENGINES if engine in available_engines
        )
        if not supported:
            supported = "none"
        selector_keyboard = _build_engine_selector_keyboard(
            active_engine=active_engine,
            available_engines=available_engines,
        )
        selector_text = (
            "🧭 **CLI 引擎设置**\n\n"
            f"当前引擎：`{active_engine}`\n"
            f"可用引擎：`{supported}`\n\n"
            "点击下方按钮即可切换；切换后会继续引导你选择最近目录与会话。\n"
            "也可手动输入：`/engine codex` 或 `/engine claude`"
        )
        if selector_keyboard is None:
            selector_text += "\n\n⚠️ 当前未检测到可用引擎，请检查配置。"

        await _reply_update_message_resilient(
            update,
            context,
            selector_text,
            parse_mode="Markdown",
            reply_markup=selector_keyboard,
        )
        return

    requested_engine = normalize_cli_engine(args[0])
    integrations = context.bot_data.get("cli_integrations") or {}
    if requested_engine not in integrations:
        await _reply_update_message_resilient(
            update,
            context,
            f"❌ 引擎 `{requested_engine}` 当前不可用。\n"
            "请检查对应 CLI 是否安装，并在配置中启用。",
            parse_mode="Markdown",
        )
        return

    if requested_engine == active_engine:
        await _sync_chat_menu_for_engine(
            context=context,
            chat_id=getattr(update.effective_chat, "id", None),
            engine=active_engine,
        )
        await _reply_update_message_resilient(
            update,
            context,
            f"ℹ️ 当前已经是 `{active_engine}` 引擎。",
            parse_mode="Markdown",
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
        chat_id=getattr(update.effective_chat, "id", None),
        engine=requested_engine,
    )

    switch_success_text = (
        "✅ **CLI 引擎已切换**\n\n"
        f"从 `{active_engine}` 切换到 `{requested_engine}`。\n"
        "已清空当前会话绑定。请先选目录，再选会话；也可在下一步直接新建会话。"
    )
    projects: list[Path] = []
    try:
        token_mgr = _get_or_create_resume_token_manager(context)
        scanner = _get_or_create_resume_scanner(
            context=context,
            settings=settings,
            engine=requested_engine,
        )
        projects = await scanner.list_projects()
    except Exception as scan_error:
        logger.warning(
            "Failed to preload resume projects after engine switch",
            engine=requested_engine,
            error=str(scan_error),
        )

    if projects:
        current_dir = scope_state.get("current_directory")
        resume_text, resume_keyboard = build_resume_project_selector(
            projects=projects,
            approved_root=settings.approved_directory,
            token_mgr=token_mgr,
            user_id=user_id,
            current_directory=Path(current_dir) if current_dir else None,
            show_all=False,
            payload_extra={"engine": requested_engine},
            engine=requested_engine,
        )
        await _reply_update_message_resilient(
            update,
            context,
            f"{switch_success_text}\n\n{resume_text}",
            parse_mode="Markdown",
            reply_markup=resume_keyboard,
        )
    else:
        await _reply_update_message_resilient(
            update,
            context,
            f"{switch_success_text}\n\n"
            "未发现可恢复的桌面会话，请直接发送消息开始新会话，"
            "或先 `/cd` 到目标目录后再发送。",
            parse_mode="Markdown",
        )

    if audit_logger:
        await audit_logger.log_command(
            user_id=user_id,
            command="engine",
            args=[requested_engine],
            success=True,
        )


async def switch_provider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /provider command to switch cc-switch API provider."""
    from ..utils.cc_switch import CCSwitchManager

    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")

    # Permission check: restrict to allowed users
    if settings.allowed_users and user_id not in settings.allowed_users:
        await _reply_update_message_resilient(update, context, "无权限执行供应商切换。")
        return

    cc_switch: CCSwitchManager | None = context.bot_data.get("cc_switch_manager")
    if not cc_switch or not cc_switch.is_available():
        await _reply_update_message_resilient(
            update,
            context,
            "cc-switch 未安装或数据库不存在。\n"
            "请先安装 cc-switch 桌面端并配置供应商。",
        )
        return

    providers = await cc_switch.list_providers("claude")
    if not providers:
        await _reply_update_message_resilient(
            update, context, "未找到 Claude 供应商配置。"
        )
        return

    # Build inline keyboard
    buttons = []
    for p in providers:
        label = p.name
        if p.is_current:
            label = f"[当前] {label}"
        buttons.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"provider:switch:{p.id}",
                )
            ]
        )

    current = next((p for p in providers if p.is_current), None)
    current_name = current.name if current else "未知"
    current_url = current.base_url or "未知" if current else "未知"

    await _reply_update_message_resilient(
        update,
        context,
        f"**API 供应商切换**\n\n"
        f"当前供应商：`{current_name}`\n"
        f"Base URL：`{current_url}`\n\n"
        "点击下方按钮切换供应商：",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

    if audit_logger:
        await audit_logger.log_command(
            user_id=user_id, command="provider", args=[], success=True
        )


async def new_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new command by starting a fresh session and clearing old context."""
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )

    # Get current directory (default to approved directory)
    current_dir = scope_state.get("current_directory", settings.approved_directory)

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
    old_session_id = reset_result.old_session_id
    active_engine = get_active_cli_engine(scope_state)
    session_message = session_interaction.build_new_session_message(
        current_dir=current_dir,
        approved_directory=settings.approved_directory,
        previous_session_id=old_session_id,
        for_callback=False,
        active_engine=active_engine,
    )

    await _reply_update_message_resilient(
        update,
        context,
        session_message.text,
        parse_mode="Markdown",
        reply_markup=build_reply_markup_from_spec(session_message.keyboard),
    )


async def continue_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /continue command with optional prompt."""
    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    active_engine, cli_integration = get_cli_integration(
        bot_data=context.bot_data,
        scope_state=scope_state,
    )
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    session_lifecycle = context.bot_data.get("session_lifecycle_service") or (
        SessionLifecycleService(
            permission_manager=context.bot_data.get("permission_manager")
        )
    )
    session_interaction = (
        context.bot_data.get("session_interaction_service")
        or SessionInteractionService()
    )

    # Parse optional prompt from command arguments
    # If no prompt provided, use a default to continue the conversation
    prompt = " ".join(context.args) if context.args else None
    default_prompt = "Please continue where we left off"

    current_dir = scope_state.get("current_directory", settings.approved_directory)
    typing_stop_event = asyncio.Event()
    typing_heartbeat_task: asyncio.Task[None] | None = None

    try:
        if not cli_integration:
            await _reply_update_message_resilient(
                update, context, session_interaction.get_integration_unavailable_text()
            )
            return

        chat_id = getattr(update.effective_chat, "id", None)
        if isinstance(chat_id, int):
            typing_heartbeat_task = asyncio.create_task(
                _send_chat_action_heartbeat(
                    bot=context.bot,
                    chat_id=chat_id,
                    action="typing",
                    stop_event=typing_stop_event,
                    message_thread_id=getattr(
                        update.effective_message, "message_thread_id", None
                    ),
                    chat_type=getattr(update.effective_chat, "type", None),
                )
            )

        # Check if there's an existing session in user context
        claude_session_id = session_lifecycle.get_active_session_id(scope_state)
        effective_chat = _require_effective_chat(update)
        permission_handler = build_permission_handler(
            bot=context.bot,
            chat_id=effective_chat.id,
            settings=settings,
            chat_type=getattr(effective_chat, "type", None),
            message_thread_id=getattr(
                update.effective_message, "message_thread_id", None
            ),
        )

        status_msg_text = session_interaction.build_continue_progress_text(
            existing_session_id=claude_session_id,
            current_dir=current_dir,
            approved_directory=settings.approved_directory,
            prompt=prompt,
        )
        status_msg = await _reply_update_message_resilient(
            update,
            context,
            status_msg_text,
            parse_mode="Markdown",
        )

        continue_result = await session_lifecycle.continue_session(
            user_id=user_id,
            scope_state=scope_state,
            current_dir=current_dir,
            claude_integration=cli_integration,
            prompt=prompt,
            default_prompt=default_prompt,
            permission_handler=permission_handler,
            use_empty_prompt_when_existing=False,
            allow_none_prompt_when_discover=False,
        )
        claude_response = continue_result.response

        if continue_result.status == "continued" and claude_response:

            # Delete status message and send response
            await status_msg.delete()

            # Format and send Claude's response
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            for msg in formatted_messages:
                await _reply_update_message_resilient(
                    update,
                    context,
                    msg.text,
                    parse_mode=msg.parse_mode,
                    reply_markup=msg.reply_markup,
                )

            # Log successful continue
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id,
                    command="continue",
                    args=[active_engine, *(context.args or [])],
                    success=True,
                )

        elif continue_result.status == "not_found":
            # No session found to continue
            not_found_message = session_interaction.build_continue_not_found_message(
                current_dir=current_dir,
                approved_directory=settings.approved_directory,
                for_callback=False,
            )
            await _edit_message_resilient(
                status_msg,
                not_found_message.text,
                parse_mode="Markdown",
                reply_markup=build_reply_markup_from_spec(not_found_message.keyboard),
            )
        else:
            await _edit_message_resilient(
                status_msg,
                session_interaction.get_integration_unavailable_text(),
            )

    except Exception as e:
        error_msg = str(e)
        logger.error("Error in continue command", error=error_msg, user_id=user_id)

        # Delete status message if it exists
        try:
            if "status_msg" in locals():
                await status_msg.delete()
        except Exception:
            pass

        # Send error response
        await _reply_update_message_resilient(
            update,
            context,
            session_interaction.build_continue_command_error_text(error_msg),
            parse_mode="Markdown",
        )

        # Log failed continue
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="continue",
                args=context.args or [],
                success=False,
            )
    finally:
        typing_stop_event.set()
        if typing_heartbeat_task and not typing_heartbeat_task.done():
            typing_heartbeat_task.cancel()
            try:
                await typing_heartbeat_task
            except asyncio.CancelledError:
                pass


async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ls command."""
    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")

    # Get current directory
    current_dir = scope_state.get("current_directory", settings.approved_directory)

    try:
        # List directory contents
        items = []
        directories = []
        files = []

        for item in sorted(current_dir.iterdir()):
            # Skip hidden files (starting with .)
            if item.name.startswith("."):
                continue

            # Escape markdown special characters in filenames
            safe_name = _escape_markdown(item.name)

            if item.is_dir():
                directories.append(f"📁 {safe_name}/")
            else:
                # Get file size
                try:
                    size = item.stat().st_size
                    size_str = _format_file_size(size)
                    files.append(f"📄 {safe_name} ({size_str})")
                except OSError:
                    files.append(f"📄 {safe_name}")

        # Combine directories first, then files
        items = directories + files

        # Format response
        relative_path = current_dir.relative_to(settings.approved_directory)
        if not items:
            message = f"📂 `{relative_path}/`\n\n_(empty directory)_"
        else:
            message = f"📂 `{relative_path}/`\n\n"

            # Limit items shown to prevent message being too long
            max_items = 50
            if len(items) > max_items:
                shown_items = items[:max_items]
                message += "\n".join(shown_items)
                message += f"\n\n_... and {len(items) - max_items} more items_"
            else:
                message += "\n".join(items)

        # Add navigation buttons if not at root
        keyboard = []
        if current_dir != settings.approved_directory:
            keyboard.append(
                [
                    InlineKeyboardButton("⬆️ Go Up", callback_data="cd:.."),
                    InlineKeyboardButton("🏠 Go to Root", callback_data="cd:/"),
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="action:refresh_ls"),
                InlineKeyboardButton(
                    "📁 Projects", callback_data="action:show_projects"
                ),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        await _reply_update_message_resilient(
            update, context, message, parse_mode="Markdown", reply_markup=reply_markup
        )

        # Log successful command
        if audit_logger:
            await audit_logger.log_command(user_id, "ls", [], True)

    except Exception as e:
        logger.error("ls command failed", error=str(e))
        error_msg = "❌ Error listing directory"
        await _reply_update_message_resilient(update, context, error_msg)

        # Log failed command
        if audit_logger:
            await audit_logger.log_command(user_id, "ls", [], False)

        logger.error("Error in list_files command", error=str(e), user_id=user_id)


async def change_directory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cd command."""
    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    security_validator: Optional[SecurityValidator] = context.bot_data.get(
        "security_validator"
    )
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")

    # Parse arguments
    if not context.args:
        # Show recent active projects for quick switch
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
                await _reply_update_message_resilient(
                    update,
                    context,
                    text,
                    parse_mode="Markdown",
                    reply_markup=markup,
                )
                return
        except Exception as e:
            logger.warning("Failed to scan recent projects", error=str(e))

        # Fallback: show usage help
        await _reply_update_message_resilient(
            update,
            context,
            "**Usage:** `/cd <directory>`\n\n"
            "**Examples:**\n"
            "• `/cd myproject` - Enter subdirectory\n"
            "• `/cd ..` - Go up one level\n"
            "• `/cd /` - Go to root of approved directory\n\n"
            "**Tips:**\n"
            "• Use `/ls` to see available directories\n"
            "• Use `/projects` to see all projects",
            parse_mode="Markdown",
        )
        return

    target_path = " ".join(context.args)
    current_dir = scope_state.get("current_directory", settings.approved_directory)

    try:
        # Validate path using security validator
        if security_validator:
            valid, resolved_path, error = security_validator.validate_path(
                target_path, current_dir
            )

            if not valid:
                await _reply_update_message_resilient(
                    update, context, f"❌ **Access Denied**\n\n{error}"
                )

                # Log security violation
                if audit_logger:
                    await audit_logger.log_security_violation(
                        user_id=user_id,
                        violation_type="path_traversal_attempt",
                        details=f"Attempted path: {target_path}",
                        severity="medium",
                    )
                return
            if resolved_path is None:
                await _reply_update_message_resilient(
                    update,
                    context,
                    "❌ **Access Denied**\n\nUnable to resolve target directory.",
                    parse_mode="Markdown",
                )
                return
        else:
            # Fallback validation without security validator
            if target_path == "/":
                resolved_path = settings.approved_directory
            elif target_path == "..":
                resolved_path = current_dir.parent
                try:
                    resolved_path.relative_to(settings.approved_directory)
                except ValueError:
                    resolved_path = settings.approved_directory
            else:
                resolved_path = (current_dir / target_path).resolve()
                try:
                    resolved_path.relative_to(settings.approved_directory)
                except ValueError:
                    await _reply_update_message_resilient(
                        update,
                        context,
                        "❌ **Access Denied**\n\nPath outside approved directory.",
                        parse_mode="Markdown",
                    )
                    return

        # Check if directory exists and is actually a directory
        if not resolved_path.exists():
            await _reply_update_message_resilient(
                update,
                context,
                f"❌ **Directory Not Found**\n\n`{target_path}` does not exist.",
            )
            return

        if not resolved_path.is_dir():
            await _reply_update_message_resilient(
                update,
                context,
                f"❌ **Not a Directory**\n\n`{target_path}` is not a directory.",
            )
            return

        # Update current directory in user data
        scope_state["current_directory"] = resolved_path

        # Clear session when changing directory to prevent cross-topic session
        # leakage.  The user can explicitly resume via /continue or /resume.
        old_session_id = scope_state.get("claude_session_id")
        scope_state["claude_session_id"] = None
        scope_state["force_new_session"] = True
        if old_session_id:
            permission_manager = context.bot_data.get("permission_manager")
            if permission_manager:
                permission_manager.clear_session(old_session_id)

        session_info = "\n🆕 Session cleared. Send a message to start a new one."

        # Send confirmation
        relative_path = resolved_path.relative_to(settings.approved_directory)
        await _reply_update_message_resilient(
            update,
            context,
            f"✅ **Directory Changed**\n\n"
            f"📂 Current directory: `{relative_path}/`"
            f"{session_info}",
            parse_mode="Markdown",
        )

        # Log successful command
        if audit_logger:
            await audit_logger.log_command(user_id, "cd", [target_path], True)

    except Exception as e:
        logger.error("cd command failed", error=str(e))
        error_msg = "❌ **Error changing directory**"
        await _reply_update_message_resilient(
            update, context, error_msg, parse_mode="Markdown"
        )

        # Log failed command
        if audit_logger:
            await audit_logger.log_command(user_id, "cd", [target_path], False)

        logger.error("Error in change_directory command", error=str(e), user_id=user_id)


async def print_working_directory(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /pwd command."""
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    current_dir = scope_state.get("current_directory", settings.approved_directory)

    relative_path = current_dir.relative_to(settings.approved_directory)
    absolute_path = str(current_dir)

    # Add quick navigation buttons
    keyboard = [
        [
            InlineKeyboardButton("📁 List Files", callback_data="action:ls"),
            InlineKeyboardButton("📋 Projects", callback_data="action:show_projects"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await _reply_update_message_resilient(
        update,
        context,
        f"📍 **Current Directory**\n\n"
        f"Relative: `{relative_path}/`\n"
        f"Absolute: `{absolute_path}`",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )


async def show_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /projects command."""
    settings: Settings = context.bot_data["settings"]

    try:
        # Get directories in approved directory (these are "projects")
        projects = []
        for item in sorted(settings.approved_directory.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                projects.append(item.name)

        if not projects:
            await _reply_update_message_resilient(
                update,
                context,
                "📁 **No Projects Found**\n\n"
                "No subdirectories found in your approved directory.\n"
                "Create some directories to organize your projects!",
            )
            return

        # Create inline keyboard with project buttons
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
                InlineKeyboardButton("🏠 Go to Root", callback_data="cd:/"),
                InlineKeyboardButton(
                    "🔄 Refresh", callback_data="action:show_projects"
                ),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard)

        project_list = "\n".join([f"• `{project}/`" for project in projects])

        await _reply_update_message_resilient(
            update,
            context,
            f"📁 **Available Projects**\n\n"
            f"{project_list}\n\n"
            f"Click a project below to navigate to it:",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    except Exception as e:
        await _reply_update_message_resilient(
            update, context, f"❌ Error loading projects: {str(e)}"
        )
        logger.error("Error in show_projects command", error=str(e))


async def session_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /context command - show real CLI session data."""
    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    session_interaction = (
        context.bot_data.get("session_interaction_service")
        or SessionInteractionService()
    )
    full_mode = _is_context_full_mode(context)
    view_spec = session_interaction.build_context_view_spec(
        for_callback=False,
        full_mode=full_mode,
    )
    loading_kwargs: dict[str, Any] = {}
    if view_spec.loading_parse_mode:
        loading_kwargs["parse_mode"] = view_spec.loading_parse_mode
    status_msg = await _reply_update_message_resilient(
        update,
        context,
        view_spec.loading_text,
        **loading_kwargs,
    )

    try:
        session_service = context.bot_data.get("session_service")
        active_engine, cli_integration = get_cli_integration(
            bot_data=context.bot_data,
            scope_state=scope_state,
        )
        engine_capabilities = get_engine_capabilities(active_engine)
        snapshot = await SessionService.build_scope_context_snapshot(
            user_id=user_id,
            scope_state=scope_state,
            approved_directory=settings.approved_directory,
            claude_integration=cli_integration,
            session_service=session_service,
            include_resumable=view_spec.include_resumable,
            include_event_summary=view_spec.include_event_summary,
            allow_precise_context_probe=(
                engine_capabilities.supports_precise_context_probe
            ),
        )
        render_result = session_interaction.build_context_render_result(
            snapshot=snapshot,
            scope_state=scope_state,
            approved_directory=settings.approved_directory,
            full_mode=full_mode,
        )
        await _edit_message_resilient(
            status_msg,
            render_result.primary_text,
            parse_mode=render_result.parse_mode,
        )
        for extra_text in render_result.extra_texts:
            await _reply_update_message_resilient(
                update,
                context,
                extra_text,
                parse_mode=render_result.parse_mode,
            )
    except Exception as e:
        logger.error("Error in context command", error=str(e), user_id=user_id)
        try:
            await _edit_message_resilient(status_msg, view_spec.error_text)
        except Exception:
            await _reply_update_message_resilient(update, context, view_spec.error_text)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command as backward-compatible alias of /context."""
    await session_status(update, context)


async def export_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /export command."""
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    features = context.bot_data.get("features")
    session_interaction = (
        context.bot_data.get("session_interaction_service")
        or SessionInteractionService()
    )

    # Check if session export is available
    session_exporter = features.get_session_export() if features else None

    if not session_exporter:
        await _reply_update_message_resilient(
            update,
            context,
            session_interaction.build_export_unavailable_text(for_callback=False),
        )
        return

    session_lifecycle = context.bot_data.get("session_lifecycle_service") or (
        SessionLifecycleService(
            permission_manager=context.bot_data.get("permission_manager")
        )
    )

    # Get current session
    claude_session_id = session_lifecycle.get_active_session_id(scope_state)

    if not claude_session_id:
        await _reply_update_message_resilient(
            update, context, session_interaction.build_export_no_active_session_text()
        )
        return

    export_selector = session_interaction.build_export_selector_message(
        claude_session_id
    )

    await _reply_update_message_resilient(
        update,
        context,
        export_selector.text,
        parse_mode="Markdown",
        reply_markup=build_reply_markup_from_spec(export_selector.keyboard),
    )


async def end_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /end command to terminate the current session."""
    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
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
    end_result = session_lifecycle.end_session(scope_state)

    if not end_result.had_active_session:
        no_active_message = session_interaction.build_end_no_active_message(
            for_callback=False
        )
        await _reply_update_message_resilient(
            update,
            context,
            no_active_message.text,
            reply_markup=build_reply_markup_from_spec(no_active_message.keyboard),
        )
        return

    # Get current directory for display
    current_dir = scope_state.get("current_directory", settings.approved_directory)
    end_message = session_interaction.build_end_success_message(
        current_dir=current_dir,
        approved_directory=settings.approved_directory,
        for_callback=False,
        title="Session Ended",
    )

    await _reply_update_message_resilient(
        update,
        context,
        end_message.text,
        parse_mode="Markdown",
        reply_markup=build_reply_markup_from_spec(end_message.keyboard),
    )

    logger.info(
        "Session ended by user",
        user_id=user_id,
        session_id=end_result.ended_session_id,
    )


async def quick_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /actions command to show quick actions."""
    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    features = context.bot_data.get("features")

    if not features or not features.is_enabled("quick_actions"):
        await _reply_update_message_resilient(
            update,
            context,
            "❌ **Quick Actions Disabled**\n\n"
            "Quick actions feature is not enabled.\n"
            "Contact your administrator to enable this feature.",
        )
        return

    # Get current directory
    current_dir = scope_state.get("current_directory", settings.approved_directory)

    try:
        quick_action_manager = features.get_quick_actions()
        if not quick_action_manager:
            await _reply_update_message_resilient(
                update,
                context,
                "❌ **Quick Actions Unavailable**\n\n"
                "Quick actions service is not available.",
            )
            return

        # Get context-aware actions
        actions = await quick_action_manager.get_suggestions(
            session_data={"working_directory": str(current_dir), "user_id": user_id}
        )

        if not actions:
            await _reply_update_message_resilient(
                update,
                context,
                "🤖 **No Actions Available**\n\n"
                "No quick actions are available for the current context.\n\n"
                "**Try:**\n"
                "• Navigating to a project directory with `/cd`\n"
                "• Creating some code files\n"
                "• Starting a Claude session with `/new`",
            )
            return

        # Create inline keyboard
        keyboard = quick_action_manager.create_inline_keyboard(actions, max_columns=2)

        relative_path = current_dir.relative_to(settings.approved_directory)
        await _reply_update_message_resilient(
            update,
            context,
            f"⚡ **Quick Actions**\n\n"
            f"📂 Context: `{relative_path}/`\n\n"
            f"Select an action to execute:",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    except Exception as e:
        await _reply_update_message_resilient(
            update, context, f"❌ **Error Loading Actions**\n\n{str(e)}"
        )
        logger.error("Error in quick_actions command", error=str(e), user_id=user_id)


async def git_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /git command to show git repository information."""
    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    features = context.bot_data.get("features")

    if not features or not features.is_enabled("git"):
        await _reply_update_message_resilient(
            update,
            context,
            "❌ **Git Integration Disabled**\n\n"
            "Git integration feature is not enabled.\n"
            "Contact your administrator to enable this feature.",
        )
        return

    # Get current directory
    current_dir = scope_state.get("current_directory", settings.approved_directory)

    try:
        git_integration = features.get_git_integration()
        if not git_integration:
            await _reply_update_message_resilient(
                update,
                context,
                "❌ **Git Integration Unavailable**\n\n"
                "Git integration service is not available.",
            )
            return

        # Check if current directory is a git repository
        if not (current_dir / ".git").exists():
            relative_dir = current_dir.relative_to(settings.approved_directory)
            await _reply_update_message_resilient(
                update,
                context,
                f"📂 **Not a Git Repository**\n\n"
                f"Current directory `{relative_dir}/` is not a git repository.\n\n"
                f"**Options:**\n"
                f"• Navigate to a git repository with `/cd`\n"
                f"• Initialize a new repository (ask Claude to help)\n"
                f"• Clone an existing repository (ask Claude to help)",
            )
            return

        # Get git status
        git_status = await git_integration.get_status(current_dir)

        # Format status message
        relative_path = current_dir.relative_to(settings.approved_directory)
        status_message = "🔗 **Git Repository Status**\n\n"
        status_message += f"📂 Directory: `{relative_path}/`\n"
        status_message += f"🌿 Branch: `{git_status.branch}`\n"

        if git_status.ahead > 0:
            status_message += f"⬆️ Ahead: {git_status.ahead} commits\n"
        if git_status.behind > 0:
            status_message += f"⬇️ Behind: {git_status.behind} commits\n"

        # Show file changes
        if not git_status.is_clean:
            status_message += "\n**Changes:**\n"
            if git_status.modified:
                status_message += f"📝 Modified: {len(git_status.modified)} files\n"
            if git_status.added:
                status_message += f"➕ Added: {len(git_status.added)} files\n"
            if git_status.deleted:
                status_message += f"➖ Deleted: {len(git_status.deleted)} files\n"
            if git_status.untracked:
                status_message += f"❓ Untracked: {len(git_status.untracked)} files\n"
        else:
            status_message += "\n✅ Working directory clean\n"

        # Create action buttons
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

        await _reply_update_message_resilient(
            update,
            context,
            status_message,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    except Exception as e:
        await _reply_update_message_resilient(
            update, context, f"❌ **Git Error**\n\n{str(e)}"
        )
        logger.error("Error in git_command", error=str(e), user_id=user_id)


async def cancel_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel command - cancel the active Claude task."""
    user_id = _require_effective_user(update).id
    scope_key, _ = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=context.bot_data["settings"].approved_directory,
    )

    task_registry: Optional[TaskRegistry] = context.bot_data.get("task_registry")
    if not task_registry:
        await _reply_update_message_resilient(
            update, context, "Task registry not available."
        )
        return

    cancelled = await task_registry.cancel(user_id, scope_key=scope_key)
    if not cancelled:
        # Fallback to user-level cancellation in case scoped key mismatches
        # between message update and callback context (e.g. topic/thread edge cases).
        cancelled = await task_registry.cancel(user_id, scope_key=None)
    if cancelled:
        await _reply_update_message_resilient(
            update, context, "Task cancellation requested."
        )
    else:
        await _reply_update_message_resilient(
            update, context, "No active task to cancel."
        )

    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    if audit_logger:
        await audit_logger.log_command(
            user_id=user_id, command="cancel", args=[], success=cancelled
        )


def _parse_queue_id(raw_arg: str) -> Optional[int]:
    """Parse queue id token like `12` or `#12`."""
    candidate = str(raw_arg or "").strip()
    if candidate.startswith("#"):
        candidate = candidate[1:]
    if not candidate.isdigit():
        return None
    parsed = int(candidate)
    if parsed <= 0:
        return None
    return parsed


async def queue_status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /queue command - show current running + queued tasks in scope."""
    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    scope_key, _ = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )

    inbound_queue: Optional[InboundTaskQueue] = context.bot_data.get(
        "inbound_task_queue"
    )
    if not inbound_queue:
        await _reply_update_message_resilient(
            update, context, "Queue service unavailable."
        )
        return

    pending_items = await inbound_queue.list_items(user_id=user_id, scope_key=scope_key)
    task_registry: Optional[TaskRegistry] = context.bot_data.get("task_registry")
    running = (
        await task_registry.get(user_id, scope_key=scope_key) if task_registry else None
    )
    lines = ["Queue status"]
    if running:
        running_summary = running.prompt_summary or "(no summary)"
        lines.append(f"Running: yes - {running_summary}")
    else:
        lines.append("Running: no")

    if not pending_items:
        lines.append("Pending: empty")
    else:
        lines.append(f"Pending: {len(pending_items)}")
        for item in pending_items:
            lines.append(f"#{item.queue_id} [{item.kind}] {item.preview}")
        lines.append("Use /dequeue <id> to remove one queued task.")

    await _reply_update_message_resilient(update, context, "\n".join(lines))

    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    if audit_logger:
        await audit_logger.log_command(
            user_id=user_id, command="queue", args=[], success=True
        )


async def dequeue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /dequeue command - remove one queued task by queue_id."""
    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    scope_key, _ = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )

    args = [str(arg).strip() for arg in (context.args or []) if str(arg).strip()]
    if not args:
        await _reply_update_message_resilient(
            update, context, "Usage: /dequeue <queue_id>"
        )
        return

    queue_id = _parse_queue_id(args[0])
    if queue_id is None:
        await _reply_update_message_resilient(
            update, context, "Invalid queue id. Example: /dequeue 12"
        )
        return

    inbound_queue: Optional[InboundTaskQueue] = context.bot_data.get(
        "inbound_task_queue"
    )
    if not inbound_queue:
        await _reply_update_message_resilient(
            update, context, "Queue service unavailable."
        )
        return

    removed = await inbound_queue.remove(
        user_id=user_id,
        scope_key=scope_key,
        queue_id=queue_id,
    )
    if not removed:
        await _reply_update_message_resilient(
            update, context, f"Queue item #{queue_id} not found."
        )
        success = False
    else:
        await _set_update_message_reaction_safe(update, context, emoji="✅")
        success = True

    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    if audit_logger:
        await audit_logger.log_command(
            user_id=user_id,
            command="dequeue",
            args=[str(queue_id)],
            success=success,
        )


def _get_cron_scheduler(
    context: ContextTypes.DEFAULT_TYPE,
) -> Optional[CronSchedulerService]:
    scheduler = context.bot_data.get("cron_scheduler_service")
    if isinstance(scheduler, CronSchedulerService):
        return scheduler
    return None


def _render_cron_usage() -> str:
    return (
        "Cron usage:\n"
        "• `/cron list`\n"
        "• `/cron add reminder <cron_expr(5段)> <text>`\n"
        "• `/cron add ai <engine> <cron_expr(5段)> <prompt>`\n"
        "• `/cron pause <id>`\n"
        "• `/cron resume <id>`\n"
        "• `/cron delete <id>`\n\n"
        "Examples:\n"
        "• `/cron add reminder 0 9 * * 1-5 standup reminder`\n"
        "• `/cron add ai claude 30 10 * * * summarize today's TODOs`\n"
    )


def _format_cron_time(
    value: Optional[datetime], *, timezone_name: str = "Asia/Shanghai"
) -> str:
    if value is None:
        return "-"
    tz = timezone.utc
    aware = value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    # Use fixed UTC+8 display to avoid host timezone drift.
    beijing = aware.astimezone(timezone(timedelta(hours=8), name="UTC+8"))
    return f"{beijing.strftime('%Y-%m-%d %H:%M:%S')} ({timezone_name})"


def _render_cron_job_line(job: Any) -> str:
    job_id = getattr(job, "id", "?")
    status = getattr(job, "status", "-")
    job_type = getattr(job, "job_type", "-")
    schedule_type = getattr(job, "schedule_type", "-")
    payload_text = str(getattr(job, "payload_text", "") or "").strip()
    if len(payload_text) > 50:
        payload_text = payload_text[:50] + "..."
    if schedule_type == "once":
        when_text = _format_cron_time(getattr(job, "run_at", None))
    else:
        when_text = str(getattr(job, "cron_expr", "") or "-")
    next_run = _format_cron_time(getattr(job, "next_run_at", None))
    return (
        f"#{job_id} [{status}] [{job_type}] {when_text}\n"
        f"  next: {next_run}\n"
        f"  text: {payload_text}"
    )


def _parse_cron_add_arguments(
    raw_args: list[str],
) -> tuple[str, Optional[str], str, str]:
    """Parse `/cron add ...` args."""
    if len(raw_args) < 7:
        raise CronValidationError("参数不足，请参考 `/cron` 用法。")
    task_type = str(raw_args[0]).strip().lower()
    if task_type in {"reminder", "remind"}:
        cron_expr = " ".join(raw_args[1:6]).strip()
        payload = " ".join(raw_args[6:]).strip()
        return CRON_JOB_TYPE_REMINDER, None, cron_expr, payload
    if task_type == "ai":
        if len(raw_args) < 8:
            raise CronValidationError(
                "AI 任务参数不足，请提供 engine + cron + prompt。"
            )
        engine = normalize_cli_engine(raw_args[1])
        cron_expr = " ".join(raw_args[2:7]).strip()
        payload = " ".join(raw_args[7:]).strip()
        return CRON_JOB_TYPE_AI_PROMPT, engine, cron_expr, payload
    raise CronValidationError("任务类型仅支持 reminder/remind 或 ai。")


async def cron_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cron command family."""
    scheduler = _get_cron_scheduler(context)
    if scheduler is None:
        await _reply_update_message_resilient(
            update, context, "Cron scheduler unavailable."
        )
        return

    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    scope_key, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    current_dir = scope_state.get("current_directory", settings.approved_directory)
    project_dir = (
        Path(current_dir)
        if isinstance(current_dir, (str, Path))
        else settings.approved_directory
    )

    args = [str(arg).strip() for arg in (context.args or []) if str(arg).strip()]
    if not args:
        await _reply_update_message_resilient(update, context, _render_cron_usage())
        return

    action = args[0].lower()
    if action in {"help", "h"}:
        await _reply_update_message_resilient(update, context, _render_cron_usage())
        return

    if action == "list":
        jobs = await scheduler.list_user_jobs(user_id=user_id)
        if not jobs:
            await _reply_update_message_resilient(
                update,
                context,
                "No cron jobs.\n\nUse natural language like `5分钟后提醒我拿奶茶` or `/cron add ...`.",
                parse_mode="Markdown",
            )
            return
        lines = ["Cron jobs:"]
        for job in jobs:
            lines.append(_render_cron_job_line(job))
        await _reply_update_message_resilient(update, context, "\n".join(lines))
        return

    if action in {"pause", "resume", "delete"}:
        if len(args) < 2:
            await _reply_update_message_resilient(
                update, context, f"Usage: `/cron {action} <id>`", parse_mode="Markdown"
            )
            return
        job_id = _parse_queue_id(args[1])
        if job_id is None:
            await _reply_update_message_resilient(update, context, "Invalid cron id.")
            return
        if action == "pause":
            changed = await scheduler.pause_job(user_id=user_id, job_id=job_id)
        elif action == "resume":
            changed = await scheduler.resume_job(user_id=user_id, job_id=job_id)
        else:
            changed = await scheduler.delete_job(user_id=user_id, job_id=job_id)

        if changed:
            await _set_update_message_reaction_safe(update, context, emoji="✅")
        else:
            await _reply_update_message_resilient(
                update, context, f"Job #{job_id} not found or state invalid."
            )
        return

    if action == "add":
        try:
            job_type, engine, cron_expr, payload = _parse_cron_add_arguments(args[1:])
            if not payload:
                raise CronValidationError("任务内容不能为空。")
            chat = _require_effective_chat(update)
            thread_id = getattr(
                getattr(update, "effective_message", None), "message_thread_id", None
            )
            created = await scheduler.create_cron_job(
                user_id=user_id,
                chat_id=chat.id,
                thread_id=int(thread_id or 0),
                scope_key=scope_key,
                project_dir=project_dir,
                job_type=job_type,
                cron_expr=cron_expr,
                payload_text=payload,
                engine=engine,
            )
        except CronValidationError as exc:
            await _reply_update_message_resilient(update, context, f"❌ {exc}")
            return

        await _reply_update_message_resilient(
            update,
            context,
            "✅ Cron job created\n\n"
            f"ID: #{created.id}\n"
            f"Type: {created.job_type}\n"
            f"Schedule: {created.cron_expr}\n"
            f"Next: {_format_cron_time(created.next_run_at)}",
        )
        return

    await _reply_update_message_resilient(update, context, _render_cron_usage())


def _split_text_chunks(text: str, max_chars: int = 3500) -> list[str]:
    """Split long text into Telegram-safe chunks while preserving line boundaries."""
    stripped = text.strip()
    if not stripped:
        return ["(empty output)"]

    lines = stripped.splitlines(keepends=True)
    chunks: list[str] = []
    current = ""

    for line in lines:
        if len(current) + len(line) <= max_chars:
            current += line
            continue

        if current:
            chunks.append(current.rstrip())
            current = ""

        # Handle single lines that are still too long.
        if len(line) > max_chars:
            start = 0
            while start < len(line):
                part = line[start : start + max_chars]
                chunks.append(part.rstrip())
                start += max_chars
        else:
            current = line

    if current:
        chunks.append(current.rstrip())

    return chunks


async def codex_diag_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /codexdiag command to diagnose codex MCP calls without manual shell."""
    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    active_engine = get_active_cli_engine(scope_state)
    capabilities = get_engine_capabilities(active_engine)
    if not capabilities.supports_codex_diag:
        await _reply_update_message_resilient(
            update,
            context,
            "ℹ️ 当前引擎不支持 `/codexdiag`。\n"
            f"当前引擎：`{active_engine}`\n"
            "请先切换：`/engine codex`",
            parse_mode="Markdown",
        )
        return

    current_dir = scope_state.get("current_directory", settings.approved_directory)
    project_dir = current_dir
    explicit_session_id = None

    args = [arg.strip() for arg in (context.args or []) if arg and arg.strip()]
    if args:
        if args[0].lower() in {"root", "/"}:
            project_dir = settings.approved_directory
            if len(args) > 1:
                explicit_session_id = args[1]
        else:
            explicit_session_id = args[0]

    if explicit_session_id:
        import re

        if not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            explicit_session_id,
            re.IGNORECASE,
        ):
            await _reply_update_message_resilient(
                update, context, "❌ 无效的 session ID 格式"
            )
            return

    script_path = (
        Path(__file__).resolve().parents[3] / "scripts" / "cc_codex_diagnose.py"
    )
    if not script_path.exists():
        await _reply_update_message_resilient(
            update,
            context,
            f"❌ 诊断脚本不存在：{script_path}\n"
            "请检查项目是否包含 `scripts/cc_codex_diagnose.py`。",
        )
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="codexdiag",
                args=context.args or [],
                success=False,
            )
        return

    status_msg = await _reply_update_message_resilient(
        update, context, "🔎 正在诊断 codex MCP 调用状态，请稍候..."
    )

    cmd = [
        sys.executable,
        str(script_path),
        "--project",
        str(project_dir),
    ]
    if explicit_session_id:
        cmd.extend(["--session-id", explicit_session_id])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
    except asyncio.TimeoutError:
        if "proc" in locals():
            proc.kill()
            await proc.communicate()
        await _edit_message_resilient(
            status_msg,
            "⏰ 诊断超时（45 秒）。\n"
            "建议稍后重试，或先检查 `~/.claude/debug/*.txt` 是否持续写入。",
        )
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="codexdiag",
                args=context.args or [],
                success=False,
            )
        return
    except Exception as e:
        logger.error("codexdiag failed", error=str(e))
        await _edit_message_resilient(status_msg, "❌ 执行诊断失败")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="codexdiag",
                args=context.args or [],
                success=False,
            )
        return

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        err_body = stderr_text or stdout_text or "无可用输出"
        err_chunks = _split_text_chunks(err_body, max_chars=3200)
        await _edit_message_resilient(
            status_msg,
            "❌ codex 诊断执行失败。\n"
            f"项目目录: {project_dir}\n"
            f"返回码: {proc.returncode}\n\n"
            f"{err_chunks[0]}",
        )
        for chunk in err_chunks[1:]:
            await _reply_update_message_resilient(update, context, chunk)
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="codexdiag",
                args=context.args or [],
                success=False,
            )
        return

    output_chunks = _split_text_chunks(stdout_text)
    total = len(output_chunks)
    header = (
        "✅ codex 诊断完成。\n"
        f"项目目录: {project_dir}\n"
        f"会话范围: {'指定会话' if explicit_session_id else '自动选择最近会话'}\n\n"
    )
    await _edit_message_resilient(status_msg, f"{header}{output_chunks[0]}")
    for idx, chunk in enumerate(output_chunks[1:], start=2):
        await _reply_update_message_resilient(
            update, context, f"[{idx}/{total}]\n{chunk}"
        )

    if audit_logger:
        await audit_logger.log_command(
            user_id=user_id,
            command="codexdiag",
            args=context.args or [],
            success=True,
        )


def _format_file_size(size: int) -> str:
    """Format file size in human-readable format."""
    size_value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if size_value < 1024:
            return f"{size_value:.1f}{unit}" if unit != "B" else f"{int(size_value)}B"
        size_value /= 1024
    return f"{size_value:.1f}TB"


def _escape_markdown(text: str) -> str:
    """Escape special markdown characters in text for Telegram."""
    # Escape characters that have special meaning in Telegram Markdown
    special_chars = [
        "_",
        "*",
        "[",
        "]",
        "(",
        ")",
        "~",
        "`",
        ">",
        "#",
        "+",
        "-",
        "=",
        "|",
        "{",
        "}",
        ".",
        "!",
    ]
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model command - show inline keyboard to select Claude model."""
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    active_engine = get_active_cli_engine(scope_state)
    capabilities = get_engine_capabilities(active_engine)
    if active_engine == ENGINE_CODEX:
        current_model = str(scope_state.get("claude_model") or "").strip()
        resolved_model = ""
        model_display = (current_model or "default").replace("`", "")

        requested_model = " ".join(context.args or []).strip()
        if requested_model:
            requested_norm = requested_model.lower()
            if requested_norm in {"default", "clear", "reset"}:
                scope_state.pop("claude_model", None)
                selected_model = "default"
            else:
                selected_model = requested_model.replace("`", "")
                scope_state["claude_model"] = selected_model

            await _reply_update_message_resilient(
                update,
                context,
                "✅ 已更新 Codex 模型设置。\n"
                f"当前设置：`{selected_model}`\n\n"
                "该设置会用于后续 Codex 请求（通过 `--model` 传递）。\n"
                "恢复默认：`/model default`",
                parse_mode="Markdown",
                reply_markup=_build_codex_model_keyboard(
                    selected_model=str(scope_state.get("claude_model") or "").strip(),
                    resolved_model=resolved_model,
                ),
            )
            return

        await _reply_update_message_resilient(
            update,
            context,
            "ℹ️ 当前引擎：`codex`\n"
            f"当前模型：`{model_display}`\n\n"
            "可直接切换：`/model <model_name>`，或点下方按钮选择。\n"
            "恢复默认：`/model default`",
            parse_mode="Markdown",
            reply_markup=_build_codex_model_keyboard(
                selected_model=current_model,
                resolved_model=resolved_model,
            ),
        )
        return

    if not capabilities.supports_model_selection:

        await _reply_update_message_resilient(
            update,
            context,
            "ℹ️ 当前引擎不支持 `/model`。\n"
            f"当前引擎：`{active_engine}`\n"
            "请先切换：`/engine claude`",
            parse_mode="Markdown",
        )
        return

    current_raw = str(scope_state.get("claude_model") or "").strip()
    if current_raw and not _is_claude_model_name(current_raw):
        scope_state.pop("claude_model", None)
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

    await _reply_update_message_resilient(
        update,
        context,
        f"Current model: `{current or 'default'}`\nSelect a model:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume command - resume a desktop session for active engine."""
    user_id = _require_effective_user(update).id
    settings: Settings = context.bot_data["settings"]
    _, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )
    active_engine = get_active_cli_engine(scope_state)
    engine_label = "Codex" if active_engine == ENGINE_CODEX else "Claude"

    scanner = _get_or_create_resume_scanner(
        context=context,
        settings=settings,
        engine=active_engine,
    )
    token_mgr = _get_or_create_resume_token_manager(context)

    try:
        # S0 -> scan projects
        projects = await scanner.list_projects()

        current_dir = scope_state.get("current_directory")

        if not projects:
            await _reply_update_message_resilient(
                update,
                context,
                f"No desktop {engine_label} sessions found.\n\n"
                f"Make sure you have used {engine_label} CLI "
                "in a project under your approved directory.",
                parse_mode="Markdown",
            )
            return

        message_text, keyboard = build_resume_project_selector(
            projects=projects,
            approved_root=settings.approved_directory,
            token_mgr=token_mgr,
            user_id=user_id,
            current_directory=Path(current_dir) if current_dir else None,
            show_all=False,
            payload_extra={"engine": active_engine},
            engine=active_engine,
        )

        await _reply_update_message_resilient(
            update,
            context,
            message_text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error("Error in resume command", error=str(e))
        await _reply_update_message_resilient(
            update, context, f"Failed to scan desktop sessions: {e}"
        )
