"""structlog JSON lines to logs/backend.log, rotating 5 x 5 MB (SDD 15.3).

Never log file contents or search/chat queries: only ids, sizes, timings and error codes.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

_handlers: list[logging.Handler] = []

_SHARED = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
]


def setup_logging(logs_dir: Path, level: str = "INFO", console: bool = True) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "backend.log"

    structlog.configure(
        processors=_SHARED + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    json_fmt = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )

    root = logging.getLogger()
    teardown_logging()

    fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=4, encoding="utf-8")
    fh.setFormatter(json_fmt)
    root.addHandler(fh)
    _handlers.append(fh)

    if console:
        console_fmt = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_SHARED,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(colors=False),
            ],
        )
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(console_fmt)
        root.addHandler(ch)
        _handlers.append(ch)

    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "watchdog", "lancedb", "multipart", "PIL", "RapidOCR"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").disabled = True  # access logs contain query strings
    return log_file


def teardown_logging() -> None:
    root = logging.getLogger()
    while _handlers:
        h = _handlers.pop()
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
