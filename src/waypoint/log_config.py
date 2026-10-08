"""Logging: readable in dev, single-line JSON in production.

Request id, tenant and run id are attached via a ContextVar so every log line
emitted during a request carries them without being threaded through calls.
"""

import contextvars
import datetime as dt
import json
import logging
import logging.config
from typing import Any, override

# A ContextVar default is shared by every context, so it must not be mutable:
# None means "nothing bound", and every bind installs a fresh dict.
_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "log_context", default=None
)

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def bind(**values: Any) -> contextvars.Token[dict[str, Any] | None]:
    """Attach key/values to every log record emitted in this context."""
    return _context.set({**(_context.get() or {}), **values})


def unbind(token: contextvars.Token[dict[str, Any] | None]) -> None:
    _context.reset(token)


def context() -> dict[str, Any]:
    return dict(_context.get() or {})


class ContextFilter(logging.Filter):
    @override
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in (_context.get() or {}).items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    @override
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        payload |= {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(*, level: str = "INFO", json_output: bool = False) -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"context": {"()": ContextFilter}},
            "formatters": {
                "json": {"()": JsonFormatter},
                "console": {
                    "format": "%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
                    "datefmt": "%H:%M:%S",
                },
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "json" if json_output else "console",
                    "filters": ["context"],
                }
            },
            "root": {"handlers": ["default"], "level": level},
            "loggers": {
                "httpx": {"level": "WARNING"},
                "httpcore": {"level": "WARNING"},
                "psycopg.pool": {"level": "WARNING"},
                "asyncio": {"level": "WARNING"},
            },
        }
    )
