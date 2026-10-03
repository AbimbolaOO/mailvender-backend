"""Structured JSON logging.

Every log line carries the request ID, authenticated user ID and account ID when
known (set by middleware / auth dependencies through context variables), the
event name, and any extra fields such as `message_id`.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
account_id_var: ContextVar[str | None] = ContextVar("account_id", default=None)

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": getattr(record, "event", None) or record.getMessage(),
            "request_id": request_id_var.get(),
            "user_id": user_id_var.get(),
            "account_id": account_id_var.get(),
        }
        if record.getMessage() != entry["event"]:
            entry["message"] = record.getMessage()
        for key, value in vars(record).items():
            if key not in _RESERVED and key != "event" and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps({k: v for k, v in entry.items() if v is not None}, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Uvicorn's access log duplicates our request log.
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"mailvender.{name}")


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: object) -> None:
    """Logs `event` with structured fields, e.g. log_event(log, "message.queued", message_id=...)."""
    logger.log(level, event, extra={"event": event, **fields})
