#!/usr/bin/env python3
"""Command line interface for SmeeMe."""
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pydantic>=2.7",
#     "httpx>=0.27",
# ]
# ///

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Optional

from .config import SmeeConfig, load_config_from_env, create_dev_config
from .exceptions import SmeeError
from .runner import SmeeMe


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser."""
    parser = argparse.ArgumentParser(
        prog="smeeme",
        description="Pythonic wrapper for smee.io client with queue-based workflow processing"
    )
    
    parser.add_argument("--url", help="smee.io channel URL")
    parser.add_argument("--target", help="Target webhook URL")
    parser.add_argument("--path", help="Path for webhook forwarding")
    parser.add_argument("--client-mode", choices=["auto", "smee", "npx"], default="auto")
    parser.add_argument("--start-timeout", type=float, default=15.0, help="Startup timeout in seconds")
    parser.add_argument("--enable-queue", action="store_true", help="Enable workflow queue")
    parser.add_argument("--queue-backend", choices=["memory", "redis"], default="memory")
    parser.add_argument("--queue-workers", type=int, default=3, help="Number of queue workers")
    parser.add_argument("--redis-url", default="redis://localhost:6379", help="Redis URL")
    parser.add_argument("--verbose", "-v", action="count", default=0, help="Increase verbosity")
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Start command
    start_parser = subparsers.add_parser("start", help="Start SmeeMe client")
    start_parser.add_argument("url", nargs="?", help="smee.io channel URL")
    start_parser.add_argument("target", nargs="?", help="Target webhook URL")
    
    # Test command
    test_parser = subparsers.add_parser("test", help="Send test event")
    test_parser.add_argument("url", nargs="?", help="smee.io channel URL")
    
    # Status command
    subparsers.add_parser("status", help="Show status")
    
    # Config command
    config_parser = subparsers.add_parser("config", help="Show configuration")
    config_parser.add_argument("--validate", action="store_true", help="Validate configuration")
    
    return parser


def setup_logging(verbose: int) -> None:
    """Setup logging based on verbosity."""
    levels = [logging.WARNING, logging.INFO, logging.DEBUG]
    level = levels[min(verbose, len(levels) - 1)]
    
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    )


def load_configuration(args: argparse.Namespace) -> SmeeConfig:
    """Load configuration from environment and arguments."""
    # Start with environment
    try:
        config = load_config_from_env(validate=False)
    except Exception:
        # Fallback to defaults if env loading fails
        config = SmeeConfig(url="", target="")
    
    # Override with command line arguments
    overrides = {}
    
    if args.url:
        overrides["url"] = args.url
    if args.target:
        overrides["target"] = args.target
    if args.path:
        overrides["path"] = args.path
    if args.client_mode != "auto":
        overrides["client_mode"] = args.client_mode
    if args.start_timeout != 15.0:
        overrides["start_timeout_s"] = args.start_timeout
    if args.enable_queue:
        overrides["enable_queue"] = True
    if args.queue_backend != "memory":
        overrides["queue_backend"] = args.queue_backend
    if args.queue_workers != 3:
        overrides["queue_workers"] = args.queue_workers
    if args.redis_url != "redis://localhost:6379":
        overrides["redis_url"] = args.redis_url
    
    # Create new config with overrides
    if overrides:
        config_data = config.model_dump()
        config_data.update(overrides)
        config = SmeeConfig(**config_data)
    
    return config


def cmd_start(args: argparse.Namespace, config: SmeeConfig) -> int:
    """Start SmeeMe client."""
    # Handle positional arguments
    if args.url:
        config_data = config.model_dump()
        config_data["url"] = args.url
        if args.target:
            config_data["target"] = args.target
        config = SmeeConfig(**config_data)
    
    if not config.url or not config.target:
        print("Error: URL and target are required", file=sys.stderr)
        print("Set SMEEME_URL and SMEEME_TARGET environment variables,")
        print("or provide them as arguments: smeeme start <url> <target>")
        return 1
    
    print(f"Starting SmeeMe...")
    print(f"Channel: {config.channel_id}")
    print(f"Target: {config.effective_target}")
    if config.enable_queue:
        print(f"Queue: {config.queue_backend.value} ({config.queue_workers} workers)")
    
    try:
        with SmeeMe(config) as smee:
            print("SmeeMe running. Press Ctrl+C to stop.")
            try:
                while True:
                    time.sleep(1.0)
                    # Print status every 30 seconds
                    if int(time.time()) % 30 == 0:
                        status = smee.get_status()
                        print(f"Status: {status.events_received} events received, "
                              f"{status.events_forwarded} forwarded")
            except KeyboardInterrupt:
                print("\nStopping SmeeMe...")
    except SmeeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 2
    
    return 0


def cmd_test(args: argparse.Namespace, config: SmeeConfig) -> int:
    """Send test event."""
    if args.url:
        config_data = config.model_dump()
        config_data["url"] = args.url
        config = SmeeConfig(**config_data)
    
    if not config.url:
        print("Error: URL is required", file=sys.stderr)
        return 1
    
    print(f"Sending test event to {config.channel_id}...")
    
    try:
        # Create temporary SmeeMe instance just for testing
        smee = SmeeMe(config)
        success = smee.send_test_event()
        
        if success:
            print("Test event sent successfully!")
            return 0
        else:
            print("Test event failed", file=sys.stderr)
            return 1
            
    except SmeeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_status(args: argparse.Namespace, config: SmeeConfig) -> int:
    """Show status (placeholder - would need shared state for real status)."""
    print("Status command not yet implemented")
    print("This would require a shared state mechanism between CLI and running instance")
    return 0


def cmd_config(args: argparse.Namespace, config: SmeeConfig) -> int:
    """Show configuration."""
    print("Current Configuration:")
    print("=" * 40)
    
    config_dict = config.model_dump()
    for key, value in config_dict.items():
        print(f"{key}: {value}")
    
    if args.validate:
        print("\nValidating configuration...")
        try:
            from .config import ConfigValidator
            ConfigValidator.validate_config(config)
            print("✓ Configuration is valid")
            
            # Check runtime requirements
            warnings = ConfigValidator.check_runtime_requirements(config)
            if warnings:
                print("\nWarnings:")
                for warning in warnings:
                    print(f"⚠ {warning}")
            
        except Exception as e:
            print(f"✗ Configuration validation failed: {e}")
            return 1
    
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)
    
    # Setup logging
    setup_logging(args.verbose)
    
    # Load configuration
    try:
        config = load_configuration(args)
    except Exception as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    
    # Handle commands
    if args.command == "start" or not args.command:
        return cmd_start(args, config)
    elif args.command == "test":
        return cmd_test(args, config)
    elif args.command == "status":
        return cmd_status(args, config)
    elif args.command == "config":
        return cmd_config(args, config)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
