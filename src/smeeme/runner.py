"""Core SmeeMe subprocess runner with threading-based monitoring."""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import threading
import time
from shutil import which
from typing import Any, Callable, Dict, List, Optional, Sequence

import httpx

from .config import SmeeConfig
from .exceptions import SmeeClientNotFoundError, SmeeProcessError, SmeeStartError
from .models import SmeeEvent, SmeeStatus, SmeeMetrics, LogLevel, SmeeClientMode
from .queue import SmeeQueue, QueueBackend

logger = logging.getLogger(__name__)


class SmeeMe:
    """Main SmeeMe runner with subprocess management and optional queuing."""

    def __init__(self, config: SmeeConfig):
        self.config = config
        self._process: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._ready_pattern = re.compile(self.config.ready_pattern)
        self._start_time: Optional[float] = None
        self._metrics = SmeeMetrics()
        self._status = SmeeStatus()
        
        # Event handlers
        self._event_handlers: List[Callable[[SmeeEvent], Any]] = []
        
        # Queue setup
        self._queue: Optional[SmeeQueue] = None
        if self.config.enable_queue:
            self._setup_queue()
        
        # Setup logging
        self._setup_logging()

    def _setup_logging(self) -> None:
        """Configure logging based on config."""
        level_map = {
            LogLevel.DEBUG: logging.DEBUG,
            LogLevel.INFO: logging.INFO,
            LogLevel.WARNING: logging.WARNING,
            LogLevel.ERROR: logging.ERROR,
        }
        logging.basicConfig(level=level_map[self.config.log_level])

    def _setup_queue(self) -> None:
        """Setup workflow queue if enabled."""
        try:
            self._queue = SmeeQueue(
                backend=self.config.queue_backend,
                redis_url=self.config.redis_url,
            )
            logger.info(f"Queue enabled with {self.config.queue_backend.value} backend")
        except Exception as e:
            logger.error(f"Failed to setup queue: {e}")
            raise

    # Lifecycle management
    def __enter__(self) -> SmeeMe:
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.stop()

    def start(self) -> None:
        """Start the smee client subprocess."""
        if self._process and self._process.poll() is None:
            logger.warning("SmeeMe already running")
            return

        # Build command
        cmd = self._build_command()
        logger.info(f"{self.config.log_prefix} Starting: {' '.join(shlex.quote(c) for c in cmd)}")

        # Reset state
        self._ready_event.clear()
        self._start_time = time.time()

        # Setup subprocess
        popen_kwargs = {
            "stdout": subprocess.PIPE if self.config.capture_stdout else None,
            "stderr": subprocess.PIPE if self.config.capture_stderr else None,
            "bufsize": 1,
            "text": True,
        }

        # Environment variables
        if self.config.environment:
            env = os.environ.copy()
            env.update(self.config.environment)
            popen_kwargs["env"] = env

        # Start process
        try:
            self._process = subprocess.Popen(cmd, **popen_kwargs)
            self._status = SmeeStatus(running=True, process_id=self._process.pid)
        except FileNotFoundError as e:
            raise SmeeClientNotFoundError(f"smee-client not found: {e}")
        except Exception as e:
            raise SmeeStartError(f"Failed to start smee-client: {e}")

        # Start monitoring threads
        if self.config.capture_stdout and self._process.stdout:
            self._stdout_thread = threading.Thread(
                target=self._pump_stream,
                args=(self._process.stdout, logging.INFO),
                daemon=True,
                name="smee-stdout",
            )
            self._stdout_thread.start()

        if self.config.capture_stderr and self._process.stderr:
            self._stderr_thread = threading.Thread(
                target=self._pump_stream,
                args=(self._process.stderr, logging.WARNING),
                daemon=True,
                name="smee-stderr",
            )
            self._stderr_thread.start()

        # Wait for ready state
        if not self.wait_ready(self.config.start_timeout_s):
            exit_code = self._process.poll()
            if exit_code is not None:
                raise SmeeStartError(f"smee-client exited with code {exit_code}")
            raise SmeeStartError("Timeout waiting for smee-client readiness")

        # Start queue workers if enabled
        if self._queue:
            self._queue.start_workers(self.config.queue_workers)

        logger.info(f"{self.config.log_prefix} Started successfully (PID: {self._process.pid})")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the smee client subprocess."""
        if not self._process or self._process.poll() is not None:
            return

        logger.info(f"{self.config.log_prefix} Stopping...")

        # Stop queue workers first
        if self._queue:
            self._queue.stop_workers(timeout=timeout)

        # Terminate process
        try:
            self._process.terminate()
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning(f"{self.config.log_prefix} Process didn't terminate gracefully, killing...")
            self._process.kill()
            self._process.wait()
        except Exception as e:
            logger.error(f"{self.config.log_prefix} Error stopping process: {e}")

        # Update metrics
        if self._start_time:
            uptime = time.time() - self._start_time
            self._metrics.uptime_seconds += uptime

        # Clean up
        self._process = None
        self._status.running = False
        logger.info(f"{self.config.log_prefix} Stopped")

    def restart(self) -> None:
        """Restart the smee client."""
        self.stop()
        self._status.restart_count += 1
        self.start()

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        """Wait for smee client to be ready."""
        return self._ready_event.wait(timeout=timeout)

    def is_running(self) -> bool:
        """Check if smee client is running."""
        return self._process is not None and self._process.poll() is None

    def get_status(self) -> SmeeStatus:
        """Get current status."""
        if self.is_running() and self._start_time:
            self._status.uptime_seconds = time.time() - self._start_time
            self._status.events_received = self._metrics.total_events
            self._status.events_forwarded = self._metrics.successful_events
            if self._queue:
                self._status.queue_size = self._queue.backend.size()

        return self._status.model_copy()

    def get_metrics(self) -> SmeeMetrics:
        """Get current metrics."""
        return self._metrics.model_copy()

    # Event handling
    def add_event_handler(self, handler: Callable[[SmeeEvent], Any]) -> None:
        """Add event handler."""
        self._event_handlers.append(handler)

    def remove_event_handler(self, handler: Callable[[SmeeEvent], Any]) -> None:
        """Remove event handler."""
        if handler in self._event_handlers:
            self._event_handlers.remove(handler)

    def register_workflow(self, workflow_type: str, handler: Callable[[Any], Any]) -> None:
        """Register workflow handler (requires queue)."""
        if not self._queue:
            raise ValueError("Queue not enabled. Set enable_queue=True in config")
        self._queue.register_workflow(workflow_type, handler)

    # Testing utilities
    def send_test_event(
        self,
        payload: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Send test event to smee channel."""
        if not payload:
            payload = {
                "type": "smeeme.test",
                "message": "Test event from SmeeMe",
                "timestamp": time.time(),
            }

        test_headers = {"X-SmeeMe-Test": "1"}
        if headers:
            test_headers.update(headers)

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(self.config.url, json=payload, headers=test_headers)
                logger.info(f"{self.config.log_prefix} Test event sent: {response.status_code}")
                return 200 <= response.status_code < 300
        except Exception as e:
            logger.error(f"{self.config.log_prefix} Test event failed: {e}")
            return False

    # Internal methods
    def _build_command(self) -> Sequence[str]:
        """Build smee client command."""
        client_mode = self._detect_client_mode()
        
        if client_mode == "smee":
            cmd = ["smee", "--url", self.config.url, "--target", self.config.effective_target]
        elif client_mode == "npx":
            cmd = [
                "npx", "--yes", "smee-client@latest",
                "--url", self.config.url, "--target", self.config.effective_target
            ]
        else:
            raise SmeeClientNotFoundError("No suitable smee client found")

        # Add extra arguments
        if self.config.extra_args:
            cmd.extend(self.config.extra_args)

        return cmd

    def _detect_client_mode(self) -> str:
        """Detect available smee client."""
        if self.config.client_mode == SmeeClientMode.SMEE:
            if not which("smee"):
                raise SmeeClientNotFoundError("smee command not found")
            return "smee"
        elif self.config.client_mode == SmeeClientMode.NPX:
            if not which("npx"):
                raise SmeeClientNotFoundError("npx command not found")
            return "npx"
        else:  # AUTO mode
            if which("smee"):
                return "smee"
            elif which("npx"):
                return "npx"
            else:
                raise SmeeClientNotFoundError(
                    "Neither smee nor npx found. Install smee-client or ensure npx is available"
                )

    def _pump_stream(self, stream, level: int) -> None:
        """Pump subprocess output stream."""
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                
                line = line.rstrip("\r\n")
                if line:
                    logger.log(level, f"{self.config.log_prefix} {line}")
                    
                    # Check for readiness
                    if not self._ready_event.is_set() and self._ready_pattern.search(line):
                        self._ready_event.set()
                        logger.info(f"{self.config.log_prefix} Ready!")
                    
                    # Process as potential event
                    self._process_line(line)
        except Exception as e:
            logger.error(f"{self.config.log_prefix} Stream pump error: {e}")
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _process_line(self, line: str) -> None:
        """Process output line as potential webhook event."""
        # This is a simplified event processing - in practice, smee client
        # forwards webhooks to the target URL, not via stdout
        # This method is here for potential future event interception
        pass

    def _handle_webhook_event(self, event: SmeeEvent) -> None:
        """Handle incoming webhook event."""
        self._metrics.total_events += 1
        
        try:
            # Queue processing if enabled
            if self._queue:
                job = self._queue.enqueue_event(event)
                logger.debug(f"Enqueued event as job {job.job_id}")
            
            # Call direct handlers
            for handler in self._event_handlers:
                try:
                    handler(event)
                except Exception as e:
                    logger.error(f"Event handler error: {e}")
                    self._metrics.failed_events += 1
                    return
            
            self._metrics.successful_events += 1
            
        except Exception as e:
            logger.error(f"Error handling webhook event: {e}")
            self._metrics.failed_events += 1
            self._status.last_error = str(e)
