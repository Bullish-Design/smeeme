"""Core SmeeMe subprocess runner with threading-based monitoring and embedded HTTP receiver."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from shutil import which
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlparse

import httpx

try:
    from rich.console import Console
    from rich.json import JSON
    from rich.panel import Panel
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from .config import SmeeConfig
from .exceptions import SmeeClientNotFoundError, SmeeProcessError, SmeeStartError
from .models import SmeeEvent, SmeeStatus, SmeeMetrics, LogLevel, SmeeClientMode
from .queue import SmeeQueue, QueueBackend

logger = logging.getLogger(__name__)


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler for embedded webhook receiver."""

    def __init__(self, smee_runner: SmeeMe, *args, **kwargs):
        self.smee_runner = smee_runner
        super().__init__(*args, **kwargs)

    def do_POST(self):
        """Handle POST requests to webhook endpoint."""
        try:
            # Parse path
            path = urlparse(self.path).path
            webhook_path = self.smee_runner.config.webhook_path

            if path != webhook_path:
                self.send_error(404, f"Not Found - Expected {webhook_path}")
                return

            # Read headers
            headers = dict(self.headers)

            # Read body
            content_length = int(headers.get("content-length", 0))
            body_bytes = self.rfile.read(content_length) if content_length > 0 else b""

            # Parse body based on content type
            content_type = headers.get("content-type", "")
            if "application/json" in content_type:
                try:
                    body = json.loads(body_bytes.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body = body_bytes.decode("utf-8", errors="replace")
            else:
                body = body_bytes.decode("utf-8", errors="replace")

            # Create event
            event = SmeeEvent(
                timestamp=time.time(),
                headers=headers,
                body=body,
                source_ip=self.client_address[0] if self.client_address else None,
                receiver_port=self.server.server_port,
            )

            # Process event through SmeeMe pipeline
            self.smee_runner._handle_webhook_event(event)

            # Send success response
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "received", "timestamp": ' + str(event.timestamp).encode() + b"}")

        except Exception as e:
            logger.error(f"Webhook handler error: {e}")
            self.send_error(500, f"Internal Server Error: {e}")

    def do_GET(self):
        """Handle GET requests for health checks."""
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "healthy", "receiver": "embedded"}')
        else:
            self.send_error(404, "Not Found")

    def log_message(self, format: str, *args) -> None:
        """Override to use our logger."""
        logger.debug(f"[embedded-receiver] {format % args}")


class SmeeMe:
    """Main SmeeMe runner with subprocess management and embedded HTTP receiver."""

    def __init__(self, config: SmeeConfig, verbose_logging: bool = False):
        self.config = config
        self.verbose_logging = verbose_logging
        self._process: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._ready_pattern = re.compile(self.config.ready_pattern)
        self._start_time: Optional[float] = None
        self._metrics = SmeeMetrics()
        self._status = SmeeStatus(running=False)

        # Rich console for pretty output
        self._console = Console() if RICH_AVAILABLE and verbose_logging else None

        # Embedded HTTP receiver
        self._http_server: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._actual_receiver_port: Optional[int] = None

        # Event logging
        self._log_lock = threading.Lock()

        # Event handlers
        self._event_handlers: List[Callable[[SmeeEvent], Any]] = []

        # Queue setup
        self._queue: Optional[SmeeQueue] = None
        if self.config.enable_queue:
            self._setup_queue()

        # Setup logging and event handlers
        self._setup_logging()
        self._setup_event_handlers()

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

    def _setup_event_handlers(self) -> None:
        """Setup default event handlers."""
        # Always add JSONL logging if configured
        if self.config.event_log_path:
            self.add_event_handler(self._log_event_to_jsonl)

    # Lifecycle management
    def __enter__(self) -> SmeeMe:
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.stop()

    def start(self) -> None:
        """Start the embedded receiver and smee client subprocess."""
        if self._process and self._process.poll() is None:
            logger.warning("SmeeMe already running")
            return

        # Start embedded HTTP receiver first
        if self.config.embedded_receiver:
            self._start_embedded_receiver()

        # Build command pointing to embedded receiver or original target
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
            self._status = SmeeStatus(
                running=True,
                process_id=self._process.pid,
                receiver_port=self._actual_receiver_port,
                receiver_running=self._http_server is not None,
            )
        except FileNotFoundError as e:
            raise SmeeClientNotFoundError(f"smee-client not found: {e}")
        except Exception as e:
            self._status.last_error = str(e)
            self._status.running = False
            self._status.process_id = None
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

        self._status.running = True
        self._status.process_id = self._process.pid if self._process else None
        self._status.last_error = None

        logger.info(f"{self.config.log_prefix} Started successfully (PID: {self._process.pid})")
        if self._actual_receiver_port:
            logger.info(f"{self.config.log_prefix} Embedded receiver on port {self._actual_receiver_port}")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the smee client subprocess and embedded receiver."""
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

        # Stop embedded receiver
        self._stop_embedded_receiver()

        # Update metrics
        if self._start_time:
            uptime = time.time() - self._start_time
            self._metrics.uptime_seconds += uptime

        # Clean up
        self._process = None
        self._status.running = False
        self._status.process_id = None
        self._status.receiver_running = False
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
            self._status.receiver_port = self._actual_receiver_port
            self._status.receiver_running = self._http_server is not None
            if self._queue:
                self._status.queue_size = self._queue.backend.size()

        return self._status.model_copy()

    def get_metrics(self) -> SmeeMetrics:
        """Get current metrics."""
        return self._metrics.model_copy()

    # Embedded HTTP receiver
    def _start_embedded_receiver(self) -> None:
        """Start embedded HTTP receiver."""
        try:
            # Create handler class with reference to self
            def handler_factory(*args, **kwargs):
                return WebhookHandler(self, *args, **kwargs)

            # Create server
            self._http_server = ThreadingHTTPServer((self.config.listen_host, self.config.listen_port), handler_factory)

            # Get actual port if auto-assigned
            self._actual_receiver_port = self._http_server.server_port

            # Start server in background thread
            self._http_thread = threading.Thread(
                target=self._http_server.serve_forever, daemon=True, name="embedded-receiver"
            )
            self._http_thread.start()

            logger.info(
                f"{self.config.log_prefix} Embedded receiver started on {self.config.listen_host}:{self._actual_receiver_port}"
            )

        except Exception as e:
            logger.error(f"{self.config.log_prefix} Failed to start embedded receiver: {e}")
            raise SmeeStartError(f"Failed to start embedded HTTP receiver: {e}")

    def _stop_embedded_receiver(self) -> None:
        """Stop embedded HTTP receiver."""
        if self._http_server:
            logger.info(f"{self.config.log_prefix} Stopping embedded receiver...")
            self._http_server.shutdown()
            if self._http_thread and self._http_thread.is_alive():
                self._http_thread.join(timeout=5.0)
            self._http_server = None
            self._http_thread = None
            self._actual_receiver_port = None
            logger.info(f"{self.config.log_prefix} Embedded receiver stopped")

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

    def _handle_webhook_event(self, event: SmeeEvent) -> None:
        """Handle incoming webhook event (tee mode: log + forward + queue)."""
        self._metrics.total_events += 1
        event_success = True

        try:
            # Call direct handlers (including JSONL logging)
            for handler in self._event_handlers:
                try:
                    handler(event)
                except Exception as e:
                    logger.error(f"Event handler error: {e}")
                    event_success = False

            # Forward to original target if configured
            if self.config.target and self.config.target.strip():
                try:
                    self._forward_to_target(event)
                except Exception as e:
                    logger.error(f"Failed to forward event to {self.config.target}: {e}")
                    event_success = False

            # Queue processing if enabled
            if self._queue:
                try:
                    job = self._queue.enqueue_event(event)
                    logger.debug(f"Enqueued event as job {job.job_id}")
                except Exception as e:
                    logger.error(f"Queue processing failed: {e}")
                    event_success = False

            if event_success:
                self._metrics.successful_events += 1
            else:
                self._metrics.failed_events += 1

        except Exception as e:
            logger.error(f"Error handling webhook event: {e}")
            self._metrics.failed_events += 1
            self._status.last_error = str(e)

    def _forward_to_target(self, event: SmeeEvent) -> None:
        """Forward event to original target URL."""
        try:
            # Prepare request
            target_url = self.config.effective_target
            headers = event.headers.copy()

            # Remove hop-by-hop headers
            hop_by_hop = {
                "connection",
                "keep-alive",
                "proxy-authenticate",
                "proxy-authorization",
                "te",
                "trailers",
                "transfer-encoding",
                "upgrade",
            }
            headers = {k: v for k, v in headers.items() if k.lower() not in hop_by_hop}

            # Prepare body
            if isinstance(event.body, dict):
                data = json.dumps(event.body)
                headers["content-type"] = "application/json"
            else:
                data = event.body

            # Forward request
            with httpx.Client(timeout=30.0) as client:
                response = client.post(target_url, content=data, headers=headers)

                if 200 <= response.status_code < 300:
                    self._metrics.events_forwarded_to_target += 1
                    event.forwarded = True
                    logger.debug(f"Forwarded event to {target_url} -> {response.status_code}")
                else:
                    event.error = f"Target returned {response.status_code}"
                    logger.warning(f"Target {target_url} returned {response.status_code}")

        except Exception as e:
            event.error = str(e)
            logger.error(f"Failed to forward event to {self.config.effective_target}: {e}")

    def _log_event_to_jsonl(self, event: SmeeEvent) -> None:
        """Log event to JSONL file with thread safety."""
        if not self.config.event_log_path:
            return

        try:
            with self._log_lock:
                with open(self.config.event_log_path, "a", encoding="utf-8") as f:
                    json_line = event.model_dump_json(exclude_none=True)
                    f.write(json_line + "\n")
                    f.flush()

                self._metrics.events_logged += 1
                logger.debug(f"Logged event to {self.config.event_log_path}")

                # Rich pretty logging if enabled
                if self._console:
                    self._rich_log_event(event)

        except Exception as e:
            logger.error(f"Failed to log event to {self.config.event_log_path}: {e}")

    def _rich_log_event(self, event: SmeeEvent) -> None:
        """Log event with Rich formatting."""
        if not self._console:
            return

        # Create status indicators
        status_icons = []
        if event.forwarded:
            status_icons.append("📤 Forwarded")
        if event.error:
            status_icons.append("❌ Error")
        if not self.config.target:
            status_icons.append("📝 Logged")

        status_text = " | ".join(status_icons) if status_icons else "📥 Received"

        # Format event data
        if isinstance(event.body, dict):
            body_preview = json.dumps(event.body, indent=2, ensure_ascii=False)
        elif isinstance(event.body, str) and len(event.body) < 200:
            body_preview = event.body
        else:
            body_preview = f"[{type(event.body).__name__}] {len(str(event.body))} chars"

        # Create panel
        panel_content = f"""
{status_text}
Source: {event.source_ip or "unknown"}
User-Agent: {event.user_agent or "none"}
Content-Type: {event.content_type or "none"}

{body_preview}"""

        panel = Panel(
            panel_content.strip(),
            title=f"Webhook Event - Port {event.receiver_port}",
            title_align="left",
            border_style="green" if not event.error else "red",
            width=80,
        )

        self._console.print(panel)

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
        """Build smee client command pointing to embedded receiver."""
        client_mode = self._detect_client_mode()

        # Determine target - use embedded receiver if available, otherwise original target
        if self.config.embedded_receiver and self._actual_receiver_port:
            target_url = f"http://{self.config.listen_host}:{self._actual_receiver_port}{self.config.webhook_path}"
        else:
            target_url = self.config.effective_target

        if client_mode == "smee":
            cmd = ["smee", "--url", self.config.url, "--target", target_url]
        elif client_mode == "npx":
            cmd = [
                "npx",
                "--yes",
                "smee-client@latest",
                "--url",
                self.config.url,
                "--target",
                target_url,
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
