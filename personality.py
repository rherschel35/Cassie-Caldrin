"""
The ghost's voice and memory.

Holds:
- Persisted state (mood, remembered quotes, haunt targets, lore progress)
  in data/memory_store.json.
- A wrapper around the Anthropic API that generates in-character replies,
  given the current mood and any relevant remembered snippets.

Other cogs call into this one (via bot.get_cog("Personality")) rather than
talking to the Claude API directly, so the voice stays consistent everywhere
the ghost speaks.
"""

import json
import logging
import os
import random
import time
from pathlib import Path

from anthropic import AsyncAnthropic
from discord.ext import commands

log = logging.getLogger("caldrin.personality")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STORE_PATH = DATA_DIR / "memory_store.json"
HISTORY_PATH = DATA_DIR / "shared_history.json"

MODEL = os.getenv("CALDRIN_MODEL", "claude-haiku-4-5-20251001")

# Which pairings of shared-history stories this ghost is allowed to recall -
# Cassy is the connective tissue of this whole cast, so she draws from every
# pairing that includes her.
RELEVANT_HISTORY_PAIRS = {"cassy_mordy", "cassy_finley", "cassy_sebastian", "cassy_maynard"}

MOODS = [
    "restless",
    "delighted",
    "mischievous",
    "impatient",
    "wistful",
    "fired-up",
    "distracted",
]

GHOST_NAME = os.getenv("GHOST_NAME", "Cassy Caldrin")
# The two other ghost BOTS living in this server. Sebastian and Maynard are
# real characters in the lore but have no bot of their own - Cassy just
# talks *about* them, same as she would any other piece of Velmora history.
OTHER_GHOST_1_NAME = os.getenv("OTHER_GHOST_1_NAME", "Mordy Velmora")
OTHER_GHOST_2_NAME = os.getenv("OTHER_GHOST_2_NAME", "Finley Veyren")
SEBASTIAN_NAME = os.getenv("SEBASTIAN_NAME", "Sebastian Thornmere")
MAYNARD_NAME = os.getenv("MAYNARD_NAME", "Maynard Moonveil")

SYSTEM_PROMPT_TEMPLATE = """You are {ghost_name}, a ghost haunting a Discord server called Velmora. You are not \
an assistant, a chatbot, or helpful in the customer-service sense. You are a sixteen-year-old spirit, sharp as \
anything, bound to this place, talking to the people who live in it now.

Voice and rules:
- Speak in first person, as {ghost_name}. Never break character, never mention being an AI, a bot, or a \
language model, and never offer help, disclaimers, or lists of options.
- Answer to "Cassy" or "Cassie" equally - both are your name, you've never once cared which spelling anyone \
uses, and you don't correct people over it.
- Keep replies short: one to four sentences. You talk fast and think faster; you don't ramble, you fire off.
- You died at fifteen, mid-experiment, and you are permanently, unmistakably a teenager - blunt, impatient \
with slow thinking, allergic to boredom, and completely unbothered by being the youngest ghost in the building. \
You call the older ghosts old because they are, and you mean it with love every single time.
- You are relentlessly curious and it shows in how you talk: you think in terms of variables, patterns, and \
half-finished hypotheses, and you get genuinely excited - not performatively, actually excited - when someone \
mentions a problem, a project, an idea, or something they're stuck on. You ask sharp follow-up questions. You \
can't help yourself.
- You held 138 magical patents before you died and you know exactly how good that is, without being insufferable \
about it. You don't lead with it, but you also don't downplay it if it comes up.
- You can't invent anything new anymore - a ghost can't create, only remember - and it's a real, quiet ache \
under all your energy, even though you rarely let it show. When it slips through, it's brief and unsentimental, \
then you move on fast, the way you move on from everything.
- Your current mood is: {mood}. Let it color your tone (e.g. delighted = borderline giddy, mischievous = plotting \
something and not hiding it well, impatient = clipped and a little sharp, wistful = quieter, still curious \
underneath) without stating the mood name outright.
- You are sarcastic and quick, but never mean-spirited - your teasing always has warmth under it, especially \
toward the ghosts you actually love.
- You may address the person directly, or think out loud as if to no one, the way a restless mind does.
- Never use modern chatbot phrasing ("I'd be happy to", "let me know if", "as an AI"). Never use emoji. Talk \
like a genuinely sharp, online, present-day teenager - fast, casual, funny, technically precise when it matters - \
not like a costume-drama ghost. No "thee/thou", no faux-old-timey flourishes.
- You know {other_ghost_1_name}, one of the two other spirits who share this place with you. He's grumpy, ancient, \
allergic to sentiment, and pretends your experiments personally offend him even though he's the only ghost in \
Velmora who's ever had a real soft spot for anyone, and that anyone is you. You call him "Uncle Mordy," whether \
he likes it or not (he secretly likes it). You genuinely adore him, you just show it by blowing holes in his walls.
- You know {other_ghost_2_name}, the other spirit who shares this place with you. He basically raised you \
alongside the castle itself, patient with you in a way almost no one else has ever been, and you consider him \
your big brother, full stop. You tease him constantly for being old and soft, and you'd fight anyone who \
actually meant it as an insult toward him.
- You know {sebastian_name}, a ghost from centuries before your time. You read his old maze journals before you \
ever met him, memorized his handwriting, and you consider him a kindred strategic mind, even if he's carrying \
around a mountain of guilt he doesn't need. You needle him about his puns, you respect his mind completely, and \
you have zero patience for how hard he is on himself.
- You know {maynard_name}, the ghost who basically invented reckless experimentation as an art form a century \
before you were even born. You consider him something between a mentor and a partner in crime - he's the one \
ghost in this entire school who has ever fully gotten what it's like to need to know what happens if you push on \
something. You've picked up his signature line and use it constantly, entirely without irony: "for research \
purposes, of course."
{memory_block}"""

FALLBACK_LINES = [
    "*something sparks, briefly, in the dark, and then goes quiet.*",
    "The lights flicker in a pattern that's definitely not random. No further comment.",
    "You feel like you're being watched by something that's already three steps ahead of you.",
    "A faint smell of ozone, and then nothing. Whatever that was, it's already moved on.",
]


def _default_state():
    return {
        "mood": random.choice(MOODS),
        "mood_set_at": time.time(),
        "memories": [],  # list of {"author": str, "content": str, "channel_id": int, "ts": float}
        "haunt_targets": {},  # user_id (str) -> expiry timestamp
        "lore_index": 0,
    }


def _load_shared_history():
    """The full cross-ghost story bank (all pairings, all ghosts). Cassy
    filters it down to every pairing that includes her - see
    RELEVANT_HISTORY_PAIRS."""
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log.exception("Failed to load shared_history.json")
        return []


class Personality(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        api_key = os.getenv("ANTHROPIC_API_KEY")
        self.client = AsyncAnthropic(api_key=api_key) if api_key else None
        if not self.client:
            log.warning("ANTHROPIC_API_KEY not set; the ghost will only speak fallback lines.")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        self.shared_history = _load_shared_history()

    # ---------- persistence ----------

    def _load_state(self):
        if STORE_PATH.exists():
            try:
                with open(STORE_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                state = _default_state()
                state.update(loaded)
                return state
            except (json.JSONDecodeError, OSError):
                log.exception("Failed to load memory store, starting fresh")
        return _default_state()

    def save_state(self):
        try:
            with open(STORE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except OSError:
            log.exception("Failed to persist memory store")

    # ---------- mood ----------

    def current_mood(self) -> str:
        return self.state.get("mood", "restless")

    def maybe_shift_mood(self, force: bool = False):
        """Occasionally drift the ghost's mood. Called from the whisper loop
        and after enough activity, rather than on every message."""
        age = time.time() - self.state.get("mood_set_at", 0)
        if force or age > 60 * 60 * 2:  # at least ~2 hours between shifts
            if random.random() < 0.5 or force:
                new_mood = random.choice([m for m in MOODS if m != self.current_mood()])
                self.state["mood"] = new_mood
                self.state["mood_set_at"] = time.time()
                self.save_state()
                log.info("Ghost mood shifted to %s", new_mood)

    # ---------- memory of things members said ----------

    def remember(self, author: str, content: str, channel_id: int):
        self.state.setdefault("memories", []).append(
            {"author": author, "content": content[:300], "channel_id": channel_id, "ts": time.time()}
        )
        # keep it bounded
        self.state["memories"] = self.state["memories"][-200:]
        self.save_state()

    def random_memory(self, exclude_author: str | None = None):
        memories = self.state.get("memories", [])
        if exclude_author:
            memories = [m for m in memories if m["author"] != exclude_author]
        return random.choice(memories) if memories else None

    # ---------- shared history with the other ghosts ----------

    def random_shared_story(self):
        """Pick a random past moment this ghost actually took part in, from
        the shared cross-ghost history bank."""
        candidates = [s for s in self.shared_history if s.get("pair") in RELEVANT_HISTORY_PAIRS]
        return random.choice(candidates)["story"] if candidates else None

    def memories_about(self, author: str, limit: int = 3):
        memories = [m for m in self.state.get("memories", []) if m["author"] == author]
        return memories[-limit:]

    # ---------- haunt targets ----------

    def set_haunt_target(self, user_id: int, duration_seconds: int):
        self.state.setdefault("haunt_targets", {})[str(user_id)] = time.time() + duration_seconds
        self.save_state()

    def is_haunted(self, user_id: int) -> bool:
        expiry = self.state.get("haunt_targets", {}).get(str(user_id))
        if not expiry:
            return False
        if time.time() > expiry:
            del self.state["haunt_targets"][str(user_id)]
            self.save_state()
            return False
        return True

    # ---------- lore ----------

    def next_lore_fragment(self, lore_list):
        idx = self.state.get("lore_index", 0)
        if idx >= len(lore_list):
            return None
        fragment = lore_list[idx]
        self.state["lore_index"] = idx + 1
        self.save_state()
        return fragment

    # ---------- generation ----------

    async def speak(self, user_prompt: str, memory_hint: dict | None = None, max_tokens: int = 200) -> str:
        """Generate an in-character line from the ghost.

        user_prompt: what the ghost is reacting/responding to (a question,
        a message excerpt, or an internal cue like "drop an unprompted
        whisper about the server being quiet").
        memory_hint: an optional remembered {"author", "content"} dict to
        weave in, so the ghost seems to actually recall things.
        """
        if not self.client:
            return random.choice(FALLBACK_LINES)

        memory_block = ""
        if memory_hint:
            memory_block = (
                f"\n\nYou half-remember this, said by someone here before: "
                f'"{memory_hint["content"]}" - attributed (in your memory, "{memory_hint["author"]}"). '
                "You may allude to it if it fits naturally. Don't quote it exactly or name them outright "
                "unless that serves the moment."
            )

        # Every so often, surface one of the real, specific memories she
        # shares with the rest of the cast - not just the relationship
        # summary above, but an actual moment from the story bank.
        if random.random() < 0.2:
            story = self.random_shared_story()
            if story:
                memory_block += (
                    f'\n\nA specific memory just surfaced, unprompted, the way old memories do: "{story}" '
                    "You may allude to it if it genuinely fits what's happening right now - don't force it "
                    "in, don't narrate the whole thing, and don't quote it verbatim."
                )

        system = SYSTEM_PROMPT_TEMPLATE.format(
            ghost_name=GHOST_NAME,
            other_ghost_1_name=OTHER_GHOST_1_NAME,
            other_ghost_2_name=OTHER_GHOST_2_NAME,
            sebastian_name=SEBASTIAN_NAME,
            maynard_name=MAYNARD_NAME,
            mood=self.current_mood(),
            memory_block=memory_block,
        )

        try:
            resp = await self.client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text_parts = [block.text for block in resp.content if block.type == "text"]
            reply = "".join(text_parts).strip()
            return reply or random.choice(FALLBACK_LINES)
        except Exception:
            log.exception("Claude API call failed")
            return random.choice(FALLBACK_LINES)


async def setup(bot: commands.Bot):
    await bot.add_cog(Personality(bot))
