"""Centralized logging configuration for the automation service.

Follows the same JSON structured-logging convention used by data_platform/logger.py:
- JSON output via python-json-logger for production / Google Cloud
- Configurable via LogSettings in automation/config.py
- ``severity`` field for GCP Cloud Logging compatibility

Note: Logging configuration is read at module load time for performance. Changes to
log settings after import require calling setup_all_loggers() to take effect.
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

from pythonjsonlogger.json import JsonFormatter

from automation.config import get_config

FILE_PREFIX = 'File "'
CWD_PREFIX = FILE_PREFIX + str(Path(os.getcwd()).parent) + "/"
_pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
SITE_PACKAGES_PREFIX = CWD_PREFIX + f".venv/lib/python{_pyver}/site-packages/"


def _get_log_settings():
    """Get current log settings from config."""
    return get_config().log


def format_stack(stack: str) -> list[str]:
    return (
        stack.replace(SITE_PACKAGES_PREFIX, FILE_PREFIX)
        .replace(CWD_PREFIX, FILE_PREFIX)
        .replace('"', "'")
        .split("\n")
    )


def _make_json_serializer(log_json_for_console: bool):
    """Create a JSON serializer with the given console formatting setting."""

    def custom_json_serializer(obj, **kwargs):
        if log_json_for_console:
            kwargs["indent"] = 2
            obj = {"ts": datetime.now().isoformat(), **obj}

            if isinstance(obj, dict):
                exc_info = obj.get("exc_info")
                if isinstance(exc_info, str):
                    obj["exc_info"] = format_stack(exc_info)
                stack_info = obj.get("stack_info")
                if isinstance(stack_info, str):
                    obj["stack_info"] = format_stack(stack_info)

        return json.dumps(obj, **kwargs)

    return custom_json_serializer


def setup_json_logger(
    logger: logging.Logger,
    level: str | None = None,
    _out: TextIO = sys.stdout,
) -> None:
    """Configure *logger* to emit JSON for Google Cloud."""
    log_settings = _get_log_settings()
    if level is None:
        level = log_settings.effective_log_level.upper()

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    handler = logging.StreamHandler(_out)
    handler.setLevel(level)

    formatter = JsonFormatter(
        "{message}{levelname}",
        style="{",
        rename_fields={"levelname": "severity"},
        json_serializer=_make_json_serializer(log_settings.log_json_for_console),
    )

    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(level)


def setup_all_loggers() -> None:
    """Apply JSON logging to the root logger and every logger registered so far.

    Call this after changing log settings to apply the new configuration.
    """
    log_settings = _get_log_settings()
    if log_settings.log_json:
        setup_json_logger(logging.getLogger())

        for name in logging.root.manager.loggerDict:
            _logger = logging.getLogger(name)
            setup_json_logger(_logger)
            _logger.propagate = False


automation_logger = logging.getLogger("automation")
setup_all_loggers()
# Set automation logger to its specific level
_init_settings = _get_log_settings()
setup_json_logger(
    automation_logger, level=_init_settings.effective_automation_log_level.upper()
)
