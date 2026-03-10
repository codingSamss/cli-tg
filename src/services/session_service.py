"""Session application service."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

import structlog

from ..bot.utils.status_usage import (
    build_model_usage_status_lines,
    build_precise_context_status_lines,
)
from ..storage.facade import Storage
from .event_service import EventService

logger = structlog.get_logger()


@dataclass
class ContextStatusSnapshot:
    """Structured snapshot for /context rendering."""

    lines: List[str]
    precise_context: Optional[Dict[str, Any]] = None
    session_info: Optional[Dict[str, Any]] = None
    resumable_payload: Optional[Dict[str, Any]] = None


class SessionService:
    """Provide session-level reusable business capabilities."""

    _codex_snapshot_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
    _codex_snapshot_ttl_seconds = 5

    def __init__(self, storage: Storage, event_service: EventService):
        self.storage = storage
        self.event_service = event_service

    @staticmethod
    def _resolve_cli_kind(claude_integration: Any) -> str:
        """Best-effort resolve current CLI kind (`claude`/`codex`)."""
        process_manager = getattr(claude_integration, "process_manager", None)
        resolve_cli_path = getattr(process_manager, "_resolve_cli_path", None)
        detect_cli_kind = getattr(process_manager, "_detect_cli_kind", None)
        if callable(resolve_cli_path) and callable(detect_cli_kind):
            try:
                detected = (
                    str(detect_cli_kind(resolve_cli_path()) or "").strip().lower()
                )
                if detected in {"claude", "codex"}:
                    return detected
            except Exception:
                pass
        return "claude"

    @staticmethod
    def _is_claude_model_name(value: Any) -> bool:
        """Whether model name belongs to Claude aliases/families."""
        normalized = str(value or "").strip().lower()
        if not normalized:
            return False
        if normalized in {"sonnet", "opus", "haiku"}:
            return True
        return any(
            token in normalized for token in ("claude", "sonnet", "opus", "haiku")
        )

    @staticmethod
    def _usage_has_context_window(model_usage: Dict[str, Any]) -> bool:
        """Whether usage payload already contains explicit context-window metadata."""
        if not isinstance(model_usage, dict):
            return False

        if any(key in model_usage for key in {"contextWindow", "context_window"}):
            return True

        for usage in model_usage.values():
            if isinstance(usage, dict) and any(
                key in usage for key in {"contextWindow", "context_window"}
            ):
                return True
        return False

    @staticmethod
    def _extract_resolved_model_from_usage(
        model_usage: Dict[str, Any],
    ) -> Optional[str]:
        """Best-effort extract resolved model name from model_usage payload."""
        if not isinstance(model_usage, dict):
            return None

        flat = str(
            model_usage.get("resolvedModel") or model_usage.get("resolved_model") or ""
        ).strip()
        if flat:
            return flat

        for model_name, usage in model_usage.items():
            if not isinstance(usage, dict):
                continue

            resolved = str(
                usage.get("resolvedModel") or usage.get("resolved_model") or ""
            ).strip()
            if resolved:
                return resolved

            name_text = str(model_name or "").strip()
            if name_text and name_text.lower() not in {"sdk", "default", "current"}:
                return name_text

        return None

    @staticmethod
    def _format_reasoning_effort(value: Any) -> str:
        """Render reasoning effort in a compact user-facing format."""
        raw = str(value or "").strip()
        if not raw:
            return ""
        normalized = raw.lower().replace("-", "").replace("_", "")
        mapping = {
            "low": "Low",
            "medium": "Medium",
            "high": "High",
            "xhigh": "X High",
        }
        return mapping.get(normalized, raw.title())

    @staticmethod
    def _resolve_display_model(
        *,
        current_model: Optional[str],
        session_info: Optional[Dict[str, Any]],
        precise_context: Optional[Dict[str, Any]],
        prefer_runtime_model: bool = False,
    ) -> str:
        """Resolve user-facing model name for /context status line."""
        current = str(current_model or "").strip()
        current_explicit = current and current.lower() not in {"default", "current"}
        runtime_model = ""

        if isinstance(precise_context, dict):
            precise_model = str(
                precise_context.get("resolved_model")
                or precise_context.get("resolvedModel")
                or ""
            ).strip()
            if precise_model:
                runtime_model = precise_model

        if not runtime_model and isinstance(session_info, dict):
            usage_model = SessionService._extract_resolved_model_from_usage(
                session_info.get("model_usage") or {}
            )
            if usage_model:
                runtime_model = usage_model

        if prefer_runtime_model:
            model = runtime_model or (current if current_explicit else "")
        else:
            model = (current if current_explicit else "") or runtime_model
        if not model:
            model = current or "default"

        effort = ""
        if isinstance(precise_context, dict):
            effort = SessionService._format_reasoning_effort(
                precise_context.get("reasoning_effort")
                or precise_context.get("reasoningEffort")
                or precise_context.get("effort")
            )
        if not effort and isinstance(session_info, dict):
            effort = SessionService._format_reasoning_effort(
                session_info.get("reasoning_effort")
                or session_info.get("reasoningEffort")
            )
        if effort and model.lower() not in {"default", "current"}:
            return f"{model} ({effort})"
        return model

    @staticmethod
    def _probe_codex_session_snapshot(session_id: str) -> Optional[Dict[str, Any]]:
        """Read latest Codex local session snapshot for model and context usage."""
        sid = str(session_id or "").strip()
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

        now = time.monotonic()
        cache_entry = SessionService._codex_snapshot_cache.get(sid)
        if cache_entry:
            cached_at, cached_snapshot = cache_entry
            if now - cached_at <= SessionService._codex_snapshot_ttl_seconds:
                return dict(cached_snapshot)

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
        if not lines:
            return None

        resolved_model: Optional[str] = None
        reasoning_effort: Optional[str] = None
        usage_payload: Optional[Dict[str, Any]] = None
        rate_limits_payload: Optional[Dict[str, Any]] = None
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            record_type = str(record.get("type") or "").strip()
            payload = record.get("payload")

            if (
                resolved_model is None
                and record_type == "turn_context"
                and isinstance(payload, dict)
            ):
                model = str(payload.get("model") or "").strip()
                if model:
                    resolved_model = model
                if reasoning_effort is None:
                    effort = str(
                        payload.get("effort") or payload.get("reasoning_effort") or ""
                    ).strip()
                    if not effort:
                        collaboration_mode = payload.get("collaboration_mode")
                        settings = (
                            collaboration_mode.get("settings")
                            if isinstance(collaboration_mode, dict)
                            else None
                        )
                        if isinstance(settings, dict):
                            effort = str(settings.get("reasoning_effort") or "").strip()
                    if effort:
                        reasoning_effort = effort

            if record_type != "event_msg" or not isinstance(payload, dict):
                continue
            if payload.get("type") != "token_count":
                continue

            if rate_limits_payload is None:
                parsed_rate_limits = SessionService._parse_codex_rate_limits(
                    payload.get("rate_limits"),
                    event_timestamp=str(record.get("timestamp") or "").strip(),
                )
                if parsed_rate_limits:
                    rate_limits_payload = parsed_rate_limits

            if usage_payload is not None:
                continue

            info_payload = payload.get("info")
            info: Dict[str, Any] = (
                info_payload if isinstance(info_payload, dict) else {}
            )
            total_usage_payload = info.get("total_token_usage")
            total_usage: Dict[str, Any] = (
                total_usage_payload if isinstance(total_usage_payload, dict) else {}
            )
            last_usage_payload = info.get("last_token_usage")
            last_usage: Dict[str, Any] = (
                last_usage_payload if isinstance(last_usage_payload, dict) else {}
            )
            context_window = int(info.get("model_context_window", 0) or 0)
            aggregate_tokens = int(
                total_usage.get("total_tokens")
                or (
                    int(total_usage.get("input_tokens", 0) or 0)
                    + int(total_usage.get("cached_input_tokens", 0) or 0)
                    + int(total_usage.get("output_tokens", 0) or 0)
                )
            )
            last_tokens = int(
                last_usage.get("total_tokens")
                or (
                    int(last_usage.get("input_tokens", 0) or 0)
                    + int(last_usage.get("cached_input_tokens", 0) or 0)
                    + int(last_usage.get("output_tokens", 0) or 0)
                )
            )
            if context_window <= 0:
                continue

            upper_bound = int(context_window * 1.2)
            used_tokens = 0
            if 0 < last_tokens <= upper_bound:
                # Codex token_count.total_token_usage is often cumulative;
                # prefer the latest-turn footprint to approximate active context.
                used_tokens = last_tokens
            elif 0 < aggregate_tokens <= upper_bound:
                used_tokens = aggregate_tokens

            if used_tokens <= 0:
                continue
            if used_tokens > context_window:
                used_tokens = context_window

            usage_payload = {
                "used_tokens": used_tokens,
                "total_tokens": context_window,
                "remaining_tokens": max(context_window - used_tokens, 0),
                "used_percent": used_tokens / context_window * 100,
                "probe_command": "/status",
                "cached": False,
            }

        if (
            usage_payload is None
            and resolved_model is None
            and rate_limits_payload is None
            and reasoning_effort is None
        ):
            return None

        snapshot = dict(usage_payload or {})
        if resolved_model:
            snapshot["resolved_model"] = resolved_model
        if reasoning_effort:
            snapshot["reasoning_effort"] = reasoning_effort
        if rate_limits_payload:
            snapshot["rate_limits"] = rate_limits_payload
        SessionService._codex_snapshot_cache[sid] = (now, dict(snapshot))
        return snapshot

    @classmethod
    def get_cached_codex_snapshot(cls, session_id: str) -> Optional[Dict[str, Any]]:
        """Return cached Codex snapshot if it is still fresh."""
        sid = str(session_id or "").strip()
        if not sid:
            return None
        entry = cls._codex_snapshot_cache.get(sid)
        if not entry:
            return None
        cached_at, snapshot = entry
        if time.monotonic() - cached_at > cls._codex_snapshot_ttl_seconds:
            cls._codex_snapshot_cache.pop(sid, None)
            return None
        return dict(snapshot)

    @classmethod
    def resolve_codex_snapshot(cls, session_id: str) -> Optional[Dict[str, Any]]:
        """Return a recent Codex snapshot, probing local session data on cache miss."""
        sid = str(session_id or "").strip()
        if not sid:
            return None

        cached_snapshot = cls.get_cached_codex_snapshot(sid)
        if isinstance(cached_snapshot, dict):
            return cached_snapshot

        return cls._probe_codex_session_snapshot(sid)

    @staticmethod
    def _parse_codex_rate_limits(
        payload: Any,
        *,
        event_timestamp: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Normalize Codex token_count.rate_limits payload."""
        if not isinstance(payload, dict):
            return None

        def _normalize_entry(entry: Any) -> Optional[Dict[str, Any]]:
            if not isinstance(entry, dict):
                return None
            used_percent_raw = entry.get("used_percent")
            window_minutes_raw = entry.get("window_minutes")
            resets_at_raw = entry.get("resets_at")
            if not isinstance(used_percent_raw, (int, float, str)):
                return None
            if not isinstance(window_minutes_raw, (int, float, str)):
                return None
            try:
                used_percent = float(used_percent_raw)
            except (TypeError, ValueError):
                return None
            try:
                window_minutes = int(window_minutes_raw)
            except (TypeError, ValueError):
                return None
            if window_minutes <= 0:
                return None

            normalized: Dict[str, Any] = {
                "used_percent": used_percent,
                "window_minutes": window_minutes,
            }
            if isinstance(resets_at_raw, (int, float, str)):
                try:
                    resets_at = int(resets_at_raw)
                    if resets_at > 0:
                        normalized["resets_at"] = resets_at
                except (TypeError, ValueError):
                    pass
            return normalized

        primary = _normalize_entry(payload.get("primary"))
        secondary = _normalize_entry(payload.get("secondary"))
        if primary is None and secondary is None:
            return None

        result: Dict[str, Any] = {}
        if primary is not None:
            result["primary"] = primary
        if secondary is not None:
            result["secondary"] = secondary

        timestamp_text = event_timestamp.strip()
        if timestamp_text:
            try:
                parsed_at = datetime.fromisoformat(
                    timestamp_text.replace("Z", "+00:00")
                )
                result["updated_at"] = (
                    parsed_at.astimezone(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except ValueError:
                result["updated_at"] = timestamp_text

        return result

    async def get_user_session_summary(self, user_id: int) -> Dict[str, Any]:
        """Return aggregated session summary for one user."""
        return await self.storage.get_user_session_summary(user_id)

    @staticmethod
    async def build_scope_context_snapshot(
        *,
        user_id: int,
        scope_state: Mapping[str, Any],
        approved_directory: Path,
        claude_integration: Any,
        session_service: Any = None,
        include_resumable: bool = True,
        include_event_summary: bool = True,
        allow_precise_context_probe: bool = True,
    ) -> ContextStatusSnapshot:
        """Build context snapshot directly from scoped state."""
        current_dir = scope_state.get("current_directory", approved_directory)
        current_model = scope_state.get("claude_model")
        session_id = scope_state.get("claude_session_id")
        if SessionService._resolve_cli_kind(claude_integration) == "claude":
            if current_model and not SessionService._is_claude_model_name(
                current_model
            ):
                current_model = None
        event_provider = None
        if include_event_summary and session_service:
            candidate = getattr(session_service, "get_context_event_lines", None)
            if callable(candidate):
                event_provider = candidate

        return await SessionService.build_context_snapshot(
            user_id=user_id,
            session_id=session_id,
            current_dir=current_dir,
            approved_directory=approved_directory,
            current_model=current_model,
            claude_integration=claude_integration,
            include_resumable=include_resumable,
            event_lines_provider=event_provider,
            allow_precise_context_probe=allow_precise_context_probe,
        )

    @staticmethod
    async def build_context_snapshot(
        *,
        user_id: int,
        session_id: Optional[str],
        current_dir: Path,
        approved_directory: Path,
        current_model: Optional[str],
        claude_integration: Any,
        include_resumable: bool = True,
        event_lines_provider: Optional[Callable[[str], Awaitable[List[str]]]] = None,
        allow_precise_context_probe: bool = True,
    ) -> ContextStatusSnapshot:
        """Build a unified /context snapshot used by command and callback handlers."""
        try:
            relative_path = current_dir.relative_to(approved_directory)
        except ValueError:
            relative_path = current_dir

        lines = [
            "**Session Context**\n",
            f"Directory: `{relative_path}/`",
            f"Model: `{current_model or 'default'}`",
        ]
        model_line_idx = 2
        cli_kind = SessionService._resolve_cli_kind(claude_integration)
        precise_context = None
        session_info = None
        resumable_payload = None

        if session_id:
            lines.append(f"Session: `{session_id[:8]}...`")
            if claude_integration:
                if allow_precise_context_probe:
                    probe_kwargs: Dict[str, Any] = {
                        "session_id": session_id,
                        "working_directory": current_dir,
                        "model": current_model,
                    }
                    # Codex status requires live probe for strict consistency.
                    if cli_kind == "codex":
                        probe_kwargs["force_refresh"] = True
                    try:
                        precise_context = (
                            await claude_integration.get_precise_context_usage(
                                **probe_kwargs
                            )
                        )
                    except TypeError:
                        # Backward compatibility: older integration may not accept
                        # force_refresh keyword argument.
                        probe_kwargs.pop("force_refresh", None)
                        precise_context = (
                            await claude_integration.get_precise_context_usage(
                                **probe_kwargs
                            )
                        )
                if precise_context:
                    lines.extend(build_precise_context_status_lines(precise_context))

                session_info = await claude_integration.get_session_info(session_id)
                if session_info:
                    lines.append(f"Messages: {session_info.get('messages', 0)}")
                    lines.append(f"Turns: {session_info.get('turns', 0)}")
                    cost_raw = session_info.get("cost", 0.0)
                    try:
                        cost_value = float(cost_raw or 0.0)
                    except (TypeError, ValueError):
                        cost_value = 0.0
                    if cost_value > 0:
                        lines.append(f"Cost: `${cost_value:.4f}`")

                    model_usage = session_info.get("model_usage")
                    if model_usage and not precise_context:
                        if cli_kind == "codex":
                            lines.extend(
                                [
                                    "",
                                    "*Context (/status)*",
                                    "实时额度获取失败。请稍后重试或手动执行 `/status`。",
                                ]
                            )
                        else:
                            lines.extend(
                                build_model_usage_status_lines(
                                    model_usage=model_usage,
                                    current_model=current_model,
                                    allow_estimated_ratio=True,
                                )
                            )
                    if cli_kind == "codex" and not precise_context and not model_usage:
                        lines.extend(
                            [
                                "",
                                "*Context (/status)*",
                                "实时额度获取失败。请稍后重试或手动执行 `/status`。",
                            ]
                        )

                    model_display = SessionService._resolve_display_model(
                        current_model=current_model,
                        session_info=session_info,
                        precise_context=precise_context,
                        prefer_runtime_model=cli_kind == "codex",
                    )
                    lines[model_line_idx] = f"Model: `{model_display}`"

            if event_lines_provider:
                try:
                    event_lines = await event_lines_provider(session_id)
                    if event_lines:
                        lines.extend(event_lines)
                except Exception as exc:
                    logger.warning(
                        "Failed to build context event summary",
                        error=str(exc),
                        user_id=user_id,
                        session_id=session_id,
                    )
        else:
            lines.append("Session: none")
            if include_resumable and claude_integration:
                existing = await claude_integration._find_resumable_session(
                    user_id, current_dir
                )
                if existing:
                    resumable_payload = {
                        "session_id": existing.session_id,
                        "message_count": existing.message_count,
                    }
                    lines.append(
                        f"Resumable: `{existing.session_id[:8]}...` "
                        f"({existing.message_count} msgs)"
                    )

        return ContextStatusSnapshot(
            lines=lines,
            precise_context=precise_context,
            session_info=session_info,
            resumable_payload=resumable_payload,
        )

    async def get_context_event_lines(
        self,
        session_id: str,
        *,
        limit: int = 12,
    ) -> List[str]:
        """Return markdown-friendly lines for /context event summary."""
        summary = await self.event_service.get_recent_event_summary(
            session_id=session_id,
            limit=limit,
            highlight_limit=0,
        )
        if int(summary.get("count", 0)) <= 0:
            return []

        lines: List[str] = [
            "",
            "*Recent Session Events*",
            f"Count: {summary.get('count', 0)}",
        ]

        latest_at = summary.get("latest_at")
        if latest_at:
            lines.append(f"Latest: `{latest_at}`")

        by_type = summary.get("by_type") or {}
        if by_type:
            lines.append("By Type:")
            for event_type, count in list(by_type.items())[:4]:
                safe_event_type = str(event_type).replace("`", "'")
                lines.append(f"- `{safe_event_type}`: {count}")

        return lines
