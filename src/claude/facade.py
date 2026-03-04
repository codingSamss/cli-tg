"""High-level Claude Code integration facade.

Provides simple interface for bot handlers.
"""

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional

import structlog

from ..config.settings import Settings, resolve_cli_timeout_seconds
from .exceptions import ClaudeProcessError, ClaudeToolValidationError
from .integration import ClaudeProcessManager, ClaudeResponse
from .monitor import ToolMonitor
from .permissions import PermissionManager, PermissionRequestCallback
from .sdk_integration import ClaudeSDKManager, strip_thinking_blocks_from_session
from .session import SessionManager

if TYPE_CHECKING:
    from .session import ClaudeSession

logger = structlog.get_logger()


class ClaudeIntegration:
    """Main integration point for Claude Code."""

    def __init__(
        self,
        config: Settings,
        process_manager: Optional[ClaudeProcessManager] = None,
        sdk_manager: Optional[ClaudeSDKManager] = None,
        session_manager: Optional[SessionManager] = None,
        tool_monitor: Optional[ToolMonitor] = None,
        permission_manager: Optional["PermissionManager"] = None,
    ):
        """Initialize Claude integration facade."""
        self.config = config
        if session_manager is None:
            raise ValueError("session_manager is required")
        if tool_monitor is None:
            raise ValueError("tool_monitor is required")
        self.permission_manager = permission_manager or PermissionManager()

        # Initialize both managers for fallback capability
        self.sdk_manager = (
            sdk_manager or ClaudeSDKManager(config) if config.use_sdk else None
        )
        self.process_manager = process_manager or ClaudeProcessManager(config)

        # Use SDK by default if configured
        self.manager: ClaudeSDKManager | ClaudeProcessManager = (
            self.sdk_manager
            if config.use_sdk and self.sdk_manager is not None
            else self.process_manager
        )

        self.session_manager: SessionManager = session_manager
        self.tool_monitor: ToolMonitor = tool_monitor
        self._sdk_failed_count = 0  # Track SDK failures for adaptive fallback
        self._context_usage_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}

    @staticmethod
    def _is_invalid_claude_request_response(response: ClaudeResponse) -> bool:
        """Whether response text indicates upstream request-shape rejection."""
        content = str(getattr(response, "content", "") or "").lower()
        return "invalid claude code request" in content

    @staticmethod
    def _normalize_permission_suggestions(raw: Any) -> List[Dict[str, Any]]:
        """Normalize SDK permission suggestions into plain dict payloads."""
        if not isinstance(raw, list):
            return []

        normalized: List[Dict[str, Any]] = []
        for suggestion in raw:
            payload: Optional[Dict[str, Any]] = None
            if isinstance(suggestion, dict):
                payload = suggestion
            elif hasattr(suggestion, "to_dict") and callable(suggestion.to_dict):
                try:
                    candidate = suggestion.to_dict()
                    if isinstance(candidate, dict):
                        payload = candidate
                except Exception:
                    payload = None
            elif hasattr(suggestion, "__dict__"):
                try:
                    candidate = dict(vars(suggestion))
                    if isinstance(candidate, dict):
                        payload = candidate
                except Exception:
                    payload = None

            if payload:
                normalized.append(payload)

        return normalized

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[Any], Awaitable[None]]] = None,
        force_new_session: bool = False,
        permission_handler: Optional[PermissionRequestCallback] = None,
        model: Optional[str] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> ClaudeResponse:
        """Run Claude Code command with full integration."""
        logger.info(
            "Running Claude command",
            user_id=user_id,
            working_directory=str(working_directory),
            session_id=session_id,
            prompt_length=len(prompt),
        )

        # If no session_id provided, try to find an existing session for this
        # user+directory combination (auto-resume), unless force_new_session is set
        if not session_id and not force_new_session:
            existing_session = await self._find_resumable_session(
                user_id, working_directory
            )
            if existing_session:
                session_id = existing_session.session_id
                logger.info(
                    "Auto-resuming existing session for project",
                    session_id=session_id,
                    project_path=str(working_directory),
                    user_id=user_id,
                )

        # Get or create session
        session = await self.session_manager.get_or_create_session(
            user_id, working_directory, session_id
        )

        # Track streaming updates and validate tool calls
        tools_validated = True
        validation_errors: list[str] = []
        blocked_tools: set[str] = set()

        async def stream_handler(update: Any) -> None:
            nonlocal tools_validated

            # Validate tool calls
            tool_calls = getattr(update, "tool_calls", None)
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    tool_name = str(tool_call.get("name") or "").strip()
                    if not tool_name:
                        continue
                    valid, error = await self.tool_monitor.validate_tool_call(
                        tool_name,
                        tool_call.get("input", {}),
                        working_directory,
                        user_id,
                    )
                    error_text = str(error or "").strip()

                    if not valid:
                        tools_validated = False
                        if error_text:
                            validation_errors.append(error_text)

                        # Track blocked tools
                        if "Tool not allowed:" in error_text:
                            blocked_tools.add(tool_name)

                        logger.error(
                            "Tool validation failed",
                            tool_name=tool_name,
                            error=error_text,
                            user_id=user_id,
                        )

                        # For critical tools, we should fail fast
                        if tool_name in ["Task", "Read", "Write", "Edit"]:
                            # Create comprehensive error message
                            admin_instructions = self._get_admin_instructions(
                                list(blocked_tools)
                            )
                            error_msg = self._create_tool_error_message(
                                list(blocked_tools),
                                self.config.claude_allowed_tools or [],
                                admin_instructions,
                            )

                            raise ClaudeToolValidationError(
                                error_msg,
                                blocked_tools=list(blocked_tools),
                                allowed_tools=self.config.claude_allowed_tools or [],
                            )

            # Pass to caller's handler
            if on_stream:
                try:
                    await on_stream(update)
                except Exception as e:
                    logger.warning("Stream callback failed", error=str(e))

        # Build permission callback for SDK if handler provided
        permission_callback = None
        if permission_handler and self.config.use_sdk:
            permission_callback = self._build_permission_callback(
                user_id=user_id,
                session_id=session.session_id,
                send_buttons_callback=permission_handler,
            )

        # Execute command
        try:
            # Continue session if we have a real (non-temporary) session ID
            is_new = getattr(session, "is_new_session", False)
            has_real_session = not is_new and not session.session_id.startswith("temp_")
            should_continue = has_real_session

            # For new sessions, don't pass the temporary session_id to Claude Code
            claude_session_id = session.session_id if has_real_session else None

            try:
                response = await self._execute_with_fallback(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=claude_session_id,
                    continue_session=should_continue,
                    stream_callback=stream_handler,
                    permission_callback=permission_callback,
                    model=model,
                    images=images,
                )
            except asyncio.CancelledError:
                raise
            except Exception as resume_error:
                resume_error_str = str(resume_error).lower()
                is_signature_failure = should_continue and (
                    "invalid signature" in resume_error_str
                )
                is_session_gone = should_continue and (
                    "no conversation found" in resume_error_str
                )

                if is_signature_failure and claude_session_id:
                    # Try stripping thinking blocks and retrying same session
                    stripped = strip_thinking_blocks_from_session(
                        claude_session_id, str(working_directory)
                    )
                    if stripped:
                        try:
                            logger.info(
                                "Retrying resume after stripping thinking blocks",
                                session_id=claude_session_id,
                            )
                            response = await self._execute_with_fallback(
                                prompt=prompt,
                                working_directory=working_directory,
                                session_id=claude_session_id,
                                continue_session=True,
                                stream_callback=stream_handler,
                                permission_callback=permission_callback,
                                model=model,
                                images=images,
                            )
                        except Exception:
                            # Strip retry failed — fall through to new-session fallback
                            is_session_gone = True
                            stripped = False
                    if not stripped:
                        is_session_gone = True

                if is_session_gone:
                    session_source = getattr(session, "source", "bot")
                    if session_source == "desktop_adopted":
                        logger.error(
                            "Adopted desktop session no longer available",
                            session_id=claude_session_id,
                            error=str(resume_error),
                        )
                        await self.session_manager.remove_session(session.session_id)
                        raise ClaudeProcessError(
                            f"Desktop session {session.session_id[:8]}... "
                            f"is no longer available. The session may have "
                            f"expired or been deleted on the desktop. "
                            f"Please use /resume to select a different "
                            f"session, or start a new one."
                        )
                    else:
                        logger.warning(
                            "Session resume failed, starting fresh session",
                            failed_session_id=claude_session_id,
                            error=str(resume_error),
                        )
                        await self.session_manager.remove_session(session.session_id)
                        session = await self.session_manager.get_or_create_session(
                            user_id, working_directory
                        )
                        response = await self._execute_with_fallback(
                            prompt=prompt,
                            working_directory=working_directory,
                            session_id=None,
                            continue_session=False,
                            stream_callback=stream_handler,
                            permission_callback=permission_callback,
                            model=model,
                            images=images,
                        )
                elif not is_signature_failure:
                    raise

            if model and self._is_invalid_claude_request_response(response):
                logger.warning(
                    "Claude request rejected with explicit model; retrying without model override",
                    model=model,
                    session_id=claude_session_id,
                    continue_session=should_continue,
                )
                response = await self._execute_with_fallback(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=claude_session_id,
                    continue_session=should_continue,
                    stream_callback=stream_handler,
                    permission_callback=permission_callback,
                    model=None,
                    images=images,
                )

            # Check if tool validation failed
            if not tools_validated:
                logger.error(
                    "Command completed but tool validation failed",
                    validation_errors=validation_errors,
                )
                blocked_tool_list = self._extract_blocked_tools(validation_errors)
                has_primary_result = bool((response.content or "").strip())
                validation_notice = self._build_tool_validation_notice(
                    blocked_tools=blocked_tool_list,
                    validation_errors=validation_errors,
                    has_primary_result=has_primary_result,
                )

                if has_primary_result:
                    response.content = (
                        f"{response.content.rstrip()}\n\n{validation_notice}"
                    )
                    # Keep the main conclusion as primary content; render policy
                    # failure as supplementary warning.
                    response.is_error = False
                    response.error_type = None
                else:
                    # No usable result was produced; surface validation failure
                    # as the primary response.
                    response.content = validation_notice
                    response.is_error = True
                    response.error_type = "tool_validation_failed"

            # Update session (this may change the session_id for new sessions)
            old_session_id = session.session_id
            await self.session_manager.update_session(session.session_id, response)

            # For new sessions, get the updated session_id from the session manager
            if hasattr(session, "is_new_session") and response.session_id:
                # The session_id has been updated to Claude's session_id
                final_session_id = response.session_id
            else:
                # Use the original session_id for continuing sessions
                final_session_id = old_session_id

            # Ensure response has the correct session_id
            response.session_id = final_session_id

            logger.info(
                "Claude command completed",
                session_id=response.session_id,
                cost=response.cost,
                duration_ms=response.duration_ms,
                num_turns=response.num_turns,
                is_error=response.is_error,
            )

            return response

        except asyncio.CancelledError:
            logger.info("Claude command cancelled by user", user_id=user_id)
            raise
        except Exception as e:
            logger.error(
                "Claude command failed",
                error=str(e),
                user_id=user_id,
                session_id=session.session_id,
            )
            raise

    # Error types that are safe to retry with subprocess fallback.
    # Parameter/permission errors should NOT be retried — they indicate
    # code bugs or user decisions, not transient transport issues.
    _RETRYABLE_ERROR_TYPES = (
        "ClaudeTimeoutError",
        "CLIConnectionError",
        "CLIJSONDecodeError",
        "ClaudeParsingError",
    )

    async def _execute_with_fallback(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[Any], Awaitable[None]]] = None,
        permission_callback: Optional[Callable] = None,
        model: Optional[str] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> ClaudeResponse:
        """Execute command with SDK->subprocess fallback on retryable errors.

        Channel selection:
        - With permission_callback -> ClaudeSDKClient (supports can_use_tool)
        - Without permission_callback -> query() function (existing path)

        Fallback strategy:
        - With permission_callback: run SDK client mode only. On error, deny by default.
        - Without permission_callback: retryable SDK errors fallback to subprocess.
        - Non-retryable errors (ValueError, permission denied) -> raise immediately
        """
        has_images = bool(images)
        permission_gate_required = permission_callback is not None
        supports_image_subprocess = False
        supports_images_fn = getattr(
            self.process_manager, "supports_image_inputs", None
        )
        if callable(supports_images_fn):
            try:
                support_result = supports_images_fn(images)
                supports_image_subprocess = (
                    support_result if isinstance(support_result, bool) else False
                )
            except TypeError:
                support_result = supports_images_fn()
                supports_image_subprocess = (
                    support_result if isinstance(support_result, bool) else False
                )
            except Exception as e:
                logger.warning(
                    "Failed to detect subprocess image capability",
                    error=str(e),
                )

        # Image analysis requires a multimodal-capable backend. We support:
        # 1) SDK multimodal input (Claude SDK mode), or
        # 2) subprocess multimodal input (Codex CLI --image file path).
        if has_images and not (
            (self.config.use_sdk and self.sdk_manager) or supports_image_subprocess
        ):
            logger.warning(
                "Image request rejected because no multimodal backend is available",
                use_sdk=self.config.use_sdk,
                has_sdk_manager=bool(self.sdk_manager),
                supports_image_subprocess=supports_image_subprocess,
            )
            raise ClaudeProcessError(
                "Image analysis requires multimodal backend support. "
                "Set USE_SDK=true and restart the bot, "
                "or use a Codex CLI integration with local image file support."
            )

        # Try SDK first if configured
        if self.config.use_sdk and self.sdk_manager:
            try:
                # Permission-gated requests must use SDK client mode because
                # query() cannot wire can_use_tool callbacks.
                use_client_mode = permission_gate_required

                if use_client_mode:
                    # Client mode: supports can_use_tool permission callbacks
                    logger.debug("Attempting Claude SDK Client execution")
                    sdk_response = await self.sdk_manager.execute_with_client(
                        prompt=prompt,
                        working_directory=working_directory,
                        session_id=session_id,
                        continue_session=continue_session,
                        stream_callback=stream_callback,
                        permission_callback=permission_callback,
                        model=model,
                        images=images,
                    )
                else:
                    # query() mode: simpler and more robust when permission
                    # callbacks are not required.
                    logger.debug("Attempting Claude SDK query execution")
                    sdk_response = await self.sdk_manager.execute_command(
                        prompt=prompt,
                        working_directory=working_directory,
                        session_id=session_id,
                        continue_session=continue_session,
                        stream_callback=stream_callback,
                        permission_callback=None,
                        model=model,
                        images=images,
                    )
                response = self._coerce_response(sdk_response)
                # Reset failure count on success
                self._sdk_failed_count = 0
                return response

            except asyncio.CancelledError:
                logger.info("SDK execution cancelled by user")
                raise
            except Exception as e:
                error_str = str(e)
                error_type = type(e).__name__

                # Check if this error is retryable with subprocess fallback
                is_retryable = (
                    error_type in self._RETRYABLE_ERROR_TYPES
                    or "TaskGroup" in error_str
                    or "ExceptionGroup" in error_str
                )

                if is_retryable:
                    self._sdk_failed_count += 1

                    # Safety first: do not bypass permission approval by
                    # falling back to subprocess when callbacks are required.
                    if permission_gate_required:
                        logger.error(
                            "Claude SDK permission-gated request failed; denying fallback",
                            error=error_str,
                            error_type=error_type,
                            failure_count=self._sdk_failed_count,
                        )
                        raise ClaudeProcessError(
                            "Tool permission approval failed. "
                            "For safety, this request is denied by default. "
                            f"Please retry. Original error: {error_str}"
                        )

                    # Do not silently degrade multimodal image requests to text-only
                    # subprocess mode, otherwise the response may ignore images.
                    if has_images and not supports_image_subprocess:
                        logger.error(
                            "Claude SDK image request failed; skipping subprocess fallback",
                            error=error_str,
                            error_type=error_type,
                            failure_count=self._sdk_failed_count,
                        )
                        raise ClaudeProcessError(
                            "Image analysis failed in SDK mode and cannot fall back "
                            "to CLI text mode. Please retry. "
                            f"Original error: {error_str}"
                        )

                    logger.warning(
                        "Claude SDK failed with retryable error, "
                        "falling back to subprocess",
                        error=error_str,
                        error_type=error_type,
                        failure_count=self._sdk_failed_count,
                    )

                    try:
                        logger.info("Executing with subprocess fallback")
                        fallback_response = await self.process_manager.execute_command(
                            prompt=prompt,
                            working_directory=working_directory,
                            session_id=None,
                            continue_session=False,
                            stream_callback=stream_callback,
                            model=model,
                            images=images,
                        )
                        response = self._coerce_response(fallback_response)
                        logger.info("Subprocess fallback succeeded")
                        return response

                    except Exception as fallback_error:
                        logger.error(
                            "Both SDK and subprocess failed",
                            sdk_error=error_str,
                            subprocess_error=str(fallback_error),
                        )
                        raise e
                else:
                    # Non-retryable: raise immediately
                    logger.error(
                        "Claude SDK failed with non-retryable error",
                        error=error_str,
                        error_type=error_type,
                    )
                    raise
        else:
            # Use subprocess directly if SDK not configured
            logger.debug("Using subprocess execution (SDK disabled)")
            fallback_response = await self.process_manager.execute_command(
                prompt=prompt,
                working_directory=working_directory,
                session_id=session_id,
                continue_session=continue_session,
                stream_callback=stream_callback,
                model=model,
                images=images,
            )
            return self._coerce_response(fallback_response)

    async def _find_resumable_session(
        self,
        user_id: int,
        working_directory: Path,
    ) -> Optional["ClaudeSession"]:
        """Find the most recent resumable session for a user in a directory.

        Returns the session if one exists that is non-expired and has a real
        (non-temporary) session ID from Claude. Returns None otherwise.
        """
        sessions = await self.session_manager._get_user_sessions(user_id)

        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory
            and not s.session_id.startswith("temp_")
            and not s.is_expired(self.config.session_timeout_hours)
        ]

        if not matching_sessions:
            return None

        return max(matching_sessions, key=lambda s: s.last_used)

    async def continue_session(
        self,
        user_id: int,
        working_directory: Path,
        prompt: Optional[str] = None,
        on_stream: Optional[Callable[[Any], Awaitable[None]]] = None,
        permission_handler: Optional[PermissionRequestCallback] = None,
    ) -> Optional[ClaudeResponse]:
        """Continue the most recent session."""
        logger.info(
            "Continuing session",
            user_id=user_id,
            working_directory=str(working_directory),
            has_prompt=bool(prompt),
        )

        # Get user's sessions
        sessions = await self.session_manager._get_user_sessions(user_id)

        # Find most recent session in this directory (exclude temporary sessions)
        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory
            and not s.session_id.startswith("temp_")
        ]

        if not matching_sessions:
            logger.info("No matching sessions found", user_id=user_id)
            return None

        # Get most recent
        latest_session = max(matching_sessions, key=lambda s: s.last_used)

        # Continue session with default prompt if none provided
        # Claude CLI requires a prompt, so we use a placeholder
        return await self.run_command(
            prompt=prompt or "Please continue where we left off",
            working_directory=working_directory,
            user_id=user_id,
            session_id=latest_session.session_id,
            on_stream=on_stream,
            permission_handler=permission_handler,
        )

    async def get_session_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session information."""
        return await self.session_manager.get_session_info(session_id)

    async def get_precise_context_usage(
        self,
        session_id: str,
        working_directory: Path,
        model: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Probe precise context usage via CLI status/context command with short cache."""
        if not session_id:
            return None

        cli_kind = "claude"
        process_manager = getattr(self, "process_manager", None)
        resolve_cli_path = getattr(process_manager, "_resolve_cli_path", None)
        detect_cli_kind = getattr(process_manager, "_detect_cli_kind", None)
        if callable(resolve_cli_path) and callable(detect_cli_kind):
            try:
                cli_kind = detect_cli_kind(resolve_cli_path())
            except Exception:
                cli_kind = "claude"
        probe_prompt = "/status" if cli_kind == "codex" else "/context"
        cache_key = f"{cli_kind}:{session_id}"

        now = asyncio.get_event_loop().time()
        ttl_seconds = max(
            int(getattr(self.config, "status_context_probe_ttl_seconds", 0) or 0), 0
        )

        cached = self._context_usage_cache.get(cache_key)
        if (
            not force_refresh
            and ttl_seconds > 0
            and cached
            and now - cached[0] <= ttl_seconds
        ):
            payload = dict(cached[1])
            payload["cached"] = True
            return payload

        probe_timeout_cfg = max(
            int(getattr(self.config, "status_context_probe_timeout_seconds", 45) or 45),
            1,
        )
        probe_timeout = max(
            1, min(resolve_cli_timeout_seconds(self.config), probe_timeout_cfg)
        )
        probe_runners: List[tuple[str, Callable[[], Any]]] = []
        process_manager = self.process_manager
        if process_manager:
            probe_runners.append(
                (
                    "subprocess",
                    lambda: process_manager.execute_command(
                        prompt=probe_prompt,
                        working_directory=working_directory,
                        session_id=session_id,
                        continue_session=True,
                        model=model,
                    ),
                )
            )
        sdk_manager = self.sdk_manager
        if self.config.use_sdk and sdk_manager:
            probe_runners.append(
                (
                    "sdk",
                    lambda: sdk_manager.execute_command(
                        prompt=probe_prompt,
                        working_directory=working_directory,
                        session_id=session_id,
                        continue_session=True,
                        model=model,
                    ),
                )
            )
        if not probe_runners:
            return None

        parsed: Optional[Dict[str, Any]] = None
        for probe_source, runner in probe_runners:
            try:
                response = await asyncio.wait_for(
                    runner(),
                    timeout=probe_timeout,
                )
            except asyncio.TimeoutError:
                logger.info(
                    "Precise context probe timed out",
                    session_id=session_id,
                    timeout_seconds=probe_timeout,
                    probe_source=probe_source,
                )
                continue
            except Exception as e:
                logger.info(
                    "Failed to probe precise context usage",
                    session_id=session_id,
                    error=str(e),
                    probe_source=probe_source,
                )
                continue

            if response.is_error:
                logger.info(
                    "Context probe returned error response",
                    session_id=session_id,
                    error_type=response.error_type,
                    content_preview=(response.content or "")[:240],
                    probe_source=probe_source,
                )
                continue

            parsed = self._parse_context_usage_text(response.content or "")
            if parsed:
                break

            logger.info(
                "Unable to parse context usage output",
                session_id=session_id,
                probe_prompt=probe_prompt,
                content_preview=(response.content or "")[:240],
                probe_source=probe_source,
            )

        if not parsed:
            return None

        payload = {
            **parsed,
            "session_id": session_id,
            "cached": False,
            "probe_command": probe_prompt,
        }
        if ttl_seconds > 0:
            self._context_usage_cache[cache_key] = (now, dict(payload))
        else:
            self._context_usage_cache.pop(cache_key, None)
        return payload

    async def get_user_sessions(self, user_id: int) -> List[Dict[str, Any]]:
        """Get all sessions for a user."""
        sessions = await self.session_manager._get_user_sessions(user_id)
        return [
            {
                "session_id": s.session_id,
                "project_path": str(s.project_path),
                "created_at": s.created_at.isoformat(),
                "last_used": s.last_used.isoformat(),
                "total_cost": s.total_cost,
                "message_count": s.message_count,
                "tools_used": s.tools_used,
                "expired": s.is_expired(self.config.session_timeout_hours),
            }
            for s in sessions
        ]

    @classmethod
    def _parse_context_usage_text(cls, text: str) -> Optional[Dict[str, Any]]:
        """Parse used/total token usage from /context output text."""
        if not text:
            return None

        numeric = r"\d[\d,._]*(?:\.\d+)?\s*[kKmMbB]?"
        pair_pattern = re.compile(rf"(?P<used>{numeric})\s*/\s*(?P<total>{numeric})")
        percent_pattern = re.compile(r"(?P<pct>\d{1,3}(?:\.\d+)?)\s*%")
        used_pattern = re.compile(
            rf"(?:used|usage|已使用|占用)\D{{0,16}}(?P<used>{numeric})",
            re.IGNORECASE,
        )
        total_pattern = re.compile(
            rf"(?:total|window|capacity|max(?:imum)?|总量|上下文窗口|窗口)\D{{0,20}}(?P<total>{numeric})",
            re.IGNORECASE,
        )
        remaining_pattern = re.compile(
            rf"(?:remaining|left|available|剩余)\D{{0,16}}(?P<remaining>{numeric})",
            re.IGNORECASE,
        )
        keyword_pattern = re.compile(
            r"(?:context|token|window|usage|上下文|令牌|剩余|已使用)",
            re.IGNORECASE,
        )

        normalized = text.replace("`", " ").replace("\r", "\n")
        lines = [line.strip() for line in normalized.split("\n") if line.strip()]
        candidates = [line for line in lines if keyword_pattern.search(line)]
        if normalized not in candidates:
            candidates.append(normalized)

        for candidate in candidates:
            for match in pair_pattern.finditer(candidate):
                used_tokens = cls._parse_token_number(match.group("used"))
                total_tokens = cls._parse_token_number(match.group("total"))
                payload = cls._build_context_usage_payload(
                    text=text,
                    candidate=candidate,
                    used_tokens=used_tokens,
                    total_tokens=total_tokens,
                    percent_pattern=percent_pattern,
                    remaining_pattern=remaining_pattern,
                )
                if payload:
                    return payload

            used_match = used_pattern.search(candidate)
            total_match = total_pattern.search(candidate)
            remaining_match = remaining_pattern.search(candidate)
            pct_match = percent_pattern.search(candidate)

            used_tokens = (
                cls._parse_token_number(used_match.group("used"))
                if used_match
                else None
            )
            total_tokens = (
                cls._parse_token_number(total_match.group("total"))
                if total_match
                else None
            )
            remaining_tokens = (
                cls._parse_token_number(remaining_match.group("remaining"))
                if remaining_match
                else None
            )
            percent = float(pct_match.group("pct")) if pct_match else None

            if (
                total_tokens is None
                and used_tokens is not None
                and remaining_tokens is not None
            ):
                total_tokens = used_tokens + remaining_tokens

            if (
                used_tokens is None
                and total_tokens is not None
                and remaining_tokens is not None
            ):
                used_tokens = max(total_tokens - remaining_tokens, 0)

            if (
                used_tokens is None
                and total_tokens is not None
                and percent is not None
                and 0 < percent < 100
            ):
                used_tokens = int(round(total_tokens * percent / 100))

            if (
                total_tokens is None
                and used_tokens is not None
                and percent is not None
                and 0 < percent <= 100
            ):
                total_tokens = int(round(used_tokens / (percent / 100)))

            payload = cls._build_context_usage_payload(
                text=text,
                candidate=candidate,
                used_tokens=used_tokens,
                total_tokens=total_tokens,
                percent_pattern=percent_pattern,
                remaining_pattern=remaining_pattern,
                remaining_tokens_override=remaining_tokens,
            )
            if payload:
                return payload

        return None

    @classmethod
    def _build_context_usage_payload(
        cls,
        *,
        text: str,
        candidate: str,
        used_tokens: Optional[int],
        total_tokens: Optional[int],
        percent_pattern: re.Pattern[str],
        remaining_pattern: re.Pattern[str],
        remaining_tokens_override: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Build normalized context-usage payload if values are valid."""
        if (
            used_tokens is None
            or total_tokens is None
            or used_tokens < 0
            or total_tokens <= 0
        ):
            return None

        pct_match = percent_pattern.search(candidate)
        used_percent = (
            float(pct_match.group("pct"))
            if pct_match
            else used_tokens / total_tokens * 100
        )

        remaining_tokens = remaining_tokens_override
        if remaining_tokens is None:
            remaining_match = remaining_pattern.search(candidate)
            remaining_tokens = (
                cls._parse_token_number(remaining_match.group("remaining"))
                if remaining_match
                else None
            )
        if remaining_tokens is None:
            remaining_tokens = max(total_tokens - used_tokens, 0)

        return {
            "used_tokens": used_tokens,
            "total_tokens": total_tokens,
            "remaining_tokens": remaining_tokens,
            "used_percent": used_percent,
            "raw_text": text,
        }

    @staticmethod
    def _parse_token_number(value: Optional[str]) -> Optional[int]:
        """Parse token count strings like '55,000', '55k', '1.2m'."""
        if not value:
            return None

        normalized = re.sub(r"\s+", "", value.strip().lower())
        if not normalized:
            return None

        multiplier = 1
        if normalized.endswith("k"):
            multiplier = 1_000
            normalized = normalized[:-1]
        elif normalized.endswith("m"):
            multiplier = 1_000_000
            normalized = normalized[:-1]
        elif normalized.endswith("b"):
            multiplier = 1_000_000_000
            normalized = normalized[:-1]

        normalized = normalized.replace(",", "").replace("_", "")
        try:
            parsed = float(normalized)
        except ValueError:
            return None

        if parsed < 0:
            return None
        return int(round(parsed * multiplier))

    async def cleanup_expired_sessions(self) -> int:
        """Clean up expired sessions."""
        return await self.session_manager.cleanup_expired_sessions()

    async def get_tool_stats(self) -> Dict[str, Any]:
        """Get tool usage statistics."""
        return self.tool_monitor.get_tool_stats()

    async def get_user_summary(self, user_id: int) -> Dict[str, Any]:
        """Get comprehensive user summary."""
        session_summary = await self.session_manager.get_user_session_summary(user_id)
        tool_usage = self.tool_monitor.get_user_tool_usage(user_id)

        return {
            "user_id": user_id,
            **session_summary,
            **tool_usage,
        }

    async def shutdown(self) -> None:
        """Shutdown integration and cleanup resources."""
        logger.info("Shutting down Claude integration")

        # Kill any active processes
        await self.manager.kill_all_processes()

        # Clean up expired sessions
        await self.cleanup_expired_sessions()

        logger.info("Claude integration shutdown complete")

    @staticmethod
    def _coerce_response(response: Any) -> ClaudeResponse:
        """Normalize SDK/subprocess responses to facade ClaudeResponse type."""
        if isinstance(response, ClaudeResponse):
            return response

        tools_used_raw = getattr(response, "tools_used", None)
        tools_used = tools_used_raw if isinstance(tools_used_raw, list) else []
        model_usage_raw = getattr(response, "model_usage", None)
        model_usage = model_usage_raw if isinstance(model_usage_raw, dict) else None
        error_type_raw = getattr(response, "error_type", None)
        error_type = str(error_type_raw).strip() if error_type_raw else None

        return ClaudeResponse(
            content=str(getattr(response, "content", "") or ""),
            session_id=str(getattr(response, "session_id", "") or ""),
            cost=float(getattr(response, "cost", 0.0) or 0.0),
            duration_ms=int(getattr(response, "duration_ms", 0) or 0),
            num_turns=int(getattr(response, "num_turns", 0) or 0),
            is_error=bool(getattr(response, "is_error", False)),
            error_type=error_type,
            tools_used=tools_used,
            model_usage=model_usage,
        )

    def _build_permission_callback(
        self,
        user_id: int,
        session_id: str,
        send_buttons_callback: PermissionRequestCallback,
    ) -> Callable:
        """Build a can_use_tool callback for the SDK using PermissionManager.

        Tools in the allowed_tools whitelist are auto-approved.
        All other tools are routed to Telegram for user approval.
        """
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        permission_manager: PermissionManager = self.permission_manager
        allowed_tools = self.config.claude_allowed_tools or []

        async def can_use_tool(
            tool_name: str,
            tool_input: dict,
            context: Any,
        ) -> Any:
            # Auto-approve tools in the whitelist
            if tool_name in allowed_tools:
                return PermissionResultAllow()

            permission_suggestions = self._normalize_permission_suggestions(
                getattr(context, "suggestions", None)
            )

            # All other tools go through Telegram approval
            allowed = await permission_manager.request_permission(
                tool_name=tool_name,
                tool_input=tool_input,
                user_id=user_id,
                session_id=session_id,
                send_buttons_callback=send_buttons_callback,
                permission_suggestions=permission_suggestions or None,
            )
            if allowed:
                return PermissionResultAllow()
            else:
                return PermissionResultDeny(
                    message="User denied permission via Telegram"
                )

        return can_use_tool

    def _get_admin_instructions(self, blocked_tools: List[str]) -> str:
        """Generate admin instructions for enabling blocked tools."""
        instructions = []

        # Check if settings file exists
        settings_file = Path(".env")

        if blocked_tools:
            # Get current allowed tools and create merged list without duplicates
            current_tools = [
                "Read",
                "Write",
                "Edit",
                "Bash",
                "Glob",
                "Grep",
                "LS",
                "Task",
                "MultiEdit",
                "NotebookRead",
                "NotebookEdit",
                "WebFetch",
                "TodoRead",
                "TodoWrite",
                "WebSearch",
            ]
            merged_tools = list(
                dict.fromkeys(current_tools + blocked_tools)
            )  # Remove duplicates while preserving order
            merged_tools_str = ",".join(merged_tools)
            merged_tools_py = ", ".join(f'"{tool}"' for tool in merged_tools)

            instructions.append("**For Administrators:**")
            instructions.append("")

            if settings_file.exists():
                instructions.append(
                    "To enable these tools, add them to your `.env` file:"
                )
                instructions.append("```")
                instructions.append(f'CLAUDE_ALLOWED_TOOLS="{merged_tools_str}"')
                instructions.append("```")
            else:
                instructions.append("To enable these tools:")
                instructions.append("1. Create a `.env` file in your project root")
                instructions.append("2. Add the following line:")
                instructions.append("```")
                instructions.append(f'CLAUDE_ALLOWED_TOOLS="{merged_tools_str}"')
                instructions.append("```")

            instructions.append("")
            instructions.append("Or modify the default in `src/config/settings.py`:")
            instructions.append("```python")
            instructions.append("claude_allowed_tools: Optional[List[str]] = Field(")
            instructions.append(f"    default=[{merged_tools_py}],")
            instructions.append('    description="List of allowed Claude tools",')
            instructions.append(")")
            instructions.append("```")

        return "\n".join(instructions)

    def _create_tool_error_message(
        self,
        blocked_tools: List[str],
        allowed_tools: List[str],
        admin_instructions: str,
    ) -> str:
        """Create a comprehensive error message for tool validation failures."""
        tool_list = ", ".join(f"`{tool}`" for tool in blocked_tools)
        allowed_list = (
            ", ".join(f"`{tool}`" for tool in allowed_tools)
            if allowed_tools
            else "None"
        )

        message = [
            "🚫 **Tool Access Blocked**",
            "",
            "Claude tried to use tools that are not currently allowed:",
            f"{tool_list}",
            "",
            "**Why this happened:**",
            "• Claude needs these tools to complete your request",
            "• These tools are not in the allowed tools list",
            "• This is a security feature to control what Claude can do",
            "",
            "**What you can do:**",
            "• Contact the administrator to request access to these tools",
            "• Try rephrasing your request to use different approaches",
            "• Use simpler requests that don't require these tools",
            "",
            "**Currently allowed tools:**",
            f"{allowed_list}",
            "",
            admin_instructions,
        ]

        return "\n".join(message)

    @staticmethod
    def _escape_markdown_text(value: str) -> str:
        """Escape Telegram legacy Markdown control characters."""
        text = str(value)
        for ch in ("\\", "`", "*", "_", "["):
            text = text.replace(ch, f"\\{ch}")
        return text

    @classmethod
    def _extract_blocked_tools(cls, validation_errors: List[str]) -> List[str]:
        """Extract blocked tool names from validation error messages."""
        blocked: list[str] = []
        for error in validation_errors:
            marker = "Tool not allowed:"
            if marker not in error:
                continue
            tool_name = error.split(marker, 1)[1].strip()
            if not tool_name:
                continue
            if tool_name not in blocked:
                blocked.append(tool_name)
        return blocked

    def _build_tool_validation_notice(
        self,
        blocked_tools: List[str],
        validation_errors: List[str],
        *,
        has_primary_result: bool,
    ) -> str:
        """Build user-facing message for non-fatal/fatal tool validation failures."""
        allowed_tools = self.config.claude_allowed_tools or []
        allowed_tools_preview = ", ".join(
            f"`{self._escape_markdown_text(tool)}`" for tool in allowed_tools[:12]
        )
        if len(allowed_tools) > 12:
            allowed_tools_preview += ", ..."

        blocked_preview = ", ".join(
            f"`{self._escape_markdown_text(tool)}`" for tool in blocked_tools[:8]
        )
        if len(blocked_tools) > 8:
            blocked_preview += ", ..."

        if has_primary_result:
            lines = [
                "⚠️ **Tool Validation Notice**",
                "",
                "Some tool calls were blocked by security policy.",
                "The main result above is preserved.",
            ]
        else:
            lines = [
                "🚫 **Tool Validation Failed**",
                "",
                "The request could not be completed because required tools were blocked by security policy.",
            ]

        if blocked_preview:
            lines.append(f"Blocked tools: {blocked_preview}")
        elif validation_errors:
            lines.append("Blocked tools: unavailable")

        lines.extend(
            [
                "",
                "**What you can do:**",
                "• Contact the administrator to request access",
                "• Rephrase your request to avoid restricted tools",
                "• Check available tools with `/context`",
            ]
        )

        if allowed_tools_preview:
            lines.extend(
                [
                    "",
                    "**Currently allowed tools:**",
                    allowed_tools_preview,
                ]
            )

        return "\n".join(lines)
