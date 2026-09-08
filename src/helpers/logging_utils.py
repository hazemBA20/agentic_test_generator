"""Small JSON logger for workflow observability.

Structured logs are emitted separately from existing CLI output so adding
observability does not change the user-facing command output.
"""

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOGGER_NAME = "agentic_test_generator"
_configured = False


def configure_logging() -> None:
    """Configure one JSON-lines handler for the current process."""
    global _configured
    if _configured:
        return

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logger.propagate = False

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(_JsonFormatter())
    logger.addHandler(stream_handler)

    log_path = os.getenv("LOG_FILE")
    if log_path:
        file_handler = logging.FileHandler(Path(log_path), encoding="utf-8")
        file_handler.setFormatter(_JsonFormatter())
        logger.addHandler(file_handler)

    _configured = True


def new_run_id() -> str:
    return str(uuid.uuid4())


def log_event(event: str, *, run_id: str | None = None, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured event while excluding sensitive request data by design."""
    configure_logging()
    payload = {"event": event, **fields}
    if run_id:
        payload["run_id"] = run_id
    logging.getLogger(LOGGER_NAME).log(level, payload)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = record.msg if isinstance(record.msg, dict) else {"message": record.getMessage()}
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            **fields,
        }
        return json.dumps(payload, default=str, ensure_ascii=False)
