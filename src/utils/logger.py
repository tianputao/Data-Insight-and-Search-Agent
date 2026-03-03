"""
Logging utility module for the Agentic RAG application.
Provides centralized logging configuration and utilities.

All application loggers share a single TimedRotatingFileHandler attached to the
root logger so that every module — including uvicorn and third-party libraries —
writes to the same date-stamped log file under logs/.
"""

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from typing import Optional

from ..config import AppConfig

# ── One-time root-level file handler setup ────────────────────────────────────

_file_handler_installed: bool = False


def _install_root_file_handler() -> None:
    """
    Attach a single TimedRotatingFileHandler to the ROOT logger so that ALL
    loggers (application code, uvicorn, fastapi, etc.) write to the daily file.
    Current file:  logs/application_YYYYMMDD.log  (date-stamped)
    Rotated files: logs/application_YYYYMMDD.log  (next date)
    Called once on first `get_logger()` invocation.
    """
    global _file_handler_installed
    if _file_handler_installed:
        return

    from datetime import datetime as _dt
    from pathlib import Path as _Path

    AppConfig.LOG_DIR.mkdir(parents=True, exist_ok=True)

    today_str = _dt.now().strftime("%Y%m%d")
    log_path = AppConfig.LOG_DIR / f"application_{today_str}.log"

    file_handler = TimedRotatingFileHandler(
        filename=str(log_path),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=False,
    )

    # Custom namer: rotated file → application_YYYYMMDD.log (new date)
    def _namer(default_name: str) -> str:
        p = _Path(default_name)
        # default_name ends with .YYYY-MM-DD appended by TimedRotatingFileHandler
        date_suffix = p.suffix.lstrip(".")          # e.g. "2026-02-25"
        date_compact = date_suffix.replace("-", "")  # "20260225"
        return str(AppConfig.LOG_DIR / f"application_{date_compact}.log")

    file_handler.namer = _namer
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.addHandler(file_handler)
    _file_handler_installed = True


# ── Public API ────────────────────────────────────────────────────────────────

def setup_logger(
    name: str,
    log_level: Optional[str] = None,
    log_file: Optional[str] = None,   # kept for backward-compat, no longer used
) -> logging.Logger:
    """
    Set up a named logger with console output.
    All loggers automatically inherit the root file handler.
    """
    _install_root_file_handler()

    logger = logging.getLogger(name)

    level_str = log_level or AppConfig.LOG_LEVEL
    level = getattr(logging, level_str.upper(), logging.INFO)
    logger.setLevel(level)

    # Ensure console handler is present (only once)
    has_console = any(isinstance(h, logging.StreamHandler) and h.stream is sys.stdout
                      for h in logger.handlers)
    if not has_console:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(console)

    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Get or create a logger with the default configuration.

    Args:
        name: Logger name (typically __name__ from calling module)

    Returns:
        Logger instance
    """
    return setup_logger(name)


class LoggerMixin:
    """
    Mixin class to add logging capability to any class.
    Usage: class MyClass(LoggerMixin): ...
    Then access via self.logger
    """
    
    @property
    def logger(self) -> logging.Logger:
        """Get logger for this class."""
        if not hasattr(self, '_logger'):
            self._logger = get_logger(self.__class__.__name__)
        return self._logger
