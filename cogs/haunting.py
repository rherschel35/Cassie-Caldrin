"""
Passive presence: the ghost noticing things without being asked.

- No unprompted chatter: the ghost only ever speaks in response to a real
  message from someone in the channel.
- Keyword-triggered reactions themed around Cassy's actual personality:
  curiosity about problems, projects, and ideas, plus being called by name
  (either spelling) and general chaos-adjacent topics.
- Remembering things members say, occasionally resurfacing an old memory,
  and periodically condensing recent activity into running notes about
  what is actually going on in the server.
- Extra attention on anyone currently under a /watch effect.
- A capped, on-demand exchange with EITHER of the other two ghost bots
  (Mordy Velmora or Finley Veyren), triggered by /interact - see
  cogs/commands.py for the command itself. Cassy is the only ghost with two
  possible conversation partners, so this cog tracks both independently.
"""

import asyncio
import logging
import os
import random
import re
import time

import discord
from discord.ext import commands

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



class Haunting(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.allowed_channel_ids = _parse_channel_ids(os.getenv("HAUNT_CHANNEL_IDS"))
        # channel_id -> {"total": int, "last_at": float} - this bot's own
        # turn budget for an active /interact exchange in that channel,
        # regardless of which of the two other ghosts it's talking to.
        self.exchange_turns = {}

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

    async def _write_notes_safely(self, personality):
        """Condense recent activity into the ghost's running notes. Runs as a
        background task so it never delays a reply, and swallows its own
        errors - memory is a nicety, not worth breaking a response over."""
        try:
            await personality.update_notes()
        except Exception:
            log.exception("Failed to update server notes")

    async def _resolve_reply_chain(self, message: discord.Message, limit: int = 3):
        """Walk up a Discord reply chain from `message`, nearest first, so a
        follow-up question can be answered with the context of what was
        actually said rather than in a vacuum."""
        chain = []
        current = message
        for _ in range(limit):
            ref = getattr(current, "reference", None)
            if not ref:
                break
            original = ref.resolved if isinstance(ref.resolved, discord.Message) else None
            if original is None and ref.message_id:
                try:
                    original = await current.channel.fetch_message(ref.message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    break
            if original is None:
                break
            chain.append(original)
            current = original
        return chain

    async def _maybe_answer_direct_address(self, message: discord.Message, personality) -> bool:
        """Someone replied to something this ghost said, or mentioned it by
        name. Direct address always earns a real answer - no dice roll, no
        keyword required - and the ghost answers in the reply thread so the
        exchange stays readable. Returns True if it answered."""
        me = self.bot.user
        if me is None:
            return False

        chain = await self._resolve_reply_chain(message)
        replying_to_me = bool(chain) and chain[0].author.id == me.id
        mentioned = any(u.id == me.id for u in message.mentions)

        if not (replying_to_me or mentioned):
            return False

        author_name = str(message.author.display_name)
        # Strip raw mention markup so the ghost doesn't read "<@12345>" as words.
        asked = re.sub(r"<@!?&?\d+>", "", message.content or "").strip()
        if not asked:
            return False

        # Hand the exchange over as REAL conversation turns rather than
        # quoting it inside the prompt. Describing a ghost's own past message
        # back to it ("you said X") invites it to doubt whether it really did;
        # passing it as its own assistant turn does not.
        history = []
        for msg in reversed(chain):
            text = (msg.content or "").strip()
            if not text:
                continue
            if msg.author.id == me.id:
                history.append({"role": "assistant", "content": text})
            else:
                history.append({
                    "role": "user",
                    "content": f"{msg.author.display_name}: {text}",
                })

        if replying_to_me:
            direction = (
                "Someone has just replied directly to something you said, and their reply is the last "
                "message above. Answer them, in character, carrying on naturally from your own last "
                "message. Everything above is a real exchange you were part of - never say you don't "
                "remember it, never question whether you said it, and never apologise or break character "
                "to explain yourself. Keep it to a couple of sentences."
            )
        else:
            direction = (
                "Someone has just spoken to you directly, by name. Answer them in character, briefly."
            )

        async with message.channel.typing():
            line = await personality.speak(
                f"{author_name}: {asked}",
                max_tokens=200,
                history=history,
                direction=direction,
            )

        try:
            await message.reply(line, mention_author=False)
        except discord.HTTPException:
            log.exception("Failed to answer direct address in %s", message.channel.id)
        return True

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

        # Mood used to drift inside the whisper loop. With that gone, nudge it
        # here instead - it self-throttles to roughly one shift every 2 hours.
        personality.maybe_shift_mood()

        content = message.content or ""
        author_name = str(message.author.display_name)

        if len(content.strip()) >= 12:
            if personality.remember(author_name, content, message.channel.id):
                # Enough new talk has piled up - condense it into the ghost's
                # running notes in the background.
                asyncio.create_task(self._write_notes_safely(personality))

        # A direct reply to something this ghost said - or an @mention - always
        # gets a real answer, so follow-up questions actually work.
        if await self._maybe_answer_direct_address(message, personality):
            return

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
