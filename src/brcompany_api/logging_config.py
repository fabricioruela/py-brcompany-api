"""Structured JSON logging and contextvars support for py-brcompany-api."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

SERVICE_NAME = "py-brcompany-api"

request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("api_request_id", default=None)
method_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("api_method", default=None)
path_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("api_path", default=None)
user_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("api_user_id", default=None)
auth_method_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("api_auth_method", default=None)
client_ip_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("api_client_ip", default=None)

_CONTEXT_VARS: dict[str, contextvars.ContextVar[Any]] = {
    "request_id": request_id_var,
    "method": method_var,
    "path": path_var,
    "user_id": user_id_var,
    "auth_method": auth_method_var,
    "client_ip": client_ip_var,
}

_UNSET = object()


@contextlib.contextmanager
def log_context(
    *,
    request_id: Any = _UNSET,
    method: Any = _UNSET,
    path: Any = _UNSET,
    user_id: Any = _UNSET,
    auth_method: Any = _UNSET,
    client_ip: Any = _UNSET,
) -> Iterator[None]:
    """Bind request-scoped logging fields via contextvars and reset on exit."""
    tokens: list[tuple[contextvars.ContextVar[Any], contextvars.Token[Any]]] = []
    values = {
        "request_id": request_id,
        "method": method,
        "path": path,
        "user_id": user_id,
        "auth_method": auth_method,
        "client_ip": client_ip,
    }
    try:
        for key, value in values.items():
            if value is not _UNSET:
                var = _CONTEXT_VARS[key]
                tokens.append((var, var.set(value)))
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def set_user_context(user_id: Optional[str], auth_method: Optional[str] = None) -> None:
    """Populate user_id and auth_method in the active request context."""
    if user_id is not None:
        user_id_var.set(str(user_id))
    if auth_method is not None:
        auth_method_var.set(auth_method)


def reset_user_context() -> None:
    """Clear user_id and auth_method from the active context."""
    user_id_var.set(None)
    auth_method_var.set(None)


def get_current_context() -> dict[str, Any]:
    """Return active non-None context fields."""
    ctx: dict[str, Any] = {}
    for key, var in _CONTEXT_VARS.items():
        val = var.get()
        if val is not None:
            ctx[key] = val
    return ctx


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects suitable for journald."""

    def __init__(self, service: str = SERVICE_NAME, env: str = "development") -> None:
        super().__init__()
        self.service = service
        self.env = env

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        payload: dict[str, Any] = {
            "timestamp": ts,
            "level": record.levelname,
            "service": self.service,
            "env": self.env,
            "logger": record.name,
            "message": record.getMessage(),
            "pid": record.process,
        }
        payload.update(get_current_context())

        extra_fields = getattr(record, "fields", None)
        if isinstance(extra_fields, dict):
            for key, value in extra_fields.items():
                if value is not None:
                    payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable formatter for local terminal debugging."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s [%(levelname)s] %(name)s%(ctx_suffix)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        ctx = get_current_context()
        extra_fields = getattr(record, "fields", None)
        if isinstance(extra_fields, dict):
            merged = {**ctx, **{k: v for k, v in extra_fields.items() if v is not None}}
        else:
            merged = ctx

        if merged:
            pairs = " ".join(f"{k}={v}" for k, v in merged.items())
            record.ctx_suffix = f" [{pairs}]"
        else:
            record.ctx_suffix = ""
        return super().format(record)


def configure_logging(
    level_name: Optional[str] = None,
    log_format: Optional[str] = None,
    env_name: Optional[str] = None,
) -> None:
    """Configure root and Uvicorn loggers with JSON (default) or console formatter."""
    resolved_level_name = (level_name or "INFO").strip().upper()
    level = getattr(logging, resolved_level_name, logging.INFO)
    resolved_format = (log_format or "json").strip().lower()
    resolved_env = (env_name or "development").strip().lower()

    handler = logging.StreamHandler(sys.stdout)
    if resolved_format == "console":
        handler.setFormatter(ConsoleFormatter())
    else:
        handler.setFormatter(JsonFormatter(service=SERVICE_NAME, env=resolved_env))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Route uvicorn lifecycle/error logs through our root handler,
    # and silence uvicorn.access because our middleware emits structured access logs.
    for uvicorn_logger_name in ("uvicorn", "uvicorn.error"):
        uv_logger = logging.getLogger(uvicorn_logger_name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True
