"""CLI engine selection and adapter lookup helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

ENGINE_CLAUDE = "claude"
ENGINE_CODEX = "codex"
SUPPORTED_CLI_ENGINES = (ENGINE_CLAUDE, ENGINE_CODEX)
ENGINE_STATE_KEY = "active_cli_engine"
ENGINE_PRIMARY_STATUS_COMMAND: dict[str, str] = {
    ENGINE_CLAUDE: "context",
    ENGINE_CODEX: "status",
}


@dataclass(frozen=True)
class EngineCapabilities:
    """Feature capabilities exposed by each CLI engine."""

    supports_model_selection: bool = False
    supports_codex_diag: bool = False
    supports_precise_context_probe: bool = False


ENGINE_CAPABILITIES: dict[str, EngineCapabilities] = {
    ENGINE_CLAUDE: EngineCapabilities(
        supports_model_selection=True,
        supports_codex_diag=False,
        supports_precise_context_probe=True,
    ),
    ENGINE_CODEX: EngineCapabilities(
        supports_model_selection=False,
        supports_codex_diag=True,
        supports_precise_context_probe=True,
    ),
}

COMMAND_ENGINE_VISIBILITY: dict[str, tuple[str, ...]] = {
    "context": (ENGINE_CLAUDE,),
    "codexdiag": (ENGINE_CODEX,),
    "status": (ENGINE_CODEX,),
    "provider": (ENGINE_CLAUDE,),
}


def normalize_cli_engine(value: str | None) -> str:
    """Normalize requested engine and fallback to Codex."""
    normalized = str(value or "").strip().lower()
    if normalized in SUPPORTED_CLI_ENGINES:
        return normalized
    return ENGINE_CODEX


def get_active_cli_engine(scope_state: Mapping[str, Any]) -> str:
    """Return active engine from scoped state with backward-compatible default."""
    return normalize_cli_engine(str(scope_state.get(ENGINE_STATE_KEY) or ""))


def set_active_cli_engine(scope_state: dict, engine: str) -> str:
    """Persist active engine into scoped state."""
    normalized = normalize_cli_engine(engine)
    scope_state[ENGINE_STATE_KEY] = normalized
    return normalized


def get_engine_capabilities(engine: str | None) -> EngineCapabilities:
    """Return capability snapshot for the requested engine."""
    normalized = normalize_cli_engine(engine)
    return ENGINE_CAPABILITIES.get(normalized, ENGINE_CAPABILITIES[ENGINE_CLAUDE])


def get_engine_primary_status_command(engine: str | None) -> str:
    """Return the canonical status/context command for current engine."""
    normalized = normalize_cli_engine(engine)
    return ENGINE_PRIMARY_STATUS_COMMAND.get(normalized, "context")


def command_visible_for_engine(command: str, engine: str | None) -> bool:
    """Whether command should be visible under active engine menu."""
    normalized = normalize_cli_engine(engine)
    visibility = COMMAND_ENGINE_VISIBILITY.get(command)
    if visibility is None:
        return True
    return normalized in visibility


def get_cli_integration(
    *,
    bot_data: Mapping[str, Any],
    scope_state: Mapping[str, Any],
) -> tuple[str, Any]:
    """Resolve active engine and corresponding integration instance."""
    engine = get_active_cli_engine(scope_state)
    integrations = bot_data.get("cli_integrations")
    if isinstance(integrations, Mapping):
        integration = integrations.get(engine) or integrations.get(ENGINE_CLAUDE)
        if integration is not None:
            return engine, integration

    # Backward-compatible fallback for old dependency key.
    return ENGINE_CLAUDE, bot_data.get("claude_integration")
