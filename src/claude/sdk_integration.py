"""Claude Code Python SDK integration.

Features:
- Native Claude Code SDK integration
- Async streaming support
- Tool execution management
- Session persistence
"""

import asyncio
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
)

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    Message,
    ProcessError,
    ResultMessage,
    SystemMessage,
    ToolUseBlock,
    UserMessage,
    query,
)

from ..config.settings import Settings, resolve_cli_timeout_seconds
from .exceptions import (
    ClaudeMCPError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeTimeoutError,
)

logger = structlog.get_logger()


def find_claude_cli(claude_cli_path: Optional[str] = None) -> Optional[str]:
    """Find Claude CLI in common locations."""
    import glob
    import shutil

    # First check if a specific path was provided via config or env
    if claude_cli_path:
        claude_cli_path = os.path.abspath(claude_cli_path)
        if os.path.exists(claude_cli_path) and os.access(claude_cli_path, os.X_OK):
            return claude_cli_path

    # Check CLAUDE_CLI_PATH environment variable
    env_path = os.environ.get("CLAUDE_CLI_PATH")
    if env_path and os.path.exists(env_path) and os.access(env_path, os.X_OK):
        return env_path

    # Check if claude is already in PATH
    claude_path = shutil.which("claude")
    if claude_path:
        return claude_path

    # Check common installation locations
    common_paths = [
        # NVM installations
        os.path.expanduser("~/.nvm/versions/node/*/bin/claude"),
        # Direct npm global install
        os.path.expanduser("~/.npm-global/bin/claude"),
        os.path.expanduser("~/node_modules/.bin/claude"),
        # System locations
        "/usr/local/bin/claude",
        "/usr/bin/claude",
        # Windows locations (for cross-platform support)
        os.path.expanduser("~/AppData/Roaming/npm/claude.cmd"),
    ]

    for pattern in common_paths:
        matches = glob.glob(pattern)
        if matches:
            # Return the first match
            return matches[0]

    return None


def update_path_for_claude(claude_cli_path: Optional[str] = None) -> bool:
    """Update PATH to include Claude CLI if found."""
    claude_path = find_claude_cli(claude_cli_path)

    if claude_path:
        # Add the directory containing claude to PATH
        claude_dir = os.path.dirname(claude_path)
        current_path = os.environ.get("PATH", "")

        if claude_dir not in current_path:
            os.environ["PATH"] = f"{claude_dir}:{current_path}"
            logger.info("Updated PATH for Claude CLI", claude_path=claude_path)

        return True

    return False


def strip_thinking_blocks_from_session(session_id: str, working_directory: str) -> bool:
    """Remove thinking blocks from a Claude session JSONL file.

    Thinking blocks carry cryptographic signatures tied to the API provider;
    after a provider switch the signatures become invalid.  Stripping them
    allows the session to be resumed without losing conversational context.

    Returns True if the file was modified, False otherwise.
    """
    home = Path.home()
    # Claude stores sessions under ~/.claude/projects/{project_hash}/{sid}.jsonl
    # project_hash: leading '/' replaced by '-', e.g. /Users/foo -> -Users-foo
    project_hash = working_directory.replace("/", "-")
    session_file = home / ".claude" / "projects" / project_hash / f"{session_id}.jsonl"

    if not session_file.exists():
        logger.warning(
            "Session file not found for thinking-block strip",
            session_id=session_id,
            path=str(session_file),
        )
        return False

    modified = False
    new_lines: list[str] = []
    for raw_line in session_file.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            new_lines.append(raw_line)
            continue
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            new_lines.append(raw_line)
            continue

        if obj.get("type") == "assistant" and isinstance(
            obj.get("message", {}).get("content"), list
        ):
            original = obj["message"]["content"]
            filtered = [b for b in original if b.get("type") != "thinking"]
            if len(filtered) != len(original):
                obj["message"]["content"] = filtered
                modified = True
                raw_line = json.dumps(obj, ensure_ascii=False)

        new_lines.append(raw_line)

    if not modified:
        return False

    # Atomic write: tmp file + replace
    tmp_file = session_file.with_suffix(".jsonl.tmp")
    tmp_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    tmp_file.replace(session_file)
    logger.info(
        "Stripped thinking blocks from session file",
        session_id=session_id,
    )
    return True


@dataclass
class ClaudeResponse:
    """Response from Claude Code SDK."""

    content: str
    session_id: str
    cost: float
    duration_ms: int
    num_turns: int
    is_error: bool = False
    error_type: Optional[str] = None
    tools_used: List[Dict[str, Any]] = field(default_factory=list)
    model_usage: Optional[Dict[str, Any]] = None


@dataclass
class StreamUpdate:
    """Streaming update from Claude SDK."""

    type: str  # 'assistant', 'user', 'system', 'result', 'tool_result', 'error', 'progress'
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None

    # Keep these fields aligned with integration.StreamUpdate so bot handlers
    # can consume SDK and subprocess updates with the same logic.
    timestamp: Optional[str] = None
    session_context: Optional[Dict[str, Any]] = None
    progress: Optional[Dict[str, Any]] = None
    error_info: Optional[Dict[str, Any]] = None
    execution_id: Optional[str] = None
    parent_message_id: Optional[str] = None

    def is_error(self) -> bool:
        """Check if this update represents an error."""
        if self.type == "error":
            return True
        if not self.metadata:
            return False
        return bool(self.metadata.get("is_error", False))

    def get_tool_names(self) -> List[str]:
        """Extract tool names from tool calls."""
        if not self.tool_calls:
            return []
        names: List[str] = []
        for call in self.tool_calls:
            name = call.get("name")
            if name:
                names.append(str(name))
        return names

    def get_progress_percentage(self) -> Optional[int]:
        """Get progress percentage if available."""
        if self.progress:
            percentage = self.progress.get("percentage")
            if isinstance(percentage, (int, float)):
                return int(percentage)
        return None

    def get_error_message(self) -> Optional[str]:
        """Get error message if this is an error update."""
        if self.error_info:
            message = self.error_info.get("message")
            return str(message) if message else None
        elif self.is_error() and self.content:
            return self.content
        return None


StreamCallback = Callable[[StreamUpdate], Awaitable[None]]


class ClaudeSDKManager:
    """Manage Claude Code SDK integration."""

    @staticmethod
    def _sanitize_runtime_env() -> None:
        """Strip env flags that make Claude CLI think it's nested inside itself."""
        removed = os.environ.pop("CLAUDECODE", None)
        if removed is not None:
            logger.info("Removed CLAUDECODE env for bot runtime isolation")

    def __init__(self, config: Settings):
        """Initialize SDK manager with configuration."""
        self.config = config
        self.active_sessions: Dict[str, Dict[str, Any]] = {}
        self._sanitize_runtime_env()

        # Try to find and update PATH for Claude CLI
        if not update_path_for_claude(config.claude_cli_path):
            logger.warning(
                "Claude CLI not found in PATH or common locations. "
                "SDK may fail if Claude is not installed or not in PATH."
            )

        # Set up environment for Claude Code SDK if API key is provided
        # If no API key is provided, the SDK will use existing CLI authentication
        if config.anthropic_api_key_str:
            os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key_str
            logger.info("Using provided API key for Claude SDK authentication")
        else:
            logger.info("No API key provided, using existing Claude CLI authentication")

    def _resolve_setting_sources(self) -> Optional[List[str]]:
        """Resolve optional setting_sources for ClaudeAgentOptions.

        Some gateway environments reject explicit setting_sources values.
        Keep default behavior (unset) unless the user explicitly configures it.
        """
        raw = getattr(self.config, "claude_setting_sources", None)
        if not raw:
            return None
        resolved = [str(item).strip() for item in raw if str(item).strip()]
        return resolved or None

    def _resolve_requested_model(
        self, model: Optional[str], working_directory: Path
    ) -> Optional[str]:
        """Resolve model for this request.

        Priority:
        1) explicit /model override from Telegram
        2) default model from Claude settings (user/project/local sources)
        """
        explicit = str(model or "").strip()
        if explicit:
            return explicit
        return self._resolve_default_model_from_settings(working_directory)

    def _resolve_default_model_from_settings(
        self, working_directory: Path
    ) -> Optional[str]:
        """Resolve default model from Claude settings files."""
        source_order = self._resolve_setting_sources() or ["user", "project", "local"]
        candidate_paths: list[Path] = []
        cwd = Path(working_directory)

        for source in source_order:
            normalized = str(source or "").strip().lower()
            if normalized == "user":
                candidate_paths.append(Path.home() / ".claude" / "settings.json")
            elif normalized == "project":
                candidate_paths.append(cwd / ".claude" / "settings.json")
            elif normalized == "local":
                candidate_paths.append(cwd / ".claude" / "settings.local.json")

        seen: set[Path] = set()
        for path in candidate_paths:
            resolved = path.expanduser().resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            model_name = self._read_model_from_settings_file(resolved)
            if model_name:
                return model_name
        return None

    def _read_model_from_settings_file(self, path: Path) -> Optional[str]:
        """Read `model` from a Claude settings JSON file."""
        if not path.exists() or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug(
                "Failed to parse Claude settings JSON while resolving model",
                path=str(path),
                error=str(exc),
            )
            return None

        if not isinstance(payload, dict):
            return None

        model_name = str(payload.get("model") or "").strip()
        if not model_name:
            return None

        # "default/auto" means defer to CLI runtime selection.
        if model_name.lower() in {"default", "auto"}:
            return None
        return model_name

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[StreamCallback] = None,
        permission_callback: Optional[Callable] = None,
        model: Optional[str] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> ClaudeResponse:
        """Execute Claude Code command via SDK."""
        start_time = asyncio.get_event_loop().time()

        logger.info(
            "Starting Claude SDK command",
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
        )
        timeout_seconds = max(1, resolve_cli_timeout_seconds(self.config))

        try:
            # Build Claude Agent options
            cli_path = find_claude_cli(self.config.claude_cli_path)
            requested_model = self._resolve_requested_model(model, working_directory)
            options_kwargs: Dict[str, Any] = {
                "max_turns": self.config.claude_max_turns,
                "cwd": str(working_directory),
                "allowed_tools": self.config.claude_allowed_tools,
                "cli_path": cli_path,
            }
            if requested_model:
                options_kwargs["model"] = requested_model
            setting_sources = self._resolve_setting_sources()
            if setting_sources is not None:
                options_kwargs["setting_sources"] = setting_sources

            options = ClaudeAgentOptions(**options_kwargs)

            # NOTE: permission_callback is NOT set on options here.
            # query() does not support can_use_tool with string prompts.
            # Use execute_with_client() for permission callback support.

            # Pass MCP server configuration if enabled
            if self.config.enable_mcp and self.config.mcp_config_path:
                options.mcp_servers = self._load_mcp_config(self.config.mcp_config_path)
                logger.info(
                    "MCP servers configured",
                    mcp_config_path=str(self.config.mcp_config_path),
                )

            # Resume previous session if we have a session_id
            if session_id and continue_session:
                options.resume = session_id
                logger.info(
                    "Resuming previous session",
                    session_id=session_id,
                )

            # Emit SDK-side init event so Telegram can preserve thinking
            # summary/collapse behavior consistent with subprocess mode.
            await self._emit_init_update(stream_callback, options)

            # Collect messages
            messages: List[Message] = []
            cost = 0.0
            tools_used: List[Dict[str, Any]] = []

            # Build multimodal prompt if images are provided
            query_prompt: str | AsyncIterable[Dict[str, Any]] = prompt
            if images:
                query_prompt = await self._build_multimodal_prompt(prompt, images)

            # Execute with streaming and timeout
            await asyncio.wait_for(
                self._execute_query_with_streaming(
                    query_prompt, options, messages, stream_callback
                ),
                timeout=timeout_seconds,
            )

            # Extract cost, tools, and session_id from result message
            cost = 0.0
            tools_used = []
            claude_session_id = None
            sdk_usage = None
            resolved_model = None
            for message in messages:
                if resolved_model is None:
                    model_name = getattr(message, "model", None)
                    if model_name:
                        resolved_model = str(model_name)
                if isinstance(message, ResultMessage):
                    cost = getattr(message, "total_cost_usd", 0.0) or 0.0
                    claude_session_id = getattr(message, "session_id", None)
                    sdk_usage = getattr(message, "usage", None)
                    tools_used = self._extract_tools_from_messages(messages)
                    logger.debug(
                        "ResultMessage details",
                        cost=cost,
                        usage=sdk_usage,
                        session_id=claude_session_id,
                    )
                    break

            # Calculate duration
            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)

            # Use Claude's session_id if available, otherwise fall back
            final_session_id = claude_session_id or session_id or str(uuid.uuid4())

            if claude_session_id and claude_session_id != session_id:
                logger.info(
                    "Got session ID from Claude",
                    claude_session_id=claude_session_id,
                    previous_session_id=session_id,
                )

            # Update session
            self._update_session(final_session_id, messages)

            return ClaudeResponse(
                content=self._extract_response_content(messages),
                session_id=final_session_id,
                cost=cost,
                duration_ms=duration_ms,
                num_turns=len(
                    [
                        m
                        for m in messages
                        if isinstance(m, (UserMessage, AssistantMessage))
                    ]
                ),
                tools_used=tools_used,
                model_usage=self._build_model_usage(
                    sdk_usage,
                    cost,
                    resolved_model=resolved_model,
                ),
            )

        except asyncio.TimeoutError:
            logger.error(
                "Claude SDK command timed out",
                timeout_seconds=timeout_seconds,
            )
            raise ClaudeTimeoutError(f"Claude SDK timed out after {timeout_seconds}s")

        except CLINotFoundError as e:
            logger.error("Claude CLI not found", error=str(e))
            error_msg = (
                "Claude Code not found. Please ensure Claude is installed:\n"
                "  npm install -g @anthropic-ai/claude-code\n\n"
                "If already installed, try one of these:\n"
                "  1. Add Claude to your PATH\n"
                "  2. Create a symlink: ln -s $(which claude) /usr/local/bin/claude\n"
                "  3. Set CLAUDE_CLI_PATH environment variable"
            )
            raise ClaudeProcessError(error_msg)

        except ProcessError as e:
            error_str = str(e)
            logger.error(
                "Claude process failed",
                error=error_str,
                exit_code=getattr(e, "exit_code", None),
            )
            # Check if the process error is MCP-related
            if "mcp" in error_str.lower():
                raise ClaudeMCPError(f"MCP server error: {error_str}")
            raise ClaudeProcessError(f"Claude process error: {error_str}")

        except CLIConnectionError as e:
            error_str = str(e)
            logger.error("Claude connection error", error=error_str)
            # Check if the connection error is MCP-related
            if "mcp" in error_str.lower() or "server" in error_str.lower():
                raise ClaudeMCPError(f"MCP server connection failed: {error_str}")
            raise ClaudeProcessError(f"Failed to connect to Claude: {error_str}")

        except CLIJSONDecodeError as e:
            logger.error("Claude SDK JSON decode error", error=str(e))
            raise ClaudeParsingError(f"Failed to decode Claude response: {str(e)}")

        except ClaudeSDKError as e:
            logger.error("Claude SDK error", error=str(e))
            raise ClaudeProcessError(f"Claude SDK error: {str(e)}")

        except Exception as e:
            # Handle ExceptionGroup from TaskGroup operations (Python 3.11+)
            if type(e).__name__ == "ExceptionGroup" or hasattr(e, "exceptions"):
                logger.error(
                    "Task group error in Claude SDK",
                    error=str(e),
                    error_type=type(e).__name__,
                    exception_count=len(getattr(e, "exceptions", [])),
                    exceptions=[
                        str(ex) for ex in getattr(e, "exceptions", [])[:3]
                    ],  # Log first 3 exceptions
                )
                # Extract the most relevant exception from the group
                exceptions = getattr(e, "exceptions", [e])
                main_exception = exceptions[0] if exceptions else e
                raise ClaudeProcessError(
                    f"Claude SDK task error: {str(main_exception)}"
                )

            # Check if it's an ExceptionGroup disguised as a regular exception
            elif hasattr(e, "__notes__") and "TaskGroup" in str(e):
                logger.error(
                    "TaskGroup related error in Claude SDK",
                    error=str(e),
                    error_type=type(e).__name__,
                )
                raise ClaudeProcessError(f"Claude SDK task error: {str(e)}")

            else:
                logger.error(
                    "Unexpected error in Claude SDK",
                    error=str(e),
                    error_type=type(e).__name__,
                )
                raise ClaudeProcessError(f"Unexpected error: {str(e)}")

    async def _execute_query_with_streaming(
        self,
        prompt: "str | AsyncIterable[Dict[str, Any]]",
        options: ClaudeAgentOptions,
        messages: List[Message],
        stream_callback: Optional[StreamCallback],
    ) -> None:
        """Execute query with streaming and collect messages."""
        model_resolved_emitted = False
        try:
            async for message in query(prompt=prompt, options=options):
                messages.append(message)

                # Emit actual resolved model once we see the first assistant message.
                if stream_callback and not model_resolved_emitted:
                    model_name = getattr(message, "model", None)
                    if model_name:
                        model_resolved_emitted = await self._emit_model_resolved_update(
                            stream_callback, str(model_name)
                        )

                # Handle streaming callback
                if stream_callback:
                    try:
                        await self._handle_stream_message(message, stream_callback)
                    except Exception as callback_error:
                        logger.warning(
                            "Stream callback failed",
                            error=str(callback_error),
                            error_type=type(callback_error).__name__,
                        )
                        # Continue processing even if callback fails

        except asyncio.CancelledError:
            logger.info("Query streaming cancelled by user")
            raise
        except Exception as e:
            # Handle both ExceptionGroups and regular exceptions
            if type(e).__name__ == "ExceptionGroup" or hasattr(e, "exceptions"):
                logger.error(
                    "TaskGroup error in streaming execution",
                    error=str(e),
                    error_type=type(e).__name__,
                )
            else:
                logger.error(
                    "Error in streaming execution",
                    error=str(e),
                    error_type=type(e).__name__,
                )
            # Re-raise to be handled by the outer try-catch
            raise

    async def _emit_init_update(
        self,
        stream_callback: Optional[StreamCallback],
        options: ClaudeAgentOptions,
    ) -> None:
        """No-op: real SDK init event carries accurate tools/capabilities."""
        return

    async def _emit_model_resolved_update(
        self,
        stream_callback: Optional[StreamCallback],
        model_name: str,
    ) -> bool:
        """Emit resolved model update from real SDK response."""
        if not stream_callback or not model_name:
            return False

        try:
            await stream_callback(
                StreamUpdate(
                    type="system",
                    metadata={
                        "subtype": "model_resolved",
                        "model": model_name,
                    },
                )
            )
            return True
        except Exception as e:
            logger.warning("Failed to emit resolved model update", error=str(e))
            return False

    async def _build_multimodal_prompt(
        self, text: str, images: List[Dict[str, str]]
    ) -> AsyncIterable[Dict[str, Any]]:
        """Build an AsyncIterable prompt with text + image content blocks.

        Args:
            text: The text prompt
            images: List of dicts with 'base64_data' and 'media_type' keys
        """

        async def _generate_messages() -> AsyncIterator[Dict[str, Any]]:
            content_blocks: List[Dict[str, Any]] = []

            # Add image blocks first
            for img in images:
                content_blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img["media_type"],
                            "data": img["base64_data"],
                        },
                    }
                )

            # Add text block
            content_blocks.append({"type": "text", "text": text})

            yield {
                "type": "user",
                "session_id": "",
                "message": {"role": "user", "content": content_blocks},
                "parent_tool_use_id": None,
            }

        return _generate_messages()

    async def _handle_stream_message(
        self, message: Message, stream_callback: StreamCallback
    ) -> None:
        """Handle streaming message from claude-agent-sdk."""
        try:
            if isinstance(message, AssistantMessage):
                # Extract content from assistant message
                assistant_content = getattr(message, "content", [])
                if assistant_content and isinstance(assistant_content, list):
                    # Extract text from TextBlock objects
                    text_parts = []
                    tool_calls = []
                    tool_results = []
                    for block in assistant_content:
                        if hasattr(block, "text"):
                            text_parts.append(block.text)
                        elif hasattr(block, "name") and hasattr(block, "input"):
                            tool_calls.append(
                                {
                                    "name": getattr(
                                        block,
                                        "name",
                                        getattr(block, "tool_name", "unknown"),
                                    ),
                                    "input": getattr(
                                        block,
                                        "input",
                                        getattr(block, "tool_input", {}),
                                    ),
                                }
                            )
                        elif hasattr(block, "tool_use_id"):
                            tool_results.append(
                                {
                                    "tool_use_id": getattr(block, "tool_use_id", None),
                                    "content": getattr(block, "content", None),
                                    "is_error": bool(getattr(block, "is_error", False)),
                                }
                            )

                    if text_parts:
                        update = StreamUpdate(
                            type="assistant",
                            content="\n".join(text_parts),
                            metadata={"source": "sdk"},
                        )
                        await stream_callback(update)

                    if tool_calls:
                        update = StreamUpdate(
                            type="assistant",
                            tool_calls=tool_calls,
                            metadata={"source": "sdk"},
                        )
                        await stream_callback(update)

                    for result in tool_results:
                        update = StreamUpdate(
                            type="tool_result",
                            content=str(result.get("content") or ""),
                            metadata={
                                "tool_use_id": result.get("tool_use_id"),
                                "is_error": result.get("is_error", False),
                            },
                            error_info=(
                                {"message": str(result.get("content") or "")}
                                if result.get("is_error")
                                else None
                            ),
                        )
                        await stream_callback(update)

                elif assistant_content:
                    # Fallback for non-list content
                    update = StreamUpdate(
                        type="assistant",
                        content=str(assistant_content),
                        metadata={"source": "sdk"},
                    )
                    await stream_callback(update)

            elif isinstance(message, UserMessage):
                raw_user_content = getattr(message, "content", "")
                user_content = str(raw_user_content).strip()
                if user_content:
                    update = StreamUpdate(
                        type="user",
                        content=user_content,
                    )
                    await stream_callback(update)
            elif isinstance(message, SystemMessage):
                raw_data = getattr(message, "data", {})
                subtype = str(getattr(message, "subtype", "") or "").strip()
                metadata = self._build_sdk_system_metadata(raw_data, subtype=subtype)

                system_content: Optional[str] = None
                if isinstance(raw_data, dict):
                    system_content = str(raw_data.get("message") or "").strip() or None

                update = StreamUpdate(
                    type="system",
                    content=system_content,
                    metadata=metadata,
                )
                await stream_callback(update)

        except Exception as e:
            logger.warning("Stream callback failed", error=str(e))

    @staticmethod
    def _build_sdk_system_metadata(payload: Any, *, subtype: str) -> Dict[str, Any]:
        """Build StreamUpdate metadata from SDK SystemMessage payload."""
        raw: Dict[str, Any] = payload if isinstance(payload, dict) else {}
        metadata: Dict[str, Any] = {"subtype": subtype}

        if subtype == "init":
            metadata.update(
                {
                    "tools": raw.get("tools", []),
                    "mcp_servers": raw.get("mcp_servers", []),
                    "model": raw.get("model"),
                    "cwd": raw.get("cwd"),
                    "permission_mode": raw.get("permissionMode"),
                }
            )

        metadata.update(ClaudeSDKManager._extract_model_capabilities(raw))
        return metadata

    @staticmethod
    def _extract_model_capabilities(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Extract optional model capability fields from SDK stream payload."""
        if not isinstance(payload, dict):
            return {}

        model_info = payload.get("modelInfo")
        if not isinstance(model_info, dict):
            model_info = payload.get("model_info")
        if not isinstance(model_info, dict):
            model_info = {}

        capabilities: Dict[str, Any] = {}

        supports_effort = payload.get("supportsEffort")
        if supports_effort is None:
            supports_effort = model_info.get("supportsEffort")
        if supports_effort is None:
            supports_effort = model_info.get("supports_effort")
        if supports_effort is not None:
            normalized_supports_effort = bool(supports_effort)
            capabilities["supports_effort"] = normalized_supports_effort
            capabilities["supportsEffort"] = normalized_supports_effort

        effort_levels = payload.get("supportedEffortLevels")
        if effort_levels is None:
            effort_levels = model_info.get("supportedEffortLevels")
        if effort_levels is None:
            effort_levels = model_info.get("supported_effort_levels")
        if isinstance(effort_levels, (list, tuple)):
            normalized = [
                str(level).strip() for level in effort_levels if str(level).strip()
            ]
            if normalized:
                capabilities["supported_effort_levels"] = normalized
                capabilities["supportedEffortLevels"] = normalized

        supports_adaptive_thinking = payload.get("supportsAdaptiveThinking")
        if supports_adaptive_thinking is None:
            supports_adaptive_thinking = model_info.get("supportsAdaptiveThinking")
        if supports_adaptive_thinking is None:
            supports_adaptive_thinking = model_info.get("supports_adaptive_thinking")
        if supports_adaptive_thinking is not None:
            normalized_supports_adaptive = bool(supports_adaptive_thinking)
            capabilities["supports_adaptive_thinking"] = normalized_supports_adaptive
            capabilities["supportsAdaptiveThinking"] = normalized_supports_adaptive

        return capabilities

    def _build_model_usage(
        self,
        sdk_usage: Optional[Dict[str, Any]],
        cost: float,
        resolved_model: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Build model_usage dict from SDK usage data.

        SDK usage has flat fields like input_tokens, output_tokens.
        We wrap them in a format compatible with CLI's modelUsage.
        """
        if not sdk_usage:
            return None
        usage_payload = {
            "inputTokens": sdk_usage.get("input_tokens", 0),
            "outputTokens": sdk_usage.get("output_tokens", 0),
            "cacheReadInputTokens": sdk_usage.get("cache_read_input_tokens", 0),
            "cacheCreationInputTokens": sdk_usage.get("cache_creation_input_tokens", 0),
            "costUSD": cost,
        }
        if resolved_model:
            usage_payload["resolvedModel"] = resolved_model
            inferred_ctx = self._estimate_context_window_tokens(resolved_model)
            if inferred_ctx:
                usage_payload["contextWindow"] = inferred_ctx
                usage_payload["contextWindowSource"] = "estimated"
        return {(resolved_model or "sdk"): usage_payload}

    @staticmethod
    def _estimate_context_window_tokens(model_name: Optional[str]) -> Optional[int]:
        """Estimate context window tokens for common Claude model names."""
        if not model_name:
            return None
        lower = str(model_name).lower()
        if (
            "claude" in lower
            or "sonnet" in lower
            or "opus" in lower
            or "haiku" in lower
        ):
            return 200_000
        return None

    def _extract_content_from_messages(self, messages: List[Message]) -> str:
        """Extract content from message list."""
        content_parts = []

        for message in messages:
            if isinstance(message, AssistantMessage):
                content = getattr(message, "content", [])
                if content and isinstance(content, list):
                    # Extract text from TextBlock objects
                    for block in content:
                        if hasattr(block, "text"):
                            content_parts.append(block.text)
                elif content:
                    # Fallback for non-list content
                    content_parts.append(str(content))

        return "\n".join(content_parts)

    def _extract_result_text_from_messages(self, messages: List[Message]) -> str:
        """Extract fallback text from ResultMessage when assistant text is absent."""
        for message in reversed(messages):
            if not isinstance(message, ResultMessage):
                continue
            result = getattr(message, "result", None)
            if result is None:
                continue
            result_text = str(result).strip()
            if result_text:
                return result_text
        return ""

    def _extract_response_content(self, messages: List[Message]) -> str:
        """Extract response content with safe fallback for command-style outputs."""
        content = self._extract_content_from_messages(messages)
        if content and content.strip():
            return content

        result_text = self._extract_result_text_from_messages(messages)
        if result_text:
            logger.debug(
                "Using ResultMessage fallback content",
                content_preview=result_text[:240],
            )
            return result_text

        local_output_text = self._extract_local_command_output_from_messages(messages)
        if local_output_text:
            logger.debug(
                "Using local-command stdout fallback content",
                content_preview=local_output_text[:240],
            )
            return local_output_text

        return content

    def _extract_local_command_output_from_messages(
        self, messages: List[Message]
    ) -> str:
        """Extract <local-command-stdout> payload carried by UserMessage replay."""
        for message in reversed(messages):
            if not isinstance(message, UserMessage):
                continue
            content = getattr(message, "content", "")
            extracted = self._extract_local_command_output(content)
            if extracted:
                return extracted
        return ""

    @staticmethod
    def _extract_local_command_output(content: Any) -> str:
        """Extract text inside <local-command-stdout|stderr> wrappers."""
        if isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if hasattr(block, "text"):
                    parts.append(str(getattr(block, "text", "")))
                elif isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif isinstance(block, str):
                    parts.append(block)
            content_text = "\n".join(parts)
        else:
            content_text = str(content or "")

        if not content_text:
            return ""

        matches = list(
            re.finditer(
                r"<local-command-(stdout|stderr)>(.*?)</local-command-\1>",
                content_text,
                flags=re.DOTALL | re.IGNORECASE,
            )
        )
        if not matches:
            return ""

        return "\n\n".join(match.group(2).strip() for match in matches).strip()

    def _extract_tools_from_messages(
        self, messages: List[Message]
    ) -> List[Dict[str, Any]]:
        """Extract tools used from message list."""
        tools_used = []
        current_time = asyncio.get_event_loop().time()

        for message in messages:
            if isinstance(message, AssistantMessage):
                content = getattr(message, "content", [])
                if content and isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolUseBlock) or (
                            hasattr(block, "name") and hasattr(block, "input")
                        ):
                            tools_used.append(
                                {
                                    "name": getattr(
                                        block,
                                        "name",
                                        getattr(block, "tool_name", "unknown"),
                                    ),
                                    "timestamp": current_time,
                                    "input": getattr(
                                        block, "input", getattr(block, "tool_input", {})
                                    ),
                                }
                            )

        return tools_used

    def _load_mcp_config(self, config_path: Path) -> Dict[str, Any]:
        """Load MCP server configuration from a JSON file.

        The new claude-agent-sdk expects mcp_servers as a dict, not a file path.
        """
        import json

        try:
            with open(config_path) as f:
                config_data = json.load(f)
            mcp_servers = config_data.get("mcpServers")
            return mcp_servers if isinstance(mcp_servers, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            logger.error(
                "Failed to load MCP config", path=str(config_path), error=str(e)
            )
            return {}

    def _update_session(self, session_id: str, messages: List[Message]) -> None:
        """Update session data."""
        if session_id not in self.active_sessions:
            self.active_sessions[session_id] = {
                "messages": [],
                "created_at": asyncio.get_event_loop().time(),
            }

        session_data = self.active_sessions[session_id]
        session_data["messages"] = messages
        session_data["last_used"] = asyncio.get_event_loop().time()

    async def kill_all_processes(self) -> None:
        """Kill all active processes (no-op for SDK)."""
        logger.info("Clearing active SDK sessions", count=len(self.active_sessions))
        self.active_sessions.clear()

    def get_active_process_count(self) -> int:
        """Get number of active sessions."""
        return len(self.active_sessions)

    async def execute_with_client(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[StreamCallback] = None,
        permission_callback: Optional[Callable] = None,
        model: Optional[str] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> ClaudeResponse:
        """Execute command via ClaudeSDKClient (short-lived connection mode).

        This method supports can_use_tool permission callbacks, which require
        the Client's streaming mode. Use this when Telegram permission approval
        is needed; otherwise use execute_command() with query().
        """
        start_time = asyncio.get_event_loop().time()

        logger.info(
            "Starting Claude SDK Client command",
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
            has_permission_callback=bool(permission_callback),
        )
        timeout_seconds = max(1, resolve_cli_timeout_seconds(self.config))

        try:
            cli_path = find_claude_cli(self.config.claude_cli_path)
            requested_model = self._resolve_requested_model(model, working_directory)
            options_kwargs: Dict[str, Any] = {
                "max_turns": self.config.claude_max_turns,
                "cwd": str(working_directory),
                "allowed_tools": self.config.claude_allowed_tools or [],
                "cli_path": cli_path,
            }
            if requested_model:
                options_kwargs["model"] = requested_model
            setting_sources = self._resolve_setting_sources()
            if setting_sources is not None:
                options_kwargs["setting_sources"] = setting_sources

            options = ClaudeAgentOptions(**options_kwargs)

            if permission_callback:
                options.can_use_tool = permission_callback

            if self.config.enable_mcp and self.config.mcp_config_path:
                options.mcp_servers = self._load_mcp_config(self.config.mcp_config_path)

            if session_id and continue_session:
                options.resume = session_id

            await self._emit_init_update(stream_callback, options)

            # Execute with Client short-lived connection
            messages: List[Message] = []
            client = ClaudeSDKClient(options)
            model_resolved_emitted = False
            disconnect_timeout = max(1, min(10, timeout_seconds))
            try:
                # connect() without prompt — establishes connection only
                await asyncio.wait_for(
                    client.connect(),
                    timeout=timeout_seconds,
                )

                async def _query_and_collect_messages() -> None:
                    nonlocal model_resolved_emitted

                    # Send prompt via client.query()
                    # Build multimodal prompt if images are provided
                    if images:
                        query_prompt = await self._build_multimodal_prompt(
                            prompt, images
                        )
                        await client.query(query_prompt)
                    else:
                        await client.query(prompt)

                    # Receive messages until ResultMessage
                    async for message in client.receive_response():
                        messages.append(message)
                        if stream_callback and not model_resolved_emitted:
                            model_name = getattr(message, "model", None)
                            if model_name:
                                model_resolved_emitted = (
                                    await self._emit_model_resolved_update(
                                        stream_callback, str(model_name)
                                    )
                                )
                        if stream_callback:
                            try:
                                await self._handle_stream_message(
                                    message, stream_callback
                                )
                            except Exception as cb_err:
                                logger.warning(
                                    "Stream callback failed",
                                    error=str(cb_err),
                                )
                        if isinstance(message, ResultMessage):
                            break

                await asyncio.wait_for(
                    _query_and_collect_messages(),
                    timeout=timeout_seconds,
                )
            finally:
                try:
                    await asyncio.wait_for(
                        client.disconnect(),
                        timeout=disconnect_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timed out while disconnecting Claude SDK Client",
                        timeout_seconds=disconnect_timeout,
                    )
                except Exception as disconnect_error:
                    logger.warning(
                        "Failed to disconnect Claude SDK Client cleanly",
                        error=str(disconnect_error),
                    )

            # Extract result data (same logic as execute_command)
            cost = 0.0
            claude_session_id = None
            sdk_usage = None
            resolved_model = None
            tools_used: List[Dict[str, Any]] = []
            for message in messages:
                if resolved_model is None:
                    model_name = getattr(message, "model", None)
                    if model_name:
                        resolved_model = str(model_name)
                if isinstance(message, ResultMessage):
                    cost = getattr(message, "total_cost_usd", 0.0) or 0.0
                    claude_session_id = getattr(message, "session_id", None)
                    sdk_usage = getattr(message, "usage", None)
                    tools_used = self._extract_tools_from_messages(messages)
                    break

            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
            final_session_id = claude_session_id or session_id or str(uuid.uuid4())

            if claude_session_id and claude_session_id != session_id:
                logger.info(
                    "Got session ID from Claude (Client mode)",
                    claude_session_id=claude_session_id,
                    previous_session_id=session_id,
                )

            self._update_session(final_session_id, messages)

            return ClaudeResponse(
                content=self._extract_response_content(messages),
                session_id=final_session_id,
                cost=cost,
                duration_ms=duration_ms,
                num_turns=len(
                    [
                        m
                        for m in messages
                        if isinstance(m, (UserMessage, AssistantMessage))
                    ]
                ),
                tools_used=tools_used,
                model_usage=self._build_model_usage(
                    sdk_usage,
                    cost,
                    resolved_model=resolved_model,
                ),
            )

        except asyncio.TimeoutError:
            logger.error(
                "Claude SDK Client timed out",
                timeout_seconds=timeout_seconds,
            )
            raise ClaudeTimeoutError(
                f"Claude SDK Client timed out after {timeout_seconds}s"
            )

        except CLINotFoundError as e:
            logger.error("Claude CLI not found", error=str(e))
            raise ClaudeProcessError(
                "Claude Code not found. Please ensure Claude is installed:\n"
                "  npm install -g @anthropic-ai/claude-code"
            )

        except ProcessError as e:
            error_str = str(e)
            logger.error("Claude process failed (Client)", error=error_str)
            if "mcp" in error_str.lower():
                raise ClaudeMCPError(f"MCP server error: {error_str}")
            raise ClaudeProcessError(f"Claude process error: {error_str}")

        except CLIConnectionError as e:
            error_str = str(e)
            logger.error("Claude connection error (Client)", error=error_str)
            if "mcp" in error_str.lower() or "server" in error_str.lower():
                raise ClaudeMCPError(f"MCP server connection failed: {error_str}")
            raise ClaudeProcessError(f"Failed to connect to Claude: {error_str}")

        except CLIJSONDecodeError as e:
            logger.error("Claude SDK Client JSON decode error", error=str(e))
            raise ClaudeParsingError(f"Failed to decode Claude response: {str(e)}")

        except ClaudeSDKError as e:
            logger.error("Claude SDK Client error", error=str(e))
            raise ClaudeProcessError(f"Claude SDK error: {str(e)}")

        except Exception as e:
            if type(e).__name__ == "ExceptionGroup" or hasattr(e, "exceptions"):
                exceptions = getattr(e, "exceptions", [e])
                main_exception = exceptions[0] if exceptions else e
                logger.error(
                    "Task group error in Claude SDK Client",
                    error=str(e),
                )
                raise ClaudeProcessError(
                    f"Claude SDK task error: {str(main_exception)}"
                )
            else:
                logger.error(
                    "Unexpected error in Claude SDK Client",
                    error=str(e),
                    error_type=type(e).__name__,
                )
                raise ClaudeProcessError(f"Unexpected error: {str(e)}")
