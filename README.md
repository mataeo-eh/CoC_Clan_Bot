# CoC_Clan_Bot

A Discord bot that keeps Clash of Clans data at your fingertips: look up wars, assign targets, steer alerts, and explore player profiles without leaving your server. I rely on ChatGPT for research, prototyping ideas, and producing reference material while iterating on the code.

## Highlights

- **Single source of truth** – configure clans once with `/set_clan`, link their alerts, and let the war loop broadcast timed reminders automatically.
- **Guided workflows** – every interactive command explains what to do after you press enter and provides buttons or dropdowns to finish the job.
- **Player intelligence** – link Discord members to their Clash accounts and surface player stats instantly with `/player_info`.
- **Safety first** – duplicate-tag protection, permission checks, and clear logging make it easy to understand what the bot is doing.

## Project Overview (tracked files)

- `bot_core.py` – Centralises shared Discord state (`bot`, `client`, intents) so multiple modules can register commands without spinning up duplicate clients.
- `main.py` – Entry point that logs into Discord/CoC, synchronises slash commands, and imports the command catalogue.
- `Discord_Commands.py` – Production command suite (help, clan configuration, alert routing, player linking, war viewers, base assignments, role helpers, and alert toggles) plus the background war-alert loop.
- `COC_API.py` – Thin wrapper around `coc.Client` for login, guild configuration, player snapshots, and war helpers.
- `logger.py` – Shared logging utility writing DEBUG-level files to `logs/` while only surfacing errors on the console.
- `ENV/Clan_Configs.py` – JSON-backed storage helpers for `clans`, `player_tags`, and `player_accounts`, keeping backward compatibility with earlier layouts.
- `ENV/notify_codex_complete.applescript` – macOS helper the automation uses to notify me when a TODO run finishes.
- `Discord_command_groups.py` – Experimental harness for alternative command grouping patterns.
- `README.md` – This detailed guide.

Everything under `ENV/` besides the files listed above (e.g., keys, documentation notes, generated configs) stays private and is ignored in the public repository.

## Command Reference

Each command behaves the same way: fill in any required options, press **Enter** to send the slash command, and then follow the menus or buttons that appear.

### `/help`
- **What it does:** Sends a short reminder of what the bot can do and links back to this README.
- **How to use:** Run the command anywhere; it always answers ephemerally so you can revisit the documentation link without spamming the channel.

### `/set_clan`
- **Purpose:** Register or update a clan name and tag for the server and choose whether war alerts should be enabled.
- **After sending:** If a conflicting tag already exists, the bot prompts you with a replace/keep choice. Success messages recap the tag, whether alerts are enabled, and suggest linking an alert channel.
- **Permissions:** Administrators only.

### `/choose_war_alert_channel`
- **Purpose:** Decide which text channel should receive time-based war alerts for a specific clan.
- **After sending:** Step 1 – choose a category; Step 2 – pick the channel (use the 🔍 filter if there are tons of channels); Step 3 – confirm. The stored channel is used until you change it again.
- **Permissions:** Administrators only.

### `/link_player`
- **Purpose:** Link or unlink Clash of Clans player tags to Discord members so `/player_info` autocomplete stays fast.
- **Options:** `action` (`link` or `unlink`), `player_tag`, optional `alias`, optional `target_member`.
- **Rules:** Non-admins can only manage their own links; admins can manage anyone. Tags are validated against the Clash API, aliases fall back to the in-game name, and multiple tags per Discord user are supported.
- **After sending:** You get a confirmation showing who the tag belongs to; use `/player_info` right away to see the linked data.

### `/clan_war_info_menu`
- **Purpose:** Pull the current (or most recent) war data for a configured clan and explore it interactively.
- **After sending:** A dropdown appears—select the data points you want (attacks, countdowns, rosters, etc.). Use the **Broadcast** button to share the current selection or the **Private Copy** button to keep it for yourself.

### `/player_info`
- **Purpose:** View detailed player stats (heroes, troops, donations, achievements, and more).
- **Options:** `player_reference` accepts a full tag (e.g., `#ABC123`), a saved alias, or a linked Discord member name.
- **After sending:** The same menu-and-buttons pattern as the war view lets you choose the sections you care about and decide whether to share or keep them private.

### `/assign_bases`
- **Purpose:** Share per-player base assignments or broadcast a general battle plan during an active war.
- **After sending:** You’re given two buttons:
  1. **Per Player Assignments** – pick a home base from the dropdown, enter one or two enemy base numbers when prompted, repeat as needed, then hit **Post Assignments** to broadcast the summary (the bot adds the alert-role mention automatically).
  2. **General Assignment Rule** – type any free-form instruction (for example, “Everyone attack your mirror”) and the bot posts it with the usual alert-role mention.
- **Permissions:** Administrators only.

### `/assign_clan_role`
- **Purpose:** Let members assign the appropriate clan role to themselves.
- **After sending:** Pick a clan from the dropdown, then choose whether the confirmation should be broadcast or private.

### `/toggle_war_alerts`
- **Purpose:** Opt in or out of the alert role so members control whether they get pinged when alerts fire.
- **Usage:** Choose **True** to receive alerts or **False** to opt out; the command explains whether the role was added or removed.

### Command Workflow Reminder
Whichever command you choose, remember the pattern:
1. Fill in the slash command’s options.
2. Press **Enter** to run it.
3. Use the dropdowns, buttons, or modals that appear to finish the workflow.

## War Alert Automation

The background loop (defined in `Discord_Commands.py`) checks every tracked clan every five minutes. It sends alerts when:

- A war is about to start (1 hour, 5 minutes) or has just begun (5 minutes after the start).
- A war is winding down (12 hours, 1 hour, 5 minutes) or just concluded (final score roundup).

Alerts respect the per-clan channel set via `/choose_war_alert_channel`; if the bot loses send permissions for that channel, it skips the alerts until you pick a new destination.

## Logging and Support Files

- The logger writes detailed DEBUG files under `logs/` while only surfacing errors on stdout to keep noisy output away from the console.
- `git_commands.md` (ignored in the public repo) is used in my private workflow to capture commit/push snippets once a TODO run finishes.
- When automation completes a TODO pass, it runs `ENV/notify_codex_complete.applescript` so I get a macOS notification.

If you have questions, open the README (via `/help`) or inspect the source—docstrings and inline comments explain the nuts and bolts of each flow. The goal is that an interested teenager—or anyone curious, regardless of technical background—can follow these instructions and get the most out of the bot. Happy raiding!
