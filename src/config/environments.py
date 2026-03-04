"""Environment-specific configuration overrides."""

from typing import Any, Dict


class DevelopmentConfig:
    """Development environment overrides."""

    debug: bool = True
    development_mode: bool = True
    log_level: str = "DEBUG"
    cli_timeout_seconds: int = 3600  # Longer timeout for long CLI tasks
    enable_telemetry: bool = False

    @classmethod
    def as_dict(cls) -> Dict[str, Any]:
        """Return config as dictionary."""
        return {
            key: value
            for key, value in cls.__dict__.items()
            if not key.startswith("_")
            and not callable(value)
            and not isinstance(value, classmethod)
        }


class TestingConfig:
    """Testing environment configuration."""

    debug: bool = True
    development_mode: bool = True
    database_url: str = "sqlite:///:memory:"
    approved_directory: str = "/tmp/test_projects"
    enable_telemetry: bool = False
    cli_timeout_seconds: int = 30  # Faster timeout for tests
    session_timeout_hours: int = 1  # Short session timeout for testing

    @classmethod
    def as_dict(cls) -> Dict[str, Any]:
        """Return config as dictionary."""
        return {
            key: value
            for key, value in cls.__dict__.items()
            if not key.startswith("_")
            and not callable(value)
            and not isinstance(value, classmethod)
        }


class ProductionConfig:
    """Production environment configuration."""

    debug: bool = False
    development_mode: bool = False
    log_level: str = "INFO"
    enable_telemetry: bool = True
    session_timeout_hours: int = 12  # Shorter session timeout

    @classmethod
    def as_dict(cls) -> Dict[str, Any]:
        """Return config as dictionary."""
        return {
            key: value
            for key, value in cls.__dict__.items()
            if not key.startswith("_")
            and not callable(value)
            and not isinstance(value, classmethod)
        }
