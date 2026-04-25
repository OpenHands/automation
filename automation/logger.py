"""Centralized logging configuration for the automation service.

Follows the same JSON structured-logging convention used by data_platform/logger.py:
- JSON output via python-json-logger for production / Google Cloud
- Configurable via LogSettings in automation/config.py
- ``severity`` field for GCP Cloud Logging compatibility
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

from pythonjsonlogger.json import JsonFormatter

from automation.config import get_log_settings


# Load settings once at module level
_log_settings = get_log_settings()

LOG_JSON = _log_settings.log_json
LOG_LEVEL = _log_settings.log_level.upper()
AUTOMATION_LOG_LEVEL = _log_settings.automation_log_level.upper()  # type: ignore[union-attr]

FILE_PREFIX = 'File "'
CWD_PREFIX = FILE_PREFIX + str(Path(os.getcwd()).parent) + "/"
_pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
SITE_PACKAGES_PREFIX = CWD_PREFIX + f".venv/lib/python{_pyver}/site-packages/"
LOG_JSON_FOR_CONSOLE = _log_settings.log_json_for_console


def format_stack(stack: str) -> list[str]:
    return (
        stack.replace(SITE_PACKAGES_PREFIX, FILE_PREFIX)
        .replace(CWD_PREFIX, FILE_PREFIX)
        .replace('"', "'")
        .split("\n")
    )


def custom_json_serializer(obj, **kwargs):
    if LOG_JSON_FOR_CONSOLE:
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


def setup_json_logger(
    logger: logging.Logger,
    level: str = LOG_LEVEL,
    _out: TextIO = sys.stdout,
) -> None:
    """Configure *logger* to emit JSON for Google Cloud."""

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    handler = logging.StreamHandler(_out)
    handler.setLevel(level)

    formatter = JsonFormatter(
        "{message}{levelname}",
        style="{",
        rename_fields={"levelname": "severity"},
        json_serializer=custom_json_serializer,
    )

    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(level)


def setup_all_loggers() -> None:
    """Apply JSON logging to the root logger and every logger registered so far."""
    if LOG_JSON:
        setup_json_logger(logging.getLogger())

        for name in logging.root.manager.loggerDict:
            _logger = logging.getLogger(name)
            setup_json_logger(_logger)
            _logger.propagate = False


automation_logger = logging.getLogger("automation")
setup_all_loggers()
setup_json_logger(automation_logger, level=AUTOMATION_LOG_LEVEL)
