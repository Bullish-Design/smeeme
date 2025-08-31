"""SmeeMe - Pythonic wrapper for smee.io client with queue-based workflow processing."""

from __future__ import annotations

__version__ = "0.1.0"
__author__ = "Bullish Design"

# Core functionality
from .config import SmeeConfig, load_config_from_env, create_dev_config, create_production_config
from .runner import SmeeMe
from .queue import SmeeQueue, WorkflowProcessor

# Models
from .models import (
    SmeeEvent,
    WorkflowJob,
    WorkflowPriority,
    SmeeStatus,
    SmeeMetrics,
    QueueBackend,
    LogLevel,
    SmeeClientMode,
)

# Exceptions
from .exceptions import (
    SmeeError,
    SmeeClientNotFoundError,
    SmeeStartError,
    SmeeProcessError,
    SmeeConfigError,
    SmeeQueueError,
    SmeeQueueConnectionError,
    SmeeWorkflowError,
)

# Optional FastAPI integration
try:
    from .fastapi import attach_smee, create_webhook_app, create_memo_webhook_app, create_development_app
    __fastapi_available__ = True
except ImportError:
    __fastapi_available__ = False

__all__ = [
    # Core classes
    "SmeeMe",
    "SmeeConfig",
    "SmeeQueue",
    "WorkflowProcessor",
    
    # Models
    "SmeeEvent",
    "WorkflowJob", 
    "WorkflowPriority",
    "SmeeStatus",
    "SmeeMetrics",
    "QueueBackend",
    "LogLevel",
    "SmeeClientMode",
    
    # Configuration helpers
    "load_config_from_env",
    "create_dev_config",
    "create_production_config",
    
    # Exceptions
    "SmeeError",
    "SmeeClientNotFoundError", 
    "SmeeStartError",
    "SmeeProcessError",
    "SmeeConfigError",
    "SmeeQueueError",
    "SmeeQueueConnectionError",
    "SmeeWorkflowError",
]

# Add FastAPI exports if available
if __fastapi_available__:
    __all__.extend([
        "attach_smee",
        "create_webhook_app", 
        "create_memo_webhook_app",
        "create_development_app",
    ])


def get_version() -> str:
    """Get package version."""
    return __version__


def check_dependencies() -> dict[str, bool]:
    """Check optional dependencies availability."""
    dependencies = {}
    
    # FastAPI
    try:
        import fastapi
        dependencies["fastapi"] = True
    except ImportError:
        dependencies["fastapi"] = False
    
    # Redis
    try:
        import redis
        dependencies["redis"] = True
    except ImportError:
        dependencies["redis"] = False
        
    return dependencies
