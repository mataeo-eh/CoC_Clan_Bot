# CoC_Clan_Bot

A Discord bot that connects to the Clash of Clans API to surface clan and war data inside servers. I rely on ChatGPT for research, prototyping ideas, and generating reference documentation while iterating on the code.

## Project Overview (tracked files only)

- `bot_core.py` – Centralises shared Discord state (`bot`, `client`, intents) so multiple modules can register commands without instantiating duplicate bots.
- `main.py` – Entry point that logs into Discord/CoC, synchronises commands, and imports the slash command catalogue.
- `Discord_Commands.py` – Production command suite featuring `/set_clan` (with duplicate-tag protection and alert prompts), `/clan_war_info_menu` and `/player_info` (interactive select menus with broadcast/private buttons), `/choose_war_alert_channel` for per-clan delivery, `/assign_clan_role`, `/toggle_war_alerts`, and `/assign_bases`, all backed by the hourly war-alert loop with refined timing guards.
- `COC_API.py` – Thin wrapper around `coc.Client` for login, guild configuration, and higher-level war/player helpers, including structured player snapshots for the new profile command.
- `logger.py` – Central logging utility that writes per-run DEBUG logs to `logs/COCbotlogfile_<timestamp>.log`, mirrors errors to the console, and tracks slash-command invocation counts.
- `ENV/Clan_Configs.py` – JSON-backed storage utilities for clan/player tags using the unified `clans -> {tag, alerts{enabled, channel_id}}` schema plus helper routines for forward/backward compatibility.
- `ENV/notify_codex_complete.applescript` – macOS notification helper executed at the end of a TODO run to announce completion.
- `Discord_command_groups.py` – Stand-alone experimental harness demonstrating alternative command grouping patterns.
- `README.md` – Project description, file summaries, and credits.

For ignored assets (e.g., documentation drafts, configuration notes), consult the `ENV/` directory directly in the workspace—they are purposefully excluded from version control. Current companions include a slash-command pattern catalog and a Clash of Clans API command cheat sheet to guide ongoing development.
