"""Exception classes for SmeeMe library."""

from __future__ import annotations


class SmeeError(Exception):
    """Base exception for all SmeeMe errors."""
    pass


class SmeeClientNotFoundError(SmeeError):
    """Raised when smee-client is not available in PATH."""
    pass


class SmeeStartError(SmeeError):
    """Raised when the smee client fails to start or become ready."""
    pass


class SmeeProcessError(SmeeError):
    """Raised when the smee subprocess encounters an error."""
    
    def __init__(self, message: str, exit_code: int | None = None, stderr: str | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class SmeeConfigError(SmeeError):
    """Raised when SmeeMe configuration is invalid."""
    pass


class SmeeQueueError(SmeeError):
    """Base exception for queue-related errors."""
    pass


class SmeeQueueConnectionError(SmeeQueueError):
    """Raised when unable to connect to queue backend."""
    pass


class SmeeWorkflowError(SmeeError):
    """Raised when workflow processing fails."""
    
    def __init__(self, message: str, job_id: str | None = None, retry_count: int = 0):
        super().__init__(message)
        self.job_id = job_id
        self.retry_count = retry_count
