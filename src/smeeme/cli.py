#!/usr/bin/env python3
"""Typer-based CLI for SmeeMe.

- Uses Enum for choice-like options (Typer/Click friendly)
- Root & subcommands: no_args_is_help=True
- Config = env (via load_config_from_env) + CLI overrides
"""

from __future__ import annotations

import logging
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Optional

import typer

from .config import SmeeConfig, load_config_from_env
from .exceptions import SmeeError
from .runner import SmeeMe

# --------------------------- enums (choices) ---------------------------


class ClientMode(str, Enum):
    auto = "auto"
    smee = "smee"
    npx = "npx"


class QueueBackend(str, Enum):
    memory = "memory"
    redis = "redis"


# --------------------------- app ---------------------------

app = typer.Typer(
    name="smeeme",
    help="Wrapper around smee.io client with embedded receiver and optional workflow queue",
    no_args_is_help=True,
    add_completion=False,
)

# --------------------------- helpers ---------------------------


def _setup_logging(verbose: int) -> None:
    levels = [logging.WARNING, logging.INFO, logging.DEBUG]
    level = levels[min(verbose, len(levels) - 1)]
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _enum_val(x):
    return getattr(x, "value", x)


def _build_config_from_options(
    *,
    url: Optional[str],
    target: Optional[str],
    path: Optional[str],
    client_mode: Optional[ClientMode],
    start_timeout: Optional[float],
    embedded_receiver: Optional[bool],
    listen_host: Optional[str],
    listen_port: Optional[int],
    event_log_path: Optional[Path],
    enable_queue: Optional[bool],
    queue_backend: Optional[QueueBackend],
    queue_workers: Optional[int],
    redis_url: Optional[str],
) -> SmeeConfig:
    # Load from env (tolerate partial env), then apply CLI overrides.
    try:
        cfg = load_config_from_env(validate=False)  # type: ignore[arg-type]
    except Exception:
        # Default config with empty target (embedded receiver mode)
        cfg = SmeeConfig(url="", target="")

    # Pydantic v2 (model_dump) / v1 (dict) compatibility
    dump = cfg.model_dump() if hasattr(cfg, "model_dump") else cfg.dict()

    def set_if(key: str, value):
        if value is not None:
            dump[key] = _enum_val(value)

    set_if("url", url)
    set_if("target", target or "")  # Default to empty string
    set_if("path", path)
    set_if("client_mode", client_mode)
    if start_timeout is not None:
        dump["start_timeout_s"] = start_timeout
    if embedded_receiver is not None:
        dump["embedded_receiver"] = embedded_receiver
    set_if("listen_host", listen_host)
    set_if("listen_port", listen_port)
    set_if("event_log_path", event_log_path)
    if enable_queue is not None:
        dump["enable_queue"] = enable_queue
    set_if("queue_backend", queue_backend)
    set_if("queue_workers", queue_workers)
    set_if("redis_url", redis_url)

    return SmeeConfig(**dump)


def _print_cfg_summary(cfg: SmeeConfig) -> None:
    channel = getattr(cfg, "channel_id", None) or getattr(cfg, "url", "")
    eff_target = getattr(cfg, "effective_target", None) or getattr(cfg, "target", "")
    typer.echo(f"Channel: {channel}")
    typer.echo(f"Target:  {eff_target}")

    # Show embedded receiver info
    if getattr(cfg, "embedded_receiver", True):
        host = getattr(cfg, "listen_host", "127.0.0.1")
        port = getattr(cfg, "listen_port", 0)
        port_info = "auto" if port == 0 else str(port)
        typer.echo(f"Receiver: {host}:{port_info}")

        log_path = getattr(cfg, "event_log_path", None)
        if log_path:
            typer.echo(f"Event log: {log_path}")

    if getattr(cfg, "enable_queue", False):
        qb = getattr(cfg, "queue_backend", None)
        qb_val = getattr(qb, "value", qb)
        workers = getattr(cfg, "queue_workers", None)
        typer.echo(f"Queue:   {qb_val} ({workers} workers)")


# --------------------------- commands ---------------------------


@app.command(no_args_is_help=True)
def start(
    url: Optional[str] = typer.Option(None, "--url", help="smee.io channel URL"),
    target: Optional[str] = typer.Option(None, "--target", help="Target webhook URL"),
    path: Optional[str] = typer.Option(None, "--path", help="Path appended to target"),
    client_mode: Optional[ClientMode] = typer.Option(None, "--client-mode", help="auto|smee|npx"),
    start_timeout: Optional[float] = typer.Option(None, "--start-timeout", help="Startup timeout (seconds)"),
    embedded_receiver: Optional[bool] = typer.Option(
        None, "--embedded-receiver/--no-embedded-receiver", help="Enable embedded HTTP receiver"
    ),
    listen_host: Optional[str] = typer.Option(None, "--listen-host", help="Embedded receiver host"),
    listen_port: Optional[int] = typer.Option(None, "--listen-port", help="Embedded receiver port (0=auto)"),
    event_log_path: Optional[Path] = typer.Option(None, "--event-log-path", help="Path to log events as JSONL"),
    enable_queue: Optional[bool] = typer.Option(None, "--enable-queue/--no-enable-queue", help="Enable workflow queue"),
    queue_backend: Optional[QueueBackend] = typer.Option(None, "--queue-backend", help="memory|redis"),
    queue_workers: Optional[int] = typer.Option(None, "--queue-workers", help="Number of queue workers"),
    redis_url: Optional[str] = typer.Option(None, "--redis-url", help="Redis URL (if queue-backend=redis)"),
    verbose: int = typer.Option(0, "--verbose", "-v", count=True, help="Increase verbosity"),
):
    """Start the Smee client with embedded receiver and block until Ctrl+C."""
    _setup_logging(verbose)
    cfg = _build_config_from_options(
        url=url,
        target=target,
        path=path,
        client_mode=client_mode,
        start_timeout=start_timeout,
        embedded_receiver=embedded_receiver,
        listen_host=listen_host,
        listen_port=listen_port,
        event_log_path=event_log_path,
        enable_queue=enable_queue,
        queue_backend=queue_backend,
        queue_workers=queue_workers,
        redis_url=redis_url,
    )

    if not getattr(cfg, "url", ""):
        typer.secho("URL is required (SMEEME_URL or --url).", fg=typer.colors.RED)
        raise typer.Exit(1)

    # Target only required if embedded receiver is disabled
    if not getattr(cfg, "embedded_receiver", True) and not getattr(cfg, "target", ""):
        typer.secho("Target is required when embedded receiver is disabled.", fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.echo("Starting SmeeMe…")
    _print_cfg_summary(cfg)

    try:
        with SmeeMe(cfg, verbose_logging=verbose > 0) as smee:
            typer.echo("SmeeMe running. Press Ctrl+C to stop.")
            last = 0
            while True:
                time.sleep(1.0)
                now = int(time.time())
                if now // 30 != last // 30:
                    last = now
                    st = smee.get_status()
                    recv = getattr(st, "events_received", None) or getattr(st, "received", 0)
                    fwd = getattr(st, "events_forwarded", None) or getattr(st, "forwarded", 0)
                    up = int(getattr(st, "uptime_seconds", 0))
                    port = getattr(st, "receiver_port", None)
                    port_info = f" receiver_port={port}" if port else ""
                    typer.echo(f"Status: received={recv} forwarded={fwd} uptime_s={up}{port_info}")
    except KeyboardInterrupt:
        typer.echo("\nStopping SmeeMe…")
    except SmeeError as e:
        typer.secho(f"Error: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)
    except Exception as e:
        typer.secho(f"Unexpected error: {e}", fg=typer.colors.RED)
        raise typer.Exit(2)


@app.command(no_args_is_help=True)
def test(
    url: Optional[str] = typer.Option(None, "--url", help="smee.io channel URL"),
    target: Optional[str] = typer.Option(None, "--target", help="Target webhook URL"),
    path: Optional[str] = typer.Option(None, "--path", help="Path appended to target"),
    client_mode: Optional[ClientMode] = typer.Option(None, "--client-mode", help="auto|smee|npx"),
    start_timeout: Optional[float] = typer.Option(None, "--start-timeout", help="Startup timeout (seconds)"),
    embedded_receiver: Optional[bool] = typer.Option(
        None, "--embedded-receiver/--no-embedded-receiver", help="Enable embedded HTTP receiver"
    ),
    listen_host: Optional[str] = typer.Option(None, "--listen-host", help="Embedded receiver host"),
    listen_port: Optional[int] = typer.Option(None, "--listen-port", help="Embedded receiver port (0=auto)"),
    event_log_path: Optional[Path] = typer.Option(None, "--event-log-path", help="Path to log events as JSONL"),
    enable_queue: Optional[bool] = typer.Option(None, "--enable-queue/--no-enable-queue", help="Enable workflow queue"),
    queue_backend: Optional[QueueBackend] = typer.Option(None, "--queue-backend", help="memory|redis"),
    queue_workers: Optional[int] = typer.Option(None, "--queue-workers", help="Number of queue workers"),
    redis_url: Optional[str] = typer.Option(None, "--redis-url", help="Redis URL (if queue-backend=redis)"),
    verbose: int = typer.Option(0, "--verbose", "-v", count=True, help="Increase verbosity"),
):
    """Send a test event to the channel."""
    _setup_logging(verbose)
    cfg = _build_config_from_options(
        url=url,
        target=target,
        path=path,
        client_mode=client_mode,
        start_timeout=start_timeout,
        embedded_receiver=embedded_receiver,
        listen_host=listen_host,
        listen_port=listen_port,
        event_log_path=event_log_path,
        enable_queue=enable_queue,
        queue_backend=queue_backend,
        queue_workers=queue_workers,
        redis_url=redis_url,
    )

    if not getattr(cfg, "url", ""):
        typer.secho("URL is required (SMEEME_URL or --url).", fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.echo("Sending test event…")
    try:
        smee = SmeeMe(cfg)
        ok = smee.send_test_event()
        if ok:
            typer.secho("✓ Test event sent", fg=typer.colors.GREEN)
            raise typer.Exit(0)
        else:
            typer.secho("Test event failed", fg=typer.colors.RED)
            raise typer.Exit(1)
    except SmeeError as e:
        typer.secho(f"Error: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)


@app.command(no_args_is_help=True)
def status(
    url: Optional[str] = typer.Option(None, "--url", help="smee.io channel URL"),
    target: Optional[str] = typer.Option(None, "--target", help="Target webhook URL"),
    path: Optional[str] = typer.Option(None, "--path", help="Path appended to target"),
    embedded_receiver: Optional[bool] = typer.Option(
        None, "--embedded-receiver/--no-embedded-receiver", help="Enable embedded HTTP receiver"
    ),
    event_log_path: Optional[Path] = typer.Option(None, "--event-log-path", help="Path to log events as JSONL"),
    verbose: int = typer.Option(0, "--verbose", "-v", count=True, help="Increase verbosity"),
):
    """Show effective configuration (local-only status)."""
    _setup_logging(verbose)
    cfg = _build_config_from_options(
        url=url,
        target=target,
        path=path,
        client_mode=None,
        start_timeout=None,
        embedded_receiver=embedded_receiver,
        listen_host=None,
        listen_port=None,
        event_log_path=event_log_path,
        enable_queue=None,
        queue_backend=None,
        queue_workers=None,
        redis_url=None,
    )

    typer.echo("Effective configuration:")
    typer.echo("-" * 40)
    dump = cfg.model_dump() if hasattr(cfg, "model_dump") else cfg.dict()
    for k, v in dump.items():
        typer.echo(f"{k}: {v}")


@app.command(no_args_is_help=True)
def config(
    validate: bool = typer.Option(False, "--validate", help="Validate configuration and runtime"),
    url: Optional[str] = typer.Option(None, "--url", help="smee.io channel URL"),
    target: Optional[str] = typer.Option(None, "--target", help="Target webhook URL"),
    path: Optional[str] = typer.Option(None, "--path", help="Path appended to target"),
    client_mode: Optional[ClientMode] = typer.Option(None, "--client-mode", help="auto|smee|npx"),
    start_timeout: Optional[float] = typer.Option(None, "--start-timeout", help="Startup timeout (seconds)"),
    embedded_receiver: Optional[bool] = typer.Option(
        None, "--embedded-receiver/--no-embedded-receiver", help="Enable embedded HTTP receiver"
    ),
    listen_host: Optional[str] = typer.Option(None, "--listen-host", help="Embedded receiver host"),
    listen_port: Optional[int] = typer.Option(None, "--listen-port", help="Embedded receiver port (0=auto)"),
    event_log_path: Optional[Path] = typer.Option(None, "--event-log-path", help="Path to log events as JSONL"),
    enable_queue: Optional[bool] = typer.Option(None, "--enable-queue/--no-enable-queue", help="Enable workflow queue"),
    queue_backend: Optional[QueueBackend] = typer.Option(None, "--queue-backend", help="memory|redis"),
    queue_workers: Optional[int] = typer.Option(None, "--queue-workers", help="Number of queue workers"),
    redis_url: Optional[str] = typer.Option(None, "--redis-url", help="Redis URL (if queue-backend=redis)"),
    verbose: int = typer.Option(0, "--verbose", "-v", count=True, help="Increase verbosity"),
):
    """Show effective configuration; optionally validate."""
    _setup_logging(verbose)
    cfg = _build_config_from_options(
        url=url,
        target=target,
        path=path,
        client_mode=client_mode,
        start_timeout=start_timeout,
        embedded_receiver=embedded_receiver,
        listen_host=listen_host,
        listen_port=listen_port,
        event_log_path=event_log_path,
        enable_queue=enable_queue,
        queue_backend=queue_backend,
        queue_workers=queue_workers,
        redis_url=redis_url,
    )

    typer.echo("Current Configuration")
    typer.echo("=" * 40)
    dump = cfg.model_dump() if hasattr(cfg, "model_dump") else cfg.dict()
    for k, v in dump.items():
        typer.echo(f"{k}: {v}")

    if validate:
        typer.echo("\nValidating configuration…")
        try:
            from .config import ConfigValidator  # optional helper

            ConfigValidator.validate_config(cfg)
            typer.secho("✓ Configuration is valid", fg=typer.colors.GREEN)
            warnings = []
            if hasattr(ConfigValidator, "check_runtime_requirements"):
                warnings = ConfigValidator.check_runtime_requirements(cfg)
            if warnings:
                typer.echo("\nWarnings:")
                for w in warnings:
                    typer.secho(f"  • {w}", fg=typer.colors.YELLOW)
        except Exception as e:
            typer.secho(f"✗ Validation failed: {e}", fg=typer.colors.RED)
            raise typer.Exit(1)


# --------------------------- entrypoint ---------------------------


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main() or 0)
