# CoC_Clan_Bot

A Discord bot that connects to the Clash of Clans API to surface clan and war data inside servers. I rely on ChatGPT for research, prototyping ideas, and generating reference documentation while iterating on the code.

## Project Overview (tracked files only)

- `bot_core.py` – Centralises shared Discord state (`bot`, `client`, intents) so multiple modules can register commands without instantiating duplicate bots.
- `main.py` – Entry point that logs into Discord/CoC, synchronises commands, and imports the slash command catalogue.
- `Discord_Commands.py` – Production command set featuring `/set_clan` (with alert toggles), an interactive `/clan_war_info_menu` (select menu + broadcast buttons), `/assign_clan_role` for self-service clan roles, `/toggle_war_alerts` for opt-in mentions, and `/assign_bases` for war target planning alongside the scheduled alert loop with refined timing guards to prevent late notifications on restart.
- `COC_API.py` – Thin wrapper around `coc.Client` for login, guild configuration, and higher-level war/player helpers (including per-clan alert preferences).
- `logger.py` – Central logging utility that writes per-run DEBUG logs to `logs/COCbotlogfile_<timestamp>.log`, mirrors errors to the console, and tracks slash-command invocation counts.
- `ENV/Clan_Configs.py` – JSON-backed storage utilities for clan/player tags and the new `Enable Alert Tracking` map.
- `Discord_command_groups.py` – Stand-alone experimental harness demonstrating alternative command grouping patterns.
- `README.md` – Project description, file summaries, and credits.

For ignored assets (e.g., documentation drafts, configuration notes), consult the `ENV/` directory directly in the workspace—they are purposefully excluded from version control. Current companions include a slash-command pattern catalog and a Clash of Clans API command cheat sheet to guide ongoing development.
