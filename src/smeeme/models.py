"""Pydantic models for SmeeMe library."""

from __future__ import annotations

import json
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union
from urllib.parse import urlparse

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator, ConfigDict


class SmeeEvent(BaseModel):
    """Represents an event received from smee.io."""

    model_config = ConfigDict(extra="forbid")

    timestamp: float = Field(description="Event timestamp")
    headers: Dict[str, str] = Field(default_factory=dict, description="HTTP headers")
    body: Union[Dict[str, Any], str] = Field(description="Event payload")
    source_ip: Optional[str] = Field(default=None, description="Source IP address")
    forwarded: bool = Field(default=False, description="Whether event was forwarded")
    error: Optional[str] = Field(default=None, description="Error message if forwarding failed")
    receiver_port: Optional[int] = Field(default=None, description="Port that received the event")

    @computed_field
    @property
    def content_type(self) -> Optional[str]:
        """Get content type from headers."""
        return self.headers.get("content-type") or self.headers.get("Content-Type")

    @computed_field
    @property
    def is_json(self) -> bool:
        """Check if event contains JSON data."""
        ct = self.content_type
        return ct is not None and "application/json" in ct

    @computed_field
    @property
    def user_agent(self) -> Optional[str]:
        """Get user agent from headers."""
        return self.headers.get("user-agent") or self.headers.get("User-Agent")

    def get_json_body(self) -> Optional[Dict[str, Any]]:
        """Get body as JSON if possible."""
        if isinstance(self.body, dict):
            return self.body
        if isinstance(self.body, str) and self.is_json:
            try:
                return json.loads(self.body)
            except json.JSONDecodeError:
                pass
        return None

    def classify_event(self) -> str:
        """Classify event type for workflow routing."""
        body = self.get_json_body()
        if not body:
            return "generic"

        # Check for common webhook types
        if "memo" in str(body).lower():
            return "memo"
        elif "github" in (self.user_agent or "").lower():
            return "github"
        elif "pull_request" in body or "push" in body:
            return "github"
        else:
            return "generic"


class WorkflowPriority(Enum):
    """Priority levels for workflow jobs."""

    LOW = 1
    NORMAL = 2
    HIGH = 3
    URGENT = 4


class WorkflowJob(BaseModel):
    """Queued workflow job."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(description="Unique job identifier")
    event: SmeeEvent = Field(description="Associated event")
    workflow_type: str = Field(description="Type of workflow to execute")
    priority: WorkflowPriority = Field(default=WorkflowPriority.NORMAL, description="Job priority")
    retry_count: int = Field(default=0, description="Number of retry attempts")
    max_retries: int = Field(default=3, description="Maximum retry attempts")
    created_at: float = Field(default_factory=time.time, description="Job creation timestamp")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional job metadata")

    @computed_field
    @property
    def age_seconds(self) -> float:
        """Age of job in seconds."""
        return time.time() - self.created_at

    @computed_field
    @property
    def can_retry(self) -> bool:
        """Whether job can be retried."""
        return self.retry_count < self.max_retries


class QueueBackend(Enum):
    """Available queue backends."""

    MEMORY = "memory"
    REDIS = "redis"


class SmeeStatus(BaseModel):
    """Status information for smee client."""

    model_config = ConfigDict(extra="forbid")

    running: bool = Field(default=False, description="Whether smee client is running")
    process_id: Optional[int] = Field(default=None, description="Process ID if running")
    uptime_seconds: Optional[float] = Field(default=None, description="Uptime in seconds")
    restart_count: int = Field(default=0, description="Number of restarts")
    last_error: Optional[str] = Field(default=None, description="Last error message")
    events_received: int = Field(default=0, description="Total events received")
    events_forwarded: int = Field(default=0, description="Events successfully forwarded")
    queue_size: int = Field(default=0, description="Current queue size")
    receiver_port: Optional[int] = Field(default=None, description="Embedded receiver port")
    receiver_running: bool = Field(default=False, description="Whether embedded receiver is running")

    @computed_field
    @property
    def success_rate(self) -> float:
        """Calculate event forwarding success rate."""
        if self.events_received == 0:
            return 1.0
        return self.events_forwarded / self.events_received

    @computed_field
    @property
    def is_healthy(self) -> bool:
        """Whether the service is healthy."""
        return self.running and self.success_rate > 0.9


class SmeeMetrics(BaseModel):
    """Metrics for monitoring SmeeMe performance."""

    model_config = ConfigDict(extra="forbid")

    total_events: int = Field(default=0, description="Total events processed")
    successful_events: int = Field(default=0, description="Successfully processed events")
    failed_events: int = Field(default=0, description="Failed events")
    queue_jobs_processed: int = Field(default=0, description="Queue jobs processed")
    queue_jobs_failed: int = Field(default=0, description="Queue jobs failed")
    average_processing_time_ms: float = Field(default=0.0, description="Average processing time")
    uptime_seconds: float = Field(default=0.0, description="Total uptime")
    events_logged: int = Field(default=0, description="Events logged to file")
    events_forwarded_to_target: int = Field(default=0, description="Events forwarded to original target")

    @computed_field
    @property
    def success_rate(self) -> float:
        """Success rate percentage."""
        if self.total_events == 0:
            return 1.0
        return self.successful_events / self.total_events

    @computed_field
    @property
    def events_per_minute(self) -> float:
        """Events processed per minute."""
        if self.uptime_seconds == 0:
            return 0.0
        return (self.total_events / self.uptime_seconds) * 60

    @computed_field
    @property
    def queue_success_rate(self) -> float:
        """Queue job success rate."""
        total_jobs = self.queue_jobs_processed + self.queue_jobs_failed
        if total_jobs == 0:
            return 1.0
        return self.queue_jobs_processed / total_jobs


class SmeeClientMode(Enum):
    """Available smee client modes."""

    AUTO = "auto"
    SMEE = "smee"
    NPX = "npx"


class LogLevel(Enum):
    """Logging levels."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class SmeeConfig(BaseModel):
    """Configuration for SmeeMe."""

    model_config = ConfigDict(extra="ignore")

    # Required settings
    url: str = Field(description="https://smee.io/<channel>")
    target: str = Field(default="", description="Where to forward deliveries, e.g., http://localhost:8000/webhook")

    # Client settings
    client_mode: SmeeClientMode = Field(default=SmeeClientMode.AUTO, description="Smee client mode")
    ready_pattern: str = Field(default=r"Forwarding .* to .*", description="Pattern to detect client readiness")
    start_timeout_s: float = Field(default=15.0, description="Startup timeout in seconds")

    # Logging
    log_level: LogLevel = Field(default=LogLevel.INFO, description="Logging level")
    log_prefix: str = Field(default="[smee]", description="Log message prefix")
    capture_stdout: bool = Field(default=True, description="Capture subprocess stdout")
    capture_stderr: bool = Field(default=True, description="Capture subprocess stderr")

    # Embedded receiver settings
    embedded_receiver: bool = Field(default=True, description="Enable embedded HTTP receiver")
    listen_host: str = Field(default="127.0.0.1", description="Embedded receiver host")
    listen_port: int = Field(default=0, description="Embedded receiver port (0 = auto)")
    event_log_path: Optional[Path] = Field(default=None, description="Path to log events as JSONL")

    # Queue settings
    enable_queue: bool = Field(default=False, description="Enable workflow queue")
    queue_backend: QueueBackend = Field(default=QueueBackend.MEMORY, description="Queue backend")
    queue_workers: int = Field(default=3, description="Number of queue workers")
    redis_url: str = Field(default="redis://localhost:6379", description="Redis URL for queue backend")

    # Advanced settings
    path: Optional[str] = Field(default=None, description="Path override for forwarding")
    extra_args: List[str] = Field(default_factory=list, description="Extra args for smee client")
    environment: Optional[Dict[str, str]] = Field(default=None, description="Environment variables")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Validate smee.io URL."""
        if not v:
            raise ValueError("URL is required")
        parsed = urlparse(v)
        if not parsed.netloc.endswith("smee.io"):
            raise ValueError("URL must be a smee.io channel")
        return v

    @field_validator("target")
    @classmethod
    def validate_target(cls, v: str) -> str:
        """Validate target URL."""
        if v and v.strip():  # Only validate if target is provided
            parsed = urlparse(v)
            if not parsed.scheme or not parsed.netloc:
                raise ValueError("Target must be a valid URL")
        return v

    @computed_field
    @property
    def channel_id(self) -> str:
        """Extract channel ID from URL."""
        return urlparse(self.url).path.lstrip("/")

    @computed_field
    @property
    def effective_target(self) -> str:
        """Get effective target URL with path."""
        if self.path:
            base = self.target.rstrip("/")
            path_part = self.path if self.path.startswith("/") else f"/{self.path}"
            return f"{base}{path_part}"
        return self.target

    @computed_field
    @property
    def webhook_path(self) -> str:
        """Get webhook path for embedded receiver."""
        return self.path or "/webhook"


class WorkflowConfig(BaseModel):
    """Configuration for workflow processing."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Workflow name")
    enabled: bool = Field(default=True, description="Whether workflow is enabled")
    timeout_seconds: int = Field(default=60, description="Workflow timeout")
    max_retries: int = Field(default=3, description="Maximum retry attempts")
    priority: WorkflowPriority = Field(default=WorkflowPriority.NORMAL, description="Default priority")
    event_filters: Dict[str, Any] = Field(default_factory=dict, description="Event filtering criteria")

    @model_validator(mode="after")
    def validate_timeout(self) -> WorkflowConfig:
        """Validate timeout is reasonable."""
        if self.timeout_seconds < 1:
            raise ValueError("Timeout must be at least 1 second")
        if self.timeout_seconds > 3600:
            raise ValueError("Timeout cannot exceed 1 hour")
        return self
