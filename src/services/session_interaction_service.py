"""Session interaction application service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple, TypedDict

ButtonSpec = Tuple[str, str]
KeyboardSpec = List[List[ButtonSpec]]


@dataclass
class SessionInteractionMessage:
    """Structured interaction message for handlers."""

    text: str
    keyboard: Optional[KeyboardSpec] = None


@dataclass(frozen=True)
class ContextViewSpec:
    """Unified context rendering options for command/callback flows."""

    loading_text: str
    loading_parse_mode: Optional[str]
    error_text: str
    include_resumable: bool
    include_event_summary: bool


@dataclass(frozen=True)
class ContextRenderResult:
    """Normalized context output for Telegram message updates."""

    primary_text: str
    parse_mode: Optional[str]
    extra_texts: Tuple[str, ...] = ()


class _CategorySummaryEntry(TypedDict):
    name: str
    tokens_raw: str
    tokens: int | None
    percent_raw: str
    percent: float | None


class _McpSummaryEntry(TypedDict):
    tool: str
    server: str
    tokens_raw: str
    tokens: int | None


def _parse_token_count(value: str | None) -> int | None:
    """Parse token string such as `1.4k`, `33,600`, `2k`."""
    if not value:
        return None

    normalized = str(value).strip().lower().replace(",", "").replace("_", "")
    if not normalized:
        return None

    match = re.search(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[kmb])?", normalized)
    if not match:
        return None

    number = float(match.group("num"))
    if number < 0:
        return None

    unit = match.group("unit") or ""
    multiplier = 1
    if unit == "k":
        multiplier = 1_000
    elif unit == "m":
        multiplier = 1_000_000
    elif unit == "b":
        multiplier = 1_000_000_000

    return int(round(number * multiplier))


def _parse_percent_value(value: str | None) -> float | None:
    """Parse percent text like `17%` or `33.7 %`."""
    if not value:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", str(value))
    if not match:
        return None
    return float(match.group(1))


def _split_markdown_row(line: str) -> list[str]:
    """Split a markdown table row into cells."""
    raw = line.strip()
    if not raw.startswith("|") or not raw.endswith("|"):
        return []
    return [cell.strip() for cell in raw.strip("|").split("|")]


def _is_markdown_separator_row(cells: list[str]) -> bool:
    """Check whether cells are markdown table separator (--- style)."""
    if not cells:
        return False
    for cell in cells:
        marker = cell.replace("-", "").replace(":", "").replace(" ", "")
        if marker:
            return False
    return True


def _extract_context_table_sections(raw_text: str) -> dict[str, list[dict[str, str]]]:
    """Extract markdown table rows grouped by `###` section title."""
    sections: dict[str, list[dict[str, str]]] = {}
    lines = [line.rstrip() for line in str(raw_text or "").splitlines()]
    current_section = "General"
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        if line.startswith("### "):
            current_section = line[4:].strip() or "General"
            idx += 1
            continue

        if idx + 1 < len(lines) and line.startswith("|"):
            headers = _split_markdown_row(line)
            separators = _split_markdown_row(lines[idx + 1].strip())
            if (
                headers
                and separators
                and len(headers) == len(separators)
                and _is_markdown_separator_row(separators)
            ):
                rows: list[dict[str, str]] = []
                idx += 2
                while idx < len(lines):
                    row_line = lines[idx].strip()
                    if not row_line.startswith("|"):
                        break
                    cells = _split_markdown_row(row_line)
                    if len(cells) != len(headers):
                        break
                    rows.append({headers[i]: cells[i] for i in range(len(headers))})
                    idx += 1

                if rows:
                    sections.setdefault(current_section, []).extend(rows)
                continue

        idx += 1

    return sections


def _find_section_rows(
    sections: dict[str, list[dict[str, str]]],
    expected_name: str,
) -> list[dict[str, str]]:
    """Find section rows by name, case-insensitive."""
    target = expected_name.strip().lower()
    for section_name, rows in sections.items():
        if section_name.strip().lower() == target:
            return rows
    return []


def _build_context_table_summary_lines(raw_text: str) -> list[str]:
    """Build concise summary lines from `/context` markdown tables."""
    sections = _extract_context_table_sections(raw_text)
    if not sections:
        return []

    lines: list[str] = []

    category_rows = _find_section_rows(sections, "Estimated usage by category")
    if category_rows:
        entries: list[_CategorySummaryEntry] = []
        for row in category_rows:
            entries.append(
                {
                    "name": (row.get("Category") or "unknown").strip(),
                    "tokens_raw": (row.get("Tokens") or "").strip(),
                    "tokens": _parse_token_count(row.get("Tokens")),
                    "percent_raw": (row.get("Percentage") or "").strip(),
                    "percent": _parse_percent_value(row.get("Percentage")),
                }
            )

        entries = sorted(
            entries,
            key=lambda item: item["tokens"] if item["tokens"] is not None else 0,
            reverse=True,
        )
        top_entries = entries[:8]
        lines.append("[Estimated Usage by Category]")
        for entry in top_entries:
            token_value = entry.get("tokens")
            token_display = (
                f"{int(token_value):,}"
                if isinstance(token_value, int)
                else entry["tokens_raw"] or "n/a"
            )
            percent_value = entry.get("percent")
            percent_display = (
                f"{float(percent_value):.1f}%"
                if isinstance(percent_value, float)
                else entry["percent_raw"] or "n/a"
            )
            lines.append(f"- {entry['name']}: {token_display} ({percent_display})")
        if len(entries) > len(top_entries):
            lines.append(f"... and {len(entries) - len(top_entries)} more categories")

    mcp_rows = _find_section_rows(sections, "MCP Tools")
    if mcp_rows:
        mcp_entries: list[_McpSummaryEntry] = []
        server_totals: dict[str, dict[str, int]] = {}
        for row in mcp_rows:
            tool = (row.get("Tool") or "unknown").strip()
            server = (row.get("Server") or "unknown").strip()
            token_raw = (row.get("Tokens") or "").strip()
            token_value = _parse_token_count(token_raw)
            mcp_entries.append(
                {
                    "tool": tool,
                    "server": server,
                    "tokens_raw": token_raw,
                    "tokens": token_value,
                }
            )
            stats = server_totals.setdefault(server, {"tokens": 0, "count": 0})
            stats["count"] += 1
            if isinstance(token_value, int):
                stats["tokens"] += token_value

        mcp_entries = sorted(
            mcp_entries,
            key=lambda item: item["tokens"] if item["tokens"] is not None else 0,
            reverse=True,
        )
        top_tools = mcp_entries[:10]
        top_servers = sorted(
            server_totals.items(),
            key=lambda item: (item[1]["tokens"], item[1]["count"]),
            reverse=True,
        )[:6]

        if lines:
            lines.append("")
        lines.append("[MCP Tools Summary]")
        lines.append(f"tool_count: {len(mcp_entries)}")
        lines.append(f"server_count: {len(server_totals)}")
        lines.append("top_servers:")
        for server, stats in top_servers:
            lines.append(
                f"- {server}: {stats['tokens']:,} tokens / {stats['count']} tools"
            )
        lines.append("top_tools:")
        for item in top_tools:
            token_value = item["tokens"]
            token_display = (
                f"{int(token_value):,}"
                if isinstance(token_value, int)
                else item["tokens_raw"] or "n/a"
            )
            lines.append(f"- {item['tool']} ({item['server']}): {token_display}")
        if len(mcp_entries) > len(top_tools):
            lines.append(f"... and {len(mcp_entries) - len(top_tools)} more tools")

    excluded_sections = {"Estimated usage by category", "MCP Tools"}
    extra_sections: list[tuple[str, int, int]] = []
    for section_name, rows in sections.items():
        if section_name in excluded_sections or not rows:
            continue
        if "Tokens" not in rows[0]:
            continue
        total_tokens = 0
        for row in rows:
            parsed = _parse_token_count(row.get("Tokens"))
            if isinstance(parsed, int):
                total_tokens += parsed
        extra_sections.append((section_name, len(rows), total_tokens))

    if extra_sections:
        extra_sections.sort(key=lambda item: item[2], reverse=True)
        if lines:
            lines.append("")
        lines.append("[Other Token Sections]")
        for section_name, row_count, total_tokens in extra_sections:
            lines.append(
                f"- {section_name}: {row_count} rows, approx {total_tokens:,} tokens"
            )

    return lines


def _render_model_usage_lines(model_usage: dict | None) -> list[str]:
    """Render model usage breakdown for full context output."""
    if not isinstance(model_usage, dict) or not model_usage:
        return ["model_usage: none"]

    lines: list[str] = []
    for model_name, usage in model_usage.items():
        if not isinstance(usage, dict):
            lines.append(f"- {model_name}: {usage}")
            continue

        input_tokens = int(usage.get("inputTokens", 0) or 0)
        output_tokens = int(usage.get("outputTokens", 0) or 0)
        cache_read = int(usage.get("cacheReadInputTokens", 0) or 0)
        cache_create = int(usage.get("cacheCreationInputTokens", 0) or 0)
        total_tokens = input_tokens + output_tokens + cache_read + cache_create
        context_window = int(usage.get("contextWindow", 0) or 0)
        window_source = usage.get("contextWindowSource")

        lines.append(f"- {model_name}")
        lines.append(f"  resolved_model: {usage.get('resolvedModel') or 'n/a'}")
        lines.append(f"  input_tokens: {input_tokens:,}")
        lines.append(f"  output_tokens: {output_tokens:,}")
        lines.append(f"  cache_read_input_tokens: {cache_read:,}")
        lines.append(f"  cache_creation_input_tokens: {cache_create:,}")
        lines.append(f"  total_tokens: {total_tokens:,}")

        if context_window > 0:
            used_percent = total_tokens / context_window * 100
            remaining = max(context_window - total_tokens, 0)
            lines.append(f"  context_window: {context_window:,}")
            lines.append(f"  context_window_source: {window_source or 'unknown'}")
            lines.append(f"  usage_percent: {used_percent:.2f}%")
            lines.append(f"  remaining_tokens: {remaining:,}")
        else:
            lines.append("  context_window: n/a")
            lines.append(f"  context_window_source: {window_source or 'n/a'}")

        if "costUSD" in usage:
            lines.append(f"  cost_usd: {usage.get('costUSD')}")
        if "maxOutputTokens" in usage:
            lines.append(f"  max_output_tokens: {usage.get('maxOutputTokens')}")

    return lines


class SessionInteractionService:
    """Provide reusable continue/export interaction texts and keyboard specs."""

    _END_NO_ACTIVE_CALLBACK_KEYBOARD: KeyboardSpec = [
        [("🆕 New Session", "action:new_session")],
        [("📊 Context", "action:context")],
    ]
    _END_SUCCESS_KEYBOARD: KeyboardSpec = [
        [
            ("🆕 New Session", "action:new_session"),
            ("📁 Change Project", "action:show_projects"),
        ],
        [
            ("📊 Context", "action:context"),
            ("❓ Help", "action:help"),
        ],
    ]
    _CONTINUE_NOT_FOUND_KEYBOARD: KeyboardSpec = [
        [
            ("🆕 New Session", "action:new_session"),
            ("📊 Context", "action:context"),
        ]
    ]
    _EXPORT_SELECTOR_KEYBOARD: KeyboardSpec = [
        [
            ("📝 Markdown", "export:markdown"),
            ("🌐 HTML", "export:html"),
        ],
        [
            ("📋 JSON", "export:json"),
            ("❌ Cancel", "export:cancel"),
        ],
    ]
    _INTEGRATION_UNAVAILABLE_TEXT = (
        "❌ **Claude Integration Not Available**\n\n"
        "Claude integration is not properly configured."
    )
    _CONTEXT_COMMAND_LOADING_TEXT = "⏳ 正在获取会话状态，请稍候..."
    _CONTEXT_CALLBACK_LOADING_TEXT = "**Session Context**\n\n⏳ 正在刷新状态，请稍候..."
    _CONTEXT_ERROR_TEXT = "❌ 获取状态失败，请稍后重试。"

    @staticmethod
    def _relative_path_label(current_dir: Path, approved_directory: Path) -> str:
        """Render directory label relative to approved directory when possible."""
        try:
            return f"{current_dir.relative_to(approved_directory)}/"
        except ValueError:
            return str(current_dir)

    @staticmethod
    def _new_session_title(active_engine: Optional[str]) -> str:
        """Render new-session title by active engine with neutral fallback."""
        normalized = str(active_engine or "").strip().lower()
        if normalized == "claude":
            return "New Claude Session"
        if normalized == "codex":
            return "New Codex Session"
        return "New AI Session"

    def get_integration_unavailable_text(self) -> str:
        """Return standard unavailable message."""
        return self._INTEGRATION_UNAVAILABLE_TEXT

    def build_context_view_spec(
        self,
        *,
        for_callback: bool,
        full_mode: bool = False,
    ) -> ContextViewSpec:
        """Build unified context loading/error templates and snapshot options."""
        if for_callback:
            return ContextViewSpec(
                loading_text=self._CONTEXT_CALLBACK_LOADING_TEXT,
                loading_parse_mode="Markdown",
                error_text=self._CONTEXT_ERROR_TEXT,
                include_resumable=False,
                include_event_summary=True,
            )

        return ContextViewSpec(
            loading_text=self._CONTEXT_COMMAND_LOADING_TEXT,
            loading_parse_mode=None,
            error_text=self._CONTEXT_ERROR_TEXT,
            include_resumable=True,
            include_event_summary=not full_mode,
        )

    @staticmethod
    def split_context_full_text(text: str, max_length: int = 3900) -> list[str]:
        """Split long context full text into Telegram-safe chunks."""
        if len(text) <= max_length:
            return [text]

        chunks: list[str] = []
        remaining = text
        while len(remaining) > max_length:
            split_at = remaining.rfind("\n", 0, max_length)
            if split_at <= 0:
                split_at = max_length
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")

        if remaining:
            chunks.append(remaining)
        return chunks

    @staticmethod
    def build_context_full_payload(
        *,
        relative_path: Path,
        current_model: str | None,
        claude_session_id: str | None,
        precise_context: dict | None,
        info: dict | None,
        resumable_payload: dict | None,
    ) -> dict:
        """Build full context payload."""
        return {
            "mode": "full",
            "directory": f"{relative_path}/",
            "model": current_model or "default",
            "session": {
                "active": bool(claude_session_id),
                "id": claude_session_id,
                "id_short": (
                    f"{claude_session_id[:8]}..." if claude_session_id else None
                ),
            },
            "context_payload": precise_context,
            "session_info": info,
            "resumable": resumable_payload,
        }

    @staticmethod
    def render_context_full_text(payload: dict) -> str:
        """Render readable full context payload text."""
        session_payload = payload.get("session")
        session: dict[str, Any] = (
            session_payload if isinstance(session_payload, dict) else {}
        )
        context_raw = payload.get("context_payload")
        context_payload: dict[str, Any] | None = (
            context_raw if isinstance(context_raw, dict) else None
        )
        session_info_raw = payload.get("session_info")
        session_info: dict[str, Any] | None = (
            session_info_raw if isinstance(session_info_raw, dict) else None
        )

        lines = [
            "Session Context (full)",
            "",
            "[Summary]",
            f"directory: {payload.get('directory')}",
            f"model: {payload.get('model')}",
            f"session_active: {session.get('active')}",
            f"session_id: {session.get('id') or 'none'}",
            f"session_id_short: {session.get('id_short') or 'none'}",
        ]

        if context_payload:
            lines.extend(
                [
                    "",
                    "[Context Payload]",
                    f"used_tokens: {int(context_payload.get('used_tokens', 0) or 0):,}",
                    f"total_tokens: {int(context_payload.get('total_tokens', 0) or 0):,}",
                    f"remaining_tokens: {int(context_payload.get('remaining_tokens', 0) or 0):,}",
                    f"used_percent: {float(context_payload.get('used_percent', 0.0) or 0.0):.2f}%",
                    f"cached: {bool(context_payload.get('cached', False))}",
                    f"context_session_id: {context_payload.get('session_id') or 'n/a'}",
                ]
            )

            raw_text = str(context_payload.get("raw_text") or "")
            if raw_text:
                structured_lines = _build_context_table_summary_lines(raw_text)
                if structured_lines:
                    lines.extend(
                        ["", "[/context Structured Summary]", *structured_lines]
                    )
                else:
                    lines.extend(
                        [
                            "",
                            "[/context Structured Summary]",
                            "No markdown table summary detected in /context output.",
                        ]
                    )
        else:
            lines.extend(["", "[Context Payload]", "context_payload: none"])

        if session_info:
            tools_used = session_info.get("tools_used")
            tools_count = len(tools_used) if isinstance(tools_used, list) else 0
            lines.extend(
                [
                    "",
                    "[Session Info]",
                    f"project: {session_info.get('project')}",
                    f"created: {session_info.get('created')}",
                    f"last_used: {session_info.get('last_used')}",
                    f"cost_usd: {session_info.get('cost')}",
                    f"turns: {session_info.get('turns')}",
                    f"messages: {session_info.get('messages')}",
                    f"expired: {session_info.get('expired')}",
                    f"tools_used_count: {tools_count}",
                    "",
                    "[Model Usage]",
                ]
            )
            lines.extend(_render_model_usage_lines(session_info.get("model_usage")))
        else:
            lines.extend(["", "[Session Info]", "session_info: none"])

        return "\n".join(lines)

    def build_context_render_result(
        self,
        *,
        snapshot: Any,
        scope_state: Mapping[str, Any],
        approved_directory: Path,
        full_mode: bool,
        max_length: int = 3900,
    ) -> ContextRenderResult:
        """Build normalized context render result for command/callback flows."""
        if not full_mode:
            return ContextRenderResult(
                primary_text="\n".join(getattr(snapshot, "lines", [])),
                parse_mode="Markdown",
            )

        current_dir = scope_state.get("current_directory", approved_directory)
        try:
            relative_path = current_dir.relative_to(approved_directory)
        except ValueError:
            relative_path = current_dir

        payload = self.build_context_full_payload(
            relative_path=relative_path,
            current_model=scope_state.get("claude_model"),
            claude_session_id=scope_state.get("claude_session_id"),
            precise_context=getattr(snapshot, "precise_context", None),
            info=getattr(snapshot, "session_info", None),
            resumable_payload=getattr(snapshot, "resumable_payload", None),
        )
        full_text = self.render_context_full_text(payload)
        chunks = self.split_context_full_text(full_text, max_length=max_length)

        return ContextRenderResult(
            primary_text=chunks[0],
            parse_mode=None,
            extra_texts=tuple(chunks[1:]),
        )

    def build_new_session_message(
        self,
        *,
        current_dir: Path,
        approved_directory: Path,
        previous_session_id: Optional[str],
        for_callback: bool,
        active_engine: Optional[str] = None,
    ) -> SessionInteractionMessage:
        """Build new-session message and actions for command/callback."""
        relative_path = self._relative_path_label(current_dir, approved_directory)
        title = self._new_session_title(active_engine)
        if for_callback:
            text = (
                f"🆕 **{title}**\n\n"
                f"📂 Working directory: `{relative_path}`\n\n"
                "Ready to help you code! Send me a message to get started."
            )
        else:
            cleared_info = ""
            if previous_session_id:
                cleared_info = (
                    f"\n🗑️ Previous session `{previous_session_id[:8]}...` cleared."
                )
            text = (
                f"🆕 **{title}**\n\n"
                f"📂 Working directory: `{relative_path}`{cleared_info}\n\n"
                "Context has been cleared. Send a message to start fresh."
            )
        return SessionInteractionMessage(text=text)

    def build_end_no_active_message(
        self,
        *,
        for_callback: bool,
    ) -> SessionInteractionMessage:
        """Build no-active-session message for end flow."""
        if for_callback:
            return SessionInteractionMessage(
                text=(
                    "ℹ️ **No Active Session**\n\n"
                    "There's no active Claude session to end.\n\n"
                    "**What you can do:**\n"
                    "• Use the button below to start a new session\n"
                    "• Check your session context\n"
                    "• Send any message to start a conversation"
                ),
                keyboard=self._END_NO_ACTIVE_CALLBACK_KEYBOARD,
            )

        return SessionInteractionMessage(
            text=(
                "ℹ️ **No Active Session**\n\n"
                "There's no active Claude session to end.\n\n"
                "**What you can do:**\n"
                "• Use `/new` to start a new session\n"
                "• Use `/context` to check your session context\n"
                "• Send any message to start a conversation"
            ),
        )

    def build_end_success_message(
        self,
        *,
        current_dir: Path,
        approved_directory: Path,
        for_callback: bool,
        title: str = "Session Ended",
    ) -> SessionInteractionMessage:
        """Build end-session success message for command/callback."""
        relative_path = self._relative_path_label(current_dir, approved_directory)
        if for_callback:
            next_steps = (
                "• Start a new session\n"
                "• Check context\n"
                "• Send any message to begin a new conversation"
            )
        else:
            next_steps = (
                "• Start a new session with `/new`\n"
                "• Check context with `/context`\n"
                "• Send any message to begin a new conversation"
            )

        return SessionInteractionMessage(
            text=(
                f"✅ **{title}**\n\n"
                "Your Claude session has been terminated.\n\n"
                "**Current Status:**\n"
                f"• Directory: `{relative_path}`\n"
                "• Session: None\n"
                "• Ready for new commands\n\n"
                "**Next Steps:**\n"
                f"{next_steps}"
            ),
            keyboard=self._END_SUCCESS_KEYBOARD,
        )

    def build_continue_progress_text(
        self,
        *,
        existing_session_id: Optional[str],
        current_dir: Path,
        approved_directory: Path,
        prompt: Optional[str],
    ) -> str:
        """Build progress text before continue execution."""
        if existing_session_id:
            return (
                f"🔄 **Continuing Session**\n\n"
                f"Session ID: `{existing_session_id[:8]}...`\n"
                f"Directory: `{self._relative_path_label(current_dir, approved_directory)}`\n\n"
                f"{'Processing your message...' if prompt else 'Continuing where you left off...'}"
            )
        return (
            "🔍 **Looking for Recent Session**\n\n"
            "Searching for your most recent session in this directory..."
        )

    def build_continue_not_found_message(
        self,
        *,
        current_dir: Path,
        approved_directory: Path,
        for_callback: bool,
    ) -> SessionInteractionMessage:
        """Build not-found message and actions for continue flow."""
        if for_callback:
            text = (
                "❌ **No Session Found**\n\n"
                f"No recent Claude session found in this directory.\n"
                f"Directory: `{self._relative_path_label(current_dir, approved_directory)}`\n\n"
                f"**What you can do:**\n"
                f"• Use the button below to start a fresh session\n"
                f"• Check your session context\n"
                f"• Navigate to a different directory"
            )
        else:
            text = (
                "❌ **No Session Found**\n\n"
                f"No recent Claude session found in this directory.\n"
                f"Directory: `{self._relative_path_label(current_dir, approved_directory)}`\n\n"
                f"**What you can do:**\n"
                f"• Use `/new` to start a fresh session\n"
                f"• Use `/context` to check your sessions\n"
                f"• Navigate to a different directory with `/cd`"
            )
        return SessionInteractionMessage(
            text=text,
            keyboard=self._CONTINUE_NOT_FOUND_KEYBOARD,
        )

    @staticmethod
    def build_continue_callback_success_text(
        content: str,
        *,
        preview_limit: int = 500,
    ) -> str:
        """Build callback continue success text with bounded preview."""
        preview = content[:preview_limit]
        suffix = "..." if len(content) > preview_limit else ""
        return f"✅ **Session Continued**\n\n{preview}{suffix}"

    def build_continue_callback_error_message(
        self, error: str
    ) -> SessionInteractionMessage:
        """Build callback continue error response with recovery action."""
        return SessionInteractionMessage(
            text=(
                f"❌ **Error Continuing Session**\n\n"
                f"An error occurred: `{error}`\n\n"
                f"Try starting a new session instead."
            ),
            keyboard=[[("🆕 New Session", "action:new_session")]],
        )

    @staticmethod
    def build_continue_command_error_text(error: str) -> str:
        """Build command continue error response."""
        return (
            f"❌ **Error Continuing Session**\n\n"
            f"An error occurred while trying to continue your session:\n\n"
            f"`{error}`\n\n"
            f"**Suggestions:**\n"
            f"• Try starting a new session with `/new`\n"
            f"• Check your session context with `/context`\n"
            f"• Contact support if the issue persists"
        )

    @staticmethod
    def build_export_unavailable_text(for_callback: bool) -> str:
        """Build export unavailable text for command/callback entry."""
        if for_callback:
            return (
                "❌ **Export Unavailable**\n\nSession export service is not available."
            )
        return (
            "📤 **Export Session**\n\n"
            "Session export functionality is not available.\n\n"
            "**Planned features:**\n"
            "• Export conversation history\n"
            "• Save session state\n"
            "• Share conversations\n"
            "• Create session backups"
        )

    @staticmethod
    def build_export_no_active_session_text() -> str:
        """Build no-active-session text for export entry."""
        return (
            "❌ **No Active Session**\n\n"
            "There's no active Claude session to export.\n\n"
            "**What you can do:**\n"
            "• Start a new session with `/new`\n"
            "• Continue an existing session with `/continue`\n"
            "• Check your context with `/context`"
        )

    def build_export_selector_message(
        self, session_id: str
    ) -> SessionInteractionMessage:
        """Build export selector message and format keyboard."""
        return SessionInteractionMessage(
            text=(
                "📤 **Export Session**\n\n"
                f"Ready to export session: `{session_id[:8]}...`\n\n"
                "**Choose export format:**"
            ),
            keyboard=self._EXPORT_SELECTOR_KEYBOARD,
        )
