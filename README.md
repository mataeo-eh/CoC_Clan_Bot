# CoC Clan Bot

CoC Clan Bot is a Discord bot for Clash of Clans servers. It centralizes war coordination, player lookups, upgrade planning, donation reporting, alert-role management, and recurring clan reports through Discord slash commands and interactive views.

## Current Project Progress

- The core runtime is in place: `main.py` logs into Discord and the Clash of Clans API, syncs slash commands, and starts the background loops.
- Persistent server configuration is implemented in `Clan_Configs.py`, including clan records, alert settings, player links, event roles, report schedules, upgrade logs, and war-alert de-duplication state.
- The main command surface is implemented in `Discord_Commands.py` and currently exposes **37 slash commands**.
- War automation is live in code: a background loop checks configured clans every 5 minutes and sends milestone alerts to the selected channel.
- Scheduled reporting is live in code: a separate loop checks stored schedules every minute and posts dashboards, donation summaries, or season summaries when due.
- AI-assisted command help is implemented through `LLM_Usage.py` with session-based follow-up support.
- Command usage analytics are implemented in `logger.py` so admins can inspect aggregate bot usage through `/help_usage`.

## Current Feature Areas

- **Clan setup and alert routing**: register clans, choose alert channels, and let members opt into war pings.
- **War coordination**: interactive war info views, war plan templates, reusable war nudges, and assignment workflows.
- **Player/account management**: link Clash player tags to Discord members and fetch interactive player info views.
- **Upgrade and donation tracking**: log upgrades, configure summary channels, and generate donation leaderboards.
- **Dashboards and recurring reports**: configure module-based dashboards, schedule automated posts, and publish season summaries.
- **Role utilities and onboarding**: self-assign clan roles, opt into event roles, and onboard members with a guided registration command.
- **Built-in help tooling**: static help commands plus a session-based AI help flow for follow-up questions.

## Project Structure

- `main.py` - Entry point that logs into Discord/CoC, syncs slash commands, and starts the background loops.
- `bot_core.py` - Shared Discord bot/client state and environment-backed runtime configuration.
- `Discord_Commands.py` - Main slash command implementation, interactive views, and background automation.
- `COC_API.py` - Clash of Clans API wrapper for player, clan, war, and CWL data retrieval.
- `Clan_Configs.py` - JSON-backed configuration storage and schema normalization.
- `LLM_Usage.py` - Session-based AI help system for explaining bot commands and workflows.
- `logger.py` - Shared logging and aggregate command-usage tracking.
- `Scripts/Generate_Command_Index.py` - Utility script that rebuilds `command_index.json` from registered commands.
- `command_index.json` - Generated command index for quick command inventory/reference.
- `Dockerfile` / `railpack.json` - Container and deployment configuration.

## Typical Workflow

Most interactive commands follow the same pattern:

1. Run the slash command and fill in any required options.
2. Submit the command in Discord.
3. Use the follow-up dropdowns, buttons, or modals to finish the workflow.

That pattern is used throughout the bot for war views, dashboards, player linking, war plans, schedules, and role selection.

## Background Automation

### War alert loop

The war alert loop polls configured clans every 5 minutes and sends time-based alerts for key war milestones. Alerts respect the clan's configured alert channel and the member opt-in role.

### Scheduled report loop

The report scheduler wakes up every minute, checks saved schedules, and posts due reports. Supported scheduled report types in the current codebase are:

- dashboards
- donation summaries
- season summaries

## Complete Slash Command List

### Help and documentation

- `/help` - Show the main bot help message and README link.
- `/help_war_info` - Explain the interactive war info workflow.
- `/help_assign_bases` - Explain how the base-assignment flow works.
- `/help_plan_upgrade` - Explain how upgrade planning works.
- `/help_dashboard` - Explain dashboard configuration and posting.
- `/help_schedule_report` - Explain recurring report scheduling.
- `/help_usage` - Show aggregate command-usage analytics for admins.
- `/help_from_ai` - Start or continue an AI help session for command questions.
- `/help_from_ai_end_session` - End the current AI help session and clear its context.

### Clan setup and alert routing

- `/set_clan` - Add or update a clan configured for the Discord server.
- `/choose_war_alert_channel` - Select which channel receives war alerts for a clan.
- `/toggle_war_alerts` - Opt in or out of the war alert role.

### War coordination

- `/clan_war_info_menu` - Open the interactive war info view for a clan.
- `/assign_bases` - Assign war targets or broadcast a general assignment rule.
- `/configure_war_nudge` - Configure reusable war nudge reasons and mention targets.
- `/war_nudge` - Send a configured war nudge for a clan.
- `/save_war_plan` - Create or update a reusable war plan template.
- `/list_war_plans` - List saved war plan templates for a clan.
- `/war_plan` - Post a saved war plan through the interactive poster flow.

### Player accounts and upgrades

- `/link_player` - Link or unlink Clash of Clans player tags to Discord members.
- `/player_info` - Open the interactive player info view for a saved alias, member, or tag.
- `/plan_upgrade` - Log a planned upgrade for a linked account.
- `/set_upgrade_channel` - Set the default channel for upgrade-plan posts.

### Dashboards, season reports, and scheduling

- `/configure_dashboard` - Configure saved dashboard modules, output format, and default destination.
- `/dashboard` - Post the configured dashboard for a clan.
- `/set_season_summary_channel` - Set the default channel for season summaries.
- `/season_summary` - Generate a season summary for a clan.
- `/schedule_report` - Create or update a scheduled report.
- `/list_schedules` - List saved schedules for the server or a specific clan.
- `/cancel_schedule` - Remove a scheduled report by ID.

### Donations

- `/configure_donation_metrics` - Configure which donation metrics are tracked for a clan.
- `/set_donation_channel` - Set the default channel for donation summaries.
- `/donation_summary` - Generate a donation leaderboard and summary for a clan.

### Roles and onboarding

- `/assign_clan_role` - Self-assign a clan role from the configured clan list.
- `/configure_event_role` - Configure opt-in roles for events like Clan Games or Raid Weekend.
- `/event_alert_opt` - Opt into or out of a configured event alert role.
- `/register_me` - Guided onboarding flow for new members.

## Notes

- The bot stores configuration as JSON and normalizes older config formats into the current schema automatically.
- The current command set is generated from `Discord_Commands.py`; if commands change, `Scripts/Generate_Command_Index.py` should be rerun so `command_index.json` stays in sync.
