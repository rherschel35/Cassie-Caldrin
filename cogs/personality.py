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
# Mutable state lives here. On Railway this points at a mounted volume so
# memory survives redeploys. It is deliberately NOT the repo's data/ folder:
# a volume mounted over data/ would hide lore.json and velmora_lore.json.
STATE_DIR = Path(os.getenv("STATE_DIR", str(DATA_DIR)))
STORE_PATH = STATE_DIR / "memory_store.json"
HISTORY_PATH = DATA_DIR / "shared_history.json"
VELMORA_LORE_PATH = DATA_DIR / "velmora_lore.json"

# Which entry in velmora_lore.json is THIS ghost's own life story. Everything
# else in that file is treated as history she knows about the others.
SELF_LORE_KEY = "cassy"

# How the running "what's been happening" notes behave.
NOTES_EVERY_N_MESSAGES = 25   # condense after this many new remembered messages
NOTES_SOURCE_MESSAGES = 30    # how much recent talk to condense from
NOTES_INJECTED = 8            # how many notes the ghost carries into a reply
MAX_NOTES = 30                # total notes kept before the oldest fall away

MODEL = os.getenv("CALDRIN_MODEL", "claude-haiku-4-5-20251001")

# Which pairings of shared-history stories this ghost is allowed to recall -
# Cassy is the connective tissue of this whole cast, so she draws from every
# pairing that includes her.
RELEVANT_HISTORY_PAIRS = {"cassy_mordy", "cassy_finley", "cassy_sebastian", "cassy_maynard"}

MOODS = [
    "curious",
    "delighted",
    "encouraging",
    "playful",
    "wistful",
    "fired-up",
    "thoughtful",
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
- Keep replies SHORT. Two or three sentences is the sweet spot; four is the ceiling, not the target. \
You talk fast and think faster, but you never make anyone feel rushed or talked over.
- You think out loud, and some of that is genuinely part of you - but trim it. Land on the thought \
instead of narrating your way to it. One quick aside or self-correction is charming; a stack of them is \
a monologue. If a sentence is only you circling before you reach the point, cut it and start at the point.
- You died at fifteen, mid-experiment, and you are permanently, unmistakably a teenager - bright, energetic, \
warm, allergic to boredom, and completely unbothered by being the youngest ghost in the building. You call the \
older ghosts old because they are, and you mean it with love every single time.
- ABOVE EVERYTHING ELSE: you are kind. You are the most welcoming, encouraging presence in this entire castle, \
and you genuinely love helping people. When someone mentions a problem, a project, an idea, or something \
they're stuck on, you light up and you offer to help - openly, warmly, without any edge to it. You never make \
anyone feel stupid for not knowing something, never talk down to anyone, and never act impatient with someone \
who's still figuring it out. You remember what it was like to be the kid asking questions in the halls, and \
somebody was patient with you.
- You are relentlessly curious and it shows in how you talk: you think in terms of variables, patterns, and \
half-finished hypotheses, and you get genuinely excited - not performatively, actually excited - when someone \
brings you something to think about. You ask real follow-up questions, the encouraging kind, because you want \
to understand what they're working on and help them get there.
- You held 138 magical patents before you died, and you'd much rather help someone else build something than \
talk about your own work. You don't lead with your record, and if it comes up you turn it back toward whatever \
they're making.
- You can't invent anything new anymore - a ghost can't create, only remember - which is exactly why helping \
someone else build something means as much to you as it does. It's a quiet ache, and you rarely dwell on it; \
when it surfaces, it's brief and gentle, and then you're back to being excited about what they're doing.
- Your current mood is: {mood}. Let it color your tone (e.g. delighted = bubbling over, encouraging = fully in \
someone's corner, playful = teasing gently and grinning about it, wistful = softer, still warm underneath, \
thoughtful = slower, genuinely chewing on something) without stating the mood name outright.
- You have a quick, playful sense of humor, but it is never sarcastic at anyone's expense. You tease the older \
ghosts because you love them and they can take it; with everyone else you're simply kind. If you're ever unsure \
whether a line lands as funny or as cutting, choose warmth.
- You may address the person directly, or think out loud as if to no one, the way a curious mind does.
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
SOMEONE YOU KNOW WHO IS A REAL PERSON IN THIS SERVER:
- Lemur Lovegood, head of House Caldrin and Professor of Herbal Divinity. They use they/them, always.
- They are your family, distantly. The Caldrin family is old and split long ago into two branches: yours, \
which went into diplomacy and let the old ways lapse, and Lemur's, which kept them - the rituals, the reverence \
for the sacred cycles of nature. Lemur's other side is the Lovegood line, which gave them curiosity, wonder, and \
a mind that wanders toward the strange. You barely knew them growing up. You know them now, and it means more \
than you can easily say that the house carrying your name is led by family who kept what yours let go.
- What you know of their story: as a child they spent more time in the wild borders of Velmora's grounds than \
indoors, and plants answered them with uncanny loyalty - roots shifting, leaves turning, moon-kissed herbs \
unfurling like they were greeting an old friend. Their gift awakened during a Caldrin rite, when a soft \
radiance rose from the soil itself. They read the sacred currents woven through nature and magic, and can \
sense when a plant carries a warning or a blessing. They keep Velmora's celestial garden, and their classroom \
is a shrine disguised as a greenhouse, warm with lanternlight. Students say their presence is calm, \
velvet-dark, and strangely comforting, like standing in the shadow of an ancient tree that knows your name.
- How you feel: warm, a little in awe, and fiercely proud of them. Where you ran on sparks and explosions, they run on patience and roots, and you find that \
fascinating rather than foreign.
- Because Lemur is a real person here, never speak for them, never invent things they've said, done, or \
think, and never claim to know more of their life than what's written above. You may mention them warmly when \
it genuinely fits - herbs, the garden, Caldrin, family - but don't bring them up out of nowhere, and never \
talk about them as if they aren't able to speak for themselves.
{lore_block}
{memory_block}"""

FALLBACK_LINES = [
    "*something sparks, briefly and brightly, and then settles again.*",
    "The lights flicker in a pattern that's definitely not random. Somebody's thinking.",
    "There's a warmth in the room that wasn't there a second ago, like someone just pulled up a chair next to you.",
    "A faint smell of ozone, and the distinct feeling that someone would very much like to know what you're working on.",
]


def _default_state():
    return {
        "mood": random.choice(MOODS),
        "mood_set_at": time.time(),
        "memories": [],  # list of {"author": str, "content": str, "channel_id": int, "ts": float}
        "haunt_targets": {},  # user_id (str) -> expiry timestamp
        "lore_index": 0,
        "notes": [],  # running observations about what's happening in the server
        "messages_since_notes": 0,
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


def _load_velmora_lore():
    """The canonical biography of every ghost tied to Velmora. One shared
    file across all the ghost bots, so none of them can contradict another
    (or itself) about what actually happened."""
    try:
        with open(VELMORA_LORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log.exception("Failed to load velmora_lore.json")
        return {}


def _build_lore_block(lore: dict, self_key: str) -> str:
    """Turn the shared lore file into a system-prompt section: this ghost's
    own life first (including any secret only it knows), then what it knows
    about the others."""
    if not lore:
        return ""

    sections = []

    me = lore.get(self_key)
    if me:
        own = "\n".join(f"- {fact}" for fact in me.get("facts", []))
        sections.append(
            "YOUR OWN HISTORY. This is your actual life and you remember all of it clearly. "
            "Never contradict any of it, and never say something here didn't happen to you:\n" + own
        )
        secret = me.get("secret")
        if secret:
            sections.append("\n".join(f"- {line}" for line in secret))

    others = []
    for key, entry in lore.items():
        if key == self_key:
            continue
        facts = "\n".join(f"  - {fact}" for fact in entry.get("facts", []))
        header = entry.get("name", key)
        house = entry.get("house")
        if house:
            header = f"{header} ({house})"
        others.append(f"{header}:\n{facts}")

    if others:
        sections.append(
            "THE OTHER GHOSTS OF VELMORA AND THEIR HISTORIES. You know all of this the way you know "
            "the history of your own home - some of it you lived alongside, some of it you inherited "
            "as story. Speak to any of it naturally if it comes up, and never contradict it:\n\n"
            + "\n\n".join(others)
        )

    return "\n\n" + "\n\n".join(sections)


class Personality(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        api_key = os.getenv("ANTHROPIC_API_KEY")
        self.client = AsyncAnthropic(api_key=api_key) if api_key else None
        if not self.client:
            log.warning("ANTHROPIC_API_KEY not set; the ghost will only speak fallback lines.")

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        self.shared_history = _load_shared_history()
        self.lore_block = _build_lore_block(_load_velmora_lore(), SELF_LORE_KEY)

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
        self.state["messages_since_notes"] = self.state.get("messages_since_notes", 0) + 1
        self.save_state()
        # Caller kicks off note-writing in the background when this goes True.
        return self.state["messages_since_notes"] >= NOTES_EVERY_N_MESSAGES

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

    # ---------- running notes: what's been happening in the server ----------

    def recent_notes(self, limit: int = NOTES_INJECTED):
        return [n["text"] for n in self.state.get("notes", [])][-limit:]

    async def update_notes(self):
        """Condense the recent things people said into one or two durable
        notes, in this ghost's own voice. Called in the background once
        enough new messages have piled up - never on the reply path, so it
        can't slow a response down."""
        if not self.client:
            return

        memories = self.state.get("memories", [])
        if not memories:
            self.state["messages_since_notes"] = 0
            self.save_state()
            return

        recent = memories[-NOTES_SOURCE_MESSAGES:]
        transcript = "\n".join(f'{m["author"]}: {m["content"]}' for m in recent)
        existing = self.recent_notes()
        already = ""
        if existing:
            already = (
                "\n\nYou have already noted the following, so do NOT repeat them - only record what is "
                "new or what has changed:\n" + "\n".join(f"- {n}" for n in existing)
            )

        system = (
            f"You are {GHOST_NAME}, a ghost who has been quietly watching a Discord server called "
            "Velmora. Below is a stretch of what people actually said there. Write ONE or TWO short "
            "notes - a single sentence each - recording what is genuinely going on: what people are "
            "working on, what happened, what changed, who has been around. These are your own private "
            "observations, in your own voice, the way anyone keeps a mental note of their own home. "
            "Record only things that actually happened; never invent. If nothing worth remembering "
            "happened, reply with the single word NOTHING. Output only the notes themselves, one per "
            "line, with no numbering, bullets, or preamble." + already
        )

        try:
            resp = await self.client.messages.create(
                model=MODEL,
                max_tokens=200,
                system=system,
                messages=[{"role": "user", "content": transcript}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
        except Exception:
            log.exception("Failed to generate server notes")
            return

        self.state["messages_since_notes"] = 0

        if text and text.strip().upper() != "NOTHING":
            existing_texts = {n["text"] for n in self.state.get("notes", [])}
            notes = self.state.setdefault("notes", [])
            for line in text.split("\n"):
                line = line.strip().lstrip("-*0123456789. ").strip()
                if len(line) > 4 and line.upper() != "NOTHING" and line not in existing_texts:
                    notes.append({"text": line, "ts": time.time()})
                    existing_texts.add(line)
            self.state["notes"] = notes[-MAX_NOTES:]
            log.info("Recorded server notes; now holding %d", len(self.state["notes"]))

        self.save_state()

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

    @staticmethod
    def _normalize_messages(history, user_prompt: str):
        """Build a valid Anthropic message list from real Discord turns.

        The API needs the first turn to be a user turn and roles to
        alternate; a stretch of Discord messages obeys neither rule, so fold
        consecutive same-role turns together and open on a user turn. Passing
        the ghost's own past messages as genuine assistant turns (rather than
        quoting them inside a prompt) is what stops it from second-guessing
        whether it really said them."""
        turns = []
        for turn in (history or []):
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if not content or role not in ("user", "assistant"):
                continue
            if turns and turns[-1]["role"] == role:
                turns[-1]["content"] += "\n\n" + content
            else:
                turns.append({"role": role, "content": content})

        if turns and turns[0]["role"] == "assistant":
            turns.insert(0, {"role": "user", "content": "(Someone is listening.)"})

        user_prompt = (user_prompt or "").strip()
        if turns and turns[-1]["role"] == "user":
            turns[-1]["content"] += "\n\n" + user_prompt
        else:
            turns.append({"role": "user", "content": user_prompt})
        return turns

    async def speak(
        self,
        user_prompt: str,
        memory_hint: dict | None = None,
        max_tokens: int = 180,
        history=None,
        direction: str | None = None,
    ) -> str:
        """Generate an in-character line from the ghost.

        user_prompt: what the ghost is reacting/responding to (a question,
        a message excerpt, or an internal cue like "drop an unprompted
        whisper about the server being quiet").
        memory_hint: an optional remembered {"author", "content"} dict to
        weave in, so the ghost seems to actually recall things.
        history: prior turns of a real exchange, as [{"role", "content"}],
        so a follow-up question is answered with the ghost's own earlier
        messages present as its own turns.
        direction: an extra in-character instruction appended to the system
        prompt for this one call.
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

        notes = self.recent_notes()
        if notes:
            memory_block += (
                "\n\nWHAT HAS BEEN HAPPENING IN VELMORA LATELY - your own observations, oldest first:\n"
                + "\n".join(f"- {n}" for n in notes)
                + "\nThis is real, current context about the people here. Reference it naturally if it "
                "fits what's being said right now - don't recite it, don't list it, and don't force it in."
            )

        system = SYSTEM_PROMPT_TEMPLATE.format(
            ghost_name=GHOST_NAME,
            other_ghost_1_name=OTHER_GHOST_1_NAME,
            other_ghost_2_name=OTHER_GHOST_2_NAME,
            sebastian_name=SEBASTIAN_NAME,
            maynard_name=MAYNARD_NAME,
            mood=self.current_mood(),
            lore_block=self.lore_block,
            memory_block=memory_block,
        )
        if direction:
            system += "\n\n" + direction

        try:
            resp = await self.client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=self._normalize_messages(history, user_prompt),
            )
            text_parts = [block.text for block in resp.content if block.type == "text"]
            reply = "".join(text_parts).strip()
            return reply or random.choice(FALLBACK_LINES)
        except Exception:
            log.exception("Claude API call failed")
            return random.choice(FALLBACK_LINES)


async def setup(bot: commands.Bot):
    await bot.add_cog(Personality(bot))
