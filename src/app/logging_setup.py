"""Structured JSON logging to stdout.

Log event names, identifiers, status and timing. Never log request bodies, query strings,
secrets or personal data.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _AppHandler(logging.StreamHandler[TextIO]):
    """Marker type so repeated configuration replaces only our own handler."""


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, _AppHandler):
            root.removeHandler(handler)
    handler = _AppHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    # Uvicorn's access log records raw paths and query strings; the request middleware
    # writes a safer per-request line instead.
    logging.getLogger("uvicorn.access").disabled = True
