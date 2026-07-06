"""Central logging setup: JSON lines to stdout, optional rotating file sink.

``setup_logging()`` is idempotent and configures the ``cce`` logger hierarchy (stdlib logging, no
extra dependency). Every record becomes one JSON object per line with ``timestamp``, ``level``,
``logger``, ``message`` and any contextual fields passed via ``logger.info(..., extra={...})``.

The file sink is opt-in via ``CCE_LOG_FILE_ENABLED=true`` (rotating, size-capped) so local CLI use
stays console-only by default.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

from cce.config import Settings, get_settings

#: LogRecord attributes that are *not* user-supplied context (used to extract ``extra`` fields).
_STANDARD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
        "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
        "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
        "message", "asctime",
    }
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line; ``extra={...}`` fields are merged in at the top level."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging(settings: Settings | None = None) -> None:
    """Configure the ``cce`` logger tree (console always; rotating file when enabled)."""
    settings = settings or get_settings()
    root = logging.getLogger("cce")
    if getattr(root, "_cce_configured", False):
        return

    root.setLevel(settings.log_level.upper())
    root.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(JsonFormatter())
    root.addHandler(console)

    if settings.log_file_enabled:
        log_path = Path(settings.log_file_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=settings.log_file_max_bytes,
            backupCount=settings.log_file_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    root._cce_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    """Logger under the ``cce`` hierarchy (``get_logger('api')`` -> ``cce.api``)."""
    return logging.getLogger(name if name.startswith("cce") else f"cce.{name}")
