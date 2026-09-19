"""
Passive presence: the ghost noticing things without being asked.

- A background loop that drops unprompted "whispers" into a random allowed
  channel every so often.
- Keyword-triggered reactions themed around Cassy's actual personality:
  curiosity about problems, projects, and ideas, plus being called by name
  (either spelling) and general chaos-adjacent topics.
- Remembering things members say, and occasionally resurfacing an old
  memory as if the ghost had been listening the whole time.
- Extra attention on anyone currently under a /watch effect.
- A capped, on-demand exchange with EITHER of the other two ghost bots
  (Mordy Velmora or Finley Veyren), triggered by /interact - see
  cogs/commands.py for the command itself. Cassy is the only ghost with two
  possible conversation partners, so this cog tracks both independently.
"""

import logging
import os
import random
import time

import discord
from discord.ext import commands, tasks

log = logging.getLogger("caldrin.haunting")


def _parse_channel_ids(env_value: str | None):
    if not env_value:
        return None
    ids = set()
    for part in env_value.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids or None


def _int_or_none(env_value: str | None):
    return int(env_value) if env_value and env_value.isdigit() else None


# The other two ghost bots Cassy can exchange a few words with via /interact.
OTHER_GHOST_1_ID = _int_or_none(os.getenv("OTHER_GHOST_1_ID"))
OTHER_GHOST_1_NAME = os.getenv("OTHER_GHOST_1_NAME", "Mordy Velmora")
OTHER_GHOST_2_ID = _int_or_none(os.getenv("OTHER_GHOST_2_ID"))
OTHER_GHOST_2_NAME = os.getenv("OTHER_GHOST_2_NAME", "Finley Veyren")

OTHER_GHOSTS_BY_ID = {}
if OTHER_GHOST_1_ID:
    OTHER_GHOSTS_BY_ID[OTHER_GHOST_1_ID] = OTHER_GHOST_1_NAME
if OTHER_GHOST_2_ID:
    OTHER_GHOSTS_BY_ID[OTHER_GHOST_2_ID] = OTHER_GHOST_2_NAME

EXCHANGE_MAX_MESSAGES = 3
EXCHANGE_TIMEOUT_SECONDS = 300

# A trailing zero-width space, invisible in Discord, appended to every
# message that's genuinely part of an /interact exchange. Must match the
# constant of the same name in cogs/commands.py, and in Mordy's/Finley's own
# code, so the bots can tell a deliberate call-out apart from an ordinary
# whisper or keyword reaction.
INTERACT_MARKER = "​"

# Words/phrases that catch Cassy's attention. Matched as substrings,
# case-insensitively, against ordinary message content.
KEYWORD_TRIGGERS = {
    "cassy": "Someone said your name. React warmly to being noticed, by name - either spelling is fine, you never correct it.",
    "cassie": "Someone said your name. React warmly to being noticed, by name - either spelling is fine, you never correct it.",
    "homework": "Someone mentioned homework. Get curious and encouraging about it - ask what they're working on, and make it clear you'd be glad to help think it through.",
    "project": "Someone mentioned a project they're working on. Light up, ask a warm follow-up question about it, and offer to help if they want it.",
    "experiment": "Someone mentioned an experiment. This is your exact language - react with real enthusiasm, ask what they're testing, and offer to help.",
    "stuck": "Someone said they're stuck on something. Be immediately, genuinely helpful - reassure them that being stuck is normal, and offer a gentle idea or ask what part is giving them trouble.",
    "idea": "Someone mentioned an idea. Ask what it is with real enthusiasm - you love hearing what people are thinking about.",
    "invent": "Someone brought up inventing or building something. React with warm interest and encouragement - this is exactly your thing, and you'd love to help.",
    "bored": "Someone said they're bored. Cheerfully offer them something interesting to think about or do - you can always find something worth being curious about.",
    "help": "Someone mentioned needing help. Offer it immediately and warmly - this is the thing you most love being asked for.",
    "explosion": "Someone brought up an explosion, literal or figurative. React with delight and a fond mention of your own track record.",
}

WHISPER_CUES = [
    "Drop an unprompted, friendly whisper into a quiet channel - something curious you've been turning over, shared warmly with whoever's around.",
    "Ask, unprompted, whether anyone's working on anything interesting right now, and make it clear you'd love to help if they are.",
    "Offer, unprompted, to help anyone who's stuck on something - homework, a project, anything. You mean it.",
    "Comment on something you've apparently been thinking about for the last hour, out loud, in a way that invites someone to join in.",
    "Mention, briefly and gently, something small you wish you could still build, then turn it into curiosity about what other people are making.",
    "Tease one of the older ghosts fondly, in absentia, just for the fun of it - warm, never mean.",
]


class Haunting(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.allowed_channel_ids = _parse_channel_ids(os.getenv("HAUNT_CHANNEL_IDS"))
        self.whisper_min = int(os.getenv("WHISPER_MIN_MINUTES", "30"))
        self.whisper_max = int(os.getenv("WHISPER_MAX_MINUTES", "120"))
        self._whisper_loop_started = False
        # channel_id -> {"total": int, "last_at": float} - this bot's own
        # turn budget for an active /interact exchange in that channel,
        # regardless of which of the two other ghosts it's talking to.
        self.exchange_turns = {}

    def cog_unload(self):
        if self.whisper_loop.is_running():
            self.whisper_loop.cancel()

    def start_whisper_loop(self):
        if not self._whisper_loop_started:
            self._whisper_loop_started = True
            self.whisper_loop.change_interval(minutes=self._next_whisper_delay())
            self.whisper_loop.start()

    def _next_whisper_delay(self) -> int:
        return random.randint(self.whisper_min, self.whisper_max)

    def _eligible_text_channels(self):
        channels = []
        for guild in self.bot.guilds:
            for channel in guild.text_channels:
                if self.allowed_channel_ids and channel.id not in self.allowed_channel_ids:
                    continue
                perms = channel.permissions_for(guild.me)
                if perms.send_messages and perms.view_channel:
                    channels.append(channel)
        return channels

    @tasks.loop(minutes=60)  # interval is overwritten before first start
    async def whisper_loop(self):
        personality = self.bot.get_cog("Personality")
        if not personality:
            return

        channels = self._eligible_text_channels()
        if channels:
            channel = random.choice(channels)
            personality.maybe_shift_mood()

            memory_hint = None
            if random.random() < 0.4:
                memory_hint = personality.random_memory()

            cue = random.choice(WHISPER_CUES)
            line = await personality.speak(cue, memory_hint=memory_hint)
            try:
                await channel.send(line)
            except discord.HTTPException:
                log.exception("Failed to send whisper to %s", channel.id)

        self.whisper_loop.change_interval(minutes=self._next_whisper_delay())

    @whisper_loop.before_loop
    async def before_whisper_loop(self):
        await self.bot.wait_until_ready()

    async def _maybe_reply_to_other_ghost(self, message: discord.Message, other_ghost_name: str):
        """Handle a message from one of the two other ghost bots during an
        /interact exchange."""
        content = message.content or ""
        if not content.endswith(INTERACT_MARKER):
            return
        content = content[: -len(INTERACT_MARKER)]

        channel_id = message.channel.id
        now = time.time()
        state = self.exchange_turns.get(channel_id)
        if state and now - state["last_at"] > EXCHANGE_TIMEOUT_SECONDS:
            state = None
        total_so_far = state["total"] if state else 0
        total_after_hearing = total_so_far + 1
        if total_after_hearing >= EXCHANGE_MAX_MESSAGES:
            return

        personality = self.bot.get_cog("Personality")
        if not personality:
            return

        cue = (
            f'{other_ghost_name}, one of the other spirits who shares this place with you, just said: '
            f'"{content}". Reply directly to them, in character, as part of a brief public back-and-forth. '
            "Keep it short and let your personalities play off each other."
        )

        async with message.channel.typing():
            line = await personality.speak(cue, max_tokens=150)

        try:
            await message.channel.send(line + INTERACT_MARKER)
        except discord.HTTPException:
            log.exception("Failed to send cross-ghost reply in %s", channel_id)
            return

        self.exchange_turns[channel_id] = {"total": total_after_hearing + 1, "last_at": time.time()}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            other_name = OTHER_GHOSTS_BY_ID.get(message.author.id)
            if other_name and message.guild:
                await self._maybe_reply_to_other_ghost(message, other_name)
            return
        if not message.guild:
            return
        if self.allowed_channel_ids and message.channel.id not in self.allowed_channel_ids:
            return

        personality = self.bot.get_cog("Personality")
        if not personality:
            return

        content = message.content or ""
        author_name = str(message.author.display_name)

        if len(content.strip()) >= 12:
            personality.remember(author_name, content, message.channel.id)

        haunted = personality.is_haunted(message.author.id)
        lowered = content.lower().replace("'", "").replace("’", "")

        matched_cue = None
        for keyword, cue in KEYWORD_TRIGGERS.items():
            if keyword in lowered:
                matched_cue = cue
                break

        should_respond = False
        cue = None

        if matched_cue:
            should_respond = True
            cue = f'{matched_cue} They said: "{content}"'
        elif haunted and random.random() < 0.35:
            should_respond = True
            cue = (
                f"You're currently keeping an eye on {author_name} specifically, the way you would with "
                f'someone whose work you actually respect. They just said: "{content}". Say something that '
                "shows you noticed - curious and present, not intrusive."
            )
        elif random.random() < 0.03:
            should_respond = True
            cue = f'Someone said: "{content}". React to it in passing, briefly, as an aside.'

        if not should_respond:
            return

        async with message.channel.typing():
            memory_hint = None
            if random.random() < 0.3:
                memory_hint = personality.random_memory(exclude_author=author_name)
            line = await personality.speak(cue, memory_hint=memory_hint)

        try:
            await message.channel.send(line)
        except discord.HTTPException:
            log.exception("Failed to send reaction in %s", message.channel.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Haunting(bot))
