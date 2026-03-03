"""Configuration management using Pydantic Settings.

Features:
- Environment variable loading
- Type validation
- Default values
- Computed properties
- Environment-specific settings
"""

import json
from pathlib import Path
from typing import Any, List, Optional

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.utils.constants import (
    DEFAULT_CLAUDE_MAX_TURNS,
    DEFAULT_CLAUDE_TIMEOUT_SECONDS,
    DEFAULT_DATABASE_URL,
    DEFAULT_MAX_SESSIONS_PER_USER,
    DEFAULT_SESSION_TIMEOUT_HOURS,
)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Bot settings
    telegram_bot_token: SecretStr = Field(
        ..., description="Telegram bot token from BotFather"
    )
    telegram_bot_username: str = Field(..., description="Bot username without @")

    # Security
    approved_directory: Path = Field(..., description="Base directory for projects")
    allowed_users: Optional[List[int]] = Field(
        None, description="Allowed Telegram user IDs"
    )

    # Claude settings
    claude_binary_path: Optional[str] = Field(
        None, description="Path to Claude CLI binary (deprecated)"
    )
    claude_cli_path: Optional[str] = Field(
        None, description="Path to Claude CLI executable"
    )
    claude_setting_sources: Optional[List[str]] = Field(
        default=["user", "project", "local"],
        description=(
            "Claude SDK setting sources (default: user,project,local). "
            "Use empty value to let special gateways decide."
        ),
    )
    anthropic_api_key: Optional[SecretStr] = Field(
        None,
        description="Anthropic API key for Claude SDK (optional if logged into Claude CLI)",
    )
    claude_model: str = Field(
        "claude-3-5-sonnet-20241022", description="Claude model to use"
    )
    claude_max_turns: int = Field(
        DEFAULT_CLAUDE_MAX_TURNS, description="Max conversation turns"
    )
    claude_timeout_seconds: int = Field(
        DEFAULT_CLAUDE_TIMEOUT_SECONDS, description="Claude timeout"
    )
    use_sdk: bool = Field(True, description="Use Python SDK instead of CLI subprocess")
    sdk_enable_tool_permission_gate: bool = Field(
        False,
        description=(
            "Enable SDK tool permission gate (requires SDK client mode with "
            "can_use_tool callback)"
        ),
    )
    enable_codex_cli: bool = Field(
        False,
        description="Enable Codex CLI adapter (subprocess mode)",
    )
    codex_enable_mcp: bool = Field(
        True,
        description="Enable MCP servers for Codex CLI sessions",
    )
    codex_cli_path: Optional[str] = Field(
        None,
        description="Path to Codex CLI executable",
    )
    claude_allowed_tools: Optional[List[str]] = Field(
        default=None,
        description=(
            "Optional allowlist for Claude tools. "
            "Unset/empty means do not pass --allowedTools (inherit CLI/runtime defaults)."
        ),
    )
    claude_disallowed_tools: Optional[List[str]] = Field(
        default=["git commit", "git push"],
        description="List of explicitly disallowed Claude tools/commands",
    )

    # Storage
    database_url: str = Field(
        DEFAULT_DATABASE_URL, description="Database connection URL"
    )
    session_timeout_hours: int = Field(
        DEFAULT_SESSION_TIMEOUT_HOURS, description="Session timeout"
    )
    session_timeout_minutes: int = Field(
        default=120,
        description="Session timeout in minutes",
        ge=10,
        le=1440,  # Max 24 hours
    )
    max_sessions_per_user: int = Field(
        DEFAULT_MAX_SESSIONS_PER_USER, description="Max concurrent sessions"
    )

    # Features
    enable_mcp: bool = Field(False, description="Enable Model Context Protocol")
    mcp_config_path: Optional[Path] = Field(
        None, description="MCP configuration file path"
    )
    enable_git_integration: bool = Field(True, description="Enable git commands")
    enable_file_uploads: bool = Field(True, description="Enable file upload handling")
    enable_quick_actions: bool = Field(False, description="Enable quick action buttons")
    image_cleanup_max_age_hours: int = Field(
        24, description="Max age in hours for uploaded images before cleanup"
    )
    resume_scan_cache_ttl_seconds: int = Field(
        30,
        description="TTL for /resume desktop session scan cache",
        ge=0,
        le=3600,
    )
    resume_history_preview_count: int = Field(
        6,
        description="Number of recent messages to show after resuming a session",
        ge=0,
        le=20,
    )
    stream_render_debounce_ms: int = Field(
        1000,
        description="Debounce interval for streaming progress message updates",
        ge=0,
        le=5000,
    )
    stream_render_min_edit_interval_ms: int = Field(
        1000,
        description="Minimum interval between Telegram progress message edits",
        ge=0,
        le=10000,
    )
    status_reactions_enabled: bool = Field(
        True,
        description="Enable multi-stage Telegram status reactions for text messages",
    )
    status_reaction_debounce_ms: int = Field(
        700,
        description="Debounce interval for non-terminal status reaction updates",
        ge=0,
        le=5000,
    )
    status_reaction_stall_soft_ms: int = Field(
        10000,
        description="Inactivity timeout for soft-stall reaction update",
        ge=0,
        le=120000,
    )
    status_reaction_stall_hard_ms: int = Field(
        30000,
        description="Inactivity timeout for hard-stall reaction update",
        ge=0,
        le=300000,
    )
    status_context_probe_ttl_seconds: int = Field(
        0,
        description="TTL for /context precise /context probe cache (0 disables cache)",
        ge=0,
        le=600,
    )
    status_context_probe_timeout_seconds: int = Field(
        45,
        description="Timeout for /context precise /context probe (seconds)",
        ge=5,
        le=300,
    )

    # Monitoring
    log_level: str = Field("INFO", description="Logging level")
    enable_telemetry: bool = Field(False, description="Enable anonymous telemetry")
    sentry_dsn: Optional[str] = Field(None, description="Sentry DSN for error tracking")

    # Development
    debug: bool = Field(False, description="Enable debug mode")
    development_mode: bool = Field(False, description="Enable development features")

    # Webhook settings (optional)
    webhook_url: Optional[str] = Field(None, description="Webhook URL for bot")
    webhook_port: int = Field(8443, description="Webhook port")
    webhook_path: str = Field("/webhook", description="Webhook path")

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    @field_validator("allowed_users", mode="before")
    @classmethod
    def parse_allowed_users(cls, v: Any) -> Optional[List[int]]:
        """Parse comma-separated user IDs."""
        if v is None:
            return None
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            return [int(uid.strip()) for uid in v.split(",") if uid.strip()]
        if isinstance(v, list):
            return [int(uid) for uid in v]
        return v  # type: ignore[no-any-return]

    @field_validator("claude_allowed_tools", mode="before")
    @classmethod
    def parse_claude_allowed_tools(cls, v: Any) -> Optional[List[str]]:
        """Parse comma-separated tool names."""
        if v is None:
            return None
        if isinstance(v, str):
            tools = [tool.strip() for tool in v.split(",") if tool.strip()]
            return tools or None
        if isinstance(v, list):
            tools = [str(tool).strip() for tool in v if str(tool).strip()]
            return tools or None
        return v  # type: ignore[no-any-return]

    @field_validator("claude_setting_sources", mode="before")
    @classmethod
    def parse_claude_setting_sources(cls, v: Any) -> Optional[List[str]]:
        """Parse optional Claude SDK setting_sources."""
        if v is None:
            return None
        if isinstance(v, str):
            sources = [item.strip() for item in v.split(",") if item.strip()]
            return sources or None
        if isinstance(v, list):
            sources = [str(item).strip() for item in v if str(item).strip()]
            return sources or None
        return v  # type: ignore[no-any-return]

    @field_validator("approved_directory")
    @classmethod
    def validate_approved_directory(cls, v: Any) -> Path:
        """Ensure approved directory exists and is absolute."""
        if isinstance(v, str):
            v = Path(v)

        path = v.resolve()
        if not path.exists():
            raise ValueError(f"Approved directory does not exist: {path}")
        if not path.is_dir():
            raise ValueError(f"Approved directory is not a directory: {path}")
        return path  # type: ignore[no-any-return]

    @field_validator("mcp_config_path", mode="before")
    @classmethod
    def validate_mcp_config(cls, v: Any, info: Any) -> Optional[Path]:
        """Validate MCP configuration path if MCP is enabled."""
        if not v:
            return v  # type: ignore[no-any-return]
        if isinstance(v, str):
            v = Path(v)
        if not v.exists():
            raise ValueError(f"MCP config file does not exist: {v}")
        # Validate that the file contains valid JSON with mcpServers
        try:
            with open(v) as f:
                config_data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"MCP config file is not valid JSON: {e}")
        if not isinstance(config_data, dict):
            raise ValueError("MCP config file must contain a JSON object")
        if "mcpServers" not in config_data:
            raise ValueError(
                "MCP config file must contain a 'mcpServers' key. "
                'Expected format: {"mcpServers": {"server-name": {"command": "...", ...}}}'
            )
        if not isinstance(config_data["mcpServers"], dict):
            raise ValueError(
                "'mcpServers' must be an object mapping server names to configurations"
            )
        if not config_data["mcpServers"]:
            raise ValueError(
                "'mcpServers' must contain at least one server configuration"
            )
        return v  # type: ignore[no-any-return]

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: Any) -> str:
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"log_level must be one of {valid_levels}")
        return v.upper()  # type: ignore[no-any-return]

    @model_validator(mode="after")
    def validate_cross_field_dependencies(self) -> "Settings":
        """Validate dependencies between fields."""
        # Check MCP requirements
        if self.enable_mcp and not self.mcp_config_path:
            raise ValueError("mcp_config_path required when enable_mcp is True")

        return self

    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return not (self.debug or self.development_mode)

    @property
    def database_path(self) -> Optional[Path]:
        """Extract path from SQLite database URL."""
        if self.database_url.startswith("sqlite:///"):
            db_path = self.database_url.replace("sqlite:///", "")
            return Path(db_path).resolve()
        return None

    @property
    def telegram_token_str(self) -> str:
        """Get Telegram token as string."""
        return self.telegram_bot_token.get_secret_value()

    @property
    def anthropic_api_key_str(self) -> Optional[str]:
        """Get Anthropic API key as string."""
        return (
            self.anthropic_api_key.get_secret_value()
            if self.anthropic_api_key
            else None
        )
