"""Claude Code subprocess management.

Features:
- Async subprocess execution
- Stream handling
- Timeout management
- Error recovery
"""

import asyncio
import json
import os
import re
import uuid
from asyncio.subprocess import Process
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import structlog

from ..config.settings import Settings
from .exceptions import (
    ClaudeMCPError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeTimeoutError,
)

logger = structlog.get_logger()


@dataclass
class ClaudeResponse:
    """Response from Claude Code."""

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
    """Enhanced streaming update from Claude with richer context."""

    type: str  # 'assistant', 'user', 'system', 'result', 'tool_result', 'error', 'progress'
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None

    # Enhanced fields for better tracking
    timestamp: Optional[str] = None
    session_context: Optional[Dict[str, Any]] = None
    progress: Optional[Dict[str, Any]] = None
    error_info: Optional[Dict[str, Any]] = None

    # Execution tracking
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


class ClaudeProcessManager:
    """Manage Claude Code subprocess execution with memory optimization."""

    def __init__(self, config: Settings):
        """Initialize process manager with configuration."""
        self.config = config
        self.active_processes: Dict[str, Process] = {}

        # Memory optimization settings
        self.max_message_buffer = 1000  # Limit message history
        self.streaming_buffer_size = (
            65536  # 64KB streaming buffer for large JSON messages
        )

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[StreamCallback] = None,
        model: Optional[str] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> ClaudeResponse:
        """Execute Claude Code command."""
        # Build command
        cmd = self._build_command(
            prompt,
            session_id,
            continue_session,
            model=model,
            images=images,
        )
        cli_kind = self._detect_cli_kind(cmd[0])
        cli_display_name = "Codex CLI" if cli_kind == "codex" else "Claude Code"

        # Create process ID for tracking
        process_id = str(uuid.uuid4())

        logger.info(
            "Starting Claude Code process",
            process_id=process_id,
            cli_kind=cli_kind,
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
        )

        try:
            if cli_kind == "codex":
                await self._emit_codex_init_updates(
                    stream_callback, working_directory, model
                )

            # Start process
            process = await self._start_process(cmd, working_directory)
            self.active_processes[process_id] = process

            # Handle output with timeout
            result = await asyncio.wait_for(
                self._handle_process_output(
                    process,
                    stream_callback,
                    cli_kind=cli_kind,
                ),
                timeout=self.config.claude_timeout_seconds,
            )

            logger.info(
                "Claude Code process completed successfully",
                process_id=process_id,
                cost=result.cost,
                duration_ms=result.duration_ms,
            )

            return result

        except asyncio.TimeoutError:
            # Kill process on timeout
            if process_id in self.active_processes:
                self.active_processes[process_id].kill()
                await self.active_processes[process_id].wait()

            logger.error(
                "Claude Code process timed out",
                process_id=process_id,
                timeout_seconds=self.config.claude_timeout_seconds,
            )

            raise ClaudeTimeoutError(
                f"{cli_display_name} timed out after "
                f"{self.config.claude_timeout_seconds}s"
            )

        except Exception as e:
            logger.error(
                "Claude Code process failed",
                process_id=process_id,
                error=str(e),
            )
            raise

        finally:
            # Clean up
            if process_id in self.active_processes:
                del self.active_processes[process_id]

    async def _emit_codex_init_updates(
        self,
        stream_callback: Optional[StreamCallback],
        working_directory: Path,
        model: Optional[str],
    ) -> None:
        """Emit synthetic Codex init/model updates for UI parity."""
        if not stream_callback:
            return

        requested_model = str(model or "").strip()
        try:
            await stream_callback(
                StreamUpdate(
                    type="system",
                    metadata={
                        "subtype": "init",
                        "tools": self.config.claude_allowed_tools or [],
                        "model": requested_model or None,
                        "cwd": str(working_directory),
                        "engine": "codex",
                    },
                )
            )
        except Exception as e:
            logger.warning("Failed to emit Codex init update", error=str(e))

        # Do not emit synthetic "model_resolved" for Codex without runtime evidence.
        # Exact resolved model should come from Codex events (e.g. turn_context).

    def _resolve_cli_path(self) -> str:
        """Resolve configured CLI executable path."""
        from .sdk_integration import find_claude_cli

        return (
            find_claude_cli(self.config.claude_cli_path)
            or self.config.claude_binary_path
            or "claude"
        )

    @staticmethod
    def _detect_cli_kind(cli_path: str) -> str:
        """Detect CLI kind by executable name."""
        basename = os.path.basename(str(cli_path)).lower()
        return "codex" if basename.startswith("codex") else "claude"

    def _build_command(
        self,
        prompt: str,
        session_id: Optional[str],
        continue_session: bool,
        model: Optional[str] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> List[str]:
        """Build CLI command with engine-specific arguments."""
        cli_path = self._resolve_cli_path()
        cli_kind = self._detect_cli_kind(cli_path)
        if cli_kind == "codex":
            return self._build_codex_command(
                cli_path=cli_path,
                prompt=prompt,
                session_id=session_id,
                continue_session=continue_session,
                model=model,
                images=images,
            )
        return self._build_claude_command(
            cli_path=cli_path,
            prompt=prompt,
            session_id=session_id,
            continue_session=continue_session,
            model=model,
        )

    def _build_claude_command(
        self,
        *,
        cli_path: str,
        prompt: str,
        session_id: Optional[str],
        continue_session: bool,
        model: Optional[str],
    ) -> List[str]:
        """Build legacy Claude CLI command."""
        cmd = [cli_path]

        if continue_session and not prompt:
            # Continue existing session without new prompt
            cmd.extend(["--continue"])
            if session_id:
                cmd.extend(["--resume", session_id])
        elif session_id and prompt and continue_session:
            # Follow-up message in existing session - use resume with new prompt
            cmd.extend(["--resume", session_id, "-p", prompt])
        elif prompt:
            # New session with prompt (including new sessions with session_id)
            cmd.extend(["-p", prompt])
        else:
            # This shouldn't happen, but fallback to new session
            cmd.extend(["-p", ""])

        # Always use streaming JSON for real-time updates
        cmd.extend(["--output-format", "stream-json"])

        # stream-json requires --verbose when using --print mode
        cmd.extend(["--verbose"])

        # Add safety limits
        cmd.extend(["--max-turns", str(self.config.claude_max_turns)])

        # Add model override if specified by user
        if model:
            cmd.extend(["--model", model])

        # Add allowed tools if configured
        if (
            hasattr(self.config, "claude_allowed_tools")
            and self.config.claude_allowed_tools
        ):
            cmd.extend(["--allowedTools", ",".join(self.config.claude_allowed_tools)])

        # Add MCP server configuration if enabled
        if self.config.enable_mcp and self.config.mcp_config_path:
            cmd.extend(["--mcp-config", str(self.config.mcp_config_path)])

        logger.debug("Built Claude Code command", command=cmd)
        return cmd

    def _build_codex_command(
        self,
        *,
        cli_path: str,
        prompt: str,
        session_id: Optional[str],
        continue_session: bool,
        model: Optional[str],
        images: Optional[List[Dict[str, str]]] = None,
    ) -> List[str]:
        """Build Codex CLI command."""
        cmd = [cli_path, "exec", "--json", "--skip-git-repo-check"]

        # Codex reads MCP servers from ~/.codex/config.toml by default.
        # Keep MCP disabled unless explicitly requested by config.
        if not self.config.codex_enable_mcp:
            cmd.extend(["-c", "mcp_servers={}"])

        if model:
            cmd.extend(["--model", model])

        image_paths = self._extract_codex_image_paths(images)
        if images and not image_paths:
            raise ClaudeProcessError(
                "Codex image input requires local file paths in images[*].file_path."
            )
        normalized_prompt = str(prompt or "").strip()
        fallback_prompt = (
            "Please analyze the attached image(s)."
            if image_paths
            else "Please continue where we left off"
        )
        effective_prompt = normalized_prompt or fallback_prompt

        if continue_session:
            # Use resume subcommand shape:
            # codex exec resume [SESSION_ID|--last] [--image ...] [PROMPT]
            cmd.append("resume")
            if session_id:
                cmd.append(session_id)
            else:
                cmd.append("--last")
            for image_path in image_paths:
                cmd.extend(["--image", image_path])
            cmd.append(effective_prompt)
        else:
            for image_path in image_paths:
                cmd.extend(["--image", image_path])
            cmd.append(effective_prompt)

        logger.debug("Built Codex CLI command", command=cmd)
        return cmd

    def supports_image_inputs(
        self, images: Optional[List[Dict[str, str]]] = None
    ) -> bool:
        """Whether current subprocess CLI can accept image attachments."""
        cli_path = self._resolve_cli_path()
        if self._detect_cli_kind(cli_path) != "codex":
            return False
        if not images:
            return True
        return bool(self._extract_codex_image_paths(images))

    @staticmethod
    def _extract_codex_image_paths(
        images: Optional[List[Dict[str, str]]],
    ) -> List[str]:
        """Extract valid image file paths for Codex CLI --image flags."""
        if not images:
            return []
        paths: List[str] = []
        for image in images:
            file_path = str(image.get("file_path") or "").strip()
            if file_path:
                paths.append(file_path)
        return paths

    async def _start_process(self, cmd: List[str], cwd: Path) -> Process:
        """Start Claude Code subprocess."""
        env = os.environ.copy()
        # Avoid nested Claude session detection when bot is launched from CLAUDECODE env.
        env.pop("CLAUDECODE", None)
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            # Limit memory usage
            limit=1024 * 1024 * 512,  # 512MB
        )

    async def _handle_process_output(
        self,
        process: Process,
        stream_callback: Optional[StreamCallback],
        *,
        cli_kind: str = "claude",
    ) -> ClaudeResponse:
        """Memory-optimized output handling with bounded buffers."""
        message_buffer: deque[Dict[str, Any]] = deque(maxlen=self.max_message_buffer)
        result = None
        parsing_errors = []
        codex_thread_id = ""
        codex_emitted_model = ""

        stdout_reader = process.stdout
        if stdout_reader is None:
            raise ClaudeProcessError("Claude subprocess stdout pipe is unavailable")

        async for line in self._read_stream_bounded(stdout_reader):
            if not line:
                continue
            if not line.lstrip().startswith("{"):
                # Some CLIs may print plain logs on stdout even in JSON mode.
                logger.debug(
                    "Skipping non-JSON stdout line",
                    cli_kind=cli_kind,
                    line=line[:200],
                )
                continue
            try:
                msg = json.loads(line)

                # Enhanced validation
                if not self._validate_message_structure(msg):
                    parsing_errors.append(f"Invalid message structure: {line[:100]}")
                    continue

                message_buffer.append(msg)

                # Process immediately to avoid memory buildup
                update = self._parse_stream_message(msg)
                if update and stream_callback:
                    try:
                        await stream_callback(update)
                    except Exception as e:
                        logger.warning(
                            "Stream callback failed",
                            error=str(e),
                            update_type=update.type,
                        )
                if (
                    cli_kind == "codex"
                    and update
                    and update.type == "system"
                    and (update.metadata or {}).get("subtype") == "model_resolved"
                ):
                    codex_emitted_model = str(
                        (update.metadata or {}).get("model") or ""
                    ).strip()

                if cli_kind == "codex":
                    msg_type = str(msg.get("type") or "").strip()
                    if msg_type == "thread.started":
                        codex_thread_id = str(msg.get("thread_id") or "").strip()
                    codex_emitted_model = (
                        await self._maybe_emit_codex_model_from_snapshot(
                            stream_callback=stream_callback,
                            thread_id=codex_thread_id,
                            emitted_model=codex_emitted_model,
                        )
                    )

                # Check for final result
                msg_type = msg.get("type")
                if msg_type == "result" or (
                    cli_kind == "codex"
                    and msg_type in {"turn.completed", "turn.failed"}
                ):
                    result = msg

            except json.JSONDecodeError as e:
                parsing_errors.append(f"JSON decode error: {e}")
                logger.warning(
                    "Failed to parse JSON line", line=line[:200], error=str(e)
                )
                continue

        # Enhanced error reporting
        if parsing_errors:
            logger.warning(
                "Parsing errors encountered",
                count=len(parsing_errors),
                errors=parsing_errors[:5],
            )

        # Wait for process to complete
        return_code = await process.wait()

        if return_code != 0:
            stderr_reader = process.stderr
            if stderr_reader is None:
                error_msg = ""
            else:
                stderr = await stderr_reader.read()
                error_msg = stderr.decode("utf-8", errors="replace")
            cli_display_name = "Codex CLI" if cli_kind == "codex" else "Claude Code"
            provider_name = "Codex" if cli_kind == "codex" else "Claude AI"
            logger.error(
                "Claude Code process failed",
                return_code=return_code,
                stderr=error_msg,
            )

            # Check for specific error types
            if "usage limit reached" in error_msg.lower():
                # Extract reset time if available
                import re

                time_match = re.search(
                    r"reset at (\d+[apm]+)", error_msg, re.IGNORECASE
                )
                timezone_match = re.search(r"\(([^)]+)\)", error_msg)

                reset_time = time_match.group(1) if time_match else "later"
                timezone = timezone_match.group(1) if timezone_match else ""

                user_friendly_msg = (
                    f"⏱️ **{provider_name} Usage Limit Reached**\n\n"
                    f"You've reached your {provider_name} usage limit for this period.\n\n"
                    f"**When will it reset?**\n"
                    f"Your limit will reset at **{reset_time}**"
                    f"{f' ({timezone})' if timezone else ''}\n\n"
                    f"**What you can do:**\n"
                    f"• Wait for the limit to reset automatically\n"
                    f"• Try again after the reset time\n"
                    f"• Use simpler requests that require less processing\n"
                    f"• Contact support if you need a higher limit"
                )

                raise ClaudeProcessError(user_friendly_msg)

            # Check for MCP-related errors
            if "mcp" in error_msg.lower():
                raise ClaudeMCPError(f"MCP server error: {error_msg}")

            # Generic error handling for other cases
            raise ClaudeProcessError(
                f"{cli_display_name} exited with code {return_code}: {error_msg}"
            )

        if cli_kind == "codex" and isinstance(result, dict):
            if result.get("type") == "turn.failed":
                failure_message = self._extract_codex_failure_message(
                    result,
                    list(message_buffer),
                )
                raise ClaudeProcessError(f"Codex turn failed: {failure_message}")

        if not result:
            logger.error(
                "No result message received from Claude Code", cli_kind=cli_kind
            )
            if cli_kind == "codex":
                raise ClaudeParsingError("No result message received from Codex CLI")
            raise ClaudeParsingError("No result message received from Claude Code")

        if cli_kind == "codex":
            codex_emitted_model = await self._maybe_emit_codex_model_from_snapshot(
                stream_callback=stream_callback,
                thread_id=codex_thread_id,
                emitted_model=codex_emitted_model,
            )

        return self._parse_result(result, list(message_buffer))

    async def _maybe_emit_codex_model_from_snapshot(
        self,
        *,
        stream_callback: Optional[StreamCallback],
        thread_id: str,
        emitted_model: str,
    ) -> str:
        """Emit best-effort Codex runtime model from local session snapshot."""
        if not stream_callback or not thread_id:
            return emitted_model

        resolved_model = self._probe_codex_model_from_local_session(thread_id)
        if not resolved_model or resolved_model == emitted_model:
            return emitted_model

        try:
            await stream_callback(
                StreamUpdate(
                    type="system",
                    metadata={
                        "subtype": "model_resolved",
                        "model": resolved_model,
                        "engine": "codex",
                    },
                )
            )
            return resolved_model
        except Exception as e:
            logger.warning(
                "Failed to emit Codex model from snapshot",
                error=str(e),
                thread_id=thread_id,
            )
            return emitted_model

    @staticmethod
    def _probe_codex_model_from_local_session(thread_id: str) -> Optional[str]:
        """Read latest Codex local session file and extract turn_context model."""
        sid = str(thread_id or "").strip()
        if not sid:
            return None

        sessions_root = Path.home() / ".codex" / "sessions"
        if not sessions_root.is_dir():
            return None

        try:
            candidates = list(sessions_root.rglob(f"*{sid}*.jsonl"))
        except OSError:
            return None
        if not candidates:
            return None

        latest_file: Optional[Path] = None
        latest_mtime = -1.0
        for candidate in candidates:
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_file = candidate
                latest_mtime = mtime
        if latest_file is None:
            return None

        try:
            size = latest_file.stat().st_size
        except OSError:
            return None
        if size <= 0:
            return None

        try:
            chunk_size = min(size, 262_144)
            with open(latest_file, "rb") as fh:
                fh.seek(max(0, size - chunk_size))
                data = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return None

        lines = [line.strip() for line in data.splitlines() if line.strip()]
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if str(record.get("type") or "").strip() != "turn_context":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            model = str(payload.get("model") or "").strip()
            if model:
                return model
        return None

    async def _read_stream(self, stream: asyncio.StreamReader) -> AsyncIterator[str]:
        """Read lines from stream."""
        while True:
            line = await stream.readline()
            if not line:
                break
            yield line.decode("utf-8", errors="replace").strip()

    async def _read_stream_bounded(
        self, stream: asyncio.StreamReader
    ) -> AsyncIterator[str]:
        """Read stream with memory bounds to prevent excessive memory usage."""
        buffer = b""

        while True:
            chunk = await stream.read(self.streaming_buffer_size)
            if not chunk:
                break

            buffer += chunk

            # Process complete lines
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield line.decode("utf-8", errors="replace").strip()

        # Process remaining buffer
        if buffer:
            yield buffer.decode("utf-8", errors="replace").strip()

    def _parse_stream_message(self, msg: Dict) -> Optional[StreamUpdate]:
        """Enhanced parsing with comprehensive message type support."""
        msg_type = msg.get("type")

        # Add support for more message types
        if msg_type == "assistant":
            return self._parse_assistant_message(msg)
        elif msg_type == "tool_result":
            return self._parse_tool_result_message(msg)
        elif msg_type == "user":
            return self._parse_user_message(msg)
        elif msg_type == "system":
            return self._parse_system_message(msg)
        elif msg_type == "error":
            return self._parse_error_message(msg)
        elif msg_type == "progress":
            return self._parse_progress_message(msg)
        elif msg_type in {
            "thread.started",
            "turn.started",
            "turn.completed",
            "turn.failed",
            "item.started",
            "item.completed",
            "turn_context",
        }:
            return self._parse_codex_stream_message(msg)

        # Unknown message type - log and continue
        logger.debug("Unknown message type", msg_type=msg_type, msg=msg)
        return None

    def _parse_codex_stream_message(self, msg: Dict) -> Optional[StreamUpdate]:
        """Parse Codex JSONL events into unified stream updates."""
        msg_type = msg.get("type")

        if msg_type == "turn_context":
            payload = msg.get("payload")
            if not isinstance(payload, dict):
                return None
            model = str(payload.get("model") or "").strip()
            if not model:
                return None
            return StreamUpdate(
                type="system",
                metadata={
                    "subtype": "model_resolved",
                    "model": model,
                    "engine": "codex",
                },
            )

        if msg_type == "thread.started":
            return StreamUpdate(
                type="system",
                metadata={"subtype": "thread.started", "engine": "codex"},
                session_context={"session_id": msg.get("thread_id")},
            )

        if msg_type == "turn.started":
            return StreamUpdate(
                type="progress",
                content="Codex turn started",
                metadata={"subtype": "turn.started", "engine": "codex"},
            )

        if msg_type == "turn.completed":
            return StreamUpdate(
                type="progress",
                content="Codex turn completed",
                metadata={
                    "usage": msg.get("usage"),
                    "subtype": "turn.completed",
                    "engine": "codex",
                },
            )

        if msg_type == "turn.failed":
            message = self._extract_codex_failure_message(msg, [msg])
            return StreamUpdate(
                type="error",
                content=message,
                metadata={
                    "subtype": "turn.failed",
                    "usage": msg.get("usage"),
                    "engine": "codex",
                },
                error_info={
                    "message": message,
                    "code": (
                        msg.get("error", {}).get("code")
                        if isinstance(msg.get("error"), dict)
                        else None
                    ),
                    "subtype": "turn.failed",
                },
            )

        item = msg.get("item")
        if not isinstance(item, dict):
            return None

        item_type = item.get("type")
        if item_type == "agent_message":
            text = str(item.get("text", "")).strip()
            return StreamUpdate(
                type="assistant",
                content=text or None,
                metadata={
                    "subtype": msg_type,
                    "item_type": item_type,
                    "engine": "codex",
                },
            )
        if item_type == "reasoning":
            text = str(item.get("text", "")).strip()
            condensed = self._condense_codex_reasoning_text(text)
            return StreamUpdate(
                type="progress",
                content=condensed or text or None,
                metadata={
                    "subtype": msg_type,
                    "item_type": item_type,
                    "engine": "codex",
                },
            )
        if item_type == "command_execution":
            status = item.get("status") or "unknown"
            command = item.get("command") or ""
            return StreamUpdate(
                type="progress",
                content=str(command).strip() or None,
                metadata={
                    "subtype": msg_type,
                    "item_type": item_type,
                    "status": status,
                    "command": command,
                    "exit_code": item.get("exit_code"),
                    "engine": "codex",
                },
                progress={"operation": "command_execution"},
            )
        return None

    @staticmethod
    def _condense_codex_reasoning_text(text: str, max_chars: int = 180) -> str:
        """Condense verbose Codex reasoning text into a concise one-liner."""
        if not text:
            return ""
        normalized = text.replace("\r\n", "\n").strip()
        first_block = next(
            (block.strip() for block in normalized.split("\n\n") if block.strip()),
            normalized,
        )
        # Drop markdown decorations for Telegram progress readability.
        cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", first_block)
        cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
        cleaned = " ".join(cleaned.split())
        if len(cleaned) > max_chars:
            cleaned = cleaned[: max_chars - 3].rstrip() + "..."
        return cleaned

    def _parse_assistant_message(self, msg: Dict) -> StreamUpdate:
        """Parse assistant message with enhanced context."""
        message = msg.get("message", {})
        content_blocks = message.get("content", [])

        # Get text content
        text_content = []
        tool_calls = []

        for block in content_blocks:
            if block.get("type") == "text":
                text_content.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "name": block.get("name"),
                        "input": block.get("input", {}),
                        "id": block.get("id"),
                    }
                )

        return StreamUpdate(
            type="assistant",
            content="\n".join(text_content) if text_content else None,
            tool_calls=tool_calls if tool_calls else None,
            timestamp=msg.get("timestamp"),
            session_context={"session_id": msg.get("session_id")},
            execution_id=msg.get("id"),
        )

    def _parse_tool_result_message(self, msg: Dict) -> StreamUpdate:
        """Parse tool execution results."""
        result = msg.get("result", {})
        content = result.get("content") if isinstance(result, dict) else str(result)

        return StreamUpdate(
            type="tool_result",
            content=content,
            metadata={
                "tool_use_id": msg.get("tool_use_id"),
                "is_error": (
                    result.get("is_error", False) if isinstance(result, dict) else False
                ),
                "execution_time_ms": (
                    result.get("execution_time_ms")
                    if isinstance(result, dict)
                    else None
                ),
            },
            timestamp=msg.get("timestamp"),
            session_context={"session_id": msg.get("session_id")},
            error_info={"message": content} if result.get("is_error", False) else None,
        )

    def _parse_user_message(self, msg: Dict) -> StreamUpdate:
        """Parse user message."""
        message = msg.get("message", {})
        content = message.get("content", "")

        # Handle both string and block format content
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            content = "\n".join(text_parts)

        return StreamUpdate(
            type="user",
            content=content if content else None,
            timestamp=msg.get("timestamp"),
            session_context={"session_id": msg.get("session_id")},
        )

    @staticmethod
    def _extract_model_capabilities(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Extract optional model capability fields from stream payload."""
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

    def _parse_system_message(self, msg: Dict) -> StreamUpdate:
        """Parse system messages including init and other subtypes."""
        subtype = msg.get("subtype")
        model_capabilities = self._extract_model_capabilities(msg)

        if subtype == "init":
            # Initial system message with available tools
            return StreamUpdate(
                type="system",
                metadata={
                    "subtype": "init",
                    "tools": msg.get("tools", []),
                    "mcp_servers": msg.get("mcp_servers", []),
                    "model": msg.get("model"),
                    "cwd": msg.get("cwd"),
                    "permission_mode": msg.get("permissionMode"),
                    **model_capabilities,
                },
                session_context={"session_id": msg.get("session_id")},
            )
        else:
            # Other system messages
            return StreamUpdate(
                type="system",
                content=msg.get("message", str(msg)),
                metadata={
                    "subtype": subtype,
                    **model_capabilities,
                },
                timestamp=msg.get("timestamp"),
                session_context={"session_id": msg.get("session_id")},
            )

    def _parse_error_message(self, msg: Dict) -> StreamUpdate:
        """Parse error messages."""
        error_message = msg.get("message", msg.get("error", str(msg)))

        return StreamUpdate(
            type="error",
            content=error_message,
            error_info={
                "message": error_message,
                "code": msg.get("code"),
                "subtype": msg.get("subtype"),
            },
            timestamp=msg.get("timestamp"),
            session_context={"session_id": msg.get("session_id")},
        )

    def _parse_progress_message(self, msg: Dict) -> StreamUpdate:
        """Parse progress update messages."""
        return StreamUpdate(
            type="progress",
            content=msg.get("message", msg.get("status")),
            progress={
                "percentage": msg.get("percentage"),
                "step": msg.get("step"),
                "total_steps": msg.get("total_steps"),
                "operation": msg.get("operation"),
            },
            timestamp=msg.get("timestamp"),
            session_context={"session_id": msg.get("session_id")},
        )

    def _validate_message_structure(self, msg: Dict) -> bool:
        """Validate message has required structure."""
        required_fields = ["type"]
        return all(field in msg for field in required_fields)

    def _parse_result(self, result: Dict, messages: List[Dict]) -> ClaudeResponse:
        """Parse final result message."""
        if result.get("type") == "turn.completed":
            return self._parse_codex_result(result, messages)
        if result.get("type") == "turn.failed":
            return self._parse_codex_failed_result(result, messages)

        # Extract tools used from messages
        tools_used = []
        assistant_texts = []  # Collect all assistant text responses
        local_command_outputs = []  # Collect /context-like local command stdout

        for msg in messages:
            if msg.get("type") == "assistant":
                message = msg.get("message", {})
                for block in message.get("content", []):
                    if block.get("type") == "tool_use":
                        tools_used.append(
                            {
                                "name": block.get("name"),
                                "timestamp": msg.get("timestamp"),
                            }
                        )
                    elif block.get("type") == "text":
                        # Collect text from assistant messages
                        text = block.get("text", "").strip()
                        if text:
                            assistant_texts.append(text)
            elif msg.get("type") == "user":
                message = msg.get("message", {})
                user_content = message.get("content", "")
                extracted = self._extract_local_command_output(user_content)
                if extracted:
                    local_command_outputs.append(extracted)

        # Get content from result, or fallback to collected assistant texts
        content = result.get("result", "")
        if not content and assistant_texts:
            # Fallback: use the last assistant text message
            content = assistant_texts[-1]
            logger.debug(
                "Using fallback content from assistant messages",
                num_texts=len(assistant_texts),
                content_length=len(content),
            )
        if not content and local_command_outputs:
            content = local_command_outputs[-1]
            logger.debug(
                "Using fallback content from local command output",
                num_outputs=len(local_command_outputs),
                content_length=len(content),
            )

        return ClaudeResponse(
            content=content,
            session_id=result.get("session_id", ""),
            cost=result.get("total_cost_usd", 0.0) or 0.0,
            duration_ms=result.get("duration_ms", 0),
            num_turns=result.get("num_turns", 0),
            is_error=result.get("is_error", False),
            error_type=result.get("subtype") if result.get("is_error") else None,
            tools_used=tools_used,
            model_usage=result.get("modelUsage"),
        )

    def _parse_codex_result(self, result: Dict, messages: List[Dict]) -> ClaudeResponse:
        """Parse Codex turn-completed event into unified ClaudeResponse."""
        thread_id = ""
        assistant_texts: List[str] = []
        tools_used: List[Dict[str, Any]] = []

        for msg in messages:
            msg_type = msg.get("type")
            if msg_type == "thread.started":
                thread_id = str(msg.get("thread_id") or thread_id)
                continue

            if msg_type != "item.completed":
                continue

            item = msg.get("item")
            if not isinstance(item, dict):
                continue

            item_type = item.get("type")
            if item_type == "agent_message":
                text = str(item.get("text", "")).strip()
                if text:
                    assistant_texts.append(text)
            elif item_type == "command_execution":
                tools_used.append(
                    {
                        "name": "Bash",
                        "command": item.get("command"),
                        "exit_code": item.get("exit_code"),
                        "timestamp": msg.get("timestamp"),
                    }
                )

        usage = result.get("usage")
        if not isinstance(usage, dict):
            usage = None

        content = assistant_texts[-1] if assistant_texts else ""
        return ClaudeResponse(
            content=content,
            session_id=thread_id,
            cost=0.0,
            duration_ms=int(result.get("duration_ms") or 0),
            num_turns=1,
            is_error=False,
            error_type=None,
            tools_used=tools_used,
            model_usage=usage,
        )

    def _parse_codex_failed_result(
        self,
        result: Dict,
        messages: List[Dict],
    ) -> ClaudeResponse:
        """Parse Codex turn-failed event into unified error response."""
        thread_id = ""
        for msg in messages:
            if msg.get("type") == "thread.started":
                thread_id = str(msg.get("thread_id") or thread_id)

        return ClaudeResponse(
            content=self._extract_codex_failure_message(result, messages),
            session_id=thread_id,
            cost=0.0,
            duration_ms=int(result.get("duration_ms") or 0),
            num_turns=1,
            is_error=True,
            error_type="turn.failed",
            tools_used=[],
            model_usage=(
                result.get("usage") if isinstance(result.get("usage"), dict) else None
            ),
        )

    @staticmethod
    def _extract_codex_failure_message(
        result: Dict[str, Any], messages: List[Dict]
    ) -> str:
        """Extract readable failure text from Codex `turn.failed`/`error` events."""
        error_payload = result.get("error")
        if isinstance(error_payload, dict):
            for key in ("message", "detail", "details", "error"):
                value = str(error_payload.get(key) or "").strip()
                if value:
                    return value
            code = str(error_payload.get("code") or "").strip()
            if code:
                return code
        elif error_payload:
            value = str(error_payload).strip()
            if value:
                return value

        for key in ("message", "reason", "detail", "details"):
            value = str(result.get(key) or "").strip()
            if value:
                return value

        for msg in reversed(messages):
            if msg.get("type") != "error":
                continue
            for key in ("message", "error"):
                value = str(msg.get(key) or "").strip()
                if value:
                    return value
            nested = msg.get("result")
            if isinstance(nested, dict):
                nested_value = str(
                    nested.get("message") or nested.get("error") or ""
                ).strip()
                if nested_value:
                    return nested_value

        return "Codex request failed."

    @classmethod
    def _extract_local_command_output(cls, content: Any) -> str:
        """Extract <local-command-stdout> payload from user replay messages."""
        if isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
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

        extracted = "\n\n".join(match.group(2).strip() for match in matches).strip()
        return extracted

    async def kill_all_processes(self) -> None:
        """Kill all active processes."""
        logger.info(
            "Killing all active Claude processes", count=len(self.active_processes)
        )

        for process_id, process in self.active_processes.items():
            try:
                process.kill()
                await process.wait()
                logger.info("Killed Claude process", process_id=process_id)
            except Exception as e:
                logger.warning(
                    "Failed to kill process", process_id=process_id, error=str(e)
                )

        self.active_processes.clear()

    def get_active_process_count(self) -> int:
        """Get number of active processes."""
        return len(self.active_processes)
