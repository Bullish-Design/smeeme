"""Configuration management for SmeeMe."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Type, TypeVar

from .exceptions import SmeeConfigError
from .models import SmeeConfig, LogLevel, SmeeClientMode, QueueBackend

T = TypeVar("T", bound=SmeeConfig)


def _as_bool(value: str | bool | None, default: bool) -> bool:
    """Convert string/bool/None to boolean."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | int | None, default: int) -> int:
    """Convert string/int/None to integer."""
    if isinstance(value, int):
        return value
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        return default


def _as_float(value: str | float | None, default: float) -> float:
    """Convert string/float/None to float."""
    if isinstance(value, float):
        return value
    if value is None:
        return default
    try:
        return float(str(value).strip())
    except ValueError:
        return default


def _as_path(value: str | None) -> Optional[Path]:
    """Convert string to Path if not None."""
    if value is None:
        return None
    return Path(str(value).strip())


class ConfigLoader:
    """Configuration loader with environment variable support."""

    def __init__(self, prefix: str = "SMEEME"):
        self.prefix = prefix

    def load_from_env(
        self,
        config_class: Type[T] = SmeeConfig,
        env_dict: Optional[Dict[str, str]] = None,
        defaults: Optional[Dict[str, Any]] = None,
    ) -> T:
        """Load configuration from environment variables."""
        env = {**os.environ, **(env_dict or {})}
        config_data = defaults.copy() if defaults else {}

        # Map environment variables to config fields
        mappings = {
            f"{self.prefix}_URL": "url",
            f"{self.prefix}_TARGET": "target",
            f"{self.prefix}_CLIENT_MODE": "client_mode",
            f"{self.prefix}_READY_PATTERN": "ready_pattern",
            f"{self.prefix}_START_TIMEOUT_S": "start_timeout_s",
            f"{self.prefix}_LOG_LEVEL": "log_level",
            f"{self.prefix}_LOG_PREFIX": "log_prefix",
            f"{self.prefix}_CAPTURE_STDOUT": "capture_stdout",
            f"{self.prefix}_CAPTURE_STDERR": "capture_stderr",
            f"{self.prefix}_EMBEDDED_RECEIVER": "embedded_receiver",
            f"{self.prefix}_LISTEN_HOST": "listen_host",
            f"{self.prefix}_LISTEN_PORT": "listen_port",
            f"{self.prefix}_EVENT_LOG_PATH": "event_log_path",
            f"{self.prefix}_ENABLE_QUEUE": "enable_queue",
            f"{self.prefix}_QUEUE_BACKEND": "queue_backend",
            f"{self.prefix}_QUEUE_WORKERS": "queue_workers",
            f"{self.prefix}_REDIS_URL": "redis_url",
            f"{self.prefix}_PATH": "path",
        }

        for env_key, config_key in mappings.items():
            if env_key in env and env[env_key]:
                config_data[config_key] = self._convert_value(config_key, env[env_key])

        # Handle extra args (comma-separated)
        extra_args_key = f"{self.prefix}_EXTRA_ARGS"
        if extra_args_key in env and env[extra_args_key]:
            config_data["extra_args"] = [arg.strip() for arg in env[extra_args_key].split(",")]

        # Handle environment variables (JSON format)
        env_vars_key = f"{self.prefix}_ENVIRONMENT"
        if env_vars_key in env and env[env_vars_key]:
            try:
                import json

                config_data["environment"] = json.loads(env[env_vars_key])
            except json.JSONDecodeError as e:
                raise SmeeConfigError(f"Invalid JSON in {env_vars_key}: {e}")

        try:
            return config_class(**config_data)
        except Exception as e:
            raise SmeeConfigError(f"Invalid configuration: {e}")

    def _convert_value(self, key: str, value: str) -> Any:
        """Convert string value to appropriate type based on field."""
        conversions = {
            "start_timeout_s": lambda v: _as_float(v, 15.0),
            "capture_stdout": lambda v: _as_bool(v, True),
            "capture_stderr": lambda v: _as_bool(v, True),
            "embedded_receiver": lambda v: _as_bool(v, True),
            "listen_port": lambda v: _as_int(v, 0),
            "event_log_path": lambda v: _as_path(v),
            "enable_queue": lambda v: _as_bool(v, False),
            "queue_workers": lambda v: _as_int(v, 3),
            "client_mode": lambda v: SmeeClientMode(v.lower()),
            "log_level": lambda v: LogLevel(v.lower()),
            "queue_backend": lambda v: QueueBackend(v.lower()),
        }

        converter = conversions.get(key)
        if converter:
            try:
                return converter(value)
            except (ValueError, TypeError) as e:
                raise SmeeConfigError(f"Invalid value for {key}: {value} ({e})")

        return value


class ConfigValidator:
    """Configuration validator with comprehensive checks."""

    @staticmethod
    def validate_config(config: SmeeConfig) -> None:
        """Validate configuration for common issues."""
        errors = []

        # URL validation
        if not config.url:
            errors.append("URL is required")
        elif not config.url.startswith("https://smee.io/"):
            errors.append("URL must be a valid smee.io channel URL")

        # Target validation - only required if embedded receiver is disabled
        if not config.embedded_receiver and not config.target:
            errors.append("Target URL is required when embedded receiver is disabled")
        elif config.target and not any(config.target.startswith(proto) for proto in ["http://", "https://"]):
            errors.append("Target must be a valid HTTP/HTTPS URL")

        # Embedded receiver validation
        if config.embedded_receiver:
            if config.listen_port < 0 or config.listen_port > 65535:
                errors.append("Listen port must be between 0-65535")
            if config.event_log_path and not config.event_log_path.parent.exists():
                errors.append(f"Event log directory does not exist: {config.event_log_path.parent}")

        # Queue validation
        if config.enable_queue:
            if config.queue_backend == QueueBackend.REDIS and not config.redis_url:
                errors.append("Redis URL required when using Redis queue backend")
            if config.queue_workers < 1:
                errors.append("Queue workers must be at least 1")
            if config.queue_workers > 50:
                errors.append("Queue workers should not exceed 50 for stability")

        # Timeout validation
        if config.start_timeout_s < 1.0:
            errors.append("Start timeout must be at least 1 second")
        if config.start_timeout_s > 300.0:
            errors.append("Start timeout should not exceed 5 minutes")

        if errors:
            raise SmeeConfigError(f"Configuration validation failed: {'; '.join(errors)}")

    @staticmethod
    def check_runtime_requirements(config: SmeeConfig) -> list[str]:
        """Check runtime requirements and return warnings."""
        warnings = []

        # Check if Redis is available when needed
        if config.enable_queue and config.queue_backend == QueueBackend.REDIS:
            try:
                import redis

                client = redis.from_url(config.redis_url, socket_connect_timeout=1)
                client.ping()
            except ImportError:
                warnings.append("Redis package not installed (install with: uv add redis)")
            except Exception:
                warnings.append(f"Cannot connect to Redis at {config.redis_url}")

        # Check system resources for queue workers
        if config.enable_queue and config.queue_workers > 10:
            warnings.append(f"High worker count ({config.queue_workers}) may consume significant resources")

        # Check event log path permissions
        if config.event_log_path:
            try:
                # Test write permissions
                config.event_log_path.touch()
            except PermissionError:
                warnings.append(f"No write permission for event log: {config.event_log_path}")
            except Exception as e:
                warnings.append(f"Cannot access event log path: {e}")

        return warnings


# Convenience functions
def load_config_from_env(
    prefix: str = "SMEEME",
    defaults: Optional[Dict[str, Any]] = None,
    validate: bool = True,
) -> SmeeConfig:
    """Load and optionally validate configuration from environment."""
    loader = ConfigLoader(prefix)
    config = loader.load_from_env(SmeeConfig, defaults=defaults)

    if validate:
        ConfigValidator.validate_config(config)

    return config


def create_dev_config(
    url: str,
    target: str = "http://localhost:8000/webhook",
    enable_queue: bool = False,
    event_log_path: Optional[str] = None,
) -> SmeeConfig:
    """Create a development configuration."""
    log_path = Path(event_log_path) if event_log_path else None
    return SmeeConfig(
        url=url,
        target=target,
        log_level=LogLevel.DEBUG,
        enable_queue=enable_queue,
        queue_workers=2,
        capture_stdout=True,
        capture_stderr=True,
        embedded_receiver=True,
        event_log_path=log_path,
    )


def create_production_config(
    url: str,
    target: str,
    redis_url: str = "redis://localhost:6379",
    workers: int = 5,
    event_log_path: Optional[str] = None,
) -> SmeeConfig:
    """Create a production configuration with queue enabled."""
    log_path = Path(event_log_path) if event_log_path else None
    return SmeeConfig(
        url=url,
        target=target,
        log_level=LogLevel.INFO,
        enable_queue=True,
        queue_backend=QueueBackend.REDIS,
        queue_workers=workers,
        redis_url=redis_url,
        capture_stdout=True,
        capture_stderr=False,
        embedded_receiver=True,
        event_log_path=log_path,
    )
