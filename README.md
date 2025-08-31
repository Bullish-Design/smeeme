# SmeeMe

Pythonic wrapper around the smee.io tunneling client with subprocess lifecycle management, queue-backed workflow processing, and optional FastAPI integration.

## Highlights

- **Drop-in wrapper for smee-client** with auto-detection of `smee` or `npx smee-client` and a readiness latch.  
- **Threaded runner** with clean start/stop/restart, log piping, and uptime/metrics tracking.  
- **Queue-backed workflows** (in-memory or Redis) with priorities, retries, and processing metrics.  
- **FastAPI integration** for lifecycle management, health, and status/metrics routes.  
- **Typed Pydantic models** for events, status, metrics, configs, and workflow jobs.  
- **CLI** for starting a tunnel, sending test events, and validating configuration.

---

## Quickstart (Library)

```python
from smeeme import SmeeMe, create_dev_config, WorkflowJob

# dev config: debug logging; optionally enable the queue
config = create_dev_config(
    url="https://smee.io/<your-channel>",
    target="http://localhost:8000/webhook",
    enable_queue=True,
)

smee = SmeeMe(config)

# Register a simple workflow handler (requires queue enabled)
def handle_memo(job: WorkflowJob):
    data = job.event.get_json_body() or {}
    print("MEMO:", data.get("memo"))

smee.register_workflow("memo", handle_memo)

# Run the smee client, then send yourself a test event
with smee:
    smee.send_test_event({"memo": {"name": "ex", "content": "hello from SmeeMe"}})
    # ...your app logic...
```

### Status & metrics

```python
status = smee.get_status()
metrics = smee.get_metrics()
print(status.model_dump())
print(metrics.model_dump())
```

---

## Quickstart (FastAPI)

Use the convenience factory, or attach to an existing `FastAPI` app:

```python
from fastapi import FastAPI
from smeeme import create_development_app, attach_smee, SmeeConfig

# 1) one-liner dev server
app = create_development_app("https://smee.io/<your-channel>")

# 2) or wire it up manually
app = FastAPI(title="My Webhook Server")
config = SmeeConfig(
    url="https://smee.io/<your-channel>",
    target="http://localhost:8000/webhook",
    enable_queue=True,
)
attach_smee(app, config, add_routes=True, routes_prefix="/smee")  # exposes /smee/status, /smee/metrics, etc.
```

**Routes provided (when `add_routes=True`):**

- `GET /smee/status` – current runner status (PID, uptime, queue size, success rate)  
- `GET /smee/metrics` – counters and averages for event/job processing  
- `POST /smee/test` – send a test event into the channel  
- `POST /smee/restart` – restart the runner

The `create_webhook_app` factory also wires a `POST /webhook` endpoint that simply echoes receipt; extend that handler for your use case.

---

## CLI

A small CLI is included under the `smeeme` command.

### Common commands

```bash
# start a tunnel and forward to your webhook target
smeeme start https://smee.io/<channel> http://localhost:8000/webhook

# send a test event to your channel
smeeme test https://smee.io/<channel>

# show and validate effective configuration
smeeme config --validate
```

### Global options (selection)

- `--url`, `--target`, `--path`
- `--client-mode {auto,smee,npx}`
- `--start-timeout <seconds>`
- `--enable-queue` / `--queue-backend {memory,redis}` / `--queue-workers <n>` / `--redis-url <url>`
- `-v/--verbose`

---

## Configuration

You can construct `SmeeConfig` directly in code or drive it from environment variables. Important fields include:

- **Client**: `client_mode`, `ready_pattern`, `start_timeout_s`, `extra_args`, `environment`
- **Logging**: `log_level`, `log_prefix`, `capture_stdout`, `capture_stderr`
- **Queue**: `enable_queue`, `queue_backend`, `queue_workers`, `redis_url`
- **Routing**: `url`, `target`, optional `path` (appended to `target` if set)

### Environment variable mapping (prefix `SMEEME_`)

- `URL`, `TARGET`, `PATH`
- `CLIENT_MODE` = `auto|smee|npx`
- `READY_PATTERN`
- `START_TIMEOUT_S`
- `LOG_LEVEL` = `debug|info|warning|error`
- `LOG_PREFIX`
- `CAPTURE_STDOUT`, `CAPTURE_STDERR`
- `ENABLE_QUEUE`
- `QUEUE_BACKEND` = `memory|redis`
- `QUEUE_WORKERS`
- `REDIS_URL`
- `EXTRA_ARGS` = comma‑separated extra flags passed to the underlying client
- `ENVIRONMENT` = JSON object merged into the process environment

**Validation rules (selection):**

- `url` must be a valid `https://smee.io/<channel>` URL.  
- `target` must be a valid HTTP(S) URL.  
- If `enable_queue` and `queue_backend=redis`, a valid `redis_url` is required.  
- `queue_workers` in `[1, 50]`; `start_timeout_s` in `[1.0, 300.0]`.
- Warnings are emitted for high worker counts or missing/unreachable Redis.

Helper builders:

- `create_dev_config(url, target, enable_queue=False)`  
- `create_production_config(url, target, redis_url, workers=5)`

---

## Events, Workflows & Queue

### Event model

`SmeeEvent` captures the webhook (headers/body), and computes helpers like `content_type`, `is_json`, and `user_agent`. `get_json_body()` safely returns a dict when possible. A simple classifier routes common payloads to `"memo"` or `"github"`, else `"generic"`.

### Workflow jobs

`WorkflowJob` wraps an event with a `workflow_type`, `priority` (`LOW|NORMAL|HIGH|URGENT`), retry bookkeeping, and metadata. `can_retry` derives from `retry_count < max_retries`.

### Queue backends

- **Memory** – per‑priority in‑process queues.  
- **Redis** – sorted‑set priority queue supporting multiple workers/processes.

### Processing

`SmeeQueue` holds a `WorkflowProcessor` where you register handlers; workers pull jobs and execute handlers with timing/metrics. Failed jobs retry up to `max_retries` before surfacing as permanent failures. Start workers by enabling the queue in config; the runner manages worker lifecycle alongside the client.

### Metrics

`SmeeMetrics` tracks totals, success rates, average processing time, and queue job stats; `events_per_minute` is derived from total uptime.

---

## Runner

`SmeeMe` builds a command for either `smee` or `npx smee-client@latest` based on `client_mode` and availability, starts the subprocess with optional stdout/stderr capture, detects readiness via `ready_pattern`, and exposes:

- `start()`, `stop()`, `restart()`  
- `wait_ready(timeout)`  
- `get_status() -> SmeeStatus` (running, pid, uptime, counts, success rate, queue size)  
- `get_metrics() -> SmeeMetrics`  
- `send_test_event(payload: dict | None)` – HTTP posts to the smee channel

Use it as a context manager to guarantee cleanup.

---

## Error Handling

Custom exceptions make failures explicit:

- `SmeeClientNotFoundError` – neither `smee` nor `npx` available / or forced mode missing.  
- `SmeeStartError` – subprocess failed to launch or didn’t become ready in time.  
- `SmeeProcessError` – runtime errors from the subprocess.  
- `SmeeConfigError` – invalid configuration/env.  
- `SmeeQueueError`, `SmeeQueueConnectionError` – queue misconfiguration or backend trouble.  
- `SmeeWorkflowError` – handler failures with job metadata.

---

## Project Metadata

- Python ≥ 3.11.  
- Package version and exported symbols are centralized; a console entry point `smeeme` is provided.  
- Optional extras: `fastapi`, `redis`, `cli`, and a convenience `all`.  
- License: MIT.

---

## FAQ

**Do I need to parse events manually?**  
Usually not. Build your webhook route and let Smee forward requests. Use the queue and workflow hooks only if you need background processing.

**Can I run without a queue?**  
Yes. The runner works without queue workers; you can still access status/metrics and forward webhooks.

**Where do I plug in my LLM or business logic?**  
Register workflow handlers (`register_workflow`) and emit jobs from your webhook route or event handlers.
