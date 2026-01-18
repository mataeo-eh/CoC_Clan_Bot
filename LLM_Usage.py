from __future__ import annotations
import sys
from pathlib import Path
from xmlrpc import client

# Add project root to path for direct script execution (VSCode run button)
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastmcp import FastMCP
from datetime import datetime
import argparse
import json
import os
from dataclasses import dataclass, asdict
from typing import List, Dict, Any
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv()  # reads .env into environment
import requests



# Example of discord slash command for structure reference
'''
# ---------------------------------------------------------------------------
# Slash command: /set_clan
# ---------------------------------------------------------------------------
@bot.tree.command(name="set_clan", description="Manage the clans configured for this server.")
@app_commands.describe(
    clan_name="Optional clan to load when opening the editor.",
)
async def set_clan(
    interaction: discord.Interaction,
    clan_name: Optional[str] = None,
) -> None:
    """Launch the interactive clan manager for this server."""
    _record_command_usage(interaction, "set_clan")
    log.debug("set_clan invoked clan=%s", clan_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "You need the Administrator permission to configure this command.",
            ephemeral=True,
        )
        return

    clan_map = _clan_names_for_guild(interaction.guild.id)
    selected_clan = clan_name if isinstance(clan_name, str) and clan_name in clan_map else None

    view = SetClanView(
        guild=interaction.guild,
        selected_clan=selected_clan,
        actor=interaction.user,
    )

    await interaction.response.send_message(
        view.render_message(),
        ephemeral=True,
        view=view,
    )
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException as exc:
        log.warning("Failed to capture set_clan view message: %s", exc)
'''

def get_client_from_env() -> OpenAI:
    """
    Create and return an OpenAI client configured to use the LiteLLM endpoint.

    Reads configuration from environment variables:
      - OPENAI_API_KEY: your LiteLLM virtual key (required)
      - OPENAI_BASE_URL: base URL for LiteLLM
      - OPENAI_MODEL: default model name to use for evaluations
    Raises:
      RuntimeError: if any required environment variable is missing.
    Returns:
      - An instance of OpenAI client configured for OpenRouter
      - The model name to use for evaluations
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY environment variable.")

    base_url = os.getenv("OPENROUTER_BASE_URL")
    if not base_url:
        raise RuntimeError("Missing API endpoint URL environment variable.")

    # You may want to validate that OPENAI_MODEL is set as well
    model = os.getenv("OPENROUTER_MODEL")
    if not model:
        raise RuntimeError("Missing OPENAI_MODEL environment variable.")

    print(f"[config] Using model: {model}")
    print(f"[config] Using base URL: {base_url}")

    client = OpenAI(base_url=base_url, api_key=api_key)
    return client, model

def debug_LLM_call(client, model):
    completion = client.chat.completions.create(
        extra_headers={
            "HTTP-Referer": "<YOUR_SITE_URL>", # Optional. Site URL for rankings on openrouter.ai.
            "X-Title": "THE_GRADE", # Optional. Site title for rankings on openrouter.ai.
        },
        extra_body={
            "reasoning": {
                "effort": "medium",
            }
        },
        model=model,
        messages=[
            {
            "role": "user",
            "content": "Hello, can you help me with a math problem?"
            }
        ]
    )
    print(completion.choices[0].message.content)


def main():
    client, model = get_client_from_env()
    debug_LLM_call(client, model)

if __name__ == "__main__":
    main()