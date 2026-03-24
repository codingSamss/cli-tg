"""Message handlers for non-command inputs."""

import asyncio
import base64
import binascii
import re
import time
from collections import Counter
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...claude.exceptions import ClaudeToolValidationError
from ...claude.task_registry import TaskRegistry
from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...security.validators import SecurityValidator
from ...services.cron_scheduler_service import CronSchedulerService, CronValidationError
from ...services.session_service import SessionService
from ...utils.codex_rate_limits import format_rate_limit_summary
from ..inbound_task_queue import InboundTaskQueue
from ..utils.cli_engine import (
    ENGINE_CLAUDE,
    ENGINE_CODEX,
    get_cli_integration,
    get_engine_primary_status_command,
    normalize_cli_engine,
)
from ..utils.scope_state import (
    build_scope_key,
    get_scope_state,
    get_scope_state_from_update,
)
from ..utils.telegram_send import (
    is_private_chat_type,
    normalize_message_thread_id,
    prepare_telegram_text_and_parse_mode,
    send_message_draft_resilient,
    send_message_resilient,
)

logger = structlog.get_logger()

_IMAGE_STATUS_TOTAL_STEPS = 6
_TELEGRAM_MESSAGE_LIMIT = 4096
_TELEGRAM_SAFE_SPLIT_LIMIT = 3900
_SUMMARY_CONTEXT_PROBE_TIMEOUT_SECONDS = 8.0
_REACTION_FEEDBACK_STATE_KEY = "pending_reaction_feedback"
_REACTION_COUNT_CACHE_KEY = "reaction_count_cache"
_REACTION_UPDATE_DEDUP_KEY = "reaction_update_dedup"
_REACTION_UPDATE_DEDUP_TTL_SECONDS = 60
_REACTION_FEEDBACK_TTL_SECONDS = 60 * 60
_INBOUND_AGGREGATION_LOCK_KEY = "inbound_aggregation_lock"
_TEXT_FRAGMENT_BUFFER_KEY = "text_fragment_buffer"
_MEDIA_GROUP_BUFFER_KEY = "media_group_buffer"
_TEXT_FRAGMENT_START_LENGTH = 3000
_TEXT_FRAGMENT_WINDOW_SECONDS = 1.2
_MEDIA_GROUP_WINDOW_SECONDS = 1.0
_AGGREGATION_STATE_TTL_SECONDS = 30
_CHAT_ACTION_HEARTBEAT_INTERVAL_SECONDS = 4.0
_AUTO_IMAGE_ATTACHMENTS_LIMIT = 3
_AUTO_IMAGE_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # Telegram bot document limit
_INBOUND_QUEUE_DISPATCH_LOCK_KEY = "inbound_queue_dispatch_lock"
_INBOUND_QUEUE_LAUNCHING_SCOPES_KEY = "inbound_queue_launching_scopes"
_AUTO_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_AUTO_IMAGE_PATH_PATTERN = re.compile(
    r"(?P<path>(?:~|/|\.{1,2}/)?[^\s`\"'<>|]+?\.(?:png|jpe?g|webp|gif|bmp))",
    re.IGNORECASE,
)
_AUTO_IMAGE_LINE_HINT_PATTERN = re.compile(
    r"(?:文件路径|路径|path)\s*[:：]\s*(?P<path>[^\s`\"'<>|]+)",
    re.IGNORECASE,
)
_IMAGE_GEN_GENERATE_COMMAND_PATTERN = re.compile(
    r"image-gen(?:\.py)?[^\n\r]*\bgenerate\b",
    re.IGNORECASE,
)
_PIL_DRAW_COMMAND_PATTERN = re.compile(
    r"\bfrom\s+pil\s+import\b|\bimport\s+pil\b|\bpillow\b|\bimagedraw\b|image\.new\(",
    re.IGNORECASE,
)
_IMAGE_GEN_FALLBACK_BLOCK_MESSAGE = (
    "❌ 图片生成失败：上游生图接口未返回可解析的图片数据。\n\n"
    "为避免误导，本次已中止，不再执行本地 PIL/Pillow 兜底绘制。\n"
    "请稍后重试，或先检查 `image-gen` provider 配置后再试。"
)
_POSITIVE_REACTION_TOKENS = {
    "emoji:👍",
    "emoji:✅",
    "emoji:👏",
    "emoji:❤️",
    "emoji:🔥",
}
_NEGATIVE_REACTION_TOKENS = {
    "emoji:👎",
    "emoji:❌",
    "emoji:😡",
    "emoji:🤬",
    "emoji:🚫",
    "emoji:💩",
}
_BOT_REACTION_PROCESSING = "👀"
_BOT_REACTION_SUCCESS = "👍"
_BOT_REACTION_FAILED = "👎"
_BOT_REACTION_THINKING = "🤔"
_BOT_REACTION_TOOL = "🔥"
_BOT_REACTION_TOOL_CODING = "👨‍💻"
_BOT_REACTION_TOOL_WEB = "⚡"
_BOT_REACTION_STALL_SOFT = "🥱"
_BOT_REACTION_STALL_HARD = "😨"
_BOT_REACTION_EMOJIS = {
    "queued": _BOT_REACTION_PROCESSING,
    "thinking": _BOT_REACTION_THINKING,
    "tool": _BOT_REACTION_TOOL,
    "coding": _BOT_REACTION_TOOL_CODING,
    "web": _BOT_REACTION_TOOL_WEB,
    "done": _BOT_REACTION_SUCCESS,
    "error": _BOT_REACTION_FAILED,
    "stall_soft": _BOT_REACTION_STALL_SOFT,
    "stall_hard": _BOT_REACTION_STALL_HARD,
}
_STATUS_REACTION_CODING_TOOL_TOKENS = (
    "exec",
    "process",
    "read",
    "write",
    "edit",
    "session_status",
    "bash",
)
_STATUS_REACTION_WEB_TOOL_TOKENS = (
    "webfetch",
    "websearch",
    "web_search",
    "web-search",
    "web_fetch",
    "web-fetch",
    "browser",
)
_EN_RELATIVE_REMINDER_PATTERN = re.compile(
    r"\b(in|after)\s+\d+\s*(second|minute|hour|day|week|month)s?\b",
    re.IGNORECASE,
)
_ZH_RELATIVE_REMINDER_PATTERN = re.compile(r"\d+\s*(秒|分钟|小时|天|周|个月)后")
_ZH_REMINDER_ADJUST_PATTERN = re.compile(
    r"(改成|改到|改为|改下|换成|提前|推迟|延后|晚点|早点|太晚|太早)"
)
_ZH_REMINDER_TIME_HINT_PATTERN = re.compile(
    r"(今天|明天|后天|早上|上午|中午|下午|晚上|凌晨|周[一二三四五六日天]|星期[一二三四五六日天])"
)
_EN_REMINDER_ADJUST_PATTERN = re.compile(
    r"\b(reschedule|move|change|instead|earlier|later|too late|too early)\b",
    re.IGNORECASE,
)


def _elapsed_monotonic_ms(started_at: float, current_at: Optional[float] = None) -> int:
    """Return non-negative milliseconds elapsed from a monotonic timestamp."""
    end_at = time.monotonic() if current_at is None else current_at
    return max(0, int((end_at - started_at) * 1000))


@dataclass
class _RequestTimingDiagnostics:
    """Collect coarse-grained timing diagnostics for one inbound text request."""

    request_started_monotonic: float
    progress_init_send_ms: Optional[int] = None
    command_wall_ms: Optional[int] = None
    first_stream_update_ms: Optional[int] = None
    first_assistant_text_ms: Optional[int] = None
    first_tool_activity_ms: Optional[int] = None
    last_stream_update_ms: Optional[int] = None
    stream_update_count: int = 0
    command_progress_count: int = 0
    tool_activity_count: int = 0
    tg_progress_edit_count: int = 0
    tg_progress_edit_total_ms: int = 0
    tg_progress_refresh_count: int = 0
    tg_progress_refresh_total_ms: int = 0
    tg_progress_timeout_count: int = 0
    final_reply_count: int = 0
    final_reply_total_ms: int = 0
    final_draft_count: int = 0
    final_draft_total_ms: int = 0

    def mark_stream_update(self, update_obj: Any) -> None:
        """Track first/last stream activity and tool-related progress."""
        current_at = time.monotonic()
        elapsed_ms = _elapsed_monotonic_ms(self.request_started_monotonic, current_at)
        self.stream_update_count += 1
        if self.first_stream_update_ms is None:
            self.first_stream_update_ms = elapsed_ms
        self.last_stream_update_ms = elapsed_ms

        update_type = str(getattr(update_obj, "type", "") or "").strip().lower()
        update_content = str(getattr(update_obj, "content", "") or "").strip()
        tool_calls = getattr(update_obj, "tool_calls", None)
        metadata = getattr(update_obj, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}

        if (
            update_type == "assistant"
            and update_content
            and not tool_calls
            and self.first_assistant_text_ms is None
        ):
            self.first_assistant_text_ms = elapsed_ms

        is_command_progress = metadata.get("item_type") == "command_execution"
        has_tool_activity = bool(tool_calls) or update_type == "tool_result"
        if is_command_progress:
            self.command_progress_count += 1
            has_tool_activity = True
        if has_tool_activity:
            self.tool_activity_count += 1
            if self.first_tool_activity_ms is None:
                self.first_tool_activity_ms = elapsed_ms

    def record_progress_edit(self, elapsed_ms: int) -> None:
        """Record one successful progress message edit."""
        self.tg_progress_edit_count += 1
        self.tg_progress_edit_total_ms += max(0, int(elapsed_ms))

    def record_progress_refresh(self, elapsed_ms: int) -> None:
        """Record one refresh-by-new-message after edit timeout/failure."""
        self.tg_progress_refresh_count += 1
        self.tg_progress_refresh_total_ms += max(0, int(elapsed_ms))

    def record_final_reply(self, elapsed_ms: int) -> None:
        """Record one outbound final reply send."""
        self.final_reply_count += 1
        self.final_reply_total_ms += max(0, int(elapsed_ms))

    def record_final_draft(self, elapsed_ms: int) -> None:
        """Record one private draft send before the final answer."""
        self.final_draft_count += 1
        self.final_draft_total_ms += max(0, int(elapsed_ms))

    def to_log_fields(self, *, total_wall_ms: int) -> dict[str, Any]:
        """Render diagnostics as structured log fields."""
        return {
            "request_total_wall_ms": max(0, int(total_wall_ms)),
            "progress_init_send_ms": self.progress_init_send_ms,
            "command_wall_ms": self.command_wall_ms,
            "first_stream_update_ms": self.first_stream_update_ms,
            "first_assistant_text_ms": self.first_assistant_text_ms,
            "first_tool_activity_ms": self.first_tool_activity_ms,
            "last_stream_update_ms": self.last_stream_update_ms,
            "stream_update_count": self.stream_update_count,
            "command_progress_count": self.command_progress_count,
            "tool_activity_count": self.tool_activity_count,
            "tg_progress_edit_count": self.tg_progress_edit_count,
            "tg_progress_edit_total_ms": self.tg_progress_edit_total_ms,
            "tg_progress_refresh_count": self.tg_progress_refresh_count,
            "tg_progress_refresh_total_ms": self.tg_progress_refresh_total_ms,
            "tg_progress_timeout_count": self.tg_progress_timeout_count,
            "final_reply_count": self.final_reply_count,
            "final_reply_total_ms": self.final_reply_total_ms,
            "final_draft_count": self.final_draft_count,
            "final_draft_total_ms": self.final_draft_total_ms,
        }


def _escape_md(text: str) -> str:
    """Escape special characters for Telegram legacy Markdown."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def _clean_path_candidate(raw_value: str) -> str:
    """Normalize a raw path-like token extracted from model output."""
    candidate = str(raw_value or "").strip()
    if not candidate:
        return ""
    candidate = candidate.strip("`'\"<>[](){}")
    candidate = candidate.rstrip("，。,:;!?)]}>'\"`")
    if "://" in candidate.lower():
        return ""
    return candidate


def _iter_tool_path_hints(payload: Any) -> list[str]:
    """Recursively collect path-like values from tool input payloads."""
    results: list[str] = []
    if isinstance(payload, str):
        cleaned = _clean_path_candidate(payload)
        if cleaned:
            results.append(cleaned)
        return results
    if isinstance(payload, list):
        for item in payload:
            results.extend(_iter_tool_path_hints(item))
        return results
    if not isinstance(payload, dict):
        return results

    path_keys = {
        "path",
        "file_path",
        "output_path",
        "image_path",
        "save_path",
        "filename",
        "target",
    }
    for key, value in payload.items():
        normalized_key = str(key or "").strip().lower()
        if normalized_key in path_keys:
            results.extend(_iter_tool_path_hints(value))
            continue
        if isinstance(value, (dict, list)):
            results.extend(_iter_tool_path_hints(value))
    return results


def _collect_candidate_image_paths_from_text(content: str) -> list[str]:
    """Extract candidate image paths from assistant free-form text."""
    candidates: list[str] = []
    for line in str(content or "").splitlines():
        line_match = _AUTO_IMAGE_LINE_HINT_PATTERN.search(line)
        if line_match:
            candidate = _clean_path_candidate(line_match.group("path"))
            if candidate:
                candidates.append(candidate)
        for match in _AUTO_IMAGE_PATH_PATTERN.finditer(line):
            candidate = _clean_path_candidate(match.group("path"))
            if candidate:
                candidates.append(candidate)
    return candidates


def _collect_candidate_image_paths_from_tools(tools_used: Any) -> list[str]:
    """Extract candidate image paths from structured tool usage records."""
    if not isinstance(tools_used, list):
        return []
    candidates: list[str] = []
    for tool in tools_used:
        if not isinstance(tool, dict):
            continue
        tool_input = tool.get("input")
        if tool_input is None:
            continue
        candidates.extend(_iter_tool_path_hints(tool_input))
    return candidates


def _extract_tool_commands(tools_used: Any) -> list[str]:
    """Extract shell commands from tool usage payloads across engine formats."""
    if not isinstance(tools_used, list):
        return []

    commands: list[str] = []
    for tool in tools_used:
        if not isinstance(tool, dict):
            continue

        direct_command = tool.get("command")
        if isinstance(direct_command, str):
            normalized = direct_command.strip()
            if normalized:
                commands.append(normalized)
                continue

        tool_input = tool.get("input")
        if isinstance(tool_input, dict):
            nested_command = tool_input.get("command")
            if isinstance(nested_command, str):
                normalized = nested_command.strip()
                if normalized:
                    commands.append(normalized)

    return commands


def _is_local_image_fallback_after_image_gen(claude_response: Any | None) -> bool:
    """Detect local PIL fallback after image-gen command execution."""
    if claude_response is None:
        return False

    commands = _extract_tool_commands(getattr(claude_response, "tools_used", None))
    if not commands:
        return False

    saw_image_gen_generate = any(
        _IMAGE_GEN_GENERATE_COMMAND_PATTERN.search(command) for command in commands
    )
    if not saw_image_gen_generate:
        return False

    return any(_PIL_DRAW_COMMAND_PATTERN.search(command) for command in commands)


def _enforce_no_local_image_fallback_for_image_gen(claude_response: Any | None) -> bool:
    """Rewrite response when image-gen failed and agent switched to local PIL fallback."""
    if not _is_local_image_fallback_after_image_gen(claude_response):
        return False
    if claude_response is None:
        return False

    try:
        setattr(claude_response, "content", _IMAGE_GEN_FALLBACK_BLOCK_MESSAGE)
    except Exception:
        return False
    return True


def _resolve_image_paths_for_delivery(
    candidates: list[str],
    *,
    working_directory: Path,
    approved_directory: Path,
    limit: int = _AUTO_IMAGE_ATTACHMENTS_LIMIT,
) -> list[Path]:
    """Resolve, validate and deduplicate image paths before Telegram delivery."""
    approved_root = Path(approved_directory).expanduser().resolve()
    work_root = Path(working_directory).expanduser().resolve()
    resolved: list[Path] = []
    seen: set[str] = set()

    for raw_candidate in candidates:
        if len(resolved) >= limit:
            break
        normalized = _clean_path_candidate(raw_candidate)
        if not normalized:
            continue

        candidate_path = Path(normalized).expanduser()
        if not candidate_path.is_absolute():
            candidate_path = (work_root / candidate_path).resolve()
        else:
            candidate_path = candidate_path.resolve()

        path_str = str(candidate_path)
        if path_str in seen:
            continue
        seen.add(path_str)

        if candidate_path.suffix.lower() not in _AUTO_IMAGE_SUFFIXES:
            continue
        if not candidate_path.exists() or not candidate_path.is_file():
            continue
        if not candidate_path.is_relative_to(approved_root):
            logger.warning(
                "Skip auto image delivery outside approved directory",
                path=path_str,
                approved_directory=str(approved_root),
            )
            continue
        try:
            size_bytes = candidate_path.stat().st_size
        except OSError:
            continue
        if size_bytes > _AUTO_IMAGE_MAX_FILE_SIZE_BYTES:
            logger.warning(
                "Skip auto image delivery because file is too large",
                path=path_str,
                size_bytes=size_bytes,
                max_size_bytes=_AUTO_IMAGE_MAX_FILE_SIZE_BYTES,
            )
            continue
        resolved.append(candidate_path)

    return resolved


def _extract_generated_image_paths(
    *,
    claude_response: Any | None,
    scope_state: dict[str, Any],
    approved_directory: Path,
) -> list[Path]:
    """Collect generated image files from response content + tool traces."""
    if claude_response is None:
        return []

    current_dir_raw = scope_state.get("current_directory", approved_directory)
    try:
        current_dir = Path(current_dir_raw)
    except TypeError:
        current_dir = Path(approved_directory)

    content_candidates = _collect_candidate_image_paths_from_text(
        str(getattr(claude_response, "content", "") or "")
    )
    tool_candidates = _collect_candidate_image_paths_from_tools(
        getattr(claude_response, "tools_used", None)
    )

    return _resolve_image_paths_for_delivery(
        [*content_candidates, *tool_candidates],
        working_directory=current_dir,
        approved_directory=Path(approved_directory),
    )


async def _send_generated_images_from_response(
    *,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    claude_response: Any | None,
    scope_state: dict[str, Any],
    reply_to_message_id: Optional[int] = None,
) -> int:
    """Auto-send generated image files back to Telegram when detectable."""
    telegram_message = getattr(update, "message", None)
    if telegram_message is None:
        return 0
    reply_document = getattr(telegram_message, "reply_document", None)
    if not callable(reply_document):
        return 0

    settings: Settings = context.bot_data["settings"]
    image_paths = _extract_generated_image_paths(
        claude_response=claude_response,
        scope_state=scope_state,
        approved_directory=settings.approved_directory,
    )
    if not image_paths:
        return 0

    sent_count = 0
    for idx, image_path in enumerate(image_paths):
        try:
            with image_path.open("rb") as image_file:
                await reply_document(
                    document=image_file,
                    filename=image_path.name,
                    caption=(
                        f"🖼 已回传生成图片：{image_path.name}" if idx == 0 else None
                    ),
                    reply_to_message_id=reply_to_message_id if idx == 0 else None,
                )
            sent_count += 1
        except Exception as e:
            logger.warning(
                "Failed to send generated image back to Telegram",
                path=str(image_path),
                error=str(e),
            )

    if sent_count:
        logger.info(
            "Auto-delivered generated images to Telegram",
            sent_count=sent_count,
            user_id=getattr(getattr(update, "effective_user", None), "id", None),
        )
    return sent_count


def _extract_tool_summary(tool_name: str, tool_input: dict) -> str:
    """Extract a concise summary of what a tool is doing from its input."""
    if not tool_input:
        return tool_name

    if tool_name == "Bash" and "command" in tool_input:
        cmd = tool_input["command"].strip()
        # Show first line, truncate long commands
        first_line = cmd.split("\n")[0]
        if len(first_line) > 80:
            first_line = first_line[:77] + "..."
        return f"Bash: `{first_line}`"

    if tool_name in ("Read", "ReadFile") and "file_path" in tool_input:
        return f"Read: `{tool_input['file_path']}`"

    if tool_name == "Write" and "file_path" in tool_input:
        return f"Write: `{tool_input['file_path']}`"

    if tool_name == "Edit" and "file_path" in tool_input:
        return f"Edit: `{tool_input['file_path']}`"

    if tool_name == "MultiEdit" and "file_path" in tool_input:
        return f"MultiEdit: `{tool_input['file_path']}`"

    if tool_name in ("Glob", "Grep") and "pattern" in tool_input:
        pattern = tool_input["pattern"]
        if len(pattern) > 60:
            pattern = pattern[:57] + "..."
        return f"{tool_name}: `{pattern}`"

    if tool_name == "WebFetch" and "url" in tool_input:
        url = tool_input["url"]
        if len(url) > 60:
            url = url[:57] + "..."
        return f"WebFetch: `{url}`"

    if tool_name == "Task" and "description" in tool_input:
        desc = tool_input["description"]
        if len(desc) > 60:
            desc = desc[:57] + "..."
        return f"Task: {desc}"

    # Generic: show tool name with first key hint
    for key in ("path", "file_path", "query", "command", "name"):
        if key in tool_input:
            val = str(tool_input[key])
            if len(val) > 60:
                val = val[:57] + "..."
            return f"{tool_name}: `{val}`"

    return tool_name


def _stream_engine_label(update_obj: Any) -> str:
    """Resolve engine label from stream update metadata."""
    metadata = getattr(update_obj, "metadata", None) or {}
    engine = str(metadata.get("engine") or "").strip().lower()
    if engine == "codex":
        return "Codex"
    return "Claude"


def _engine_label(engine: str | None) -> str:
    """Render normalized engine label for user-facing messages."""
    normalized = normalize_cli_engine(engine)
    if normalized == ENGINE_CODEX:
        return "Codex"
    return "Claude"


def _engine_badge(engine: str | None) -> str:
    """Render a compact engine badge for Telegram message bubbles."""
    normalized = normalize_cli_engine(engine)
    marker = "⬜" if normalized == ENGINE_CODEX else "🟧"
    return f"{marker} `{_engine_label(normalized)} CLI`"


async def _send_chat_action_heartbeat(
    *,
    message: Any,
    action: str,
    stop_event: asyncio.Event,
    interval_seconds: float = _CHAT_ACTION_HEARTBEAT_INTERVAL_SECONDS,
    message_thread_id: Optional[int] = None,
    chat_type: Optional[str] = None,
) -> None:
    """Keep Telegram chat action visible during long-running processing."""
    wait_timeout = max(interval_seconds, 0.1)
    normalized_thread_id = normalize_message_thread_id(
        message_thread_id, chat_type=chat_type
    )
    while not stop_event.is_set():
        try:
            if normalized_thread_id is None:
                await message.chat.send_action(action)
            else:
                await message.chat.send_action(
                    action, message_thread_id=normalized_thread_id
                )
        except Exception as e:
            logger.debug(
                "Failed to send chat action heartbeat",
                action=action,
                error=str(e),
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            continue


def _looks_like_reminder_request(text: str) -> bool:
    """Best-effort classifier for reminder-like prompts."""
    raw_text = str(text or "").strip()
    if not raw_text:
        return False
    normalized = raw_text.lower()
    keyword_tokens = (
        "提醒",
        "提醒我",
        "闹钟",
        "定时",
        "叫我",
        "通知我",
        "remind",
        "alarm",
        "cron",
        "every day",
        "every week",
        "every month",
        "每天",
        "每周",
        "每月",
        "工作日",
    )
    if any(token in normalized for token in keyword_tokens):
        return True
    if _EN_RELATIVE_REMINDER_PATTERN.search(normalized):
        return True
    return _ZH_RELATIVE_REMINDER_PATTERN.search(raw_text) is not None


def _looks_like_reminder_adjustment(text: str) -> bool:
    """Best-effort classifier for follow-up reminder update prompts."""
    raw_text = str(text or "").strip()
    if not raw_text:
        return False
    normalized = raw_text.lower()
    if _ZH_REMINDER_ADJUST_PATTERN.search(raw_text):
        return True
    if _EN_REMINDER_ADJUST_PATTERN.search(normalized):
        return True
    if _ZH_REMINDER_TIME_HINT_PATTERN.search(raw_text):
        return True
    return False


def _is_claude_model_name(value: str | None) -> bool:
    """Return whether model id is a Claude alias/name."""
    normalized = str(value or "").strip().lower()
    if not normalized:
        return False
    if normalized in {"sonnet", "opus", "haiku"}:
        return True
    return any(token in normalized for token in ("claude", "sonnet", "opus", "haiku"))


def _detect_integration_cli_kind(cli_integration: Any | None) -> str | None:
    """Best-effort detect CLI kind from integration; None means unknown."""
    process_manager = getattr(cli_integration, "process_manager", None)
    resolve_cli_path = getattr(process_manager, "_resolve_cli_path", None)
    detect_cli_kind = getattr(process_manager, "_detect_cli_kind", None)
    if not callable(resolve_cli_path) or not callable(detect_cli_kind):
        return None
    try:
        detected = str(detect_cli_kind(resolve_cli_path()) or "").strip().lower()
    except Exception:
        return None
    if detected in {"claude", "codex"}:
        return detected
    return None


def _resolve_model_override(
    scope_state: dict[str, Any],
    active_engine: str | None,
    cli_integration: Any | None = None,
) -> str | None:
    """Resolve safe model override for current engine."""
    selected_model = str(scope_state.get("claude_model") or "").strip()
    if not selected_model:
        return None
    normalized_engine = normalize_cli_engine(active_engine)
    if cli_integration is not None:
        detected_kind = _detect_integration_cli_kind(cli_integration)
        if detected_kind:
            normalized_engine = normalize_cli_engine(detected_kind)
    if normalized_engine == ENGINE_CODEX:
        return selected_model
    if _is_claude_model_name(selected_model):
        return selected_model
    logger.warning(
        "Ignoring non-Claude model override in Claude mode",
        model=selected_model,
    )
    return None


def _with_engine_badge(text: str, engine: str | None) -> str:
    """Attach engine badge to a bubble text, keeping payload readable."""
    body = str(text or "").strip()
    badge = _engine_badge(engine)
    if not body:
        return badge
    return f"{badge}\n{body}"


def _is_markdown_parse_error(error: Exception) -> bool:
    """Whether a Telegram send failure is caused by Markdown entity parsing."""
    error_text = str(error).lower()
    return "can't parse entities" in error_text or "cannot parse entities" in error_text


def _is_message_too_long_error(error: Exception) -> bool:
    """Whether a Telegram send failure is caused by message length overflow."""
    error_text = str(error).lower()
    return (
        "message is too long" in error_text
        or "text is too long" in error_text
        or "entity is too long" in error_text
    )


def _split_text_for_telegram(
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


async def _reply_text_resilient(
    telegram_message: Any,
    text: str,
    *,
    parse_mode: Optional[str] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    reply_to_message_id: Optional[int] = None,
    bot: Any | None = None,
    chat_type: Optional[str] = None,
) -> Any:
    """Send reply text with fallback for Markdown parse and long text errors."""
    original_text = str(text or "")
    prepared_text, prepared_parse_mode, html_upgraded_from_markdown = (
        prepare_telegram_text_and_parse_mode(original_text, parse_mode)
    )

    resolved_bot = bot
    if resolved_bot is None:
        get_bot = getattr(telegram_message, "get_bot", None)
        if callable(get_bot):
            try:
                resolved_bot = get_bot()
            except Exception:
                resolved_bot = None

    message_chat_id = getattr(telegram_message, "chat_id", None)
    if message_chat_id is None:
        message_chat = getattr(telegram_message, "chat", None)
        message_chat_id = getattr(message_chat, "id", None)
    else:
        message_chat = getattr(telegram_message, "chat", None)

    message_thread_id = getattr(telegram_message, "message_thread_id", None)
    resolved_chat_type = chat_type
    if resolved_chat_type is None:
        resolved_chat_type = getattr(message_chat, "type", None)
    should_quote_reply = str(resolved_chat_type or "").strip().lower() != "private"

    if resolved_bot is not None and isinstance(message_chat_id, int):
        return await send_message_resilient(
            bot=resolved_bot,
            chat_id=message_chat_id,
            text=original_text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
            chat_type=resolved_chat_type,
        )

    send_kwargs: dict[str, Any] = {}
    if prepared_parse_mode:
        send_kwargs["parse_mode"] = prepared_parse_mode
    if reply_markup is not None:
        send_kwargs["reply_markup"] = reply_markup
    if should_quote_reply and reply_to_message_id is not None:
        send_kwargs["reply_to_message_id"] = reply_to_message_id

    try:
        return await telegram_message.reply_text(prepared_text, **send_kwargs)
    except Exception as send_error:
        final_error: Exception = send_error

    # Markdown parsing can fail with raw stack traces or unescaped symbols.
    if prepared_parse_mode and _is_markdown_parse_error(final_error):
        if (
            html_upgraded_from_markdown
            and str(parse_mode or "").strip().lower() == "markdown"
        ):
            md_kwargs = dict(send_kwargs)
            md_kwargs["parse_mode"] = "Markdown"
            try:
                return await telegram_message.reply_text(original_text, **md_kwargs)
            except Exception as markdown_error:
                final_error = markdown_error

        no_md_kwargs = dict(send_kwargs)
        no_md_kwargs.pop("parse_mode", None)
        try:
            return await telegram_message.reply_text(original_text, **no_md_kwargs)
        except Exception as no_md_error:
            final_error = no_md_error

    if (
        _is_message_too_long_error(final_error)
        or len(original_text) > _TELEGRAM_MESSAGE_LIMIT
    ):
        chunks = _split_text_for_telegram(original_text)
        last_message = None
        for idx, chunk in enumerate(chunks):
            chunk_kwargs: dict[str, Any] = {}
            if idx == 0 and reply_markup is not None:
                chunk_kwargs["reply_markup"] = reply_markup
            if idx == 0 and should_quote_reply and reply_to_message_id is not None:
                chunk_kwargs["reply_to_message_id"] = reply_to_message_id
            last_message = await telegram_message.reply_text(chunk, **chunk_kwargs)
        return last_message

    raise final_error


async def _send_private_final_response_draft(
    *,
    bot: Any,
    chat_id: Optional[int],
    chat_type: Optional[str],
    message_thread_id: Optional[int],
    draft_id: int,
    text: str,
    parse_mode: Optional[str] = None,
) -> bool:
    """Send a private-chat final-response draft preview when available."""
    if not isinstance(chat_id, int):
        return False
    if not is_private_chat_type(chat_type):
        return False

    normalized_text = str(text or "").strip()
    if not normalized_text:
        return False

    try:
        return await send_message_draft_resilient(
            bot=bot,
            chat_id=chat_id,
            draft_id=draft_id,
            text=normalized_text,
            parse_mode=parse_mode,
            message_thread_id=message_thread_id,
            chat_type=chat_type,
        )
    except Exception as error:
        logger.info(
            "sendMessageDraft unavailable for final response, fallback to reply_text",
            chat_id=chat_id,
            error=str(error),
        )
        return False


async def _set_message_reaction_safe(
    bot: Any,
    *,
    chat_id: Optional[int],
    message_id: Optional[int],
    emoji: Optional[str],
    is_big: bool = False,
) -> bool:
    """Best-effort wrapper for Telegram set_message_reaction API."""
    if (
        bot is None
        or chat_id is None
        or message_id is None
        or not hasattr(bot, "set_message_reaction")
    ):
        return False

    normalized_emoji = str(emoji or "").strip()
    reaction_payload: list[str] = [normalized_emoji] if normalized_emoji else []

    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=reaction_payload,
            is_big=is_big,
        )
        return True
    except Exception as e:
        logger.debug(
            "Failed to set Telegram message reaction",
            chat_id=chat_id,
            message_id=message_id,
            emoji=normalized_emoji or None,
            error=str(e),
        )
        return False


def _resolve_status_reaction_tool_emoji(tool_name: Optional[str]) -> str:
    """Resolve reaction emoji based on tool name hints."""
    normalized = str(tool_name or "").strip().lower()
    if not normalized:
        return _BOT_REACTION_EMOJIS["tool"]
    if any(token in normalized for token in _STATUS_REACTION_WEB_TOOL_TOKENS):
        return _BOT_REACTION_EMOJIS["web"]
    if any(token in normalized for token in _STATUS_REACTION_CODING_TOOL_TOKENS):
        return _BOT_REACTION_EMOJIS["coding"]
    return _BOT_REACTION_EMOJIS["tool"]


class _MessageStatusReactionController:
    """Handle staged Telegram reactions for one inbound user message."""

    def __init__(
        self,
        *,
        enabled: bool,
        bot: Any,
        chat_id: Optional[int],
        message_id: Optional[int],
        debounce_ms: int,
        stall_soft_ms: int,
        stall_hard_ms: int,
    ) -> None:
        self._enabled = bool(enabled)
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id
        self._debounce_seconds = max(float(debounce_ms), 0.0) / 1000.0
        self._stall_soft_seconds = max(float(stall_soft_ms), 0.0) / 1000.0
        self._stall_hard_seconds = max(float(stall_hard_ms), 0.0) / 1000.0

        self._current_emoji = ""
        self._pending_emoji: Optional[str] = None
        self._finished = False
        self._debounce_task: Optional[asyncio.Task] = None
        self._stall_soft_task: Optional[asyncio.Task] = None
        self._stall_hard_task: Optional[asyncio.Task] = None

    @staticmethod
    def _cancel_task(task: Optional[asyncio.Task]) -> None:
        if task and not task.done():
            task.cancel()

    def _cancel_scheduled_tasks(self) -> None:
        self._cancel_task(self._debounce_task)
        self._cancel_task(self._stall_soft_task)
        self._cancel_task(self._stall_hard_task)
        self._debounce_task = None
        self._stall_soft_task = None
        self._stall_hard_task = None

    async def _set_emoji(
        self,
        emoji: Optional[str],
        *,
        force: bool = False,
        reset_stall: bool = True,
    ) -> None:
        if not self._enabled:
            return
        if self._finished and not force:
            return

        normalized = str(emoji or "").strip()
        self._pending_emoji = None
        if normalized and normalized == self._current_emoji:
            if reset_stall:
                self._reset_stall_timers()
            return

        ok = await _set_message_reaction_safe(
            self._bot,
            chat_id=self._chat_id,
            message_id=self._message_id,
            emoji=normalized or None,
        )
        if ok:
            self._current_emoji = normalized
        if reset_stall:
            self._reset_stall_timers()

    async def _emit_stall(self, stage: str) -> None:
        delay = (
            self._stall_soft_seconds if stage == "soft" else self._stall_hard_seconds
        )
        if delay <= 0:
            return
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._finished:
            return
        stall_emoji = (
            _BOT_REACTION_EMOJIS["stall_soft"]
            if stage == "soft"
            else _BOT_REACTION_EMOJIS["stall_hard"]
        )
        await self._set_emoji(stall_emoji, reset_stall=False)

    def _reset_stall_timers(self) -> None:
        if not self._enabled or self._finished:
            return
        self._cancel_task(self._stall_soft_task)
        self._cancel_task(self._stall_hard_task)
        self._stall_soft_task = None
        self._stall_hard_task = None
        if self._stall_soft_seconds > 0:
            self._stall_soft_task = asyncio.create_task(self._emit_stall("soft"))
        if self._stall_hard_seconds > 0:
            self._stall_hard_task = asyncio.create_task(self._emit_stall("hard"))

    async def _flush_pending_emoji(self, expected_emoji: str) -> None:
        try:
            if self._debounce_seconds > 0:
                await asyncio.sleep(self._debounce_seconds)
        except asyncio.CancelledError:
            return
        if self._finished:
            return
        if self._pending_emoji != expected_emoji:
            return
        await self._set_emoji(expected_emoji, reset_stall=False)

    def _schedule_debounced_emoji(self, emoji: Optional[str]) -> None:
        if not self._enabled or self._finished:
            return
        normalized = str(emoji or "").strip()
        if not normalized:
            return
        if normalized == self._current_emoji:
            self._pending_emoji = None
            self._reset_stall_timers()
            return

        self._pending_emoji = normalized
        self._cancel_task(self._debounce_task)
        self._debounce_task = asyncio.create_task(self._flush_pending_emoji(normalized))
        self._reset_stall_timers()

    async def set_queued(self) -> None:
        await self._set_emoji(_BOT_REACTION_EMOJIS["queued"])

    async def set_thinking(self) -> None:
        self._schedule_debounced_emoji(_BOT_REACTION_EMOJIS["thinking"])

    async def set_tool(self, tool_name: Optional[str] = None) -> None:
        self._schedule_debounced_emoji(_resolve_status_reaction_tool_emoji(tool_name))

    async def set_done(self) -> None:
        if not self._enabled:
            return
        self._finished = True
        self._cancel_scheduled_tasks()
        await self._set_emoji(
            _BOT_REACTION_EMOJIS["done"], force=True, reset_stall=False
        )

    async def set_error(self) -> None:
        if not self._enabled:
            return
        self._finished = True
        self._cancel_scheduled_tasks()
        await self._set_emoji(
            _BOT_REACTION_EMOJIS["error"], force=True, reset_stall=False
        )

    async def clear(self) -> None:
        if not self._enabled:
            return
        self._finished = True
        self._cancel_scheduled_tasks()
        await self._set_emoji(None, force=True, reset_stall=False)

    async def shutdown(self) -> None:
        """Cancel background timers/tasks without changing current reaction."""
        self._cancel_scheduled_tasks()


async def _update_stream_reaction_status(
    controller: Optional[_MessageStatusReactionController],
    update_obj: Any,
) -> None:
    """Translate stream events to reaction state transitions."""
    if controller is None:
        return

    if update_obj.type == "error":
        await controller.set_error()
        return

    if update_obj.type == "assistant" and update_obj.tool_calls:
        first_tool_name = None
        first_tool = update_obj.tool_calls[0] if update_obj.tool_calls else None
        if isinstance(first_tool, dict):
            first_tool_name = str(first_tool.get("name") or "").strip() or None
        await controller.set_tool(first_tool_name)
        return

    if update_obj.type == "progress":
        metadata = update_obj.metadata or {}
        if metadata.get("item_type") == "command_execution":
            command_name = str(metadata.get("command") or "").strip() or "bash"
            await controller.set_tool(command_name)
            return

    if update_obj.type in {"assistant", "progress", "tool_result", "system"}:
        await controller.set_thinking()


def _integration_supports_image_analysis(cli_integration: Any) -> bool:
    """Whether the integration can process multimodal image requests."""
    if not cli_integration:
        return False
    config = getattr(cli_integration, "config", None)
    sdk_manager = getattr(cli_integration, "sdk_manager", None)
    if getattr(config, "use_sdk", False) and sdk_manager is not None:
        return True
    process_manager = getattr(cli_integration, "process_manager", None)
    supports_images = getattr(process_manager, "supports_image_inputs", None)
    if callable(supports_images):
        try:
            return bool(supports_images())
        except Exception:
            return False
    return False


def _integration_uses_cli_image_files(cli_integration: Any) -> bool:
    """Whether integration needs local image files for subprocess image input."""
    if not cli_integration:
        return False
    config = getattr(cli_integration, "config", None)
    if getattr(config, "use_sdk", False):
        return False
    process_manager = getattr(cli_integration, "process_manager", None)
    supports_images = getattr(process_manager, "supports_image_inputs", None)
    if callable(supports_images):
        try:
            return bool(supports_images())
        except Exception:
            return False
    return False


def _persist_cli_image_file(
    *,
    base64_data: str,
    image_format: str,
    working_directory: Path,
) -> Path:
    """Persist uploaded image bytes to local file for Codex CLI --image."""
    try:
        payload = base64.b64decode(base64_data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("图片编码无效，无法提交给 Codex。") from exc

    images_dir = working_directory / ".claude-images"
    images_dir.mkdir(parents=True, exist_ok=True)

    normalized = (image_format or "jpeg").strip().lower()
    ext_map = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "gif": "gif", "webp": "webp"}
    extension = ext_map.get(normalized, "jpg")
    image_path = images_dir / f"tg-upload-{uuid4().hex}.{extension}"
    image_path.write_bytes(payload)
    return image_path


def _cleanup_cli_image_file(image_path: Optional[Path]) -> None:
    """Best-effort deletion for temporary CLI image file."""
    if not image_path:
        return
    try:
        image_path.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(
            "Failed to cleanup temporary CLI image file",
            image_path=str(image_path),
            error=str(e),
        )


def _get_inbound_aggregation_lock(context: ContextTypes.DEFAULT_TYPE) -> asyncio.Lock:
    """Get shared lock used for inbound aggregation state updates."""
    lock = context.bot_data.get(_INBOUND_AGGREGATION_LOCK_KEY)
    if isinstance(lock, asyncio.Lock):
        return lock

    created = asyncio.Lock()
    context.bot_data[_INBOUND_AGGREGATION_LOCK_KEY] = created
    return created


def _get_inbound_queue_dispatch_lock(
    context: ContextTypes.DEFAULT_TYPE,
) -> asyncio.Lock:
    """Get lock that serializes queue dispatch attempts."""
    lock = context.bot_data.get(_INBOUND_QUEUE_DISPATCH_LOCK_KEY)
    if isinstance(lock, asyncio.Lock):
        return lock

    created = asyncio.Lock()
    context.bot_data[_INBOUND_QUEUE_DISPATCH_LOCK_KEY] = created
    return created


def _get_inbound_queue_launching_scopes(
    context: ContextTypes.DEFAULT_TYPE,
) -> set[str]:
    """Get/set of scopes currently launching one queued task."""
    raw = context.bot_data.get(_INBOUND_QUEUE_LAUNCHING_SCOPES_KEY)
    if isinstance(raw, set):
        return raw

    created: set[str] = set()
    context.bot_data[_INBOUND_QUEUE_LAUNCHING_SCOPES_KEY] = created
    return created


def _build_queue_preview(text: str, *, max_length: int = 96) -> str:
    """Build one-line preview string for queue display."""
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return "(empty)"
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3].rstrip() + "..."


def _build_queue_remove_keyboard(queue_id: int) -> InlineKeyboardMarkup:
    """Build inline keyboard for removing one queued task quickly."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "撤回这条排队任务",
                    callback_data=f"queue:dequeue:{queue_id}",
                )
            ]
        ]
    )


def _build_queued_text_update(
    *,
    update: Update,
    queued_text: str,
    source_message_id: Optional[int],
) -> Any:
    """Build lightweight update clone so replay keeps merged text payload."""
    original_message = getattr(update, "message", None)
    message_id = (
        source_message_id
        if isinstance(source_message_id, int) and source_message_id > 0
        else getattr(original_message, "message_id", None)
    )
    queued_message = SimpleNamespace(
        text=queued_text,
        message_id=message_id,
        message_thread_id=getattr(original_message, "message_thread_id", None),
        reply_text=getattr(original_message, "reply_text"),
        chat=getattr(original_message, "chat", None),
    )
    return SimpleNamespace(
        effective_user=getattr(update, "effective_user", None),
        effective_chat=getattr(update, "effective_chat", None),
        effective_message=queued_message,
        message=queued_message,
    )


async def _enqueue_busy_text_task(
    *,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    scope_key: str,
    message_text: str,
    source_message_id: Optional[int],
) -> Optional[tuple[int, int]]:
    """Queue text request and return (queue_id, position) if queue is enabled."""
    inbound_queue: Optional[InboundTaskQueue] = context.bot_data.get(
        "inbound_task_queue"
    )
    if not isinstance(inbound_queue, InboundTaskQueue):
        return None

    queued_update = _build_queued_text_update(
        update=update,
        queued_text=message_text,
        source_message_id=source_message_id,
    )

    async def _replay() -> None:
        await handle_text_message(queued_update, context)

    queued_item, position = await inbound_queue.enqueue(
        user_id=user_id,
        scope_key=scope_key,
        kind="text",
        preview=_build_queue_preview(message_text),
        source_message_id=source_message_id,
        executor=_replay,
    )
    logger.info(
        "Queued inbound text task",
        user_id=user_id,
        scope_key=scope_key,
        queue_id=queued_item.queue_id,
        queue_position=position,
        source_message_id=source_message_id,
    )
    return queued_item.queue_id, position


def _build_photo_queue_preview(caption: Optional[str], photo_count: int) -> str:
    """Build queue preview for photo requests."""
    normalized_caption = " ".join(str(caption or "").split()).strip()
    if normalized_caption:
        return _build_queue_preview(normalized_caption)
    if photo_count <= 1:
        return "photo upload"
    return f"{photo_count} photos"


def _build_queued_photo_update(
    *,
    update: Update,
    grouped_photos: list[Any],
    grouped_caption: Optional[str],
    source_message_id: Optional[int],
) -> Any:
    """Build lightweight update clone for queued photo replay."""
    original_message = getattr(update, "message", None)
    message_id = (
        source_message_id
        if isinstance(source_message_id, int) and source_message_id > 0
        else getattr(original_message, "message_id", None)
    )
    queued_message = SimpleNamespace(
        photo=list(grouped_photos),
        caption=grouped_caption,
        message_id=message_id,
        message_thread_id=getattr(original_message, "message_thread_id", None),
        reply_text=getattr(original_message, "reply_text"),
        chat=getattr(original_message, "chat", None),
    )
    return SimpleNamespace(
        effective_user=getattr(update, "effective_user", None),
        effective_chat=getattr(update, "effective_chat", None),
        effective_message=queued_message,
        message=queued_message,
        _queued_grouped_photos=list(grouped_photos),
        _queued_grouped_caption=grouped_caption,
        _queued_source_message_id=message_id,
    )


async def _enqueue_busy_photo_task(
    *,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    scope_key: str,
    grouped_photos: list[Any],
    grouped_caption: Optional[str],
    source_message_id: Optional[int],
) -> Optional[tuple[int, int]]:
    """Queue photo request and return (queue_id, position) if queue is enabled."""
    inbound_queue: Optional[InboundTaskQueue] = context.bot_data.get(
        "inbound_task_queue"
    )
    if not isinstance(inbound_queue, InboundTaskQueue):
        return None

    queued_update = _build_queued_photo_update(
        update=update,
        grouped_photos=grouped_photos,
        grouped_caption=grouped_caption,
        source_message_id=source_message_id,
    )

    async def _replay() -> None:
        await handle_photo(queued_update, context)

    queued_item, position = await inbound_queue.enqueue(
        user_id=user_id,
        scope_key=scope_key,
        kind="photo",
        preview=_build_photo_queue_preview(grouped_caption, len(grouped_photos)),
        source_message_id=source_message_id,
        executor=_replay,
    )
    return queued_item.queue_id, position


async def _dispatch_next_inbound_task(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    scope_key: str,
) -> None:
    """Dispatch one queued task for the scope when there is no running task."""
    inbound_queue: Optional[InboundTaskQueue] = context.bot_data.get(
        "inbound_task_queue"
    )
    if not isinstance(inbound_queue, InboundTaskQueue):
        return

    task_registry: Optional[TaskRegistry] = context.bot_data.get("task_registry")
    dispatch_lock = _get_inbound_queue_dispatch_lock(context)
    launching_scopes = _get_inbound_queue_launching_scopes(context)
    queued_item = None

    async with dispatch_lock:
        if scope_key in launching_scopes:
            return
        if task_registry and await task_registry.is_busy(user_id, scope_key=scope_key):
            return

        queued_item = await inbound_queue.pop_next(user_id=user_id, scope_key=scope_key)
        if not queued_item:
            return
        launching_scopes.add(scope_key)

    async def _run_queued_task() -> None:
        try:
            await queued_item.executor()
        except Exception as exc:
            logger.error(
                "Queued task execution failed",
                error=str(exc),
                user_id=user_id,
                scope_key=scope_key,
                queue_id=queued_item.queue_id,
            )
        finally:
            async with dispatch_lock:
                _get_inbound_queue_launching_scopes(context).discard(scope_key)
            await _dispatch_next_inbound_task(
                context=context,
                user_id=user_id,
                scope_key=scope_key,
            )

    logger.info(
        "Dispatching queued task",
        user_id=user_id,
        scope_key=scope_key,
        queue_id=queued_item.queue_id,
        kind=queued_item.kind,
        queue_wait_ms=max(
            0,
            int((datetime.now() - queued_item.created_at).total_seconds() * 1000),
        ),
    )
    asyncio.create_task(_run_queued_task())


def _evict_stale_aggregation_states(
    state_map: MutableMapping[str, Any], *, now: float
) -> None:
    """Drop stale inbound aggregation buffers."""
    cutoff = now - _AGGREGATION_STATE_TTL_SECONDS
    stale_keys = []
    for key, raw_state in state_map.items():
        if not isinstance(raw_state, dict):
            stale_keys.append(key)
            continue
        updated_at = raw_state.get("updated_at")
        if not isinstance(updated_at, (int, float)) or float(updated_at) < cutoff:
            stale_keys.append(key)

    for key in stale_keys:
        state_map.pop(key, None)


def _resolve_thread_id_for_aggregation(update: Update) -> int:
    """Resolve thread id for inbound aggregation keys."""
    raw_thread_id = getattr(update.effective_message, "message_thread_id", None)
    try:
        return int(raw_thread_id) if raw_thread_id is not None else 0
    except (TypeError, ValueError):
        return 0


async def _collect_text_fragments(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[bool, str, Optional[int], int]:
    """Collect split long-text fragments and return merged payload when ready.

    Returns:
    - ready: whether caller should continue processing now
    - merged_text: merged text when ready=True
    - source_message_id: first fragment message id when merged
    - fragment_count: number of merged fragments
    """
    telegram_message = getattr(update, "message", None)
    raw_text = getattr(telegram_message, "text", None)
    message_text = str(raw_text or "")

    raw_message_id = getattr(telegram_message, "message_id", None)
    message_id = raw_message_id if isinstance(raw_message_id, int) else None
    if message_id is None:
        return True, message_text, None, 1

    should_start_buffer = len(message_text) >= _TEXT_FRAGMENT_START_LENGTH
    chat_id = getattr(update.effective_chat, "id", None)
    user_id = getattr(update.effective_user, "id", None)
    if (
        not isinstance(chat_id, int)
        or not isinstance(user_id, int)
        or update.effective_message is None
    ):
        return True, message_text, message_id, 1

    thread_id = _resolve_thread_id_for_aggregation(update)
    aggregation_key = f"{chat_id}:{thread_id}:{user_id}"
    now = time.monotonic()
    lock = _get_inbound_aggregation_lock(context)
    candidate_message_id: Optional[int] = None

    async with lock:
        raw_map = context.bot_data.get(_TEXT_FRAGMENT_BUFFER_KEY)
        if not isinstance(raw_map, dict):
            raw_map = {}
            context.bot_data[_TEXT_FRAGMENT_BUFFER_KEY] = raw_map
        _evict_stale_aggregation_states(raw_map, now=now)
        buffer_map: MutableMapping[str, Any] = raw_map

        current_state = buffer_map.get(aggregation_key)
        if not isinstance(current_state, dict):
            if not should_start_buffer:
                return True, message_text, message_id, 1
            current_state = {
                "updated_at": now,
                "latest_message_id": message_id,
                "parts": [{"message_id": message_id, "text": message_text}],
            }
            buffer_map[aggregation_key] = current_state
            candidate_message_id = message_id
        else:
            last_updated = current_state.get("updated_at")
            state_is_recent = isinstance(last_updated, (int, float)) and (
                now - float(last_updated) <= max(_TEXT_FRAGMENT_WINDOW_SECONDS * 2, 0.6)
            )
            if not state_is_recent:
                if not should_start_buffer:
                    buffer_map.pop(aggregation_key, None)
                    return True, message_text, message_id, 1
                current_state = {
                    "updated_at": now,
                    "latest_message_id": message_id,
                    "parts": [{"message_id": message_id, "text": message_text}],
                }
                buffer_map[aggregation_key] = current_state
                candidate_message_id = message_id
            else:
                parts = current_state.get("parts")
                if not isinstance(parts, list):
                    parts = []
                    current_state["parts"] = parts
                already_seen = any(
                    isinstance(item, dict) and item.get("message_id") == message_id
                    for item in parts
                )
                if not already_seen:
                    parts.append({"message_id": message_id, "text": message_text})
                current_state["updated_at"] = now
                current_state["latest_message_id"] = message_id
                candidate_message_id = message_id

    if candidate_message_id is None:
        return True, message_text, message_id, 1

    if _TEXT_FRAGMENT_WINDOW_SECONDS > 0:
        await asyncio.sleep(_TEXT_FRAGMENT_WINDOW_SECONDS)

    async with lock:
        raw_map = context.bot_data.get(_TEXT_FRAGMENT_BUFFER_KEY)
        if not isinstance(raw_map, dict):
            return False, "", None, 0
        second_pass_buffer_map: MutableMapping[str, Any] = raw_map
        current_state = second_pass_buffer_map.get(aggregation_key)
        if not isinstance(current_state, dict):
            return False, "", None, 0
        if current_state.get("latest_message_id") != candidate_message_id:
            return False, "", None, 0

        parts = current_state.get("parts")
        if not isinstance(parts, list):
            parts = []
        second_pass_buffer_map.pop(aggregation_key, None)

    normalized_parts: list[dict[str, Any]] = []
    for item in parts:
        if not isinstance(item, dict):
            continue
        part_message_id = item.get("message_id")
        if not isinstance(part_message_id, int):
            continue
        normalized_parts.append(
            {
                "message_id": part_message_id,
                "text": str(item.get("text") or ""),
            }
        )

    if not normalized_parts:
        return True, message_text, message_id, 1

    normalized_parts.sort(key=lambda item: item["message_id"])
    combined_text = "\n".join(item["text"] for item in normalized_parts if item["text"])
    if not combined_text:
        combined_text = message_text
    return (
        True,
        combined_text,
        normalized_parts[0]["message_id"],
        len(normalized_parts),
    )


async def _collect_media_group_photos(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[bool, list[Any], Optional[str], Optional[int]]:
    """Collect photo media_group messages and return one aggregated batch."""
    telegram_message = getattr(update, "message", None)
    photos = getattr(telegram_message, "photo", None) or []
    if not photos:
        return True, [], getattr(telegram_message, "caption", None), None

    largest_photo = photos[-1]
    raw_message_id = getattr(telegram_message, "message_id", None)
    message_id = raw_message_id if isinstance(raw_message_id, int) else None
    if message_id is None:
        return True, [largest_photo], getattr(telegram_message, "caption", None), None

    media_group_id = str(getattr(telegram_message, "media_group_id", "") or "").strip()
    if not media_group_id:
        return (
            True,
            [largest_photo],
            getattr(telegram_message, "caption", None),
            message_id,
        )

    chat_id = getattr(update.effective_chat, "id", None)
    if not isinstance(chat_id, int):
        return (
            True,
            [largest_photo],
            getattr(telegram_message, "caption", None),
            message_id,
        )

    aggregation_key = f"{chat_id}:{media_group_id}"
    now = time.monotonic()
    lock = _get_inbound_aggregation_lock(context)

    async with lock:
        raw_map = context.bot_data.get(_MEDIA_GROUP_BUFFER_KEY)
        if not isinstance(raw_map, dict):
            raw_map = {}
            context.bot_data[_MEDIA_GROUP_BUFFER_KEY] = raw_map
        _evict_stale_aggregation_states(raw_map, now=now)
        buffer_map: MutableMapping[str, Any] = raw_map

        current_state = buffer_map.get(aggregation_key)
        if not isinstance(current_state, dict):
            current_state = {
                "updated_at": now,
                "latest_message_id": message_id,
                "items": [],
            }
            buffer_map[aggregation_key] = current_state

        items = current_state.get("items")
        if not isinstance(items, list):
            items = []
            current_state["items"] = items

        already_seen = any(
            isinstance(item, dict) and item.get("message_id") == message_id
            for item in items
        )
        if not already_seen:
            items.append(
                {
                    "message_id": message_id,
                    "photo": largest_photo,
                    "caption": getattr(telegram_message, "caption", None),
                }
            )
        current_state["updated_at"] = now
        current_state["latest_message_id"] = message_id

    if _MEDIA_GROUP_WINDOW_SECONDS > 0:
        await asyncio.sleep(_MEDIA_GROUP_WINDOW_SECONDS)

    async with lock:
        raw_map = context.bot_data.get(_MEDIA_GROUP_BUFFER_KEY)
        if not isinstance(raw_map, dict):
            return False, [], None, None
        second_pass_buffer_map: MutableMapping[str, Any] = raw_map

        current_state = second_pass_buffer_map.get(aggregation_key)
        if not isinstance(current_state, dict):
            return False, [], None, None
        if current_state.get("latest_message_id") != message_id:
            return False, [], None, None

        items = current_state.get("items")
        if not isinstance(items, list):
            items = []
        second_pass_buffer_map.pop(aggregation_key, None)

    normalized_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        part_message_id = item.get("message_id")
        if not isinstance(part_message_id, int):
            continue
        photo_obj = item.get("photo")
        if photo_obj is None:
            continue
        normalized_items.append(
            {
                "message_id": part_message_id,
                "photo": photo_obj,
                "caption": item.get("caption"),
            }
        )

    if not normalized_items:
        return (
            True,
            [largest_photo],
            getattr(telegram_message, "caption", None),
            message_id,
        )

    normalized_items.sort(key=lambda item: item["message_id"])
    merged_photos = [item["photo"] for item in normalized_items]
    merged_caption = next(
        (
            str(item.get("caption")).strip()
            for item in normalized_items
            if str(item.get("caption") or "").strip()
        ),
        None,
    )
    source_message_id = normalized_items[0]["message_id"]
    return True, merged_photos, merged_caption, source_message_id


async def _format_progress_update(update_obj: Any) -> Optional[str]:
    """Format progress updates with enhanced context and visual indicators."""
    if update_obj.type == "tool_result":
        # Show tool completion status
        tool_name = "Unknown"
        if update_obj.metadata and update_obj.metadata.get("tool_use_id"):
            # Try to extract tool name from context if available
            tool_name = update_obj.metadata.get("tool_name", "Tool")

        safe_tool_name = _escape_md(tool_name)

        if update_obj.is_error():
            safe_error = _escape_md(update_obj.get_error_message())
            return f"❌ *{safe_tool_name} failed*\n\n{safe_error}"
        else:
            execution_time = ""
            if update_obj.metadata and update_obj.metadata.get("execution_time_ms"):
                time_ms = update_obj.metadata["execution_time_ms"]
                execution_time = f" ({time_ms}ms)"
            return f"✅ *{safe_tool_name} completed*{execution_time}"

    elif update_obj.type == "progress":
        # Handle progress updates
        metadata = update_obj.metadata or {}
        if metadata.get("subtype") == "turn.started":
            engine_label = _stream_engine_label(update_obj)
            return f"🤖 *{engine_label} is working...*"
        if metadata.get("item_type") == "command_execution":
            status = str(metadata.get("status") or "").strip().lower()
            command = str(metadata.get("command") or update_obj.content or "").strip()
            first_line = command.split("\n")[0] if command else ""
            if len(first_line) > 100:
                first_line = first_line[:97] + "..."
            safe_command = _escape_md(first_line or "(empty)")
            if status == "in_progress":
                return f"🔧 *Running command*\n\n`{safe_command}`"
            if status == "completed":
                exit_code = metadata.get("exit_code")
                suffix = (
                    f" \\(exit {int(exit_code)}\\)"
                    if isinstance(exit_code, int)
                    else ""
                )
                return f"✅ *Command completed*{suffix}\n\n`{safe_command}`"
            if status in {"failed", "error", "cancelled"}:
                exit_code = metadata.get("exit_code")
                suffix = (
                    f" \\(exit {int(exit_code)}\\)"
                    if isinstance(exit_code, int)
                    else ""
                )
                return f"❌ *Command {status}*{suffix}\n\n`{safe_command}`"

        safe_content = _escape_md(update_obj.content or "Working...")
        progress_text = f"🔄 *{safe_content}*"

        percentage = update_obj.get_progress_percentage()
        if percentage is not None:
            # Create a simple progress bar
            filled = int(percentage / 10)  # 0-10 scale
            bar = "█" * filled + "░" * (10 - filled)
            progress_text += f"\n\n`{bar}` {percentage}%"

        if update_obj.progress:
            step = update_obj.progress.get("step")
            total_steps = update_obj.progress.get("total_steps")
            if step and total_steps:
                progress_text += f"\n\nStep {step} of {total_steps}"

        return progress_text

    elif update_obj.type == "error":
        # Handle error messages
        safe_error = _escape_md(update_obj.get_error_message())
        return f"❌ *Error*\n\n{safe_error}"

    elif update_obj.type == "assistant" and update_obj.tool_calls:
        # Show when tools are being called with operation details
        summaries = []
        for tc in update_obj.tool_calls:
            name = tc.get("name", "unknown")
            inp = tc.get("input", {})
            summaries.append(_escape_md(_extract_tool_summary(name, inp)))
        if summaries:
            return "\n".join(f"🔧 {s}" for s in summaries)

    elif update_obj.type == "assistant" and update_obj.content:
        # Regular content updates with preview
        content_preview = (
            update_obj.content[:150] + "..."
            if len(update_obj.content) > 150
            else update_obj.content
        )
        safe_preview = _escape_md(content_preview)
        # Keep assistant stream preview concise; engine context is already shown
        # in the message badge/header.
        return f"🤔 {safe_preview}"

    elif update_obj.type == "system":
        # System initialization or other system messages
        if update_obj.metadata and update_obj.metadata.get("subtype") == "init":
            # Avoid showing potentially stale requested/default model names here.
            # Actual model should be shown only after resolution.
            engine_label = _stream_engine_label(update_obj)
            return f"🚀 *Starting {engine_label}*"
        if (
            update_obj.metadata
            and update_obj.metadata.get("subtype") == "model_resolved"
        ):
            model = _escape_md(update_obj.metadata.get("model", "Claude"))
            return f"🧠 *Using model:* {model}"

    return None


def _format_error_message(error_str: str, *, engine: str = ENGINE_CLAUDE) -> str:
    """Format error messages for user-friendly display."""
    normalized_engine = normalize_cli_engine(engine)
    engine_label = _engine_label(normalized_engine)
    status_command = get_engine_primary_status_command(normalized_engine)

    if "usage limit reached" in error_str.lower():
        # Usage limit error - already user-friendly from integration.py
        return error_str
    elif "tool not allowed" in error_str.lower():
        # Tool validation error - already handled in facade.py
        return error_str
    elif "no conversation found" in error_str.lower():
        return (
            f"🔄 **Session Not Found**\n\n"
            f"The {engine_label} session could not be found or has expired.\n\n"
            f"**What you can do:**\n"
            f"• Use `/new` to start a fresh session\n"
            f"• Try your request again\n"
            f"• Use `/{status_command}` to check your current session"
        )
    elif "rate limit" in error_str.lower():
        return (
            f"⏱️ **Rate Limit Reached**\n\n"
            f"Too many requests in a short time period.\n\n"
            f"**What you can do:**\n"
            f"• Wait a moment before trying again\n"
            f"• Use simpler requests\n"
            f"• Check your current usage with `/{status_command}`"
        )
    elif "timeout" in error_str.lower():
        return (
            "⏰ **Request Timeout**\n\n"
            "Your request took too long to process and timed out.\n\n"
            "**What you can do:**\n"
            "• Try breaking down your request into smaller parts\n"
            "• Use simpler commands\n"
            "• Try again in a moment"
        )
    else:
        # Generic error handling
        # Escape special markdown characters in error message
        # Replace problematic chars that break Telegram markdown
        safe_error = (
            error_str.replace("_", "\\_")
            .replace("*", "\\*")
            .replace("`", "\\`")
            .replace("[", "\\[")
        )
        # Truncate very long errors
        if len(safe_error) > 200:
            safe_error = safe_error[:200] + "..."

        return (
            f"❌ **{engine_label} CLI Error**\n\n"
            f"Failed to process your request: {safe_error}\n\n"
            f"Please try again or contact the administrator if the problem persists."
        )


def _is_timeout_error(error: Exception | str | None) -> bool:
    """Detect if a Telegram API error was caused by a timeout."""

    if error is None:
        return False
    raw = error if isinstance(error, str) else str(error)
    return "timeout" in raw.lower()


def _get_stream_merge_key(update_obj: Any) -> Optional[str]:
    """Return merge key for high-frequency stream updates, or None if not mergeable."""
    if (
        update_obj.type == "assistant"
        and update_obj.content
        and not update_obj.tool_calls
    ):
        return "assistant_content"
    if update_obj.type == "progress":
        metadata = getattr(update_obj, "metadata", None) or {}
        if metadata.get("item_type") == "command_execution":
            command = str(metadata.get("command") or update_obj.content or "").strip()
            command_head = command.split("\n", 1)[0].strip()
            if command_head:
                return f"command_execution:{command_head}"
            return "command_execution"
        return "progress"
    return None


def _should_collect_thinking_update(update_obj: Any) -> bool:
    """Whether this stream update should appear in expandable thinking details."""
    update_type = str(getattr(update_obj, "type", "") or "").strip().lower()
    content = str(getattr(update_obj, "content", "") or "").strip()
    metadata = getattr(update_obj, "metadata", None) or {}
    if not isinstance(metadata, dict):
        metadata = {}

    item_type = str(metadata.get("item_type") or "").strip().lower()
    if update_type == "progress":
        if item_type == "command_execution":
            return False
        return bool(content)

    if update_type != "assistant":
        return False
    if getattr(update_obj, "tool_calls", None):
        return False
    if item_type == "agent_message":
        return False
    return bool(content)


def _is_turn_started_update(update_obj: Any) -> bool:
    """Whether stream update is a progress turn.started marker."""
    if update_obj.type != "progress":
        return False
    metadata = getattr(update_obj, "metadata", None) or {}
    return metadata.get("subtype") == "turn.started"


def _is_high_priority_stream_update(update_obj: Any) -> bool:
    """Whether a stream update should bypass debounce and flush immediately."""
    if update_obj.type in {"error", "tool_result"}:
        return True

    if update_obj.type == "assistant" and update_obj.tool_calls:
        return True

    if update_obj.type == "system" and update_obj.metadata:
        return update_obj.metadata.get("subtype") in {"init", "model_resolved"}

    return False


def _is_noop_edit_error(error: Exception) -> bool:
    """Check whether Telegram rejected edit because content is unchanged."""
    return "message is not modified" in str(error).lower()


def _append_progress_line_with_merge(
    progress_lines: list[str],
    progress_merge_keys: list[Optional[str]],
    progress_text: str,
    merge_key: Optional[str],
) -> None:
    """Append progress line or merge into previous line when merge key matches."""
    if merge_key and merge_key.startswith("command_execution"):
        # Command execution status updates can arrive interleaved.
        # Replace the latest matching command entry instead of appending
        # another block, so UI keeps a single line per command.
        for idx in range(len(progress_merge_keys) - 1, -1, -1):
            if progress_merge_keys[idx] == merge_key:
                progress_lines[idx] = progress_text
                progress_merge_keys[idx] = merge_key
                return

    if (
        merge_key
        and progress_lines
        and progress_merge_keys
        and progress_merge_keys[-1] == merge_key
    ):
        progress_lines[-1] = progress_text
        progress_merge_keys[-1] = merge_key
        return

    # Skip exact consecutive duplicates to reduce noisy UI refreshes.
    if progress_lines and progress_lines[-1] == progress_text:
        return

    progress_lines.append(progress_text)
    progress_merge_keys.append(merge_key)


def _is_command_execution_progress_block(text: str) -> bool:
    """Whether rendered progress text is a command execution status block."""
    normalized = str(text or "").strip()
    return (
        normalized.startswith("🔧 *Running command*")
        or normalized.startswith("✅ *Command ")
        or normalized.startswith("❌ *Command ")
    )


def _is_assistant_progress_block(text: str) -> bool:
    """Whether rendered line is an assistant narration block."""
    normalized = str(text or "").strip()
    return normalized.startswith("🤔 ") or normalized.startswith("💬 ")


def _join_progress_lines_for_display(lines: list[str]) -> str:
    """Join progress lines, adding spacing around command execution blocks."""
    if not lines:
        return ""

    rendered_parts: list[str] = [str(lines[0])]
    for index in range(1, len(lines)):
        previous = str(lines[index - 1] or "")
        current = str(lines[index] or "")
        separator = (
            "\n\n"
            if _is_command_execution_progress_block(previous)
            or _is_command_execution_progress_block(current)
            or _is_assistant_progress_block(previous)
            or _is_assistant_progress_block(current)
            else "\n"
        )
        rendered_parts.append(separator)
        rendered_parts.append(current)
    return "".join(rendered_parts)


def _build_context_tag(
    scope_state: dict,
    approved_directory: Path,
    active_engine: str,
    session_id: Optional[str],
    session_context_summary: Optional[str] = None,
    rate_limit_summary: Optional[str] = None,
) -> str:
    """Build a compact context tag line for display in thinking summary or reply header.

    Format: engine_badge | project_name | sid_short
    """
    current_dir = scope_state.get("current_directory", approved_directory)
    project_name = current_dir.name if current_dir and current_dir.name else "~"
    sid_short = (session_id or "no-session")[:8]
    lines = [f"{_engine_badge(active_engine)} | `{project_name}` | `{sid_short}`"]
    if session_context_summary:
        lines.append(session_context_summary)
    if rate_limit_summary:
        rate_lines = [
            line.strip()
            for line in str(rate_limit_summary).splitlines()
            if str(line).strip()
        ]
        if rate_lines:
            lines.append(f"🔋 {rate_lines[0]}")
            for line in rate_lines[1:]:
                lines.append(f"   {line}")
    return "\n".join(lines)


def _build_session_context_summary(snapshot: Optional[dict[str, Any]]) -> Optional[str]:
    """Render current session context usage summary from cached Codex snapshot."""
    if not isinstance(snapshot, dict):
        return None

    used_percent: Optional[float] = None
    used_percent_raw = snapshot.get("used_percent")
    if used_percent_raw is not None:
        try:
            used_percent = float(used_percent_raw)
        except (TypeError, ValueError):
            used_percent = None

    total_tokens_raw = snapshot.get("total_tokens")
    remaining_tokens_raw = snapshot.get("remaining_tokens")
    remaining_percent: Optional[float] = None
    try:
        total_tokens = int(total_tokens_raw or 0)
        remaining_tokens = int(remaining_tokens_raw or 0)
        if total_tokens > 0:
            remaining_percent = max(
                min(remaining_tokens / total_tokens * 100, 100.0), 0.0
            )
    except (TypeError, ValueError):
        remaining_percent = None

    if remaining_percent is None and used_percent is not None:
        remaining_percent = max(min(100.0 - used_percent, 100.0), 0.0)

    if remaining_percent is None:
        return None

    return "🔋 Session context: " f"`{remaining_percent:.1f}%` remaining"


async def _resolve_codex_context_snapshot(
    *,
    active_engine: str | None,
    session_id: Optional[str],
    cli_integration: Any | None = None,
    working_directory: Optional[Path] = None,
    current_model: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str]]:
    """Resolve Codex context snapshot, preferring a live status probe."""
    if normalize_cli_engine(active_engine) != ENGINE_CODEX:
        return None, None, None

    sid = str(session_id or "").strip()
    if not sid:
        return None, None, None

    snapshot: Optional[dict[str, Any]] = None
    get_precise_context_usage = getattr(
        cli_integration, "get_precise_context_usage", None
    )
    if callable(get_precise_context_usage) and isinstance(working_directory, Path):
        probe_kwargs: dict[str, Any] = {
            "session_id": sid,
            "working_directory": working_directory,
            "model": current_model,
        }
        try:
            try:
                snapshot = await asyncio.wait_for(
                    get_precise_context_usage(
                        **probe_kwargs,
                        force_refresh=True,
                    ),
                    timeout=_SUMMARY_CONTEXT_PROBE_TIMEOUT_SECONDS,
                )
            except TypeError:
                snapshot = await asyncio.wait_for(
                    get_precise_context_usage(**probe_kwargs),
                    timeout=_SUMMARY_CONTEXT_PROBE_TIMEOUT_SECONDS,
                )
        except asyncio.TimeoutError:
            logger.info(
                "Live Codex context probe timed out; falling back to local snapshot",
                session_id=sid,
                timeout_seconds=_SUMMARY_CONTEXT_PROBE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.info(
                "Live Codex context probe failed; falling back to local snapshot",
                session_id=sid,
                error=str(exc),
            )

        if isinstance(snapshot, dict):
            SessionService.cache_codex_snapshot(sid, snapshot)

    if not isinstance(snapshot, dict):
        snapshot = SessionService.resolve_codex_snapshot(sid)
    if not isinstance(snapshot, dict):
        return None, None, None

    return (
        snapshot,
        _build_session_context_summary(snapshot),
        format_rate_limit_summary(snapshot.get("rate_limits")),
    )


def _extract_model_from_model_usage(model_usage: Any) -> Optional[str]:
    """Best-effort extract resolved model name from response model_usage payload."""
    if not isinstance(model_usage, dict) or not model_usage:
        return None

    def _pick_model(payload: dict[str, Any]) -> str:
        for key in ("resolvedModel", "resolved_model", "model"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        return ""

    direct = _pick_model(model_usage)
    if direct:
        return direct

    for model_name, usage in model_usage.items():
        if not isinstance(usage, dict):
            continue
        nested = _pick_model(usage)
        if nested:
            return nested
        candidate = str(model_name or "").strip()
        if candidate and candidate.lower() not in {"sdk", "current", "default"}:
            return candidate

    return None


def _resolve_collapsed_fallback_model(
    *,
    active_engine: str | None,
    scope_state: dict[str, Any],
    claude_response: Any | None = None,
    codex_snapshot: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve model line fallback for collapsed summary when stream lacks model event."""
    usage_model = _extract_model_from_model_usage(
        getattr(claude_response, "model_usage", None)
    )
    if usage_model:
        return usage_model

    if normalize_cli_engine(active_engine) == ENGINE_CODEX and isinstance(
        codex_snapshot, dict
    ):
        resolved_model = str(codex_snapshot.get("resolved_model") or "").strip()
        if resolved_model:
            return resolved_model

    selected_model = str(scope_state.get("claude_model") or "").strip()
    if not selected_model:
        return None
    if normalize_cli_engine(active_engine) == ENGINE_CODEX:
        return selected_model
    if _is_claude_model_name(selected_model):
        return selected_model
    return None


def _generate_thinking_summary(all_progress_lines: list[str]) -> str:
    """Generate a one-line summary from progress lines."""
    # Match both old format "Using tools:" and new format "🔧 ToolName:"
    tool_count = sum(
        1
        for line in all_progress_lines
        if "Using tools:" in line or (line.startswith("🔧") and ":" in line)
    )
    complete_count = sum(1 for line in all_progress_lines if "completed" in line)
    error_count = sum(
        1 for line in all_progress_lines if "failed" in line or "Error" in line
    )

    parts = []
    if tool_count:
        parts.append(f"{tool_count} tools called")
    if complete_count:
        parts.append(f"{complete_count} completed")
    if error_count:
        parts.append(f"{error_count} errors")

    summary = "Thinking done"
    if parts:
        summary += " -- " + ", ".join(parts)
    return summary


def _extract_resolved_model_line(all_progress_lines: list[str]) -> Optional[str]:
    """Extract the latest resolved model line from progress updates."""
    for line in reversed(all_progress_lines):
        if line.strip().startswith("🧠 *Using model:*"):
            return line.strip()
    return None


def _build_collapsed_thinking_summary(
    all_progress_lines: list[str],
    context_tag: str,
    fallback_model: Optional[str] = None,
) -> str:
    """Build final collapsed thinking summary text with model and compact context."""

    def _compact_context_tag(raw: str) -> str:
        """Keep only session identity line + session context usage for collapsed UI."""
        context_lines = [
            line.strip() for line in str(raw or "").splitlines() if line.strip()
        ]
        if not context_lines:
            return ""

        compact_lines: list[str] = [context_lines[0]]
        for line in context_lines[1:]:
            if "Session context:" in line:
                compact_lines.append(line)
                break
        return "\n".join(compact_lines)

    lines: list[str] = []
    compact_context = _compact_context_tag(context_tag)
    if compact_context:
        lines.append(compact_context)

    model_line = _extract_resolved_model_line(all_progress_lines)
    if not model_line:
        candidate = str(fallback_model or "").strip()
        if candidate:
            model_line = f"🧠 *Using model:* {_escape_md(candidate)}"
    if model_line:
        if lines:
            lines.append("")
        lines.append(model_line)

    if not lines:
        # Defensive fallback to avoid empty collapsed message.
        lines.append(f"💭 {_generate_thinking_summary(all_progress_lines)}")
    return "\n".join(lines)


def _cache_thinking_data(
    context: ContextTypes.DEFAULT_TYPE,
    message_id: int,
    lines: list[str],
    summary: str,
    max_cache: int = 5,
) -> None:
    """Cache thinking process into context.user_data, keep latest max_cache entries."""
    user_data = context.user_data
    if user_data is None:
        return

    sanitized_lines: list[str] = []
    fallback_lines: list[str] = []
    for raw_line in lines:
        normalized = str(raw_line or "").strip()
        if not normalized:
            continue
        fallback_lines.append(normalized)
        if _is_command_execution_progress_block(normalized):
            continue
        sanitized_lines.append(normalized)
    if not sanitized_lines:
        sanitized_lines = fallback_lines

    cache_key = f"thinking:{message_id}"
    user_data[cache_key] = {
        "lines": sanitized_lines,
        "summary": summary,
    }

    # Clean old cache: only keep latest max_cache entries
    thinking_keys = sorted(
        [k for k in user_data if isinstance(k, str) and k.startswith("thinking:")],
        key=lambda k: int(k.split(":")[1]),
    )
    while len(thinking_keys) > max_cache:
        oldest = thinking_keys.pop(0)
        user_data.pop(oldest, None)


def _format_elapsed_time(total_seconds: int) -> str:
    """Format elapsed seconds as mm:ss."""
    minutes = max(total_seconds, 0) // 60
    seconds = max(total_seconds, 0) % 60
    return f"{minutes:02d}:{seconds:02d}"


def _image_heartbeat_interval_seconds(elapsed_seconds: int) -> int:
    """Adaptive heartbeat interval for image analysis status updates."""
    if elapsed_seconds < 30:
        return 6
    if elapsed_seconds < 90:
        return 12
    return 20


def _build_image_stage_status(
    step: int,
    title: str,
    detail: Optional[str] = None,
) -> str:
    """Build a user-friendly status message for image processing."""
    lines = [
        "📸 **图片分析中**",
        "",
        f"`{step}/{_IMAGE_STATUS_TOTAL_STEPS}` {title}",
    ]
    if detail:
        lines.extend(["", detail])
    return "\n".join(lines)


def _build_image_analyzing_status(
    elapsed_seconds: int, engine_label: str = "当前引擎"
) -> str:
    """Build analysis-stage status with elapsed-time heartbeat text."""
    detail = f"已等待 `{_format_elapsed_time(elapsed_seconds)}`"
    if elapsed_seconds >= 90:
        detail += f"\n⏳ 响应时间较长，但 {engine_label} 仍在处理中。"
    return _build_image_stage_status(
        5, f"{engine_label} 正在分析图片...", detail=detail
    )


async def _run_with_image_analysis_heartbeat(
    *,
    run_coro: Awaitable[Any],
    update_status: Callable[[str], Awaitable[None]],
    engine_label: str = "当前引擎",
) -> Any:
    """Run image analysis while sending adaptive heartbeat updates."""
    task: asyncio.Future[Any] = asyncio.ensure_future(run_coro)
    loop = asyncio.get_event_loop()
    start_time = loop.time()
    last_heartbeat_at = 0

    while True:
        done, _ = await asyncio.wait({task}, timeout=1)
        if task in done:
            return await task

        elapsed = int(loop.time() - start_time)
        interval = _image_heartbeat_interval_seconds(elapsed)
        if elapsed > 0 and (elapsed - last_heartbeat_at) >= interval:
            await update_status(
                _build_image_analyzing_status(elapsed, engine_label=engine_label)
            )
            last_heartbeat_at = elapsed


def _emoji_from_reaction_token(token: str) -> str:
    """Extract user-facing emoji from normalized reaction token."""
    token_text = str(token or "").strip()
    if token_text.startswith("emoji:"):
        emoji = token_text.split(":", 1)[1].strip()
        return emoji or token_text
    return token_text or "unknown"


def _resolve_reaction_feedback_signal(
    added_tokens: list[str],
) -> Optional[dict[str, str]]:
    """Map reaction token delta to positive/negative feedback signal."""
    for token in added_tokens:
        normalized = str(token or "").strip()
        if normalized in _NEGATIVE_REACTION_TOKENS:
            return {
                "signal": "negative",
                "token": normalized,
                "emoji": _emoji_from_reaction_token(normalized),
            }
    for token in added_tokens:
        normalized = str(token or "").strip()
        if normalized in _POSITIVE_REACTION_TOKENS:
            return {
                "signal": "positive",
                "token": normalized,
                "emoji": _emoji_from_reaction_token(normalized),
            }
    return None


def _store_pending_reaction_feedback(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    actor_id: int,
    chat_id: int,
    thread_id: int,
    feedback: dict[str, Any],
) -> bool:
    """Store reaction feedback into actor scoped state for next text turn."""
    settings: Optional[Settings] = context.bot_data.get("settings")
    if settings is None:
        return False

    application = getattr(context, "application", None)
    user_data_map = getattr(application, "user_data", None) if application else None
    if not isinstance(user_data_map, MutableMapping):
        return False

    actor_user_data = user_data_map.setdefault(actor_id, {})
    if not isinstance(actor_user_data, dict):
        return False

    scope_key = build_scope_key(user_id=actor_id, chat_id=chat_id, thread_id=thread_id)
    scope_state = get_scope_state(
        user_data=actor_user_data,
        scope_key=scope_key,
        default_directory=settings.approved_directory,
    )
    scope_state[_REACTION_FEEDBACK_STATE_KEY] = feedback
    return True


def _get_pending_reaction_feedback(
    scope_state: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Get valid pending reaction feedback from scope state."""
    payload = scope_state.get(_REACTION_FEEDBACK_STATE_KEY)
    if not isinstance(payload, dict):
        return None

    signal = str(payload.get("signal") or "").strip().lower()
    if signal not in {"positive", "negative"}:
        scope_state.pop(_REACTION_FEEDBACK_STATE_KEY, None)
        return None

    timestamp_raw = payload.get("timestamp")
    if timestamp_raw is None:
        scope_state.pop(_REACTION_FEEDBACK_STATE_KEY, None)
        return None
    try:
        timestamp = float(timestamp_raw)
    except (TypeError, ValueError):
        scope_state.pop(_REACTION_FEEDBACK_STATE_KEY, None)
        return None

    if timestamp <= 0 or (time.time() - timestamp) > _REACTION_FEEDBACK_TTL_SECONDS:
        scope_state.pop(_REACTION_FEEDBACK_STATE_KEY, None)
        return None

    return payload


def _clear_pending_reaction_feedback(scope_state: dict[str, Any]) -> None:
    """Clear consumed reaction feedback from scope state."""
    scope_state.pop(_REACTION_FEEDBACK_STATE_KEY, None)


def _resolve_pending_reaction_feedback(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    thread_id: int,
    scope_state: dict[str, Any],
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """Resolve pending reaction feedback with fallback to chat-level scope."""
    feedback = _get_pending_reaction_feedback(scope_state)
    if feedback is not None:
        return feedback, scope_state

    if thread_id == 0:
        return None, scope_state

    settings: Optional[Settings] = context.bot_data.get("settings")
    if settings is None:
        return None, scope_state

    fallback_scope_key = build_scope_key(user_id=user_id, chat_id=chat_id, thread_id=0)
    fallback_scope_state = get_scope_state(
        user_data=context.user_data,
        scope_key=fallback_scope_key,
        default_directory=settings.approved_directory,
    )
    fallback_feedback = _get_pending_reaction_feedback(fallback_scope_state)
    if fallback_feedback is None:
        return None, scope_state

    return fallback_feedback, fallback_scope_state


def _compose_prompt_with_reaction_feedback(
    message_text: str, feedback: Optional[dict[str, Any]]
) -> str:
    """Build model prompt with optional reaction feedback hint."""
    if not feedback:
        return message_text

    signal = str(feedback.get("signal") or "").strip().lower()
    emoji = str(feedback.get("emoji") or "").strip() or "unknown"
    if signal == "negative":
        prefix = (
            "系统提示：用户刚通过 Telegram reaction 对上一条回复表达了不满意"
            f"（{emoji}）。请先简短修正上次可能的问题，再高质量回答本次请求，避免冗余重复。\n\n"
        )
        return prefix + message_text
    if signal == "positive":
        prefix = (
            "系统提示：用户刚通过 Telegram reaction 对上一条回复表达了认可"
            f"（{emoji}）。请保持当前方向与风格，继续回答本次请求。\n\n"
        )
        return prefix + message_text
    return message_text


async def handle_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle regular text messages as Claude prompts."""
    effective_user = update.effective_user
    telegram_message = update.message
    effective_chat = update.effective_chat
    if effective_user is None or telegram_message is None or effective_chat is None:
        return

    user_id = effective_user.id
    message_text = str(telegram_message.text or "")
    input_chat_id = effective_chat.id
    if not isinstance(input_chat_id, int):
        return
    input_message_id = getattr(telegram_message, "message_id", None)
    settings: Settings = context.bot_data["settings"]
    scope_key, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )

    # Get services
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")

    aggregated_ready, aggregated_text, aggregated_source_message_id, fragment_count = (
        await _collect_text_fragments(update, context)
    )
    if not aggregated_ready:
        return
    message_text = aggregated_text
    if aggregated_source_message_id is not None:
        input_message_id = aggregated_source_message_id
    if fragment_count > 1:
        logger.info(
            "Merged inbound text fragments",
            user_id=user_id,
            scope_key=scope_key,
            fragment_count=fragment_count,
            merged_length=len(message_text),
            source_message_id=input_message_id,
        )

    cron_scheduler = context.bot_data.get("cron_scheduler_service")
    should_try_nl_reminder = False
    if isinstance(cron_scheduler, CronSchedulerService):
        if _looks_like_reminder_request(message_text):
            should_try_nl_reminder = True
        elif _looks_like_reminder_adjustment(message_text):
            try:
                should_try_nl_reminder = await cron_scheduler.has_active_reminder(
                    user_id=user_id,
                    scope_key=scope_key,
                )
            except Exception as exc:
                logger.debug(
                    "Failed to probe active reminder before NL update",
                    user_id=user_id,
                    error=str(exc),
                )

    initial_thinking_line = "🤔 正在处理你的请求..."
    cancel_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Cancel", callback_data="cancel:task")]]
    )
    progress_msg: Any = None
    reaction_controller: Optional[_MessageStatusReactionController] = None
    request_timing = _RequestTimingDiagnostics(
        request_started_monotonic=time.monotonic()
    )

    if isinstance(cron_scheduler, CronSchedulerService) and should_try_nl_reminder:
        nl_active_engine, nl_cli_integration = get_cli_integration(
            bot_data=context.bot_data,
            scope_state=scope_state,
        )
        if nl_cli_integration is not None:
            if getattr(settings, "status_reactions_enabled", True):
                await _set_message_reaction_safe(
                    getattr(context, "bot", None),
                    chat_id=input_chat_id,
                    message_id=input_message_id,
                    emoji=_BOT_REACTION_EMOJIS["queued"],
                )

            reminder_typing_stop_event = asyncio.Event()
            reminder_typing_heartbeat_task: Optional[asyncio.Task] = (
                asyncio.create_task(
                    _send_chat_action_heartbeat(
                        message=telegram_message,
                        action="typing",
                        stop_event=reminder_typing_stop_event,
                        message_thread_id=getattr(
                            telegram_message, "message_thread_id", None
                        ),
                        chat_type=getattr(effective_chat, "type", None),
                    )
                )
            )
            current_dir_raw = scope_state.get(
                "current_directory", settings.approved_directory
            )
            current_dir = (
                Path(current_dir_raw)
                if isinstance(current_dir_raw, (str, Path))
                else settings.approved_directory
            )
            if progress_msg is None:
                progress_init_started_at = time.monotonic()
                progress_msg = await _reply_text_resilient(
                    telegram_message,
                    _with_engine_badge(initial_thinking_line, nl_active_engine),
                    parse_mode="Markdown",
                    reply_to_message_id=input_message_id,
                    reply_markup=cancel_keyboard,
                )
                request_timing.progress_init_send_ms = _elapsed_monotonic_ms(
                    progress_init_started_at
                )
                reaction_controller = _MessageStatusReactionController(
                    enabled=getattr(settings, "status_reactions_enabled", True),
                    bot=context.bot,
                    chat_id=input_chat_id,
                    message_id=input_message_id,
                    debounce_ms=getattr(settings, "status_reaction_debounce_ms", 700),
                    stall_soft_ms=getattr(
                        settings, "status_reaction_stall_soft_ms", 10000
                    ),
                    stall_hard_ms=getattr(
                        settings, "status_reaction_stall_hard_ms", 30000
                    ),
                )
                await reaction_controller.set_queued()

            probe_progress_lines: list[str] = [initial_thinking_line]
            probe_progress_merge_keys: list[Optional[str]] = [None]
            probe_last_progress_text = _with_engine_badge(
                initial_thinking_line, nl_active_engine
            )

            async def _reminder_probe_stream_handler(update_obj: Any) -> None:
                nonlocal probe_last_progress_text
                try:
                    progress_text = await _format_progress_update(update_obj)
                    if not progress_text:
                        return
                    merge_key = _get_stream_merge_key(update_obj)
                    _append_progress_line_with_merge(
                        progress_lines=probe_progress_lines,
                        progress_merge_keys=probe_progress_merge_keys,
                        progress_text=progress_text,
                        merge_key=merge_key,
                    )
                    full_text = _with_engine_badge(
                        _join_progress_lines_for_display(probe_progress_lines),
                        nl_active_engine,
                    )
                    if full_text == probe_last_progress_text:
                        return
                    probe_last_progress_text = full_text
                    if progress_msg is not None:
                        await progress_msg.edit_text(
                            full_text,
                            parse_mode="Markdown",
                            reply_markup=cancel_keyboard,
                        )
                except Exception as exc:
                    logger.debug(
                        "Failed to process reminder probe update",
                        error=str(exc),
                    )

            try:
                reminder_result = await cron_scheduler.create_natural_language_reminder(
                    text=message_text,
                    user_id=user_id,
                    chat_id=input_chat_id,
                    thread_id=int(
                        getattr(telegram_message, "message_thread_id", 0) or 0
                    ),
                    scope_key=scope_key,
                    project_dir=current_dir,
                    cli_integration=nl_cli_integration,
                    on_stream=_reminder_probe_stream_handler,
                )
            except CronValidationError as exc:
                if reaction_controller is not None:
                    await reaction_controller.set_error()
                elif getattr(settings, "status_reactions_enabled", True):
                    await _set_message_reaction_safe(
                        getattr(context, "bot", None),
                        chat_id=input_chat_id,
                        message_id=input_message_id,
                        emoji=_BOT_REACTION_EMOJIS["error"],
                    )
                error_text = f"⚠️ {exc}"
                if progress_msg is not None:
                    try:
                        await progress_msg.edit_text(error_text)
                    except Exception:
                        await _reply_text_resilient(telegram_message, error_text)
                else:
                    await _reply_text_resilient(telegram_message, error_text)
                reminder_typing_stop_event.set()
                if (
                    reminder_typing_heartbeat_task
                    and not reminder_typing_heartbeat_task.done()
                ):
                    reminder_typing_heartbeat_task.cancel()
                    try:
                        await reminder_typing_heartbeat_task
                    except asyncio.CancelledError:
                        pass
                return
            finally:
                reminder_typing_stop_event.set()
                if (
                    reminder_typing_heartbeat_task
                    and not reminder_typing_heartbeat_task.done()
                ):
                    reminder_typing_heartbeat_task.cancel()
                    try:
                        await reminder_typing_heartbeat_task
                    except asyncio.CancelledError:
                        pass

            if reminder_result is not None:
                reminder_job = reminder_result.job
                reminder_action = str(getattr(reminder_result, "action", "") or "")
                pending_count = 0
                try:
                    pending_count = await cron_scheduler.count_user_pending_reminders(
                        user_id=user_id
                    )
                except Exception:
                    pending_count = 0

                if str(getattr(reminder_job, "schedule_type", "")) == "cron":
                    next_run = getattr(reminder_job, "next_run_at", None)
                    if isinstance(next_run, datetime):
                        next_local = (
                            next_run.replace(tzinfo=timezone.utc)
                            .astimezone(timezone(timedelta(hours=8), name="UTC+8"))
                            .strftime("%Y-%m-%d %H:%M:%S")
                        )
                    else:
                        next_local = "unknown"
                    title = (
                        "✅ 已更新周期提醒"
                        if reminder_action == "updated"
                        else "✅ 已设置周期提醒"
                    )
                    confirmation_text = (
                        f"{title}\n\n"
                        f"• 时间规则：{reminder_job.cron_expr}\n"
                        f"• 下次提醒（北京时间）：{next_local}\n"
                        f"• 内容：{reminder_job.payload_text}\n"
                        f"• 当前待提醒：{pending_count} 条"
                    )
                else:
                    run_at = getattr(reminder_job, "run_at", None)
                    if isinstance(run_at, datetime):
                        run_at_local = (
                            run_at.replace(tzinfo=timezone.utc)
                            .astimezone(timezone(timedelta(hours=8), name="UTC+8"))
                            .strftime("%Y-%m-%d %H:%M:%S")
                        )
                    else:
                        run_at_local = "unknown"
                    title = (
                        "✅ 已更新提醒"
                        if reminder_action == "updated"
                        else "✅ 已设置提醒"
                    )
                    confirmation_text = (
                        f"{title}\n\n"
                        f"• 时间：北京时间 {run_at_local}\n"
                        f"• 内容：{reminder_job.payload_text}\n"
                        f"• 当前待提醒：{pending_count} 条"
                    )
                if progress_msg is not None:
                    try:
                        await progress_msg.edit_text(confirmation_text)
                    except Exception:
                        await _reply_text_resilient(telegram_message, confirmation_text)
                else:
                    await _reply_text_resilient(telegram_message, confirmation_text)
                if reaction_controller is not None:
                    await reaction_controller.set_done()
                elif getattr(settings, "status_reactions_enabled", True):
                    await _set_message_reaction_safe(
                        getattr(context, "bot", None),
                        chat_id=input_chat_id,
                        message_id=input_message_id,
                        emoji=_BOT_REACTION_EMOJIS["done"],
                    )
                if audit_logger:
                    await audit_logger.log_command(
                        user_id=user_id,
                        command="cron_nl",
                        args=[str(reminder_job.id or "")],
                        success=True,
                    )
                return

            if progress_msg is not None:
                try:
                    await progress_msg.edit_text(
                        _with_engine_badge(initial_thinking_line, nl_active_engine),
                        parse_mode="Markdown",
                        reply_markup=cancel_keyboard,
                    )
                except Exception:
                    pass

    logger.info(
        "Processing text message", user_id=user_id, message_length=len(message_text)
    )

    typing_stop_event = asyncio.Event()
    typing_heartbeat_task: Optional[asyncio.Task] = None
    all_progress_lines: list[str] = []
    frozen_messages: list[Any] = []
    request_timing = request_timing or _RequestTimingDiagnostics(
        request_started_monotonic=time.monotonic()
    )
    request_outcome = "running"
    active_engine = ENGINE_CLAUDE
    claude_response = None
    response_message_count = 0

    try:
        # Check if user already has an active task
        task_registry: Optional[TaskRegistry] = context.bot_data.get("task_registry")
        if task_registry and await task_registry.is_busy(user_id, scope_key=scope_key):
            queued_meta = await _enqueue_busy_text_task(
                update=update,
                context=context,
                user_id=user_id,
                scope_key=scope_key,
                message_text=message_text,
                source_message_id=input_message_id,
            )
            if queued_meta is None:
                request_outcome = "busy_rejected"
                await _reply_text_resilient(
                    telegram_message,
                    "A task is already running. Use /cancel to cancel it.",
                )
            else:
                queue_id, position = queued_meta
                pending_before = max(position - 1, 0)
                request_outcome = "queued"
                await _reply_text_resilient(
                    telegram_message,
                    (
                        f"Task is running, queued as #{queue_id}. "
                        f"{pending_before} task(s) ahead. "
                        f"Use /queue to view, /dequeue {queue_id} to remove."
                    ),
                    reply_markup=_build_queue_remove_keyboard(queue_id),
                )
            if progress_msg is not None:
                try:
                    await progress_msg.delete()
                except Exception:
                    logger.debug("Failed to delete pending progress message")
            if reaction_controller is not None:
                await reaction_controller.clear()
            return

        # Keep typing indicator alive while the thinking/progress flow is running.
        typing_heartbeat_task = asyncio.create_task(
            _send_chat_action_heartbeat(
                message=telegram_message,
                action="typing",
                stop_event=typing_stop_event,
                message_thread_id=getattr(telegram_message, "message_thread_id", None),
                chat_type=getattr(effective_chat, "type", None),
            )
        )

        # Resolve active CLI engine integration and storage from context
        active_engine, cli_integration = get_cli_integration(
            bot_data=context.bot_data,
            scope_state=scope_state,
        )
        storage = context.bot_data.get("storage")

        if not cli_integration:
            request_outcome = "engine_unavailable"
            await _reply_text_resilient(
                telegram_message,
                _with_engine_badge(
                    "❌ **CLI 引擎不可用**\n\n"
                    "当前 CLI 引擎未正确配置。"
                    " "
                    "Please contact the administrator.",
                    active_engine,
                ),
                parse_mode="Markdown",
            )
            return

        if progress_msg is None:
            progress_init_started_at = time.monotonic()
            progress_msg = await _reply_text_resilient(
                telegram_message,
                _with_engine_badge(initial_thinking_line, active_engine),
                parse_mode="Markdown",
                reply_to_message_id=input_message_id,
                reply_markup=cancel_keyboard,
            )
            request_timing.progress_init_send_ms = _elapsed_monotonic_ms(
                progress_init_started_at
            )
            reaction_controller = _MessageStatusReactionController(
                enabled=getattr(settings, "status_reactions_enabled", True),
                bot=context.bot,
                chat_id=input_chat_id,
                message_id=input_message_id,
                debounce_ms=getattr(settings, "status_reaction_debounce_ms", 700),
                stall_soft_ms=getattr(settings, "status_reaction_stall_soft_ms", 10000),
                stall_hard_ms=getattr(settings, "status_reaction_stall_hard_ms", 30000),
            )
            await reaction_controller.set_queued()

        # Get current directory
        current_dir = scope_state.get("current_directory", settings.approved_directory)

        # Get existing session ID
        session_id = scope_state.get("claude_session_id")
        # Read but don't consume yet -- consume only after successful execution
        # so that the protection survives retries on failure.
        force_new_session = scope_state.get("force_new_session", False)
        resolved_chat_type = getattr(effective_chat, "type", None)
        current_message_thread_id = getattr(telegram_message, "message_thread_id", None)
        draft_seed = input_message_id if isinstance(input_message_id, int) else 0
        if draft_seed <= 0:
            draft_seed = int(time.time() * 1000) % 2147483647
        draft_id = draft_seed or 1

        # Enhanced stream updates handler with accumulated progress tracking
        # Keep an assistant-style thinking line visible while streaming, so the
        # bubble semantics stay consistent with 🤔 reaction status.
        progress_lines: list[str] = [initial_thinking_line]
        progress_merge_keys: list[Optional[str]] = [None]
        all_progress_lines = []  # 完整思考过程（不受溢出 clear 影响）
        all_progress_merge_keys: list[Optional[str]] = []
        frozen_messages = []  # 被冻结的旧进度消息
        last_progress_text = ""
        pending_progress_text: Optional[str] = None
        progress_flush_task: Optional[asyncio.Task] = None
        progress_flush_lock = asyncio.Lock()
        stream_loop = asyncio.get_event_loop()
        debounce_seconds = max(settings.stream_render_debounce_ms, 0) / 1000
        min_edit_interval_seconds = (
            max(settings.stream_render_min_edit_interval_ms, 0) / 1000
        )
        last_progress_edit_ts = stream_loop.time()
        turn_started_shown = False

        async def _flush_pending_progress(force: bool = False) -> None:
            nonlocal last_progress_text, last_progress_edit_ts

            async with progress_flush_lock:
                if not pending_progress_text:
                    return

                now = stream_loop.time()
                wait_seconds = 0.0
                if not force:
                    wait_seconds = max(
                        0.0, min_edit_interval_seconds - (now - last_progress_edit_ts)
                    )
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)

                # Always use latest pending content (it may have changed while waiting).
                text_to_send = pending_progress_text
                if not text_to_send or text_to_send == last_progress_text:
                    return

                async def _refresh_with_new_message() -> None:
                    nonlocal progress_msg, last_progress_edit_ts
                    try:
                        await progress_msg.edit_reply_markup(reply_markup=None)
                    except Exception:
                        pass
                    refresh_started_at = time.monotonic()
                    progress_msg = await _reply_text_resilient(
                        progress_msg,
                        text_to_send,
                        parse_mode="Markdown",
                        reply_markup=cancel_keyboard,
                    )
                    request_timing.record_progress_refresh(
                        _elapsed_monotonic_ms(refresh_started_at)
                    )
                    last_progress_edit_ts = stream_loop.time()

                try:
                    edit_started_at = time.monotonic()
                    await progress_msg.edit_text(
                        text_to_send,
                        parse_mode="Markdown",
                        reply_markup=cancel_keyboard,
                    )
                    request_timing.record_progress_edit(
                        _elapsed_monotonic_ms(edit_started_at)
                    )
                    last_progress_text = text_to_send
                    last_progress_edit_ts = stream_loop.time()
                except Exception as e:
                    if _is_noop_edit_error(e):
                        last_progress_text = text_to_send
                        last_progress_edit_ts = stream_loop.time()
                        return

                    fallback_error: Exception | None = None
                    timeout_error = _is_timeout_error(e)
                    try:
                        fallback_edit_started_at = time.monotonic()
                        await progress_msg.edit_text(
                            text_to_send,
                            reply_markup=cancel_keyboard,
                        )
                        request_timing.record_progress_edit(
                            _elapsed_monotonic_ms(fallback_edit_started_at)
                        )
                        last_progress_text = text_to_send
                        last_progress_edit_ts = stream_loop.time()
                    except Exception as exc:
                        fallback_error = exc
                        if _is_noop_edit_error(exc):
                            last_progress_text = text_to_send
                            last_progress_edit_ts = stream_loop.time()
                            return
                        timeout_error = timeout_error or _is_timeout_error(exc)
                    if timeout_error:
                        request_timing.tg_progress_timeout_count += 1
                        await _refresh_with_new_message()
                        last_progress_text = text_to_send
                        return
                    logger.warning(
                        "Failed to update progress message",
                        error=str(e),
                        fallback_error=str(fallback_error) if fallback_error else None,
                    )

        def _schedule_progress_flush() -> None:
            nonlocal progress_flush_task

            if progress_flush_task and not progress_flush_task.done():
                return

            async def _runner() -> None:
                try:
                    if debounce_seconds > 0:
                        await asyncio.sleep(debounce_seconds)
                    await _flush_pending_progress(force=False)
                except asyncio.CancelledError:
                    return

            progress_flush_task = asyncio.create_task(_runner())

        async def _cancel_progress_flush_task() -> None:
            nonlocal progress_flush_task
            if progress_flush_task and not progress_flush_task.done():
                progress_flush_task.cancel()
                try:
                    await progress_flush_task
                except asyncio.CancelledError:
                    pass
            progress_flush_task = None

        async def stream_handler(update_obj: Any) -> None:
            nonlocal progress_msg, last_progress_text, pending_progress_text
            nonlocal last_progress_edit_ts, turn_started_shown
            try:
                request_timing.mark_stream_update(update_obj)
                await _update_stream_reaction_status(reaction_controller, update_obj)
                if _is_turn_started_update(update_obj):
                    if turn_started_shown:
                        return
                    turn_started_shown = True
                progress_text = await _format_progress_update(update_obj)
                if not progress_text:
                    return

                merge_key = _get_stream_merge_key(update_obj)
                _append_progress_line_with_merge(
                    progress_lines=progress_lines,
                    progress_merge_keys=progress_merge_keys,
                    progress_text=progress_text,
                    merge_key=merge_key,
                )
                # Keep expandable thinking focused on assistant narration only.
                if _should_collect_thinking_update(update_obj):
                    _append_progress_line_with_merge(
                        progress_lines=all_progress_lines,
                        progress_merge_keys=all_progress_merge_keys,
                        progress_text=progress_text,
                        merge_key=merge_key,
                    )
                full_text = _with_engine_badge(
                    _join_progress_lines_for_display(progress_lines), active_engine
                )

                # If accumulated text exceeds Telegram limit, freeze current
                # message and start a new one
                if len(full_text) > 3800:
                    await _cancel_progress_flush_task()
                    pending_progress_text = None
                    frozen_messages.append(progress_msg)
                    progress_lines.clear()
                    progress_merge_keys.clear()
                    _append_progress_line_with_merge(
                        progress_lines=progress_lines,
                        progress_merge_keys=progress_merge_keys,
                        progress_text=progress_text,
                        merge_key=merge_key,
                    )
                    full_text = _with_engine_badge(progress_text, active_engine)
                    last_progress_text = ""
                    # Remove cancel button from old message
                    try:
                        await progress_msg.edit_reply_markup(reply_markup=None)
                    except Exception:
                        pass
                    progress_msg = await _reply_text_resilient(
                        progress_msg,
                        full_text,
                        parse_mode="Markdown",
                        reply_markup=cancel_keyboard,
                    )
                    last_progress_text = full_text
                    last_progress_edit_ts = stream_loop.time()
                    return

                # Skip edit if content hasn't changed
                if full_text == last_progress_text:
                    return

                pending_progress_text = full_text
                if _is_high_priority_stream_update(update_obj):
                    await _cancel_progress_flush_task()
                    await _flush_pending_progress(force=True)
                else:
                    _schedule_progress_flush()
            except Exception as e:
                logger.warning("Failed to process stream update", error=str(e))

        # Build permission handler only when SDK permission gate is enabled
        settings_obj: Settings = context.bot_data["settings"]
        permission_handler = build_permission_handler(
            bot=context.bot,
            chat_id=input_chat_id,
            settings=settings_obj,
            chat_type=getattr(effective_chat, "type", None),
            message_thread_id=getattr(
                update.effective_message, "message_thread_id", None
            ),
        )
        current_thread_id_raw = getattr(
            update.effective_message, "message_thread_id", 0
        )
        try:
            current_thread_id = (
                int(current_thread_id_raw) if current_thread_id_raw is not None else 0
            )
        except (TypeError, ValueError):
            current_thread_id = 0
        reaction_feedback, reaction_feedback_scope_state = (
            _resolve_pending_reaction_feedback(
                context=context,
                user_id=user_id,
                chat_id=input_chat_id,
                thread_id=current_thread_id,
                scope_state=scope_state,
            )
        )
        model_prompt = _compose_prompt_with_reaction_feedback(
            message_text, reaction_feedback
        )
        if reaction_feedback:
            logger.info(
                "Applying pending reaction feedback to model prompt",
                user_id=user_id,
                scope_key=scope_key,
                signal=reaction_feedback.get("signal"),
                emoji=reaction_feedback.get("emoji"),
                source_message_id=reaction_feedback.get("message_id"),
            )

        # Run Claude command as cancellable task

        async def _run_claude() -> Any:
            return await cli_integration.run_command(
                prompt=model_prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=stream_handler,
                force_new_session=force_new_session,
                permission_handler=permission_handler,
                model=_resolve_model_override(
                    scope_state, active_engine, cli_integration
                ),
            )

        task = asyncio.create_task(_run_claude())

        # Register task for cancel support
        if task_registry:
            await task_registry.register(
                user_id,
                task,
                prompt_summary=message_text,
                progress_message_id=progress_msg.message_id,
                chat_id=input_chat_id,
                scope_key=scope_key,
            )

        command_succeeded = False
        blocked_local_image_fallback = False
        command_wait_started = time.monotonic()
        try:
            claude_response = await task
            command_succeeded = True

            # Mark task as completed
            if task_registry:
                await task_registry.complete(user_id, scope_key=scope_key)

            # Update session ID
            scope_state["claude_session_id"] = claude_response.session_id
            # Consume force_new_session only after success
            scope_state.pop("force_new_session", None)
            if reaction_feedback:
                _clear_pending_reaction_feedback(reaction_feedback_scope_state)

            # Check if Claude changed the working directory and update our tracking
            _update_working_directory_from_claude_response(
                claude_response, scope_state, settings, user_id
            )
            blocked_local_image_fallback = (
                _enforce_no_local_image_fallback_for_image_gen(claude_response)
            )
            if blocked_local_image_fallback:
                logger.warning(
                    "Blocked local PIL fallback after image-gen failure",
                    user_id=user_id,
                    scope_key=scope_key,
                    engine=active_engine,
                )

            # Log interaction to storage
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=message_text,
                        response=claude_response,
                        ip_address=None,  # Telegram doesn't provide IP
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction to storage", error=str(e))

            # Format response
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

        except asyncio.CancelledError:
            request_outcome = "cancelled"
            logger.info("Claude task cancelled by user", user_id=user_id)
            await _cancel_progress_flush_task()
            if task_registry:
                await task_registry.remove(user_id, scope_key=scope_key)
            # Preserve thinking process with cancelled label
            if all_progress_lines:
                summary_text = "[Cancelled] " + _generate_thinking_summary(
                    all_progress_lines
                )
                thinking_keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "View thinking process",
                                callback_data=f"thinking:expand:{progress_msg.message_id}",
                            )
                        ]
                    ]
                )
                try:
                    await progress_msg.edit_text(
                        summary_text,
                        parse_mode="Markdown",
                        reply_markup=thinking_keyboard,
                    )
                    _cache_thinking_data(
                        context,
                        progress_msg.message_id,
                        all_progress_lines,
                        summary_text,
                    )
                except Exception:
                    pass
            else:
                try:
                    await progress_msg.edit_text("Task cancelled.", reply_markup=None)
                except Exception:
                    pass
            # Clean up frozen messages
            for frozen_msg in frozen_messages:
                try:
                    await frozen_msg.delete()
                except Exception:
                    pass
            if reaction_controller:
                await reaction_controller.clear()
            else:
                await _set_message_reaction_safe(
                    context.bot,
                    chat_id=input_chat_id,
                    message_id=input_message_id,
                    emoji=None,
                )
            return
        except ClaudeToolValidationError as e:
            request_outcome = "tool_validation_error"
            # Tool validation error with detailed instructions
            logger.error(
                "Tool validation error",
                error=str(e),
                user_id=user_id,
                blocked_tools=e.blocked_tools,
            )
            # Error message already formatted, create FormattedMessage
            from ..utils.formatting import FormattedMessage

            formatted_messages = [FormattedMessage(str(e), parse_mode="Markdown")]
        except Exception as e:
            request_outcome = "command_error"
            logger.error(
                "CLI integration failed",
                error=str(e),
                user_id=user_id,
                engine=active_engine,
            )
            if task_registry:
                await task_registry.fail(user_id, scope_key=scope_key)
            # Format error and create FormattedMessage
            from ..utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(
                    _format_error_message(str(e), engine=active_engine),
                    parse_mode="Markdown",
                )
            ]
        finally:
            if request_timing.command_wall_ms is None:
                request_timing.command_wall_ms = _elapsed_monotonic_ms(
                    command_wait_started
                )

        # Clean up task registry
        if task_registry:
            await task_registry.remove(user_id, scope_key=scope_key)
        await _cancel_progress_flush_task()

        # Build context tag for display in thinking summary or reply header
        codex_snapshot, session_context_summary, rate_limit_summary = (
            await _resolve_codex_context_snapshot(
                active_engine=active_engine,
                session_id=scope_state.get("claude_session_id"),
                cli_integration=cli_integration,
                working_directory=Path(
                    scope_state.get("current_directory", settings.approved_directory)
                ),
                current_model=scope_state.get("claude_model"),
            )
        )
        context_tag = _build_context_tag(
            scope_state=scope_state,
            approved_directory=settings.approved_directory,
            active_engine=active_engine,
            session_id=scope_state.get("claude_session_id"),
            session_context_summary=session_context_summary,
            rate_limit_summary=rate_limit_summary,
        )
        collapsed_fallback_model = _resolve_collapsed_fallback_model(
            active_engine=active_engine,
            scope_state=scope_state,
            claude_response=claude_response,
            codex_snapshot=codex_snapshot,
        )
        has_thinking_summary = False

        # Collapse progress message into summary with expand button
        if all_progress_lines:
            summary_text = _build_collapsed_thinking_summary(
                all_progress_lines,
                context_tag,
                fallback_model=collapsed_fallback_model,
            )
            has_thinking_summary = True
            thinking_keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "View thinking process",
                            callback_data=f"thinking:expand:{progress_msg.message_id}",
                        )
                    ]
                ]
            )
            try:
                await progress_msg.edit_text(
                    summary_text,
                    parse_mode="Markdown",
                    reply_markup=thinking_keyboard,
                )
                _cache_thinking_data(
                    context, progress_msg.message_id, all_progress_lines, summary_text
                )
            except Exception as e:
                logger.warning("Failed to edit progress to summary", error=str(e))
                try:
                    await progress_msg.delete()
                except Exception:
                    pass
        else:
            try:
                await progress_msg.delete()
            except Exception:
                pass

        # Delete frozen progress messages (from overflow)
        for frozen_msg in frozen_messages:
            try:
                await frozen_msg.delete()
            except Exception:
                pass

        if reaction_controller:
            if command_succeeded:
                await reaction_controller.set_done()
            else:
                await reaction_controller.set_error()

        # Send formatted responses (may be multiple messages)
        final_draft_sent = False
        response_message_count = len(formatted_messages)
        for i, message in enumerate(formatted_messages):
            try:
                msg_text = message.text
                reply_to_id = input_message_id if i == 0 else None
                # Prepend context tag to the first message when no thinking summary
                if i == 0 and not has_thinking_summary and context_tag:
                    context_prefix = context_tag + "\n\n"
                    if len(context_prefix) + len(msg_text) <= _TELEGRAM_MESSAGE_LIMIT:
                        msg_text = context_prefix + msg_text
                    else:
                        context_send_started_at = time.monotonic()
                        await _reply_text_resilient(
                            telegram_message,
                            context_tag,
                            parse_mode="Markdown",
                            reply_to_message_id=reply_to_id,
                            bot=context.bot,
                            chat_type=getattr(effective_chat, "type", None),
                        )
                        request_timing.record_final_reply(
                            _elapsed_monotonic_ms(context_send_started_at)
                        )
                        reply_to_id = None

                if i == 0 and not final_draft_sent:
                    final_draft_started_at = time.monotonic()
                    final_draft_sent = await _send_private_final_response_draft(
                        bot=context.bot,
                        chat_id=input_chat_id,
                        chat_type=resolved_chat_type,
                        message_thread_id=current_message_thread_id,
                        draft_id=draft_id,
                        text=msg_text,
                        parse_mode=message.parse_mode,
                    )
                    if final_draft_sent:
                        request_timing.record_final_draft(
                            _elapsed_monotonic_ms(final_draft_started_at)
                        )

                final_reply_started_at = time.monotonic()
                await _reply_text_resilient(
                    telegram_message,
                    msg_text,
                    parse_mode=message.parse_mode,
                    reply_markup=message.reply_markup,
                    reply_to_message_id=reply_to_id,
                    bot=context.bot,
                    chat_type=getattr(effective_chat, "type", None),
                )
                request_timing.record_final_reply(
                    _elapsed_monotonic_ms(final_reply_started_at)
                )

                # Small delay between messages to avoid rate limits
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(
                    "Failed to send response message", error=str(e), message_index=i
                )
                # Try to send error message
                await _reply_text_resilient(
                    telegram_message,
                    _with_engine_badge(
                        f"❌ {_engine_label(active_engine)} 响应发送失败，请重试。",
                        active_engine,
                    ),
                    reply_to_message_id=input_message_id if i == 0 else None,
                    bot=context.bot,
                    chat_type=getattr(effective_chat, "type", None),
                )

        if not blocked_local_image_fallback:
            await _send_generated_images_from_response(
                update=update,
                context=context,
                claude_response=claude_response,
                scope_state=scope_state,
                reply_to_message_id=input_message_id,
            )

        # Update session info
        scope_state["last_message"] = message_text

        # Add conversation enhancements if available
        features = context.bot_data.get("features")
        conversation_enhancer = (
            features.get_conversation_enhancer() if features else None
        )

        if conversation_enhancer and claude_response:
            try:
                # Update conversation context
                conversation_enhancer.update_context(
                    user_id=user_id,
                    response=claude_response,
                )
                # 关闭自动会话建议按钮，避免额外 UI 干扰。
                # 保留上下文更新，后续如需恢复可在此处重新启用发送逻辑。

            except Exception as e:
                logger.warning(
                    "Conversation enhancement failed", error=str(e), user_id=user_id
                )

        # Log successful message processing
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],  # First 100 chars
                success=True,
            )

        request_outcome = "success"
        logger.info("Text message processed successfully", user_id=user_id)

    except Exception as e:
        request_outcome = "handler_error"
        # Clean up progress message: collapse to summary if possible
        try:
            if all_progress_lines:
                summary_text = "[Error] " + _generate_thinking_summary(
                    all_progress_lines
                )
                thinking_keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "View thinking process",
                                callback_data=f"thinking:expand:{progress_msg.message_id}",
                            )
                        ]
                    ]
                )
                await progress_msg.edit_text(
                    summary_text,
                    parse_mode="Markdown",
                    reply_markup=thinking_keyboard,
                )
                _cache_thinking_data(
                    context, progress_msg.message_id, all_progress_lines, summary_text
                )
            else:
                await progress_msg.delete()
        except Exception:
            pass

        # Clean up frozen messages
        for frozen_msg in frozen_messages:
            try:
                await frozen_msg.delete()
            except Exception:
                pass

        error_msg = _format_error_message(
            str(e),
            engine=locals().get("active_engine", ENGINE_CLAUDE),
        )
        await _reply_text_resilient(
            telegram_message,
            _with_engine_badge(error_msg, locals().get("active_engine", ENGINE_CLAUDE)),
            parse_mode="Markdown",
        )
        if reaction_controller:
            await reaction_controller.set_error()
        else:
            await _set_message_reaction_safe(
                context.bot,
                chat_id=input_chat_id,
                message_id=input_message_id,
                emoji=_BOT_REACTION_FAILED,
            )

        # Log failed processing
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=False,
            )

        logger.error("Error processing text message", error=str(e), user_id=user_id)
    finally:
        logger.info(
            "Text message timing diagnostics",
            user_id=user_id,
            scope_key=scope_key,
            outcome=request_outcome,
            engine=active_engine,
            session_id=(
                getattr(claude_response, "session_id", None)
                or scope_state.get("claude_session_id")
            ),
            cli_reported_duration_ms=getattr(claude_response, "duration_ms", None),
            response_message_count=response_message_count,
            **request_timing.to_log_fields(
                total_wall_ms=_elapsed_monotonic_ms(
                    request_timing.request_started_monotonic
                )
            ),
        )
        if reaction_controller:
            await reaction_controller.shutdown()
        typing_stop_event.set()
        if typing_heartbeat_task and not typing_heartbeat_task.done():
            typing_heartbeat_task.cancel()
            try:
                await typing_heartbeat_task
            except asyncio.CancelledError:
                pass
        await _dispatch_next_inbound_task(
            context=context,
            user_id=user_id,
            scope_key=scope_key,
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file uploads."""
    effective_user = update.effective_user
    telegram_message = update.message
    effective_chat = update.effective_chat
    if effective_user is None or telegram_message is None or effective_chat is None:
        return

    document = telegram_message.document
    if document is None:
        return

    chat_id = effective_chat.id
    if not isinstance(chat_id, int):
        return

    user_id = effective_user.id
    document_name = str(document.file_name or "uploaded_file")
    raw_file_size = document.file_size
    document_size = (
        int(raw_file_size)
        if isinstance(raw_file_size, (int, float)) and raw_file_size >= 0
        else 0
    )
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

    # Get services
    security_validator: Optional[SecurityValidator] = context.bot_data.get(
        "security_validator"
    )
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")

    logger.info(
        "Processing document upload",
        user_id=user_id,
        filename=document_name,
        file_size=document_size,
    )

    try:
        # Validate filename using security validator
        if security_validator:
            valid, error = security_validator.validate_filename(document_name)
            if not valid:
                await _reply_text_resilient(
                    telegram_message, f"❌ **File Upload Rejected**\n\n{error}"
                )

                # Log security violation
                if audit_logger:
                    await audit_logger.log_security_violation(
                        user_id=user_id,
                        violation_type="invalid_file_upload",
                        details=f"Filename: {document_name}, Error: {error}",
                        severity="medium",
                    )
                return

        # Check file size limits
        max_size = 10 * 1024 * 1024  # 10MB
        if document_size > max_size:
            await _reply_text_resilient(
                telegram_message,
                f"❌ **File Too Large**\n\n"
                f"Maximum file size: {max_size // 1024 // 1024}MB\n"
                f"Your file: {document_size / 1024 / 1024:.1f}MB",
            )
            return

        # Send processing indicator
        await telegram_message.chat.send_action("upload_document")

        progress_msg = await _reply_text_resilient(
            telegram_message,
            _with_engine_badge(
                f"📄 Processing file: `{document_name}`...",
                active_engine,
            ),
            parse_mode="Markdown",
        )

        # Check if enhanced file handler is available
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None

        if file_handler:
            # Use enhanced file handler
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    telegram_message.caption or "Please review this file:",
                )
                prompt = processed_file.prompt

                # Update progress message with file type info
                await progress_msg.edit_text(
                    _with_engine_badge(
                        f"📄 Processing {processed_file.type} file: `{document_name}`...",
                        active_engine,
                    ),
                    parse_mode="Markdown",
                )

            except Exception as e:
                logger.warning(
                    "Enhanced file handler failed, falling back to basic handler",
                    error=str(e),
                )
                file_handler = None  # Fall back to basic handling

        if not file_handler:
            # Fall back to basic file handling
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()

            # Try to decode as text
            try:
                content = file_bytes.decode("utf-8")

                # Check content length
                max_content_length = 50000  # 50KB of text
                if len(content) > max_content_length:
                    content = (
                        content[:max_content_length]
                        + "\n... (file truncated for processing)"
                    )

                # Create prompt with file content
                caption = telegram_message.caption or "Please review this file:"
                prompt = (
                    f"{caption}\n\n**File:** `{document_name}`\n\n```\n{content}\n```"
                )

            except UnicodeDecodeError:
                await progress_msg.edit_text(
                    _with_engine_badge(
                        "❌ **File Format Not Supported**\n\n"
                        "File must be text-based and UTF-8 encoded.\n\n"
                        "**Supported formats:**\n"
                        "• Source code files (.py, .js, .ts, etc.)\n"
                        "• Text files (.txt, .md)\n"
                        "• Configuration files (.json, .yaml, .toml)\n"
                        "• Documentation files",
                        active_engine,
                    )
                )
                return

        # Delete progress message
        await progress_msg.delete()

        # Create a new progress message for CLI processing
        claude_progress_msg = await _reply_text_resilient(
            telegram_message,
            _with_engine_badge("🤖 正在处理文件...", active_engine),
            parse_mode="Markdown",
        )

        if not cli_integration:
            await claude_progress_msg.edit_text(
                _with_engine_badge(
                    "❌ **CLI 引擎不可用**\n\n" "当前 CLI 引擎未正确配置。",
                    active_engine,
                ),
                parse_mode="Markdown",
            )
            return

        # Get current directory and session
        current_dir = scope_state.get("current_directory", settings.approved_directory)
        session_id = scope_state.get("claude_session_id")
        force_new_session = scope_state.get("force_new_session", False)
        permission_handler = build_permission_handler(
            bot=context.bot,
            chat_id=chat_id,
            settings=settings,
            chat_type=getattr(effective_chat, "type", None),
            message_thread_id=getattr(
                update.effective_message, "message_thread_id", None
            ),
        )

        # Process with Claude
        blocked_local_image_fallback = False
        try:
            claude_response = await cli_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                force_new_session=force_new_session,
                permission_handler=permission_handler,
                model=_resolve_model_override(
                    scope_state, active_engine, cli_integration
                ),
            )

            # Update session ID
            scope_state["claude_session_id"] = claude_response.session_id
            scope_state.pop("force_new_session", None)

            # Check if Claude changed the working directory and update our tracking
            _update_working_directory_from_claude_response(
                claude_response, scope_state, settings, user_id
            )
            blocked_local_image_fallback = (
                _enforce_no_local_image_fallback_for_image_gen(claude_response)
            )
            if blocked_local_image_fallback:
                logger.warning(
                    "Blocked local PIL fallback after image-gen failure",
                    user_id=user_id,
                    engine=active_engine,
                )

            # Format and send response
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            # Delete progress message
            await claude_progress_msg.delete()

            # Build context tag for CLI mode reply header
            _, cli_session_context_summary, cli_rate_limit_summary = (
                await _resolve_codex_context_snapshot(
                    active_engine=active_engine,
                    session_id=scope_state.get("claude_session_id"),
                    cli_integration=cli_integration,
                    working_directory=Path(
                        scope_state.get(
                            "current_directory", settings.approved_directory
                        )
                    ),
                    current_model=scope_state.get("claude_model"),
                )
            )
            cli_context_tag = _build_context_tag(
                scope_state=scope_state,
                approved_directory=settings.approved_directory,
                active_engine=active_engine,
                session_id=scope_state.get("claude_session_id"),
                session_context_summary=cli_session_context_summary,
                rate_limit_summary=cli_rate_limit_summary,
            )

            # Send responses
            for i, message in enumerate(formatted_messages):
                msg_text = message.text
                reply_to_id = telegram_message.message_id if i == 0 else None
                if i == 0 and cli_context_tag:
                    context_prefix = cli_context_tag + "\n\n"
                    if len(context_prefix) + len(msg_text) <= _TELEGRAM_MESSAGE_LIMIT:
                        msg_text = context_prefix + msg_text
                    else:
                        await _reply_text_resilient(
                            telegram_message,
                            cli_context_tag,
                            parse_mode="Markdown",
                            reply_to_message_id=reply_to_id,
                            bot=context.bot,
                            chat_type=getattr(effective_chat, "type", None),
                        )
                        reply_to_id = None

                await _reply_text_resilient(
                    telegram_message,
                    msg_text,
                    parse_mode=message.parse_mode,
                    reply_markup=message.reply_markup,
                    reply_to_message_id=reply_to_id,
                    bot=context.bot,
                    chat_type=getattr(effective_chat, "type", None),
                )

                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

            if not blocked_local_image_fallback:
                await _send_generated_images_from_response(
                    update=update,
                    context=context,
                    claude_response=claude_response,
                    scope_state=scope_state,
                    reply_to_message_id=telegram_message.message_id,
                )

        except Exception as e:
            await claude_progress_msg.edit_text(
                _with_engine_badge(
                    _format_error_message(str(e), engine=active_engine),
                    active_engine,
                ),
                parse_mode="Markdown",
            )
            logger.error(
                "CLI file processing failed",
                error=str(e),
                user_id=user_id,
                engine=active_engine,
            )

        # Log successful file processing
        if audit_logger:
            await audit_logger.log_file_access(
                user_id=user_id,
                file_path=document_name,
                action="upload_processed",
                success=True,
                file_size=document_size,
            )

    except Exception as e:
        try:
            await progress_msg.delete()
        except Exception:
            pass

        error_msg = _with_engine_badge(
            _format_error_message(str(e), engine=active_engine),
            active_engine,
        )
        await _reply_text_resilient(telegram_message, error_msg, parse_mode="Markdown")

        # Log failed file processing
        if audit_logger:
            await audit_logger.log_file_access(
                user_id=user_id,
                file_path=document_name,
                action="upload_failed",
                success=False,
                file_size=document_size,
            )

        logger.error("Error processing document", error=str(e), user_id=user_id)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo uploads."""
    effective_user = update.effective_user
    telegram_message = update.message
    effective_chat = update.effective_chat
    if effective_user is None or telegram_message is None or effective_chat is None:
        return

    chat_id = effective_chat.id
    if not isinstance(chat_id, int):
        return

    user_id = effective_user.id
    queued_grouped_photos = getattr(update, "_queued_grouped_photos", None)
    queued_grouped_caption = getattr(update, "_queued_grouped_caption", None)
    queued_source_message_id = getattr(update, "_queued_source_message_id", None)
    if isinstance(queued_grouped_photos, list) and queued_grouped_photos:
        media_group_ready = True
        grouped_photos = list(queued_grouped_photos)
        grouped_caption = (
            str(queued_grouped_caption)
            if isinstance(queued_grouped_caption, str)
            else None
        )
        source_message_id = (
            int(queued_source_message_id)
            if isinstance(queued_source_message_id, int)
            else getattr(telegram_message, "message_id", None)
        )
    else:
        (
            media_group_ready,
            grouped_photos,
            grouped_caption,
            source_message_id,
        ) = await _collect_media_group_photos(update, context)
    if not media_group_ready:
        return

    reply_target_message_id = (
        source_message_id
        if isinstance(source_message_id, int) and source_message_id > 0
        else getattr(telegram_message, "message_id", None)
    )
    photo_count = len(grouped_photos)
    if photo_count > 1:
        logger.info(
            "Merged inbound photo media_group",
            user_id=user_id,
            media_group_id=getattr(telegram_message, "media_group_id", None),
            photo_count=photo_count,
            source_message_id=reply_target_message_id,
        )

    settings: Settings = context.bot_data["settings"]
    scope_key, scope_state = get_scope_state_from_update(
        user_data=context.user_data,
        update=update,
        default_directory=settings.approved_directory,
    )

    # Check if enhanced image handler is available
    features = context.bot_data.get("features")
    image_handler = features.get_image_handler() if features else None

    if image_handler:
        task_registry: Optional[TaskRegistry] = context.bot_data.get("task_registry")
        if task_registry and await task_registry.is_busy(user_id, scope_key=scope_key):
            queued_meta = await _enqueue_busy_photo_task(
                update=update,
                context=context,
                user_id=user_id,
                scope_key=scope_key,
                grouped_photos=grouped_photos,
                grouped_caption=grouped_caption,
                source_message_id=reply_target_message_id,
            )
            if queued_meta is None:
                await _reply_text_resilient(
                    telegram_message,
                    "A task is already running. Use /cancel to cancel it.",
                )
            else:
                queue_id, position = queued_meta
                pending_before = max(position - 1, 0)
                await _reply_text_resilient(
                    telegram_message,
                    (
                        f"Task is running, queued as #{queue_id}. "
                        f"{pending_before} task(s) ahead. "
                        f"Use /queue to view, /dequeue {queue_id} to remove."
                    ),
                    reply_markup=_build_queue_remove_keyboard(queue_id),
                )
            return

        try:
            last_status_text = ""
            thinking_lines: list[str] = []
            thinking_merge_keys: list[Optional[str]] = []
            progress_msg: Any = None
            cancel_keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Cancel", callback_data="cancel:task")]]
            )
            active_engine, cli_integration = get_cli_integration(
                bot_data=context.bot_data,
                scope_state=scope_state,
            )
            progress_lines: list[str] = []
            progress_merge_keys: list[Optional[str]] = []
            stream_mode = False
            pending_stream_text: Optional[str] = None
            stream_flush_task: Optional[asyncio.Task] = None
            stream_flush_lock = asyncio.Lock()
            turn_started_shown = False
            resolved_chat_type = getattr(effective_chat, "type", None)
            current_message_thread_id = getattr(
                telegram_message, "message_thread_id", None
            )
            draft_seed = (
                reply_target_message_id
                if isinstance(reply_target_message_id, int)
                and reply_target_message_id > 0
                else int(getattr(telegram_message, "message_id", 0) or 0)
            )
            if draft_seed <= 0:
                draft_seed = int(time.time() * 1000) % 2147483647
            draft_id = draft_seed or 1
            stream_loop = asyncio.get_event_loop()
            debounce_ms = int(getattr(settings, "stream_render_debounce_ms", 1000) or 0)
            min_edit_interval_ms = int(
                getattr(settings, "stream_render_min_edit_interval_ms", 1000) or 0
            )
            debounce_seconds = max(debounce_ms, 0) / 1000
            min_edit_interval_seconds = max(min_edit_interval_ms, 0) / 1000
            last_status_edit_ts = stream_loop.time()

            async def _edit_progress_message(rendered_text: str) -> None:
                nonlocal progress_msg, last_status_text, last_status_edit_ts

                if rendered_text == last_status_text:
                    return
                try:
                    await progress_msg.edit_text(
                        rendered_text,
                        parse_mode="Markdown",
                        reply_markup=cancel_keyboard,
                    )
                    last_status_text = rendered_text
                    last_status_edit_ts = stream_loop.time()
                except Exception as e:
                    if _is_noop_edit_error(e):
                        last_status_text = rendered_text
                        last_status_edit_ts = stream_loop.time()
                        return

                    fallback_error: Exception | None = None
                    timeout_error = _is_timeout_error(e)
                    try:
                        await progress_msg.edit_text(
                            rendered_text,
                            reply_markup=cancel_keyboard,
                        )
                        last_status_text = rendered_text
                        last_status_edit_ts = stream_loop.time()
                        return
                    except Exception as exc:
                        fallback_error = exc
                        if _is_noop_edit_error(exc):
                            last_status_text = rendered_text
                            last_status_edit_ts = stream_loop.time()
                            return
                        timeout_error = timeout_error or _is_timeout_error(exc)

                    if timeout_error:
                        try:
                            await progress_msg.edit_reply_markup(reply_markup=None)
                        except Exception:
                            pass
                        progress_msg = await _reply_text_resilient(
                            progress_msg,
                            rendered_text,
                            parse_mode="Markdown",
                            reply_markup=cancel_keyboard,
                        )
                        last_status_text = rendered_text
                        last_status_edit_ts = stream_loop.time()
                        return

                    logger.warning(
                        "Failed to update image status message",
                        error=str(e),
                        fallback_error=str(fallback_error) if fallback_error else None,
                        user_id=user_id,
                    )
                    try:
                        progress_msg = await _reply_text_resilient(
                            telegram_message,
                            rendered_text,
                            parse_mode="Markdown",
                            reply_to_message_id=reply_target_message_id,
                            reply_markup=cancel_keyboard,
                        )
                        last_status_text = rendered_text
                        last_status_edit_ts = stream_loop.time()
                    except Exception as send_error:
                        logger.warning(
                            "Failed to send fallback image status message",
                            error=str(send_error),
                            user_id=user_id,
                        )

            async def _set_image_status(
                text: str, *, force_when_streaming: bool = False
            ) -> None:
                bubble_text = _with_engine_badge(text, active_engine)
                if stream_mode and not force_when_streaming:
                    return
                await _edit_progress_message(bubble_text)

            async def _flush_pending_stream(force: bool = False) -> None:
                nonlocal pending_stream_text

                async with stream_flush_lock:
                    if not pending_stream_text:
                        return

                    now = stream_loop.time()
                    wait_seconds = 0.0
                    if not force:
                        wait_seconds = max(
                            0.0, min_edit_interval_seconds - (now - last_status_edit_ts)
                        )
                    if wait_seconds > 0:
                        await asyncio.sleep(wait_seconds)

                    text_to_send = pending_stream_text
                    if not text_to_send:
                        return
                    pending_stream_text = None
                    await _edit_progress_message(text_to_send)

            def _schedule_stream_flush() -> None:
                nonlocal stream_flush_task

                if stream_flush_task and not stream_flush_task.done():
                    return

                async def _runner() -> None:
                    try:
                        if debounce_seconds > 0:
                            await asyncio.sleep(debounce_seconds)
                        await _flush_pending_stream(force=False)
                    except asyncio.CancelledError:
                        return

                stream_flush_task = asyncio.create_task(_runner())

            async def _cancel_stream_flush_task() -> None:
                nonlocal stream_flush_task
                if stream_flush_task and not stream_flush_task.done():
                    stream_flush_task.cancel()
                    try:
                        await stream_flush_task
                    except asyncio.CancelledError:
                        pass
                stream_flush_task = None

            async def _image_stream_handler(update_obj: Any) -> None:
                nonlocal stream_mode, pending_stream_text, turn_started_shown
                try:
                    if _is_turn_started_update(update_obj):
                        if turn_started_shown:
                            return
                        turn_started_shown = True
                    progress_text = await _format_progress_update(update_obj)
                    if not progress_text:
                        return

                    stream_mode = True
                    merge_key = _get_stream_merge_key(update_obj)
                    _append_progress_line_with_merge(
                        progress_lines=progress_lines,
                        progress_merge_keys=progress_merge_keys,
                        progress_text=progress_text,
                        merge_key=merge_key,
                    )
                    full_text = _with_engine_badge(
                        _join_progress_lines_for_display(progress_lines), active_engine
                    )
                    while len(full_text) > 3800 and progress_lines:
                        progress_lines.pop(0)
                        if progress_merge_keys:
                            progress_merge_keys.pop(0)
                        full_text = _with_engine_badge(
                            _join_progress_lines_for_display(progress_lines),
                            active_engine,
                        )
                    if full_text == last_status_text:
                        return
                    pending_stream_text = full_text

                    # Keep behavior aligned with text flow and only cache
                    # assistant narration for expandable thinking details.
                    if _should_collect_thinking_update(update_obj):
                        _append_progress_line_with_merge(
                            progress_lines=thinking_lines,
                            progress_merge_keys=thinking_merge_keys,
                            progress_text=progress_text,
                            merge_key=merge_key,
                        )

                    if _is_high_priority_stream_update(update_obj):
                        await _cancel_stream_flush_task()
                        await _flush_pending_stream(force=True)
                    else:
                        _schedule_stream_flush()
                except Exception as e:
                    logger.warning(
                        "Failed to collect image stream progress",
                        error=str(e),
                        user_id=user_id,
                    )

            # Send processing indicator (single message that will be updated)
            received_hint = (
                f"已接收 {photo_count} 张图片（同组）"
                if photo_count > 1
                else "已接收图片"
            )
            initial_status = _with_engine_badge(
                _build_image_stage_status(1, received_hint),
                active_engine,
            )
            progress_msg = await _reply_text_resilient(
                telegram_message,
                initial_status,
                parse_mode="Markdown",
                reply_to_message_id=reply_target_message_id,
                reply_markup=cancel_keyboard,
            )
            last_status_text = initial_status

            if not cli_integration:
                await _set_image_status(
                    "❌ **CLI 引擎不可用**\n\n"
                    "当前 CLI 引擎未正确配置，请检查服务配置。"
                )
                return

            if not _integration_supports_image_analysis(cli_integration):
                integrations = context.bot_data.get("cli_integrations") or {}
                claude_integration = integrations.get(ENGINE_CLAUDE)
                if (
                    active_engine != ENGINE_CLAUDE
                    and _integration_supports_image_analysis(claude_integration)
                ):
                    await _set_image_status(
                        "📸 **当前引擎不支持图片分析**\n\n"
                        f"当前引擎：`{active_engine}`\n"
                        "图片分析仅在 `claude` 引擎（SDK 模式）可用。\n\n"
                        "**处理方式：**先执行 `/engine claude`，再重新上传图片。"
                    )
                    return

                await _set_image_status(
                    "📸 **图片分析需要 SDK 模式**\n\n"
                    "当前运行模式不支持图片多模态输入。\n\n"
                    "**处理方式：**将 `.env` 中 `USE_SDK` 设为 `true` 并重启机器人。"
                )
                return

            if not grouped_photos:
                await _set_image_status(
                    "❌ **图片内容为空**\n\n未检测到可处理的图片，请重试。"
                )
                return

            async def _image_progress(stage: str) -> None:
                if stage == "downloading":
                    await _set_image_status(
                        _build_image_stage_status(2, "正在从 Telegram 下载图片...")
                    )
                elif stage == "validating":
                    await _set_image_status(
                        _build_image_stage_status(3, "正在校验图片格式与大小...")
                    )
                elif stage == "encoding":
                    await _set_image_status(
                        _build_image_stage_status(3, "正在编码图片数据...")
                    )

            # Process image(s) with enhanced handler
            processed_images = []
            for idx, photo in enumerate(grouped_photos):
                processed = await image_handler.process_image(
                    photo,
                    grouped_caption if idx == 0 else None,
                    on_progress=_image_progress,
                )
                processed_images.append(processed)

            model_prompt = str(grouped_caption or "").strip()
            if not model_prompt and processed_images:
                if len(processed_images) == 1:
                    model_prompt = processed_images[0].prompt
                else:
                    model_prompt = (
                        f"Please analyze these {len(processed_images)} images in order "
                        "and provide one consolidated response."
                    )

            # Get current directory and session
            current_dir = Path(
                scope_state.get("current_directory", settings.approved_directory)
            )
            session_id = scope_state.get("claude_session_id")
            force_new_session = scope_state.get("force_new_session", False)
            permission_handler = build_permission_handler(
                bot=context.bot,
                chat_id=chat_id,
                settings=settings,
                chat_type=getattr(effective_chat, "type", None),
                message_thread_id=getattr(
                    update.effective_message, "message_thread_id", None
                ),
            )

            # Process with Claude
            cli_image_files: list[Path] = []
            blocked_local_image_fallback = False
            try:
                # Build image data for multimodal input
                images = []
                for processed_image in processed_images:
                    img_format = processed_image.metadata.get("format", "jpeg")
                    if img_format == "unknown":
                        img_format = "jpeg"  # Default to JPEG for unknown formats
                    images.append(
                        {
                            "base64_data": processed_image.base64_data,
                            "media_type": f"image/{img_format}",
                        }
                    )

                if _integration_uses_cli_image_files(cli_integration):
                    for idx, processed_image in enumerate(processed_images):
                        img_format = processed_image.metadata.get("format", "jpeg")
                        if img_format == "unknown":
                            img_format = "jpeg"
                        cli_image_file = _persist_cli_image_file(
                            base64_data=processed_image.base64_data,
                            image_format=img_format,
                            working_directory=current_dir,
                        )
                        cli_image_files.append(cli_image_file)
                        images[idx]["file_path"] = str(cli_image_file)
                engine_label = "Codex" if active_engine == "codex" else "Claude"
                await _set_image_status(
                    _build_image_stage_status(
                        4,
                        (
                            f"正在提交 {len(images)} 张图片给 {engine_label}..."
                            if len(images) > 1
                            else f"正在提交图片给 {engine_label}..."
                        ),
                    )
                )
                await _set_image_status(
                    _build_image_analyzing_status(0, engine_label=engine_label)
                )

                async def _run_image_claude() -> Any:
                    return await _run_with_image_analysis_heartbeat(
                        run_coro=cli_integration.run_command(
                            prompt=model_prompt,
                            working_directory=current_dir,
                            user_id=user_id,
                            session_id=session_id,
                            on_stream=_image_stream_handler,
                            force_new_session=force_new_session,
                            permission_handler=permission_handler,
                            model=_resolve_model_override(
                                scope_state, active_engine, cli_integration
                            ),
                            images=images,
                        ),
                        update_status=_set_image_status,
                        engine_label=engine_label,
                    )

                image_task = asyncio.create_task(_run_image_claude())
                if task_registry:
                    await task_registry.register(
                        user_id,
                        image_task,
                        prompt_summary=model_prompt,
                        progress_message_id=progress_msg.message_id,
                        chat_id=chat_id,
                        scope_key=scope_key,
                    )

                try:
                    claude_response = await image_task
                    await _cancel_stream_flush_task()
                    await _flush_pending_stream(force=True)
                    if task_registry:
                        await task_registry.complete(user_id, scope_key=scope_key)
                except asyncio.CancelledError:
                    logger.info("Image Claude task cancelled by user", user_id=user_id)
                    await _cancel_stream_flush_task()
                    if thinking_lines:
                        summary_text = "[Cancelled] " + _generate_thinking_summary(
                            thinking_lines
                        )
                        thinking_keyboard = InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "View thinking process",
                                        callback_data=f"thinking:expand:{progress_msg.message_id}",
                                    )
                                ]
                            ]
                        )
                        try:
                            await progress_msg.edit_text(
                                summary_text,
                                parse_mode="Markdown",
                                reply_markup=thinking_keyboard,
                            )
                            _cache_thinking_data(
                                context,
                                progress_msg.message_id,
                                thinking_lines,
                                summary_text,
                            )
                        except Exception:
                            pass
                    else:
                        try:
                            await progress_msg.edit_text(
                                "Task cancelled.", reply_markup=None
                            )
                        except Exception:
                            pass
                    return
                except Exception:
                    await _cancel_stream_flush_task()
                    if task_registry:
                        await task_registry.fail(user_id, scope_key=scope_key)
                    raise
                finally:
                    await _cancel_stream_flush_task()
                    if task_registry:
                        await task_registry.remove(user_id, scope_key=scope_key)

                # Update session ID
                scope_state["claude_session_id"] = claude_response.session_id
                scope_state.pop("force_new_session", None)
                blocked_local_image_fallback = (
                    _enforce_no_local_image_fallback_for_image_gen(claude_response)
                )
                if blocked_local_image_fallback:
                    logger.warning(
                        "Blocked local PIL fallback after image-gen failure",
                        user_id=user_id,
                        engine=active_engine,
                    )

                # Format and send response
                from ..utils.formatting import ResponseFormatter

                if not stream_mode:
                    await _set_image_status(
                        _build_image_stage_status(6, "正在整理回复内容..."),
                        force_when_streaming=True,
                    )
                formatter = ResponseFormatter(settings)
                formatted_messages = formatter.format_claude_response(
                    claude_response.content
                )

                # Build context tag for image response
                (
                    img_codex_snapshot,
                    img_session_context_summary,
                    img_rate_limit_summary,
                ) = await _resolve_codex_context_snapshot(
                    active_engine=active_engine,
                    session_id=scope_state.get("claude_session_id"),
                    cli_integration=cli_integration,
                    working_directory=Path(
                        scope_state.get(
                            "current_directory", settings.approved_directory
                        )
                    ),
                    current_model=scope_state.get("claude_model"),
                )
                img_context_tag = _build_context_tag(
                    scope_state=scope_state,
                    approved_directory=settings.approved_directory,
                    active_engine=active_engine,
                    session_id=scope_state.get("claude_session_id"),
                    session_context_summary=img_session_context_summary,
                    rate_limit_summary=img_rate_limit_summary,
                )
                img_fallback_model = _resolve_collapsed_fallback_model(
                    active_engine=active_engine,
                    scope_state=scope_state,
                    claude_response=claude_response,
                    codex_snapshot=img_codex_snapshot,
                )
                img_has_thinking = False

                # Collapse progress message into thinking summary when available
                if thinking_lines:
                    summary_text = _build_collapsed_thinking_summary(
                        thinking_lines,
                        img_context_tag,
                        fallback_model=img_fallback_model,
                    )
                    img_has_thinking = True
                    thinking_keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "View thinking process",
                                    callback_data=f"thinking:expand:{progress_msg.message_id}",
                                )
                            ]
                        ]
                    )
                    try:
                        await progress_msg.edit_text(
                            summary_text,
                            parse_mode="Markdown",
                            reply_markup=thinking_keyboard,
                        )
                        _cache_thinking_data(
                            context,
                            progress_msg.message_id,
                            thinking_lines,
                            summary_text,
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to collapse image progress to summary",
                            error=str(e),
                            user_id=user_id,
                        )
                        try:
                            await progress_msg.delete()
                        except Exception:
                            pass
                else:
                    try:
                        await progress_msg.delete()
                    except Exception:
                        pass

                # Send responses
                final_draft_sent = False
                for i, message in enumerate(formatted_messages):
                    msg_text = message.text
                    reply_to_id = reply_target_message_id if i == 0 else None
                    if i == 0 and not img_has_thinking and img_context_tag:
                        context_prefix = img_context_tag + "\n\n"
                        if (
                            len(context_prefix) + len(msg_text)
                            <= _TELEGRAM_MESSAGE_LIMIT
                        ):
                            msg_text = context_prefix + msg_text
                        else:
                            await _reply_text_resilient(
                                telegram_message,
                                img_context_tag,
                                parse_mode="Markdown",
                                reply_to_message_id=reply_to_id,
                                bot=context.bot,
                                chat_type=getattr(effective_chat, "type", None),
                            )
                            reply_to_id = None

                    if i == 0 and not final_draft_sent:
                        final_draft_sent = await _send_private_final_response_draft(
                            bot=context.bot,
                            chat_id=chat_id,
                            chat_type=resolved_chat_type,
                            message_thread_id=current_message_thread_id,
                            draft_id=draft_id,
                            text=msg_text,
                            parse_mode=message.parse_mode,
                        )

                    await _reply_text_resilient(
                        telegram_message,
                        msg_text,
                        parse_mode=message.parse_mode,
                        reply_markup=message.reply_markup,
                        reply_to_message_id=reply_to_id,
                        bot=context.bot,
                        chat_type=getattr(effective_chat, "type", None),
                    )

                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)

                if not blocked_local_image_fallback:
                    await _send_generated_images_from_response(
                        update=update,
                        context=context,
                        claude_response=claude_response,
                        scope_state=scope_state,
                        reply_to_message_id=reply_target_message_id,
                    )

            except Exception as e:
                error_text = _format_error_message(str(e), engine=active_engine)
                error_bubble = _with_engine_badge(error_text, active_engine)
                try:
                    if thinking_lines:
                        summary_text = "[Error] " + _generate_thinking_summary(
                            thinking_lines
                        )
                        thinking_keyboard = InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "View thinking process",
                                        callback_data=f"thinking:expand:{progress_msg.message_id}",
                                    )
                                ]
                            ]
                        )
                        await progress_msg.edit_text(
                            summary_text,
                            parse_mode="Markdown",
                            reply_markup=thinking_keyboard,
                        )
                        _cache_thinking_data(
                            context,
                            progress_msg.message_id,
                            thinking_lines,
                            summary_text,
                        )
                        await _reply_text_resilient(
                            telegram_message,
                            error_bubble,
                            parse_mode="Markdown",
                            reply_to_message_id=reply_target_message_id,
                        )
                    else:
                        await progress_msg.edit_text(
                            error_bubble,
                            parse_mode="Markdown",
                            reply_markup=None,
                        )
                except Exception as send_error:
                    logger.warning(
                        "Failed to edit image progress message with error",
                        error=str(send_error),
                        original_error=str(e),
                        user_id=user_id,
                    )
                    await _reply_text_resilient(
                        telegram_message,
                        error_bubble,
                        parse_mode="Markdown",
                        reply_to_message_id=reply_target_message_id,
                    )
                logger.error(
                    "CLI image processing failed",
                    error=str(e),
                    user_id=user_id,
                    engine=active_engine,
                )
            finally:
                for cli_image_file in cli_image_files:
                    _cleanup_cli_image_file(cli_image_file)

        except Exception as e:
            logger.error("Image processing failed", error=str(e), user_id=user_id)
            await _reply_text_resilient(
                telegram_message,
                _with_engine_badge(
                    _format_error_message(
                        str(e),
                        engine=locals().get("active_engine", ENGINE_CLAUDE),
                    ),
                    locals().get("active_engine", ENGINE_CLAUDE),
                ),
                parse_mode="Markdown",
            )
        finally:
            await _dispatch_next_inbound_task(
                context=context,
                user_id=user_id,
                scope_key=scope_key,
            )
    else:
        # Fall back to unsupported message
        await _reply_text_resilient(
            telegram_message,
            "📸 **Photo Upload**\n\n"
            "Photo processing is not yet supported.\n\n"
            "**Currently supported:**\n"
            "• Text files (.py, .js, .md, etc.)\n"
            "• Configuration files\n"
            "• Documentation files\n\n"
            "**Coming soon:**\n"
            "• Image analysis\n"
            "• Screenshot processing\n"
            "• Diagram interpretation",
        )


def _normalize_reaction_token(reaction: Any) -> str:
    """Normalize Telegram reaction object into stable token string."""
    if reaction is None:
        return "unknown"
    if isinstance(reaction, str):
        normalized = reaction.strip()
        return normalized or "unknown"

    reaction_type = getattr(reaction, "type", None)
    if reaction_type == "emoji":
        emoji = str(getattr(reaction, "emoji", "")).strip()
        return f"emoji:{emoji}" if emoji else "emoji:unknown"
    if reaction_type == "custom_emoji":
        custom_id = str(getattr(reaction, "custom_emoji_id", "")).strip()
        return f"custom_emoji:{custom_id}" if custom_id else "custom_emoji:unknown"
    if isinstance(reaction_type, str) and reaction_type.strip():
        return reaction_type.strip()

    return type(reaction).__name__.lower()


def _extract_reaction_tokens(reactions: Any) -> list[str]:
    """Extract normalized reaction tokens from Telegram reaction payload."""
    if reactions is None:
        return []
    if isinstance(reactions, (str, bytes)):
        return [_normalize_reaction_token(reactions)]

    try:
        iterator = iter(reactions)
    except TypeError:
        return [_normalize_reaction_token(reactions)]

    tokens: list[str] = []
    for item in iterator:
        tokens.append(_normalize_reaction_token(item))
    return tokens


def _diff_reaction_tokens(
    old_tokens: list[str], new_tokens: list[str]
) -> tuple[list[str], list[str]]:
    """Compute added/removed reaction tokens with multiset semantics."""
    old_counter = Counter(old_tokens)
    new_counter = Counter(new_tokens)
    added: list[str] = []
    removed: list[str] = []

    for token, count in new_counter.items():
        delta = count - old_counter.get(token, 0)
        if delta > 0:
            added.extend([token] * delta)
    for token, count in old_counter.items():
        delta = count - new_counter.get(token, 0)
        if delta > 0:
            removed.extend([token] * delta)

    return added, removed


def _extract_reaction_count_counter(reactions: Any) -> Counter[str]:
    """Extract normalized reaction counters from message_reaction_count payload."""
    if reactions is None:
        return Counter()
    if isinstance(reactions, (str, bytes)):
        return Counter({_normalize_reaction_token(reactions): 1})

    try:
        iterator = iter(reactions)
    except TypeError:
        iterator = [reactions]

    counter: Counter[str] = Counter()
    for item in iterator:
        token = _normalize_reaction_token(getattr(item, "type", item))
        raw_total = getattr(item, "total_count", 1)
        try:
            total = int(raw_total)
        except (TypeError, ValueError):
            total = 0
        if total <= 0:
            continue
        counter[token] += total
    return counter


def _diff_reaction_counters(
    old_counter: Counter[str], new_counter: Counter[str]
) -> tuple[list[str], list[str]]:
    """Compute added/removed reaction tokens from counters."""
    added: list[str] = []
    removed: list[str] = []

    for token, count in new_counter.items():
        delta = count - old_counter.get(token, 0)
        if delta > 0:
            added.extend([token] * delta)
    for token, count in old_counter.items():
        delta = count - new_counter.get(token, 0)
        if delta > 0:
            removed.extend([token] * delta)

    return added, removed


def _counter_to_reaction_tokens(counter: Counter[str]) -> list[str]:
    """Expand reaction counter to token list (for logging/debug)."""
    tokens: list[str] = []
    for token, count in counter.items():
        if count <= 0:
            continue
        tokens.extend([token] * count)
    return tokens


def _resolve_actor_id_for_reaction_count(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    chat_type: Optional[str],
) -> Optional[int]:
    """Resolve actor id for anonymous reaction-count updates."""
    if chat_type == "private" and chat_id != 0:
        return chat_id

    settings: Optional[Settings] = context.bot_data.get("settings")
    allowed_users = getattr(settings, "allowed_users", None)
    if isinstance(allowed_users, list) and len(allowed_users) == 1:
        try:
            return int(allowed_users[0])
        except (TypeError, ValueError):
            return None

    return None


def _mark_reaction_update_seen(
    context: ContextTypes.DEFAULT_TYPE, update_id: Optional[int]
) -> bool:
    """Track reaction update IDs and return True when the update was seen."""
    if update_id is None:
        return False

    dedup_cache = context.bot_data.get(_REACTION_UPDATE_DEDUP_KEY)
    if not isinstance(dedup_cache, dict):
        dedup_cache = {}
        context.bot_data[_REACTION_UPDATE_DEDUP_KEY] = dedup_cache

    now = time.time()
    cutoff = now - _REACTION_UPDATE_DEDUP_TTL_SECONDS
    expired_keys = [
        key
        for key, raw_timestamp in dedup_cache.items()
        if not isinstance(raw_timestamp, (int, float)) or float(raw_timestamp) < cutoff
    ]
    for key in expired_keys:
        dedup_cache.pop(key, None)

    cache_key = str(update_id)
    if cache_key in dedup_cache:
        return True

    dedup_cache[cache_key] = now
    return False


async def handle_reaction_update_fallback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Fallback router for reaction updates via a generic TypeHandler."""
    has_reaction = getattr(update, "message_reaction", None) is not None
    has_reaction_count = getattr(update, "message_reaction_count", None) is not None
    if not has_reaction and not has_reaction_count:
        return

    logger.info(
        "Routing reaction update via generic fallback handler",
        update_id=getattr(update, "update_id", None),
        has_message_reaction=has_reaction,
        has_message_reaction_count=has_reaction_count,
    )
    await handle_message_reaction(update, context)


async def handle_message_reaction(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle Telegram message reaction updates."""
    reaction_update = getattr(update, "message_reaction", None)
    reaction_count_update = getattr(update, "message_reaction_count", None)
    if reaction_update is None and reaction_count_update is None:
        return

    update_id = getattr(update, "update_id", None)
    if _mark_reaction_update_seen(context, update_id):
        logger.debug("Skipping duplicate reaction update", update_id=update_id)
        return

    try:
        update_kind = (
            "message_reaction"
            if reaction_update is not None
            else "message_reaction_count"
        )

        actor_id: Optional[int] = None
        actor_display = "unknown"
        scoped_thread_id = 0

        if reaction_update is not None:
            actor_user = getattr(reaction_update, "user", None)
            actor_chat = getattr(reaction_update, "actor_chat", None)

            actor_id_raw = getattr(actor_user, "id", None)
            if actor_id_raw is None:
                actor_id_raw = getattr(actor_chat, "id", None)
            if actor_id_raw is not None:
                try:
                    actor_id = int(actor_id_raw)
                except (TypeError, ValueError):
                    actor_id = None

            old_tokens = _extract_reaction_tokens(
                getattr(reaction_update, "old_reaction", None)
            )
            new_tokens = _extract_reaction_tokens(
                getattr(reaction_update, "new_reaction", None)
            )
            added_tokens, removed_tokens = _diff_reaction_tokens(old_tokens, new_tokens)

            chat = getattr(reaction_update, "chat", None)
            chat_id = getattr(chat, "id", None)
            chat_type = getattr(chat, "type", None)
            message_id = getattr(reaction_update, "message_id", None)

            raw_thread_id = getattr(reaction_update, "message_thread_id", None)
            try:
                scoped_thread_id = (
                    int(raw_thread_id) if raw_thread_id is not None else 0
                )
            except (TypeError, ValueError):
                scoped_thread_id = 0

            actor_username = getattr(actor_user, "username", None) or getattr(
                actor_chat, "username", None
            )
            actor_title = getattr(actor_chat, "title", None)
            actor_name = " ".join(
                str(part).strip()
                for part in (
                    getattr(actor_user, "first_name", None),
                    getattr(actor_user, "last_name", None),
                )
                if str(part).strip()
            ).strip()
            actor_display = (
                actor_name
                or actor_title
                or (
                    f"@{actor_username}"
                    if isinstance(actor_username, str) and actor_username.strip()
                    else None
                )
                or (f"id:{actor_id}" if actor_id is not None else "unknown")
            )
        else:
            old_tokens = []
            message_reaction_count = reaction_count_update
            chat = getattr(message_reaction_count, "chat", None)
            chat_id = getattr(chat, "id", None)
            chat_type = getattr(chat, "type", None)
            message_id = getattr(message_reaction_count, "message_id", None)

            try:
                scoped_chat_for_cache = int(chat_id) if chat_id is not None else 0
            except (TypeError, ValueError):
                scoped_chat_for_cache = 0
            cache_key = f"{scoped_chat_for_cache}:{message_id}"

            reaction_count_cache = context.bot_data.get(_REACTION_COUNT_CACHE_KEY)
            if not isinstance(reaction_count_cache, dict):
                reaction_count_cache = {}
                context.bot_data[_REACTION_COUNT_CACHE_KEY] = reaction_count_cache

            old_counter: Counter[str] = Counter()
            raw_old_counter = reaction_count_cache.get(cache_key)
            if isinstance(raw_old_counter, dict):
                for token, raw_count in raw_old_counter.items():
                    token_text = str(token or "").strip()
                    if not token_text:
                        continue
                    try:
                        count = int(raw_count)
                    except (TypeError, ValueError):
                        continue
                    if count > 0:
                        old_counter[token_text] = count

            new_counter = _extract_reaction_count_counter(
                getattr(message_reaction_count, "reactions", None)
            )
            reaction_count_cache[cache_key] = dict(new_counter)

            added_tokens, removed_tokens = _diff_reaction_counters(
                old_counter, new_counter
            )
            new_tokens = _counter_to_reaction_tokens(new_counter)

            actor_id = _resolve_actor_id_for_reaction_count(
                context=context,
                chat_id=scoped_chat_for_cache,
                chat_type=chat_type,
            )
            actor_display = (
                f"id:{actor_id}" if actor_id is not None else "anonymous_reaction_count"
            )

        feedback_signal = _resolve_reaction_feedback_signal(added_tokens)
        feedback_stored = False
        try:
            scoped_chat_id = int(chat_id) if chat_id is not None else 0
        except (TypeError, ValueError):
            scoped_chat_id = 0

        auth_manager = context.bot_data.get("auth_manager")
        if (
            actor_id is not None
            and auth_manager
            and hasattr(auth_manager, "is_authenticated")
        ):
            try:
                if not bool(auth_manager.is_authenticated(actor_id)):
                    logger.debug(
                        "Ignoring reaction from unauthenticated actor",
                        actor_id=actor_id,
                        update_kind=update_kind,
                    )
                    return
            except Exception as auth_error:
                logger.warning(
                    "Reaction auth check failed",
                    actor_id=actor_id,
                    update_kind=update_kind,
                    error=str(auth_error),
                )
                return

        if not added_tokens and not removed_tokens:
            return

        if feedback_signal and actor_id is not None and scoped_chat_id != 0:
            feedback_stored = _store_pending_reaction_feedback(
                context,
                actor_id=actor_id,
                chat_id=scoped_chat_id,
                thread_id=scoped_thread_id,
                feedback={
                    "signal": feedback_signal["signal"],
                    "token": feedback_signal["token"],
                    "emoji": feedback_signal["emoji"],
                    "chat_id": scoped_chat_id,
                    "thread_id": scoped_thread_id,
                    "message_id": message_id,
                    "timestamp": time.time(),
                },
            )

        logger.info(
            "Telegram message reaction received",
            update_kind=update_kind,
            update_id=update_id,
            chat_id=chat_id,
            chat_type=chat_type,
            message_id=message_id,
            actor_id=actor_id,
            actor=actor_display,
            added_reactions=added_tokens,
            removed_reactions=removed_tokens,
            current_reactions=new_tokens,
            feedback_signal=feedback_signal["signal"] if feedback_signal else None,
            feedback_stored=feedback_stored,
        )

        audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
        if audit_logger and actor_id is not None:
            try:
                await audit_logger.log_session_event(
                    user_id=actor_id,
                    action="telegram_reaction",
                    details={
                        "chat_id": chat_id,
                        "chat_type": chat_type,
                        "message_id": message_id,
                        "actor": actor_display,
                        "added_reactions": added_tokens,
                        "removed_reactions": removed_tokens,
                        "current_reactions": new_tokens,
                        "feedback_signal": (
                            feedback_signal["signal"] if feedback_signal else None
                        ),
                        "feedback_stored": feedback_stored,
                    },
                )
            except Exception as audit_error:
                logger.warning(
                    "Failed to persist reaction audit event",
                    actor_id=actor_id,
                    error=str(audit_error),
                )
    except Exception as e:
        logger.warning("Failed to process reaction update", error=str(e))


def _estimate_text_processing_cost(text: str) -> float:
    """Estimate cost for processing text message."""
    # Base cost
    base_cost = 0.001

    # Additional cost based on length
    length_cost = len(text) * 0.00001

    # Additional cost for complex requests
    complex_keywords = [
        "analyze",
        "generate",
        "create",
        "build",
        "implement",
        "refactor",
        "optimize",
        "debug",
        "explain",
        "document",
    ]

    text_lower = text.lower()
    complexity_multiplier = 1.0

    for keyword in complex_keywords:
        if keyword in text_lower:
            complexity_multiplier += 0.5

    return (base_cost + length_cost) * min(complexity_multiplier, 3.0)


def _estimate_file_processing_cost(file_size: int) -> float:
    """Estimate cost for processing uploaded file."""
    # Base cost for file handling
    base_cost = 0.005

    # Additional cost based on file size (per KB)
    size_cost = (file_size / 1024) * 0.0001

    return base_cost + size_cost


async def _generate_placeholder_response(
    message_text: str, context: ContextTypes.DEFAULT_TYPE
) -> dict[str, str]:
    """Generate placeholder response until Claude integration is implemented."""
    settings: Settings = context.bot_data["settings"]
    current_dir = get_scope_state(
        user_data=context.user_data,
        scope_key="placeholder",
        default_directory=settings.approved_directory,
    ).get("current_directory", settings.approved_directory)
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Analyze the message for intent
    message_lower = message_text.lower()

    if any(
        word in message_lower for word in ["list", "show", "see", "directory", "files"]
    ):
        response_text = (
            f"🤖 **Claude Code Response** _(Placeholder)_\n\n"
            f"I understand you want to see files. Try using the `/ls` command to list files "
            f"in your current directory (`{relative_path}/`).\n\n"
            f"**Available commands:**\n"
            f"• `/ls` - List files\n"
            f"• `/cd <dir>` - Change directory\n"
            f"• `/projects` - Show projects\n\n"
            f"_Note: Full Claude Code integration will be available in the next phase._"
        )

    elif any(word in message_lower for word in ["create", "generate", "make", "build"]):
        response_text = (
            f"🤖 **Claude Code Response** _(Placeholder)_\n\n"
            f"I understand you want to create something! Once the Claude Code integration "
            f"is complete, I'll be able to:\n\n"
            f"• Generate code files\n"
            f"• Create project structures\n"
            f"• Write documentation\n"
            f"• Build complete applications\n\n"
            f"**Current directory:** `{relative_path}/`\n\n"
            f"_Full functionality coming soon!_"
        )

    elif any(word in message_lower for word in ["help", "how", "what", "explain"]):
        response_text = (
            "🤖 **Claude Code Response** _(Placeholder)_\n\n"
            "I'm here to help! Try using `/help` for available commands.\n\n"
            "**What I can do now:**\n"
            "• Navigate directories (`/cd`, `/ls`, `/pwd`)\n"
            "• Show projects (`/projects`)\n"
            "• Manage sessions (`/new`, `/context`)\n\n"
            "**Coming soon:**\n"
            "• Full Claude Code integration\n"
            "• Code generation and editing\n"
            "• File operations\n"
            "• Advanced programming assistance"
        )

    else:
        response_text = (
            f"🤖 **Claude Code Response** _(Placeholder)_\n\n"
            f"I received your message: \"{message_text[:100]}{'...' if len(message_text) > 100 else ''}\"\n\n"
            f"**Current Status:**\n"
            f"• Directory: `{relative_path}/`\n"
            f"• Bot core: ✅ Active\n"
            f"• Claude integration: 🔄 Coming soon\n\n"
            f"Once Claude Code integration is complete, I'll be able to process your "
            f"requests fully and help with coding tasks!\n\n"
            f"For now, try the available commands like `/ls`, `/cd`, and `/help`."
        )

    return {"text": response_text, "parse_mode": "Markdown"}


def _update_working_directory_from_claude_response(
    claude_response: Any,
    scope_state: dict[str, Any],
    settings: Settings,
    user_id: int,
) -> None:
    """Update the working directory based on Claude's response content."""
    import re
    from pathlib import Path

    # Look for directory changes in Claude's response
    # This searches for common patterns that indicate directory changes
    patterns = [
        r"(?:^|\n).*?cd\s+([^\s\n]+)",  # cd command
        r"(?:^|\n).*?Changed directory to:?\s*([^\s\n]+)",  # explicit directory change
        r"(?:^|\n).*?Current directory:?\s*([^\s\n]+)",  # current directory indication
        r"(?:^|\n).*?Working directory:?\s*([^\s\n]+)",  # working directory indication
    ]

    content = claude_response.content.lower()
    current_dir = scope_state.get("current_directory", settings.approved_directory)

    for pattern in patterns:
        matches = re.findall(pattern, content, re.MULTILINE | re.IGNORECASE)
        for match in matches:
            try:
                # Clean up the path
                new_path = match.strip().strip("\"'`")

                # Handle relative paths
                if new_path.startswith("./") or new_path.startswith("../"):
                    new_path = (current_dir / new_path).resolve()
                elif not new_path.startswith("/"):
                    # Relative path without ./
                    new_path = (current_dir / new_path).resolve()
                else:
                    # Absolute path
                    new_path = Path(new_path).resolve()

                # Validate that the new path is within the approved directory
                if (
                    new_path.is_relative_to(settings.approved_directory)
                    and new_path.exists()
                ):
                    scope_state["current_directory"] = new_path
                    logger.info(
                        "Updated working directory from Claude response",
                        old_dir=str(current_dir),
                        new_dir=str(new_path),
                        user_id=user_id,
                    )
                    return  # Take the first valid match

            except (ValueError, OSError) as e:
                # Invalid path, skip this match
                logger.debug(
                    "Invalid path in Claude response", path=match, error=str(e)
                )
                continue


def _format_tool_input_summary(tool_name: str, tool_input: dict) -> str:
    """Format a short summary of tool input for the permission prompt."""
    if not tool_input:
        return ""

    def _escape_md_text(value: Any) -> str:
        text = str(value)
        for ch in ("\\", "`", "*", "_", "["):
            text = text.replace(ch, f"\\{ch}")
        return text

    def _safe_code(value: Any, max_len: int) -> str:
        text = " ".join(str(value).split()).replace("`", "'")
        if len(text) > max_len:
            return text[:max_len] + "..."
        return text

    parts = []
    if tool_name in ("Write", "Edit", "Read") and "file_path" in tool_input:
        parts.append(f"File: `{_safe_code(tool_input['file_path'], 140)}`")
    elif tool_name == "Bash" and "command" in tool_input:
        parts.append(f"Command: `{_safe_code(tool_input['command'], 160)}`")
    elif tool_name == "WebFetch" and "url" in tool_input:
        parts.append(f"URL: `{_safe_code(tool_input['url'], 180)}`")
    else:
        # Generic: show first key-value pair
        for key, value in list(tool_input.items())[:2]:
            parts.append(f"{_escape_md_text(key)}: `{_safe_code(value, 100)}`")

    return "\n".join(parts)


def _format_permission_suggestions(
    permission_suggestions: Optional[list[dict[str, Any]]],
) -> str:
    """Render compact permission suggestion lines for Telegram approval prompt."""
    if not permission_suggestions:
        return ""

    def _safe_code(value: Any, max_len: int = 180) -> str:
        text = " ".join(str(value).split()).replace("`", "'")
        if len(text) > max_len:
            return text[:max_len] + "..."
        return text

    rendered: list[str] = []
    for suggestion in permission_suggestions[:3]:
        if not isinstance(suggestion, dict):
            continue

        suggestion_type = str(suggestion.get("type") or "").strip()
        behavior = str(suggestion.get("behavior") or "").strip()
        mode = str(suggestion.get("mode") or "").strip()
        destination = str(suggestion.get("destination") or "").strip()

        labels: list[str] = []
        if suggestion_type:
            labels.append(suggestion_type)
        if behavior:
            labels.append(f"behavior={behavior}")
        if mode:
            labels.append(f"mode={mode}")
        if destination:
            labels.append(f"dest={destination}")

        rule_preview = ""
        rules = suggestion.get("rules")
        if isinstance(rules, list) and rules:
            first_rule = rules[0]
            if isinstance(first_rule, dict):
                tool_name = str(
                    first_rule.get("toolName") or first_rule.get("tool_name") or ""
                ).strip()
                rule_content = str(
                    first_rule.get("ruleContent")
                    or first_rule.get("rule_content")
                    or ""
                ).strip()
                if tool_name and rule_content:
                    rule_preview = f"{tool_name}: {rule_content}"
                elif tool_name:
                    rule_preview = tool_name
                elif rule_content:
                    rule_preview = rule_content

        rendered_line = ", ".join(labels) if labels else "permission update"
        if rule_preview:
            rendered_line = f"{rendered_line}; rule={rule_preview}"
        rendered.append(f"• `{_safe_code(rendered_line)}`")

    if not rendered:
        return ""

    extra_count = max(len(permission_suggestions) - len(rendered), 0)
    if extra_count > 0:
        rendered.append(f"• `{extra_count}` more suggestion(s)")

    return "Suggested permission updates:\n" + "\n".join(rendered)


def build_permission_handler(
    bot: Any,
    chat_id: int,
    settings: Any,
    chat_type: Optional[str] = None,
    message_thread_id: Optional[int] = None,
) -> Optional[Callable]:
    """Build a permission button sender callback for SDK tool permission requests.

    Returns None unless SDK mode and permission gate are both enabled. The
    returned callback can be passed as ``permission_handler`` to
    ``ClaudeIntegration.run_command``.
    """
    if not getattr(settings, "use_sdk", False):
        return None

    if not getattr(settings, "sdk_enable_tool_permission_gate", False):
        return None

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    async def send_permission_buttons(
        request_id: str,
        tool_name: str,
        tool_input: dict,
        sess_id: str,
        permission_suggestions: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        input_summary = _format_tool_input_summary(tool_name, tool_input)
        suggestions_summary = _format_permission_suggestions(permission_suggestions)
        tool_label = str(tool_name or "unknown").replace("`", "'")
        session_label = str(sess_id or "").replace("`", "'")
        short_session = f"{session_label[:8]}..." if session_label else "n/a"

        keyboard = [
            [
                InlineKeyboardButton(
                    "Allow",
                    callback_data=f"permission:allow:{request_id}",
                ),
                InlineKeyboardButton(
                    "Allow All",
                    callback_data=f"permission:allow_all:{request_id}",
                ),
                InlineKeyboardButton(
                    "Deny",
                    callback_data=f"permission:deny:{request_id}",
                ),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        text_lines = [
            "**Tool Permission Request**",
            "",
            f"CLI wants to use: `{tool_label}`",
            f"Request: `{request_id}`",
            f"Session: `{short_session}`",
        ]
        if input_summary:
            text_lines.extend([input_summary])
        if suggestions_summary:
            text_lines.extend(["", suggestions_summary])
        text_lines.extend(["", "Allow this action?"])

        await send_message_resilient(
            bot=bot,
            chat_id=chat_id,
            text="\n".join(text_lines),
            parse_mode="Markdown",
            reply_markup=reply_markup,
            message_thread_id=message_thread_id,
            chat_type=chat_type,
        )

    return send_permission_buttons
