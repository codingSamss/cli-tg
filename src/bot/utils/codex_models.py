"""Codex model menu helpers."""

from __future__ import annotations

from typing import Iterable, List, Optional

# Default Codex model shortcuts shown in /model inline menu.
CODEX_MODEL_MENU_DEFAULTS: tuple[str, ...] = (
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.3-codex",
)


def build_codex_model_candidates(
    *,
    selected_model: Optional[str] = None,
    resolved_model: Optional[str] = None,
    defaults: Iterable[str] = CODEX_MODEL_MENU_DEFAULTS,
) -> List[str]:
    """Build deduplicated Codex model candidates for inline keyboards."""
    candidates: list[str] = []

    for candidate in (resolved_model, selected_model, *defaults):
        value = str(candidate or "").strip().replace("`", "")
        if not value or value.lower() in {"default", "current"}:
            continue
        if value not in candidates:
            candidates.append(value)

    return candidates
