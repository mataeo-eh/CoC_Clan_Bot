# CoC_Clan_Bot

A Discord bot that connects to the Clash of Clans API to surface clan and war data inside servers. I rely on ChatGPT for research, prototyping ideas, and generating reference documentation while iterating on the code.

## Project Overview (tracked files only)

- `main.py` – Primary bot entry point. Logs into Discord and the CoC API, manages guild clan configuration, and exposes the `/clan_war_info` slash command with selectable data sections.
- `COC_API.py` – Thin wrapper around `coc.Client` that handles authentication, guild-specific clan storage, and higher-level helpers for fetching player/war details.
- `Discord_command_groups.py` – Stand-alone bot harness showcasing the `/clan war_info` command group with auto-complete clan selection and opt-in result categories.
- `README.md` – Project description, file summaries, and credits.

For ignored assets (e.g., documentation drafts, configuration notes), consult the `ENV/` directory directly in the workspace—they are purposefully excluded from version control. Current companions include a slash-command pattern catalog and a Clash of Clans API command cheat sheet to guide ongoing development.
