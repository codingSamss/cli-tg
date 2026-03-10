"""Helpers for formatting status context/token usage."""

from datetime import datetime
from typing import Any, Dict, List, Optional


def estimate_context_window_tokens(model_name: Optional[str]) -> Optional[int]:
    """Estimate context window size from model name/alias.

    Returns None when the model is unknown.
    """
    if not model_name:
        return None

    lower = str(model_name).strip().lower()
    if not lower:
        return None

    # Claude Code aliases exposed by /model command.
    if lower in {"sonnet", "opus", "haiku"}:
        return 200_000

    # Common Claude model identifiers.
    if "claude" in lower or "sonnet" in lower or "opus" in lower or "haiku" in lower:
        return 200_000

    return None


def build_model_usage_status_lines(
    model_usage: Dict[str, Any],
    current_model: Optional[str] = None,
    allow_estimated_ratio: bool = True,
) -> List[str]:
    """Build context/token usage lines for /context output."""
    status_lines: List[str] = []
    entries = _iter_model_usage_entries(
        model_usage=model_usage,
        current_model=current_model,
    )

    for model_name, usage in entries:
        input_t = int(usage.get("inputTokens", usage.get("input_tokens", 0)) or 0)
        output_t = int(usage.get("outputTokens", usage.get("output_tokens", 0)) or 0)
        cache_read = int(
            usage.get(
                "cacheReadInputTokens",
                usage.get(
                    "cache_read_input_tokens", usage.get("cached_input_tokens", 0)
                ),
            )
            or 0
        )
        cache_create = int(
            usage.get(
                "cacheCreationInputTokens",
                usage.get("cache_creation_input_tokens", 0),
            )
            or 0
        )
        total_tokens = input_t + output_t + cache_read + cache_create

        resolved_model = usage.get("resolvedModel") or usage.get("resolved_model")
        display_model = resolved_model or (
            current_model if model_name == "sdk" and current_model else model_name
        )

        raw_ctx_window = usage.get("contextWindow", usage.get("context_window", 0)) or 0
        ctx_window_source = (
            str(
                usage.get(
                    "contextWindowSource", usage.get("context_window_source") or ""
                )
            )
            .strip()
            .lower()
        )
        inferred_ctx_window = estimate_context_window_tokens(
            resolved_model
            or (None if model_name == "sdk" else model_name)
            or current_model
        )
        ctx_window = int(raw_ctx_window or inferred_ctx_window or 0)
        estimated = bool(
            (ctx_window > 0 and ctx_window_source != "exact")
            or (not raw_ctx_window and inferred_ctx_window)
        )

        status_lines.append(f"\n*Context ({display_model})*")
        if ctx_window > 0 and (allow_estimated_ratio or not estimated):
            used_pct = total_tokens / ctx_window * 100
            remaining = max(ctx_window - total_tokens, 0)
            usage_line = (
                f"Usage: `{total_tokens:,}` / `{ctx_window:,}` ({used_pct:.1f}%)"
            )
            if estimated:
                usage_line += " _(estimated)_"
            status_lines.append(usage_line)
            status_lines.append(f"Remaining: `{remaining:,}` tokens")
        else:
            status_lines.append(f"Tokens: `{total_tokens:,}`")

        status_lines.append(f"  Input: `{input_t:,}` | Output: `{output_t:,}`")
        status_lines.append(
            f"  Cache read: `{cache_read:,}` | Cache create: `{cache_create:,}`"
        )
        max_output = int(
            usage.get("maxOutputTokens", usage.get("max_output_tokens", 0)) or 0
        )
        if max_output:
            status_lines.append(f"  Max output: `{max_output:,}`")

    return status_lines


def _iter_model_usage_entries(
    *,
    model_usage: Dict[str, Any],
    current_model: Optional[str],
) -> List[tuple[str, Dict[str, Any]]]:
    """Normalize usage payloads from Claude (nested) and Codex (flat)."""
    flat_keys = {
        "inputTokens",
        "outputTokens",
        "cacheReadInputTokens",
        "cacheCreationInputTokens",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
    }
    if any(key in model_usage for key in flat_keys):
        model_name = str(model_usage.get("resolvedModel") or current_model or "current")
        return [(model_name, model_usage)]

    entries: List[tuple[str, Dict[str, Any]]] = []
    for model_name, usage in model_usage.items():
        if isinstance(usage, dict):
            entries.append((str(model_name), usage))
    return entries


def build_precise_context_status_lines(context_usage: Dict[str, Any]) -> List[str]:
    """Build status lines from exact /context probe output."""
    used_tokens = int(context_usage.get("used_tokens", 0) or 0)
    total_tokens = int(context_usage.get("total_tokens", 0) or 0)
    lines: List[str] = []
    if total_tokens > 0:
        used_percent = float(
            context_usage.get("used_percent", used_tokens / total_tokens * 100)
        )
        remaining_tokens = int(
            context_usage.get("remaining_tokens", max(total_tokens - used_tokens, 0))
        )
        cached = bool(context_usage.get("cached", False))
        probe_command = str(context_usage.get("probe_command") or "/context").strip()
        if not probe_command.startswith("/"):
            probe_command = f"/{probe_command}"
        if not probe_command:
            probe_command = "/context"

        header = f"\n*Context ({probe_command})*"
        if cached:
            header = f"\n*Context ({probe_command}, cached)*"
        lines.extend(
            [
                header,
                f"Usage: `{used_tokens:,}` / `{total_tokens:,}` ({used_percent:.1f}%)",
                f"Remaining: `{remaining_tokens:,}` tokens",
            ]
        )

    rate_limit_lines = _build_rate_limit_lines(context_usage.get("rate_limits"))
    if rate_limit_lines:
        lines.extend(rate_limit_lines)

    return lines


def _build_rate_limit_lines(rate_limits: Any) -> List[str]:
    """Build Codex status window usage lines from token_count.rate_limits."""
    if not isinstance(rate_limits, dict):
        return []

    entries: List[Dict[str, Any]] = []
    for key in ("primary", "secondary"):
        entry = rate_limits.get(key)
        if not isinstance(entry, dict):
            continue
        used_percent_raw = entry.get("used_percent")
        window_minutes_raw = entry.get("window_minutes")
        if used_percent_raw is None or window_minutes_raw is None:
            continue
        try:
            used_percent = float(used_percent_raw)
            window_minutes = int(window_minutes_raw)
        except (TypeError, ValueError):
            continue
        if window_minutes <= 0:
            continue

        normalized: Dict[str, Any] = {
            "used_percent": used_percent,
            "window_minutes": window_minutes,
        }
        reset_text_raw = str(entry.get("resets_at_text") or "").strip()
        if reset_text_raw:
            normalized["resets_at_text"] = reset_text_raw
        resets_at_raw = entry.get("resets_at")
        if resets_at_raw is not None:
            try:
                resets_at = int(resets_at_raw)
                if resets_at > 0:
                    normalized["resets_at"] = resets_at
            except (TypeError, ValueError):
                pass
        entries.append(normalized)

    if not entries:
        return []

    entries.sort(key=lambda item: int(item.get("window_minutes", 0)))

    lines: List[str] = ["", "*Usage Limits (/status)*"]
    updated_at = str(rate_limits.get("updated_at") or "").strip()
    if updated_at:
        lines.append(f"Updated: `{updated_at}`")

    for entry in entries:
        window_minutes = int(entry["window_minutes"])
        label = _window_label(window_minutes)
        used_percent = max(min(float(entry["used_percent"]), 100.0), 0.0)
        remaining_percent = max(min(100.0 - used_percent, 100.0), 0.0)
        reset_text = str(entry.get("resets_at_text") or "").strip()
        if not reset_text:
            reset_text = _format_unix_timestamp(entry.get("resets_at"))
        line = f"{label}: `{remaining_percent:.1f}% remaining`"
        if reset_text:
            line += f" (resets `{reset_text}`)"
        lines.append(line)

    return lines


def _window_label(window_minutes: int) -> str:
    """Render compact rate-limit window labels."""
    if window_minutes % 10_080 == 0:
        days = window_minutes // 1_440
        return f"{days}d window"
    if window_minutes % 60 == 0:
        hours = window_minutes // 60
        return f"{hours}h window"
    return f"{window_minutes}m window"


def _format_unix_timestamp(value: Any) -> str:
    """Format unix timestamp as local wall-clock text."""
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""
