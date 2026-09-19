"""
Slash commands for interacting with the ghost directly:

- /ask <question>      - ask Cassy something, get a sharp, in-character answer
- /watch <user>        - she starts quietly keeping an eye on that member
- /invention           - request the next unrevealed fragment of her old work
- /mood                - (admin-only) peek at the ghost's current mood
- /interact <who>      - call out to Mordy or Finley for a brief exchange
"""

import json
import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("caldrin.commands")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LORE_PATH = DATA_DIR / "lore.json"

WATCH_DURATION_SECONDS = 60 * 60 * 6  # 6 hours

OTHER_GHOST_1_NAME = os.getenv("OTHER_GHOST_1_NAME", "Mordy Velmora")
OTHER_GHOST_2_NAME = os.getenv("OTHER_GHOST_2_NAME", "Finley Veyren")

# Must match the constant of the same name in cogs/haunting.py.
INTERACT_MARKER = "​"


def _load_lore():
    try:
        with open(LORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log.exception("Failed to load lore.json")
        return []


class GhostCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lore = _load_lore()

    def _personality(self):
        return self.bot.get_cog("Personality")

    @app_commands.command(name="ask", description="Ask the Caldrin ghost of Velmora a question.")
    @app_commands.describe(question="What do you want to ask her?")
    async def ask(self, interaction: discord.Interaction, question: str):
        personality = self._personality()
        if not personality:
            await interaction.response.send_message(
                "No one's answering right now.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        asker = str(interaction.user.display_name)
        memory_hint = None
        prior = personality.memories_about(asker, limit=1)
        if prior:
            memory_hint = prior[0]

        cue = (
            f'{asker} asks you directly: "{question}". Answer as yourself - warm, genuinely engaged with '
            "whatever they actually asked, and glad they came to you with it."
        )
        line = await personality.speak(cue, memory_hint=memory_hint, max_tokens=250)

        embed = discord.Embed(
            description=line,
            color=discord.Color.dark_teal(),
        )
        embed.set_author(name=f"{asker} asks into the quiet...")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="watch", description="Ask Cassy to keep an eye on a specific member for a while.")
    @app_commands.describe(user="Who should she look out for?")
    async def watch(self, interaction: discord.Interaction, user: discord.Member):
        personality = self._personality()
        if not personality:
            await interaction.response.send_message(
                "No response right now.", ephemeral=True
            )
            return

        if user.bot:
            await interaction.response.send_message(
                "She has no interest in watching the hollow ones.", ephemeral=True
            )
            return

        personality.set_haunt_target(user.id, WATCH_DURATION_SECONDS)

        cue = (
            f"You've just been asked to keep an eye on {user.display_name} specifically, for a while. "
            "Announce it in character - warm and glad to do it, like someone volunteering to look out for "
            "a friend, not ominous."
        )
        line = await personality.speak(cue, max_tokens=150)

        embed = discord.Embed(
            description=line,
            color=discord.Color.dark_gold(),
        )
        embed.set_footer(text=f"{user.display_name} is being watched.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="invention", description="Ask Cassy to share one of her old inventions or patents.")
    async def invention(self, interaction: discord.Interaction):
        personality = self._personality()
        if not personality:
            await interaction.response.send_message(
                "That story stays untold tonight.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        fragment = personality.next_lore_fragment(self.lore)
        if fragment is None:
            line = await personality.speak(
                "Someone has asked you to share another one of your inventions, but you've already run "
                "through everything you're ready to talk about tonight. Deflect fast and a little "
                "restlessly, in character - not a refusal, more like 'ask me tomorrow' - without "
                "explaining that you've run out of material.",
                max_tokens=120,
            )
            embed = discord.Embed(description=line, color=discord.Color.dark_grey())
            await interaction.followup.send(embed=embed)
            return

        cue = (
            f'Tell whoever\'s listening about this one, in your own voice, not verbatim but true to it: '
            f'"{fragment}"'
        )
        line = await personality.speak(cue, max_tokens=200)

        embed = discord.Embed(
            title="From the patent files...",
            description=line,
            color=discord.Color.teal(),
        )
        remaining = len(self.lore) - personality.state.get("lore_index", 0)
        embed.set_footer(text=f"{remaining} invention(s) left unmentioned.")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="mood", description="(admin) Peek at the ghost's current mood.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def mood(self, interaction: discord.Interaction):
        personality = self._personality()
        if not personality:
            await interaction.response.send_message("No mood to report.", ephemeral=True)
            return

        from cogs.personality import GHOST_NAME

        await interaction.response.send_message(
            f"{GHOST_NAME}'s current mood: `{personality.current_mood()}`", ephemeral=True
        )

    @mood.error
    async def mood_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need permission for this one.",
                ephemeral=True,
            )
        else:
            log.exception("Unhandled error in /mood", exc_info=error)

    @app_commands.command(name="interact", description="Call out to Mordy or Finley for a brief exchange.")
    @app_commands.describe(who="Which ghost do you want her to call out to?")
    @app_commands.choices(who=[
        app_commands.Choice(name="Mordy Velmora", value="mordy"),
        app_commands.Choice(name="Finley Veyren", value="finley"),
    ])
    async def interact_command(self, interaction: discord.Interaction, who: app_commands.Choice[str] = None):
        personality = self._personality()
        haunting = self.bot.get_cog("Haunting")
        if not personality or not haunting:
            await interaction.response.send_message("No answer comes.", ephemeral=True)
            return

        target_name = OTHER_GHOST_1_NAME if (who and who.value == "mordy") else OTHER_GHOST_2_NAME
        if who is None:
            import random as _random
            target_name = _random.choice([OTHER_GHOST_1_NAME, OTHER_GHOST_2_NAME])

        channel_id = interaction.channel_id
        haunting.exchange_turns[channel_id] = {"total": 1, "last_at": time.time()}

        await interaction.response.defer(thinking=True)

        cue = (
            f"Call out, in character, to {target_name}, one of the two other spirits who share this place "
            "with you - address them directly, in front of everyone, inviting a response."
        )
        line = await personality.speak(cue, max_tokens=150)
        await interaction.delete_original_response()
        await interaction.channel.send(line + INTERACT_MARKER)


async def setup(bot: commands.Bot):
    await bot.add_cog(GhostCommands(bot))
