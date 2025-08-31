"""Minimal SmeeMe usage example.

Run with environment variables:
  SMEEME_URL=https://smee.io/<your-channel>
  SMEEME_TARGET=http://127.0.0.1:8000/webhook
"""
from __future__ import annotations

import os
import time

try:
    # Public API as exposed by src/smeeme/__init__.py
    from smeeme import SmeeMe, create_dev_config, WorkflowJob  # type: ignore
except Exception as e:  # pragma: no cover
    raise SystemExit(f"Unable to import 'smeeme' package: {e}") from e


def handle_memo(job: "WorkflowJob") -> None:
    data = getattr(job, "event", None)
    payload = {}
    if data is not None and hasattr(data, "get_json_body"):
        payload = data.get_json_body() or {}
    memo = payload.get("memo") or {}
    print(f"[workflow:memo] name={memo.get('name')} content={memo.get('content')}")


def main() -> None:
    url = os.environ.get("SMEEME_URL", "https://smee.io/CHANGE_ME")
    target = os.environ.get("SMEEME_TARGET", "http://127.0.0.1:8000/webhook")

    # Queue isn't required, but enables the workflow demo
    cfg = create_dev_config(url=url, target=target, enable_queue=True)  # type: ignore
    smee = SmeeMe(cfg)  # type: ignore

    smee.register_workflow("memo", handle_memo)  # type: ignore

    # Start the underlying smee client, send a test event, then print status/metrics
    with smee:
        smee.send_test_event({"memo": {"name": "example", "content": "hello from examples/quickstart"}})  # type: ignore
        time.sleep(1.5)
        status = smee.get_status()  # type: ignore
        metrics = smee.get_metrics()  # type: ignore
        try:
            print("STATUS:", status.model_dump())
            print("METRICS:", metrics.model_dump())
        except Exception:
            # If models aren't pydantic v2, fall back to repr
            print("STATUS:", status)
            print("METRICS:", metrics)


if __name__ == "__main__":
    main()
