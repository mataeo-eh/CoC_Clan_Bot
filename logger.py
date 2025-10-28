from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional


LOG_NAME = "coc_bot"
LOG_DIRECTORY = Path("logs")
_logger = logging.getLogger(LOG_NAME)
_command_counters: Counter[str] = Counter()


def setup_logger() -> logging.Logger:
    """Configure the shared logger for the bot."""
    if _logger.handlers:
        # Already configured.
        return _logger

    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIRECTORY / f"COCbotlogfile_{timestamp}.log"

    _logger.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.ERROR)
    console_handler.setFormatter(formatter)

    _logger.addHandler(file_handler)
    _logger.addHandler(console_handler)
    _logger.debug("Logger initialised with file %s", log_file)
    return _logger


def get_logger() -> logging.Logger:
    """Return the shared logger instance."""
    return _logger


def log_command_call(command_name: str) -> None:
    """Track how many times a slash command has been executed."""
    _command_counters[command_name] += 1
    _logger.info(
        "Command %s invoked (%d total)",
        command_name,
        _command_counters[command_name],
    )


def get_command_count(command_name: str) -> int:
    """Return the current invocation count for a command."""
    return _command_counters[command_name]
