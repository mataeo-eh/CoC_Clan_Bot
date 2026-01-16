from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
import os
from dotenv import load_dotenv

load_dotenv()
try:
    LOG_NAME = os.getenv("LOG_NAME", "coc_bot")
    if LOG_NAME is None:
        raise ValueError("LOG_NAME environment variable not set")
    LOG_DIRECTORY = Path(os.getenv("LOG_DIRECTORY", "/data/logs"))
    if LOG_DIRECTORY is None:
        raise ValueError("LOG_DIRECTORY environment variable not set")
    LOG_RETENTION_DAYS = (os.getenv("LOG_RETENTION_DAYS", 7))
    if LOG_RETENTION_DAYS is None:
        raise ValueError("LOG_RETENTION_DAYS environment variable not set")
except Exception as e:
    print(f"Error loading logging configuration: {e}")
    raise

_logger = logging.getLogger(LOG_NAME)
_command_counters: Counter[str] = Counter()
_command_metadata: Dict[str, Dict[str, Optional[datetime]]] = {}
_user_counters: Counter[int] = Counter()
_command_user_counters: Dict[str, Counter[int]] = defaultdict(Counter)


def setup_logger() -> logging.Logger:
    """Configure the shared logger for the bot."""
    if _logger.handlers:
        # Already configured.
        return _logger

    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    _prune_old_logs()
    timestamp = datetime.now().strftime("%Y_%m_%d_%H%M%S")
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


def _prune_old_logs(retention_days: int = LOG_RETENTION_DAYS) -> None:
    """Remove log files older than the retention window."""
    cutoff = datetime.now() - timedelta(days=retention_days)
    for log_file in LOG_DIRECTORY.glob("COCbotlogfile_*.log"):
        try:
            created_at = datetime.fromtimestamp(log_file.stat().st_ctime)
        except FileNotFoundError:
            continue
        if created_at < cutoff:
            log_file.unlink(missing_ok=True)


def log_command_call(command_name: str, *, user_id: Optional[int] = None) -> None:
    """Track how many times a slash command has been executed.

    Parameters:
        command_name (str, required): Canonical name of the command.
        user_id (Optional[int], optional): Discord user identifier to aggregate anonymised usage statistics.
    """
    _command_counters[command_name] += 1
    metadata = _command_metadata.setdefault(
        command_name,
        {"count": 0, "last_invoked": None, "first_invoked": datetime.utcnow()},
    )
    metadata["count"] = metadata.get("count", 0) + 1
    metadata["last_invoked"] = datetime.utcnow()
    if user_id is not None:
        _user_counters[user_id] += 1
        _command_user_counters[command_name][user_id] += 1
    _logger.info(
        "Command %s invoked (%d total)",
        command_name,
        _command_counters[command_name],
    )


def get_command_count(command_name: str) -> int:
    """Return the current invocation count for a command."""
    return _command_counters[command_name]


def get_command_stats() -> Dict[str, Dict[str, Optional[datetime]]]:
    """Return metadata about recorded command executions."""
    return _command_metadata.copy()


def get_usage_summary(limit: int = 5) -> Dict[str, Any]:
    """Return aggregate usage statistics suitable for help analytics.

    Parameters:
        limit (int, optional): Maximum number of top results to surface for commands and anonymous user counts.
    """
    command_stats = {
        name: {
            "count": data.get("count", 0),
            "first_invoked": data.get("first_invoked"),
            "last_invoked": data.get("last_invoked"),
        }
        for name, data in _command_metadata.items()
    }
    total_invocations = sum(stat["count"] for stat in command_stats.values())
    top_commands: List[Dict[str, Any]] = []
    for name, stat in sorted(
        command_stats.items(),
        key=lambda item: item[1]["count"],
        reverse=True,
    )[:limit]:
        top_commands.append(
            {
                "name": name,
                "count": stat["count"],
                "last_invoked": stat["last_invoked"],
            }
        )

    anonymous_user_counts = sorted(_user_counters.values(), reverse=True)[:limit]

    return {
        "total_invocations": total_invocations,
        "unique_users": len(_user_counters),
        "top_commands": top_commands,
        "top_user_counts": anonymous_user_counts,
        "average_per_user": (total_invocations / len(_user_counters)) if _user_counters else 0.0,
    }
