# SmeeMe

Pythonic wrapper around the smee.io tunneling client with embedded HTTP receiver, subprocess lifecycle management, queue-backed workflow processing, and optional FastAPI integration.

## Highlights

- **Embedded HTTP receiver** with always-on tee mode - intercepts all webhook events for logging and processing while forwarding to your target
- **JSONL event logging** - automatic audit trail of all webhook events with thread-safe logging
- **Drop-in wrapper for smee-client** with auto-detection of `smee` or `npx smee-client` and a readiness latch  
- **Threaded runner** with clean start/stop/restart, log piping, and uptime/metrics tracking
- **Queue-backed workflows** (in-memory or Redis) with priorities, retries, and processing metrics
- **FastAPI integration** for lifecycle management, health, and status/metrics routes
- **Typed Pydantic models** for events, status, metrics, configs, and workflow jobs
- **CLI** for starting a tunnel, sending test events, and validating configuration

---

## Quickstart (Library)

```python
from smeeme import SmeeMe, create_dev_config, WorkflowJob

# dev config with embedded receiver and event logging
config = create_dev_config(
    url="https://smee.io/<your-channel>",
    target="http://localhost:8000/webhook",
    enable_queue=True,
    event_log_path="events.jsonl",  # automatic JSONL logging
)

smee = SmeeMe(config)

# Register a simple workflow handler (requires queue enabled)
def handle_memo(job: WorkflowJob):
    data = job.event.get_json_body() or {}
    print("MEMO:", data.get("memo"))

smee.register_workflow("memo", handle_memo)

# Run the smee client with embedded receiver
with smee:
    smee.send_test_event({"memo": {"name": "ex", "content": "hello from SmeeMe"}})
    # Events automatically logged to events.jsonl AND forwarded to your target
    # ...your app logic...
```

### Tee Mode Behavior

SmeeMe now operates in **always-on tee mode** by default:

1. **Embedded receiver** intercepts all webhook events
2. **Automatic logging** to JSONL file (if configured)  
3. **Event forwarding** to your original target URL
4. **Queue processing** for registered workflows
5. **Metrics tracking** for all event flows

```python
# All events flow through: smee.io → embedded receiver → [log] → [forward] → [queue]
status = smee.get_status()
metrics = smee.get_metrics()

print(f"Events received: {status.events_received}")
print(f"Events logged: {metrics.events_logged}")
print(f"Events forwarded: {metrics.events_forwarded_to_target}")
print(f"Receiver port: {status.receiver_port}")
```

---

## Quickstart (FastAPI)

Use the convenience factory, or attach to an existing `FastAPI` app:

```python
from fastapi import FastAPI
from smeeme import create_development_app, attach_smee, SmeeConfig

# 1) one-liner dev server with embedded receiver
app = create_development_app("https://smee.io/<your-channel>")

# 2) or wire it up manually
app = FastAPI(title="My Webhook Server")
config = SmeeConfig(
    url="https://smee.io/<your-channel>",
    target="http://localhost:8000/webhook",
    enable_queue=True,
    event_log_path="webhooks.jsonl",  # automatic event logging
)
attach_smee(app, config, add_routes=True, routes_prefix="/smee")
```

**Routes provided (when `add_routes=True`):**

- `GET /smee/status` – current runner status (PID, uptime, receiver port, queue size, success rate)  
- `GET /smee/metrics` – counters for events received, logged, forwarded, and queue processing
- `POST /smee/test` – send a test event into the channel  
- `POST /smee/restart` – restart the runner

The embedded receiver exposes:
- `POST /webhook` – webhook endpoint (or custom path via config)
- `GET /health` – receiver health check

---

## CLI

Enhanced CLI with embedded receiver support.

### Common commands

```bash
# start a tunnel with embedded receiver and event logging
smeeme start https://smee.io/<channel> http://localhost:8000/webhook \
  --event-log-path events.jsonl --listen-port 3001

# send a test event to your channel
smeeme test https://smee.io/<channel>

# show configuration including receiver settings
smeeme config --validate
```

### New embedded receiver options

- `--embedded-receiver` / `--no-embedded-receiver` (default: enabled)
- `--listen-host <host>` (default: 127.0.0.1)
- `--listen-port <port>` (default: 0 for auto-assignment)
- `--event-log-path <path>` (optional JSONL event logging)

---

## Configuration

### New embedded receiver fields

- **Embedded receiver**: `embedded_receiver` (default: `True`), `listen_host`, `listen_port`
- **Event logging**: `event_log_path` for automatic JSONL audit trail
- **Tee mode**: Always enabled when `embedded_receiver=True` and `target` is set

### Environment variable mapping (prefix `SMEEME_`)

New variables:
- `EMBEDDED_RECEIVER` = `true|false`
- `LISTEN_HOST` = receiver bind host
- `LISTEN_PORT` = receiver port (0=auto)  
- `EVENT_LOG_PATH` = path to JSONL event log

Existing variables: `URL`, `TARGET`, `PATH`, `CLIENT_MODE`, `ENABLE_QUEUE`, etc.

**Validation rules:**

- `listen_port` must be in range `0-65535`
- `event_log_path` directory must be writable
- Standard smee.io URL and target validations

Helper builders now include event logging:

```python
# Development with event logging
config = create_dev_config(
    url="https://smee.io/<channel>",
    target="http://localhost:8000/webhook",
    enable_queue=True,
    event_log_path="dev-events.jsonl"
)

# Production with Redis queue and event auditing  
config = create_production_config(
    url="https://smee.io/<channel>",
    target="http://localhost:8000/webhook",
    redis_url="redis://localhost:6379",
    workers=5,
    event_log_path="/var/log/webhooks.jsonl"
)
```

---

## Events, Workflows & Queue

### Enhanced event model

`SmeeEvent` now includes:
- `receiver_port` – port that received the event
- `forwarded` – whether successfully forwarded to target
- `error` – forwarding error message if failed

### Tee mode processing flow

1. **Receive** – embedded receiver gets webhook from smee-client
2. **Log** – append to JSONL file (if `event_log_path` configured)
3. **Forward** – send to original `target` URL (maintains headers/body)
4. **Queue** – enqueue for workflow processing (if `enable_queue=True`)
5. **Handlers** – call registered event handlers

### Enhanced metrics

`SmeeMetrics` now tracks:
- `events_logged` – events written to JSONL
- `events_forwarded_to_target` – successful forwards to original target
- All existing queue and processing metrics

---

## Runner

### Embedded receiver lifecycle

`SmeeMe` now manages both the smee-client subprocess and embedded HTTP receiver:

- **Start** – launches HTTP server, then points smee-client at embedded receiver
- **Tee mode** – all events flow through receiver for logging/processing/forwarding
- **Stop** – gracefully shuts down both HTTP server and smee process
- **Status** – includes receiver port and running state

```python
status = smee.get_status()
print(f"Receiver running: {status.receiver_running}")
print(f"Receiver port: {status.receiver_port}")
print(f"Events logged: {smee.get_metrics().events_logged}")
```

### Event flow architecture

```
smee.io channel
      ↓
smee-client subprocess  
      ↓
embedded HTTP receiver (always-on)
      ↓
[JSONL logging] → [target forwarding] → [queue processing] → [event handlers]
```

---

## Error Handling

Enhanced error handling includes receiver-specific exceptions:

- **SmeeStartError** – HTTP receiver failed to start or bind port
- **SmeeConfigError** – invalid receiver configuration (port, log path, etc.)
- Existing workflow and queue error handling maintained

---

## Project Metadata

- Python ≥ 3.11
- New embedded receiver functionality with minimal dependencies (stdlib `http.server`)
- Optional extras: `fastapi`, `redis`, `cli`, and convenience `all`
- License: MIT

---

## FAQ

**Do events still go to my target URL?**  
Yes! Tee mode forwards all events to your original target while also logging/processing them.

**Can I disable the embedded receiver?**  
Set `embedded_receiver=False` to use direct forwarding like before (loses event logging/interception).

**Where are events logged?**  
Set `event_log_path` to enable JSONL logging. Each line contains a complete `SmeeEvent` as JSON.

**Can I customize the webhook path?**  
Use the `path` config option - embedded receiver serves webhooks at `/webhook` by default or your custom path.

**What if my target is down?**  
Events are still logged and queued for processing. Forwarding errors are captured in event metadata.

**Can I run multiple instances?**  
Yes - use `listen_port=0` for auto port assignment, or specify different ports for each instance.
