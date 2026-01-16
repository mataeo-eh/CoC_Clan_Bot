import json
import copy
from pathlib import Path
from typing import Any, Dict, List, Optional


CONFIG_PATH = Path("/data/clan_configs.json")

# Default scaffold mirrors the new schema.
_DEFAULT_CONFIG: Dict[int, Dict[str, Any]] = {}
MAX_UPGRADE_LOG_ENTRIES = 250


def _deep_copy_config(config: Dict[int, Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    return copy.deepcopy(config)


def _ensure_clan_entry(clan_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a single clan entry to the expected schema."""
    alerts = data.get("alerts", {}) if isinstance(data.get("alerts", {}), dict) else {}
    war_plans = data.get("war_plans", {}) if isinstance(data.get("war_plans", {}), dict) else {}
    war_nudge = data.get("war_nudge", {}) if isinstance(data.get("war_nudge", {}), dict) else {}
    donation_tracking = (
        data.get("donation_tracking", {})
        if isinstance(data.get("donation_tracking", {}), dict)
        else {}
    )
    season_summary = (
        data.get("season_summary", {})
        if isinstance(data.get("season_summary", {}), dict)
        else {}
    )
    dashboard = data.get("dashboard", {}) if isinstance(data.get("dashboard", {}), dict) else {}

    modules = dashboard.get("modules") if isinstance(dashboard.get("modules"), list) else ["war_overview"]
    if not modules:
        modules = ["war_overview"]
    dashboard_format = dashboard.get("format", "embed")
    if dashboard_format not in {"embed", "csv", "both"}:
        dashboard_format = "embed"

    return {
        "tag": data.get("tag", ""),
        "alerts": {
            "enabled": bool(alerts.get("enabled", True)),
            "channel_id": alerts.get("channel_id"),
        },
        "war_plans": war_plans,
        "war_nudge": {
            "reasons": war_nudge.get("reasons", [])
            if isinstance(war_nudge.get("reasons", []), list)
            else []
        },
        "dashboard": {
            "modules": modules,
            "format": dashboard_format,
            "channel_id": dashboard.get("channel_id"),
        },
        "donation_tracking": {
            "metrics": {
                "top_donors": bool(
                    donation_tracking.get("metrics", {}).get("top_donors", True)
                ),
                "low_donors": bool(
                    donation_tracking.get("metrics", {}).get("low_donors", False)
                ),
                "negative_balance": bool(
                    donation_tracking.get("metrics", {}).get("negative_balance", False)
                ),
            },
            "channel_id": donation_tracking.get("channel_id"),
        },
        "season_summary": {
            "channel_id": season_summary.get("channel_id"),
        },
    }


def _normalise_player_accounts(raw_accounts: Any) -> Dict[str, List[Dict[str, Optional[str]]]]:
    """Coerce stored player account mappings into the expected structure."""
    normalised: Dict[str, List[Dict[str, Optional[str]]]] = {}
    if not isinstance(raw_accounts, dict):
        return normalised

    for user_id, records in raw_accounts.items():
        key = str(user_id)
        entries: List[Dict[str, Optional[str]]] = []

        if isinstance(records, list):
            source_iterable = records
        elif isinstance(records, dict):
            # Legacy style alias -> tag mapping.
            source_iterable = [
                {"alias": alias, "tag": tag} for alias, tag in records.items()
            ]
        else:
            continue

        for record in source_iterable:
            if isinstance(record, dict):
                tag = record.get("tag")
                if not isinstance(tag, str) or not tag.strip():
                    continue
                alias = record.get("alias")
                entries.append(
                    {
                        "tag": tag.strip().upper(),
                        "alias": alias.strip() if isinstance(alias, str) and alias.strip() else None,
                    }
                )
            elif isinstance(record, str) and record.strip():
                entries.append({"tag": record.strip().upper(), "alias": None})

        if entries:
            normalised[key] = entries

    return normalised


def _normalise_event_roles(raw_roles: Any) -> Dict[str, Optional[int]]:
    """Ensure event role mappings are stored as a mapping of event keys to role IDs."""
    if not isinstance(raw_roles, dict):
        return {
            "clan_games": None,
            "raid_weekend": None,
        }

    result: Dict[str, Optional[int]] = {
        "clan_games": None,
        "raid_weekend": None,
    }
    for key in result.keys():
        value = raw_roles.get(key)
        if isinstance(value, int):
            result[key] = value
    return result


def _normalise_schedules(raw_schedules: Any) -> List[Dict[str, Any]]:
    """Normalise stored report schedules."""
    normalised: List[Dict[str, Any]] = []
    if not isinstance(raw_schedules, list):
        return normalised

    for entry in raw_schedules:
        if not isinstance(entry, dict):
            continue
        schedule = {
            "id": entry.get("id"),
            "type": entry.get("type", "dashboard"),
            "clan_name": entry.get("clan_name", ""),
            "frequency": entry.get("frequency", "daily"),
            "time_utc": entry.get("time_utc", "00:00"),
            "weekday": entry.get("weekday"),
            "channel_id": entry.get("channel_id"),
            "next_run": entry.get("next_run"),
            "options": entry.get("options", {}),
        }
        normalised.append(schedule)
    return normalised


def _normalise_upgrade_log(raw_log: Any) -> List[Dict[str, Any]]:
    """Normalise stored upgrade log entries."""
    if not isinstance(raw_log, list):
        return []

    normalised: List[Dict[str, Any]] = []
    for record in raw_log[-MAX_UPGRADE_LOG_ENTRIES:]:
        if isinstance(record, dict):
            normalised.append(record)
    return normalised


def _normalise_war_alert_state(raw_state: Any) -> Dict[str, Dict[str, List[str]]]:
    """Normalise the persisted war alert de-duplication cache."""
    if not isinstance(raw_state, dict):
        return {}

    normalised: Dict[str, Dict[str, List[str]]] = {}
    for clan_name, wars in raw_state.items():
        if not isinstance(clan_name, str) or not clan_name.strip():
            continue
        if not isinstance(wars, dict):
            continue

        clan_state: Dict[str, List[str]] = {}
        for war_tag, sent_ids in wars.items():
            if not isinstance(war_tag, str) or not war_tag.strip():
                continue
            if not isinstance(sent_ids, list):
                continue
            cleaned = [value for value in sent_ids if isinstance(value, str) and value]
            if cleaned:
                clan_state[war_tag] = cleaned

        if clan_state:
            normalised[clan_name] = clan_state

    return normalised


def _convert_legacy_entry(config: Dict[str, Any]) -> Dict[str, Any]:
    """Convert legacy {Clan tags, Enable Alert Tracking} shapes to the new schema."""
    legacy_clan_tags: Dict[str, str] = config.get("Clan tags", {})
    legacy_alerts: Dict[str, bool] = config.get("Enable Alert Tracking", {})
    clans: Dict[str, Dict[str, Any]] = {
        clan_name: _ensure_clan_entry(
            clan_name,
            {
                "tag": tag,
                "alerts": {
                    "enabled": legacy_alerts.get(clan_name, True),
                    "channel_id": None,
                },
            },
        )
        for clan_name, tag in legacy_clan_tags.items()
    }

    return {
        "clans": clans,
        "player_tags": config.get("Player tags", {}),
        "player_accounts": _normalise_player_accounts(config.get("player_accounts", {})),
        "upgrade_log": _normalise_upgrade_log(config.get("upgrade_log", [])),
        "channels": {
            "upgrade": None,
            "donation": None,
            **(
                config.get("channels", {})
                if isinstance(config.get("channels", {}), dict)
                else {}
            ),
        },
        "event_roles": _normalise_event_roles(config.get("event_roles", {})),
        "schedules": _normalise_schedules(config.get("schedules", [])),
        "war_alert_state": _normalise_war_alert_state(config.get("war_alert_state", {})),
    }


def _load_server_config() -> Dict[int, Dict[str, Any]]:
    if not CONFIG_PATH.exists():
        return _deep_copy_config(_DEFAULT_CONFIG)

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
            raw_config: Dict[str, Dict[str, Any]] = json.load(config_file)
    except (json.JSONDecodeError, OSError):
        return _deep_copy_config(_DEFAULT_CONFIG)

    loaded: Dict[int, Dict[str, Any]] = {}
    for guild_id_str, config in raw_config.items():
        guild_id = int(guild_id_str)
        if "clans" in config:
            # Already migrated; ensure structure and defaults.
            clans = {
                clan_name: _ensure_clan_entry(clan_name, clan_data)
                for clan_name, clan_data in config.get("clans", {}).items()
            }
            loaded[guild_id] = {
                "clans": clans,
                "player_tags": config.get("player_tags", {}),
                "player_accounts": _normalise_player_accounts(config.get("player_accounts", {})),
                "upgrade_log": _normalise_upgrade_log(config.get("upgrade_log", [])),
                "channels": {
                    "upgrade": None,
                    "donation": None,
                    **(
                        config.get("channels", {})
                        if isinstance(config.get("channels", {}), dict)
                        else {}
                    ),
                },
                "event_roles": _normalise_event_roles(config.get("event_roles", {})),
                "schedules": _normalise_schedules(config.get("schedules", [])),
                "war_alert_state": _normalise_war_alert_state(config.get("war_alert_state", {})),
            }
        else:
            # Legacy format; convert.
            loaded[guild_id] = _convert_legacy_entry(config)
    return loaded


def save_server_config() -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    serializable_config = {str(guild_id): config for guild_id, config in server_config.items()}
    with CONFIG_PATH.open("w", encoding="utf-8") as config_file:
        json.dump(serializable_config, config_file, indent=4)

server_config: Dict[int, Dict[str, Any]] = _load_server_config()
# Persist defaults when the file is missing or missing keys
save_server_config()
