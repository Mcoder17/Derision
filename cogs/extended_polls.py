from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from env import OWNER_ID

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Resolve the data directory independently of the process working
# directory, which varies between hosts. Anchoring to this file's location
# (cogs/polls.py -> project root/data) means moving the bot to another host
# keeps finding data/polls.json regardless of where the process is launched
# from. POLL_DATA_DIR overrides it if you keep data somewhere else.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("POLL_DATA_DIR") or os.path.join(_PROJECT_ROOT, "data")
POLL_FILE = os.path.join(DATA_DIR, "polls.json")
SCHEMA_VERSION = 4

MIN_OPTIONS = 2
MAX_OPTIONS_BUTTONS = 20  # 4 rows of 5 buttons; row 5 holds the controls
MAX_OPTIONS_RANKED = 99   # ranked polls vote via modal, so no button limit
MAX_QUESTION_LEN = 240
MAX_OPTION_LEN = 80
MAX_WINNERS = 20  # a poll may elect up to this many options (must stay < options)

MIN_DURATION = 1  # minutes
MAX_DURATION = 60 * 24 * 7

MAX_POLLS_PER_GUILD = 25
MAX_BALLOTS_PER_POLL = 10_000
CLOSED_RETENTION_DAYS = 7

UPDATE_INTERVAL = 5.0  # seconds between flushes of dirty polls
# Bulk message edits (a batch of expiries, or many dirty polls after a
# restart) are throttled so a single IP never fires a flood of edits at
# Discord's edge, which can trip a Cloudflare 1015 block distinct from the
# normal API rate limiter. At most EDITS_PER_TICK edits happen per flush,
# spaced by EDIT_SPACING seconds; anything over the cap waits for the next
# tick. A 429 stops the rest of the batch immediately.
EDITS_PER_TICK = 5
EDIT_SPACING = 0.4  # seconds between edits within one flush pass
RATE_LIMIT_BACKOFF = 30.0  # seconds to pause the flush loop after a 429
# Options are normally comma separated. If any option needs to contain a
# comma, the whole list can be split on OPTION_SEPARATOR_ALT instead - the
# parser uses it automatically whenever it appears in the input.
OPTION_SEPARATOR = ","
OPTION_SEPARATOR_ALT = "|"

LETTERS = "ABCDEFGHIJKLMNOPQRST"  # labels for button-style polls (<= 20)
BAR_WIDTH = 12
BAR_FULL = "\u2588"
BAR_EMPTY = "\u2591"

MAX_EMBED_OPTION_LINES = 25   # embed rows before "... and N more"
MAX_VOTER_NAMES_PER_OPTION = 15
MAX_RANKED_BALLOT_LINES = 20
RUNOFF_CHAR_BUDGET = 760      # leave room for final-round + winner lines (field cap 1024)

COLOUR_OPEN = discord.Colour.blurple()
COLOUR_CLOSED = discord.Colour.dark_grey()

STYLE_SINGLE = "single"
STYLE_MULTI = "multi"
STYLE_RANKED = "ranked"

STYLE_LABELS = {
    STYLE_SINGLE: "Single choice",
    STYLE_MULTI: "Multiple choice",
    STYLE_RANKED: "Ranked choice (instant runoff)",
}

NO_MENTIONS = discord.AllowedMentions.none()

_RANK_TOKEN = re.compile(r"\d+")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _clean(text: str) -> str:
    """Escape markdown and mentions in user supplied text."""
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _bar(value: int, total: int) -> str:
    filled = 0 if total <= 0 else round(BAR_WIDTH * value / total)
    return BAR_FULL * filled + BAR_EMPTY * (BAR_WIDTH - filled)


def _pct(value: int, total: int) -> float:
    return 0.0 if total <= 0 else 100.0 * value / total


# --------------------------------------------------------------------------
# Poll state
# --------------------------------------------------------------------------


class Poll:
    """A single poll, open or closed.

    ``ballots`` maps a user id to a style dependent payload:

    * ``single`` -> ``int``   option index
    * ``multi``  -> ``int``   bitmask of option indices
    * ``ranked`` -> ``bytes`` option indices, most preferred first

    ``names`` maps a user id to the display name captured when the user
    last voted. It exists so the JSON store is human readable; every code
    path keys off the id.
    """

    __slots__ = (
        "id",
        "guild_id",
        "channel_id",
        "message_id",
        "author_id",
        "question",
        "options",
        "style",
        "created_at",
        "ends_at",
        "closed_at",
        "anonymous",
        "hidden",
        "published",
        "winners",
        "allow_change",
        "role_id",
        "ballots",
        "names",
        "closed",
        "dirty",
        "busy",
    )

    def __init__(
        self,
        *,
        id: str,
        guild_id: int,
        channel_id: int,
        message_id: int,
        author_id: int,
        question: str,
        options: Sequence[str],
        style: str,
        created_at: float,
        ends_at: Optional[float],
        closed_at: Optional[float] = None,
        anonymous: bool = False,
        hidden: bool = False,
        published: bool = False,
        winners: int = 1,
        allow_change: bool = True,
        role_id: Optional[int] = None,
        ballots: Optional[Dict[int, Any]] = None,
        names: Optional[Dict[int, str]] = None,
        closed: bool = False,
    ) -> None:
        self.id = id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message_id = message_id
        self.author_id = author_id
        self.question = question
        self.options = list(options)
        self.style = style
        self.created_at = created_at
        self.ends_at = ends_at
        self.closed_at = closed_at
        self.anonymous = anonymous
        self.hidden = hidden
        self.published = published
        self.winners = max(1, winners)
        self.allow_change = allow_change
        self.role_id = role_id
        self.ballots: Dict[int, Any] = ballots or {}
        self.names: Dict[int, str] = names or {}
        self.closed = closed
        self.dirty = False
        self.busy = False  # guards close/reopen/cancel races

    # -- labelling -------------------------------------------------------

    def label(self, index: int) -> str:
        """``A``..``T`` for button polls, ``1``..``99`` for ranked polls."""
        if self.style == STYLE_RANKED:
            return str(index + 1)
        return LETTERS[index]

    def option_line(self, index: int, limit: int = MAX_OPTION_LEN) -> str:
        return f"{self.label(index)}. {_clean(_truncate(self.options[index], limit))}"

    def plain_line(self, index: int) -> str:
        """Unescaped option text, for the JSON store rather than an embed."""
        if 0 <= index < len(self.options):
            return f"{self.label(index)}. {self.options[index]}"
        return f"?{index}"

    # -- serialisation ---------------------------------------------------

    def _ballot_entry(self, user_id: int, ballot: Any) -> Dict[str, Any]:
        """One self-contained, human readable ballot record.

        ``vote`` is authoritative and is the only field read back on load.
        ``name`` and ``choice`` exist so the file can be read by eye - a
        bare index or bitmask tells a human nothing - and are regenerated
        on every save, so they can never drift into being load-bearing.
        """
        if self.style == STYLE_RANKED:
            vote: Any = ">".join(str(i) for i in ballot)
            choice = " > ".join(self.plain_line(i) for i in ballot)
        elif self.style == STYLE_MULTI:
            vote = ballot
            picked = [i for i in range(len(self.options)) if ballot & (1 << i)]
            choice = ", ".join(self.plain_line(i) for i in picked)
        else:
            vote = ballot
            choice = self.plain_line(ballot)
        return {
            "name": self.names.get(user_id, ""),
            "vote": vote,
            "choice": choice,
        }

    def to_dict(self) -> Dict[str, Any]:
        ballots = {
            str(uid): self._ballot_entry(uid, ballot)
            for uid, ballot in self.ballots.items()
        }
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "author_id": self.author_id,
            "question": self.question,
            "options": self.options,
            "style": self.style,
            "created_at": self.created_at,
            "ends_at": self.ends_at,
            "closed_at": self.closed_at,
            "anonymous": self.anonymous,
            "hidden": self.hidden,
            "published": self.published,
            "winners": self.winners,
            "allow_change": self.allow_change,
            "role_id": self.role_id,
            "closed": self.closed,
            # Each record carries its voter's name inline; ids remain the
            # only thing any logic keys off.
            "ballots": ballots,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Poll":
        style = raw["style"]
        ballots: Dict[int, Any] = {}
        names: Dict[int, str] = {}
        # Schema 2 kept names in a sibling map; still accepted on load.
        legacy_names = {
            int(uid): str(name)
            for uid, name in (raw.get("voter_names") or {}).items()
        }

        for uid, record in (raw.get("ballots") or {}).items():
            key = int(uid)
            if isinstance(record, dict):
                vote = record.get("vote")
                name = record.get("name")
            else:
                vote = record
                name = legacy_names.get(key)

            if vote is None or vote == "":
                continue
            if style == STYLE_RANKED:
                ballots[key] = bytes(
                    int(part) for part in str(vote).split(">") if part != ""
                )
            else:
                ballots[key] = int(vote)
            if name:
                names[key] = str(name)
        return cls(
            id=raw["id"],
            guild_id=raw["guild_id"],
            channel_id=raw["channel_id"],
            message_id=raw["message_id"],
            author_id=raw["author_id"],
            question=raw["question"],
            options=raw["options"],
            style=style,
            created_at=raw["created_at"],
            ends_at=raw.get("ends_at"),
            closed_at=raw.get("closed_at"),
            anonymous=raw.get("anonymous", False),
            hidden=raw.get("hidden", False),
            published=raw.get("published", False),
            winners=raw.get("winners", 1),
            allow_change=raw.get("allow_change", True),
            role_id=raw.get("role_id"),
            ballots=ballots,
            names=names,
            closed=raw.get("closed", False),
        )

    # -- tallying --------------------------------------------------------

    @property
    def voter_count(self) -> int:
        return len(self.ballots)

    @property
    def max_options(self) -> int:
        return MAX_OPTIONS_RANKED if self.style == STYLE_RANKED else MAX_OPTIONS_BUTTONS

    def first_preferences(self) -> List[int]:
        """Counts for single/multi, or first choices for ranked ballots."""
        counts = [0] * len(self.options)
        if self.style == STYLE_MULTI:
            for mask in self.ballots.values():
                for index in range(len(self.options)):
                    if mask & (1 << index):
                        counts[index] += 1
        elif self.style == STYLE_SINGLE:
            for index in self.ballots.values():
                if 0 <= index < len(counts):
                    counts[index] += 1
        else:
            for ballot in self.ballots.values():
                if ballot and ballot[0] < len(counts):
                    counts[ballot[0]] += 1
        return counts

    @property
    def multi_winner(self) -> bool:
        return self.winners > 1

    def winner_set(self) -> List[int]:
        """The winning option indices for this poll's style and seat count.

        For single/multiple choice: the top ``winners`` options by count,
        expanding to include ties at the cutoff. For ranked: the STV result.
        Returns an empty list when there are no votes.
        """
        if not self.ballots:
            return []
        if self.style == STYLE_RANKED:
            _, winners = single_transferable_vote(
                self.ballots.values(), len(self.options), self.winners
            )
            return winners
        return top_n_with_ties(self.first_preferences(), self.winners)

    def describe_ballot(self, user_id: int) -> Optional[str]:
        ballot = self.ballots.get(user_id)
        if ballot is None:
            return None
        if self.style == STYLE_SINGLE:
            return self.option_line(ballot)
        if self.style == STYLE_MULTI:
            chosen = [i for i in range(len(self.options)) if ballot & (1 << i)]
            if not chosen:
                return None
            return ", ".join(self.option_line(i, 40) for i in chosen)
        return " > ".join(self.option_line(i, 40) for i in ballot)

    def voter_name(self, user_id: int) -> str:
        return _clean(self.names.get(user_id, f"user {user_id}"))


# --------------------------------------------------------------------------
# Instant runoff tabulation
# --------------------------------------------------------------------------


def instant_runoff(
    ballots: Iterable[bytes], option_count: int
) -> Tuple[List[Tuple[List[int], int, Optional[int]]], Optional[int]]:
    """Run an instant-runoff count.

    Returns ``(rounds, winner)`` where each round is
    ``(counts, exhausted, eliminated)``. ``winner`` is ``None`` when the
    count cannot resolve (no ballots, or a dead tie between finalists).

    Elimination order is deterministic: fewest votes this round, broken by
    fewest first preferences, then by highest option index.
    """
    papers = [b for b in ballots if b]
    rounds: List[Tuple[List[int], int, Optional[int]]] = []
    if not papers:
        return rounds, None

    first_prefs = [0] * option_count
    for paper in papers:
        if paper[0] < option_count:
            first_prefs[paper[0]] += 1

    active = set(range(option_count))
    for _ in range(option_count):
        counts = [0] * option_count
        exhausted = 0
        for paper in papers:
            for index in paper:
                if index in active:
                    counts[index] += 1
                    break
            else:
                exhausted += 1

        remaining = len(papers) - exhausted
        if remaining <= 0:
            rounds.append((counts, exhausted, None))
            return rounds, None

        leader = max(active, key=lambda i: (counts[i], -i))
        if counts[leader] * 2 > remaining or len(active) == 1:
            rounds.append((counts, exhausted, None))
            return rounds, leader

        fewest = min(counts[i] for i in active)
        losers = [i for i in active if counts[i] == fewest]
        if len(losers) == len(active):
            rounds.append((counts, exhausted, None))
            return rounds, None

        losers.sort(key=lambda i: (first_prefs[i], -i))
        eliminated = losers[0]
        active.discard(eliminated)
        rounds.append((counts, exhausted, eliminated))

    return rounds, None


def single_transferable_vote(
    ballots: Iterable[bytes],
    option_count: int,
    seats: int,
) -> Tuple[List[Dict[str, Any]], List[int]]:
    """Weighted-Gregory Single Transferable Vote (proportional, multi-winner).

    Returns ``(rounds, winners)``. Each round dict has: ``counts`` (list of
    Fraction weights per option), ``quota`` (Fraction), ``elected`` (indices
    elected that round), ``eliminated`` (index or None), ``exhausted``
    (Fraction weight with no continuing preference).

    A candidate reaching the Droop quota is elected and their surplus above
    quota transfers to next preferences, scaled by surplus/total so only the
    excess flows on. When no one reaches quota the weakest is eliminated and
    their votes transfer at full weight. Elimination ties break
    deterministically: fewest first preferences, then highest index.
    """
    papers = [b for b in ballots if b]
    rounds: List[Dict[str, Any]] = []
    if not papers or seats <= 0:
        return rounds, []

    seats = min(seats, option_count)
    weights = [Fraction(1) for _ in papers]
    elected: List[int] = []
    eliminated: set = set()

    first_prefs = [0] * option_count
    for paper in papers:
        first_prefs[paper[0]] += 1

    def active_index(paper: bytes) -> Optional[int]:
        for idx in paper:
            if idx not in elected and idx not in eliminated:
                return idx
        return None

    total = Fraction(len(papers))
    guard = 0
    while len(elected) < seats and guard < option_count * 2 + 5:
        guard += 1

        counts = [Fraction(0)] * option_count
        exhausted = Fraction(0)
        for paper, weight in zip(papers, weights):
            idx = active_index(paper)
            if idx is None:
                exhausted += weight
            else:
                counts[idx] += weight

        continuing = total - exhausted
        quota = Fraction(int(continuing / (seats + 1)) + 1)
        remaining = seats - len(elected)
        contenders = [
            i for i in range(option_count) if i not in elected and i not in eliminated
        ]

        # Fill by acclamation when only enough candidates remain.
        if len(contenders) <= remaining:
            ordered = sorted(contenders, key=lambda i: (-counts[i], i))
            elected.extend(ordered)
            rounds.append(
                {
                    "counts": counts,
                    "quota": quota,
                    "elected": ordered,
                    "eliminated": None,
                    "exhausted": exhausted,
                }
            )
            break

        meeting = [i for i in contenders if counts[i] >= quota]
        if meeting:
            meeting.sort(key=lambda i: (-counts[i], i))
            newly: List[int] = []
            for winner in meeting:
                if len(elected) >= seats:
                    break
                surplus = counts[winner] - quota
                # Capture the winner's ballots BEFORE marking it elected, or
                # active_index would skip it and scale the wrong ballots.
                on_winner = [
                    k for k, paper in enumerate(papers)
                    if active_index(paper) == winner
                ]
                elected.append(winner)
                newly.append(winner)
                if surplus > 0 and counts[winner] > 0:
                    factor = surplus / counts[winner]
                    for k in on_winner:
                        weights[k] *= factor
            rounds.append(
                {
                    "counts": counts,
                    "quota": quota,
                    "elected": newly,
                    "eliminated": None,
                    "exhausted": exhausted,
                }
            )
            continue

        fewest = min(counts[i] for i in contenders)
        losers = [i for i in contenders if counts[i] == fewest]
        losers.sort(key=lambda i: (first_prefs[i], -i))
        drop = losers[0]
        eliminated.add(drop)
        rounds.append(
            {
                "counts": counts,
                "quota": quota,
                "elected": [],
                "eliminated": drop,
                "exhausted": exhausted,
            }
        )

    return rounds, elected


def top_n_with_ties(counts: Sequence[int], seats: int) -> List[int]:
    """Indices of the ``seats`` highest counts, expanding to include ties.

    A count of zero never wins. If options tie at the cutoff, all tied
    options are included, so the result may exceed ``seats``.
    """
    ranked = sorted(range(len(counts)), key=lambda i: (-counts[i], i))
    winners: List[int] = []
    for position, index in enumerate(ranked):
        if counts[index] <= 0:
            break
        if position < seats:
            winners.append(index)
        elif counts[index] == counts[ranked[seats - 1]]:
            winners.append(index)  # tied with the last winning slot
        else:
            break
    return winners
# --------------------------------------------------------------------------


class OptionButton(discord.ui.Button["PollView"]):
    def __init__(self, cog: "Polls", poll: Poll, index: int, row: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=f"{poll.label(index)}. {_truncate(poll.options[index], 70)}",
            custom_id=f"poll:{poll.id}:{index}",
            row=row,
        )
        self.cog = cog
        self.poll_id = poll.id
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_choice(interaction, self.poll_id, self.index)


class RankButton(discord.ui.Button["PollView"]):
    def __init__(self, cog: "Polls", poll_id: str, row: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Rank choices",
            emoji="\U0001f522",
            custom_id=f"poll:{poll_id}:rank",
            row=row,
        )
        self.cog = cog
        self.poll_id = poll_id

    async def callback(self, interaction: discord.Interaction) -> None:
        poll = self.cog.polls.get(self.poll_id)
        if poll is None or poll.closed:
            await self.cog.reject(interaction, "This poll is not open.")
            return
        problem = self.cog.check_eligibility(interaction, poll)
        if problem:
            await self.cog.reject(interaction, problem)
            return
        await interaction.response.send_modal(RankModal(self.cog, poll))


class MyVoteButton(discord.ui.Button["PollView"]):
    def __init__(self, cog: "Polls", poll_id: str, row: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="My vote",
            custom_id=f"poll:{poll_id}:mine",
            row=row,
        )
        self.cog = cog
        self.poll_id = poll_id

    async def callback(self, interaction: discord.Interaction) -> None:
        poll = self.cog.polls.get(self.poll_id)
        if poll is None:
            await self.cog.reject(interaction, "This poll is no longer available.")
            return
        described = poll.describe_ballot(interaction.user.id)
        message = (
            f"Your current ballot:\n{described}"
            if described
            else "You have not voted in this poll yet."
        )
        await interaction.response.send_message(message, ephemeral=True)


class RetractButton(discord.ui.Button["PollView"]):
    def __init__(self, cog: "Polls", poll_id: str, row: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Retract",
            custom_id=f"poll:{poll_id}:retract",
            row=row,
        )
        self.cog = cog
        self.poll_id = poll_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_retract(interaction, self.poll_id)


class PollView(discord.ui.View):
    """Persistent view attached to an open poll message."""

    def __init__(self, cog: "Polls", poll: Poll):
        super().__init__(timeout=None)
        if poll.style == STYLE_RANKED:
            control_row = 0
            self.add_item(RankButton(cog, poll.id, row=0))
        else:
            option_rows = (len(poll.options) - 1) // 5 + 1
            control_row = min(4, option_rows)
            for index in range(len(poll.options)):
                self.add_item(OptionButton(cog, poll, index, row=index // 5))

        self.add_item(MyVoteButton(cog, poll.id, row=control_row))
        if poll.allow_change:
            self.add_item(RetractButton(cog, poll.id, row=control_row))


class RankModal(discord.ui.Modal):
    """Collects an ordered ballot as numbers, e.g. ``2 1 3``."""

    def __init__(self, cog: "Polls", poll: Poll):
        super().__init__(title="Rank your choices", timeout=300)
        self.cog = cog
        self.poll_id = poll.id
        count = len(poll.options)
        self.ranking: discord.ui.TextInput = discord.ui.TextInput(
            label=f"Order of preference (numbers 1-{count})",
            placeholder="e.g. 2 1 3 - best first, partial ballots are fine",
            max_length=min(400, count * 4),
            required=True,
        )
        self.add_item(self.ranking)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_ranking(interaction, self.poll_id, self.ranking.value)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:  # pragma: no cover - defensive
        log.exception("Ranked ballot submission failed", exc_info=error)
        await self.cog.reject(interaction, "Something went wrong recording that ballot.")


# --------------------------------------------------------------------------
# Cog
# --------------------------------------------------------------------------


class Polls(commands.Cog, name="Polls"):
    """Create single, multiple and ranked choice polls with visibility modes."""

    poll_group = app_commands.Group(
        name="poll",
        description="Create and manage polls.",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.polls: Dict[str, Poll] = {}
        self._views: Dict[str, PollView] = {}
        self._lock = asyncio.Lock()
        self._state_dirty = False
        self._backoff_until = 0.0  # flush loop pauses edits until this time

    # -- lifecycle -------------------------------------------------------

    async def cog_load(self) -> None:
        exists = os.path.exists(POLL_FILE)
        # Printed (not just logged) so it is visible regardless of the
        # host's logging configuration - the first thing to check when
        # polls do not come back after a move is this path.
        print(
            f"[Polls] Loading state from: {POLL_FILE} "
            f"(exists: {exists})"
        )
        await self._load_state()
        active = sum(1 for p in self.polls.values() if not p.closed)
        for poll in self.polls.values():
            if not poll.closed:
                self._register_view(poll)
        self.flush_loop.start()
        print(
            f"[Polls] Restored {len(self.polls)} poll(s) "
            f"({active} open) from {POLL_FILE}"
        )
        if exists and not self.polls:
            print(
                "[Polls] ⚠️ The file exists but no polls loaded - it may be "
                "empty or from an incompatible version (check for "
                f"{POLL_FILE}.corrupt)."
            )
        log.info("Polls cog loaded with %d poll(s)", len(self.polls))

    async def cog_unload(self) -> None:
        self.flush_loop.cancel()
        for view in self._views.values():
            view.stop()
        self._views.clear()
        async with self._lock:
            await self._save_state()

    def _register_view(self, poll: Poll) -> None:
        view = PollView(self, poll)
        self._views[poll.id] = view
        self.bot.add_view(view, message_id=poll.message_id)

    def _stop_view(self, poll_id: str) -> None:
        view = self._views.pop(poll_id, None)
        if view is not None:
            view.stop()

    def _discard(self, poll: Poll) -> None:
        """Drop a poll from memory entirely and mark the store dirty."""
        self.polls.pop(poll.id, None)
        self._stop_view(poll.id)
        self._state_dirty = True

    # -- persistence -----------------------------------------------------

    async def _load_state(self) -> None:
        def read() -> Optional[Dict[str, Any]]:
            if not os.path.exists(POLL_FILE):
                return None
            with open(POLL_FILE, "r", encoding="utf-8") as fp:
                return json.load(fp)

        try:
            raw = await asyncio.to_thread(read)
        except (OSError, json.JSONDecodeError):
            log.exception("Could not read %s - starting with no polls", POLL_FILE)
            await asyncio.to_thread(self._quarantine_file)
            return

        if not raw:
            return

        entries = raw.get("polls", [])
        skipped = 0
        for entry in entries:
            try:
                poll = Poll.from_dict(entry)
            except (KeyError, TypeError, ValueError) as exc:
                skipped += 1
                # Printed loudly: a silently dropped poll is invisible on
                # hosts that don't surface WARNING logs. Identify which
                # entry failed and why so it can be fixed by hand.
                ident = entry.get("id", "?") if isinstance(entry, dict) else "?"
                question = (
                    str(entry.get("question", ""))[:50]
                    if isinstance(entry, dict)
                    else ""
                )
                print(
                    f"[Polls] ⚠️ Skipped poll {ident!r} ({question!r}): "
                    f"{type(exc).__name__}: {exc}"
                )
                log.warning("Skipping malformed poll entry %s", ident, exc_info=True)
                continue
            self.polls[poll.id] = poll

        if skipped:
            print(
                f"[Polls] ⚠️ {skipped} of {len(entries)} poll(s) in the file "
                "could not be loaded (see reasons above)."
            )

    @staticmethod
    def _quarantine_file() -> None:
        try:
            if os.path.exists(POLL_FILE):
                os.replace(POLL_FILE, POLL_FILE + ".corrupt")
        except OSError:
            log.exception("Failed to quarantine %s", POLL_FILE)

    async def _save_state(self) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "polls": [poll.to_dict() for poll in self.polls.values()],
        }

        def write() -> None:
            os.makedirs(DATA_DIR, exist_ok=True)
            handle, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as fp:
                    json.dump(payload, fp, indent=1, ensure_ascii=False)
                os.replace(tmp_path, POLL_FILE)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

        try:
            await asyncio.to_thread(write)
            self._state_dirty = False
        except OSError:
            log.exception("Failed to persist polls to %s", POLL_FILE)

    # -- background flush ------------------------------------------------

    @tasks.loop(seconds=UPDATE_INTERVAL)
    async def flush_loop(self) -> None:
        now = _now()
        retention = CLOSED_RETENTION_DAYS * 86400

        async with self._lock:
            expired = [
                p
                for p in self.polls.values()
                if not p.closed and p.ends_at is not None and p.ends_at <= now and not p.busy
            ]
            stale = [
                p
                for p in self.polls.values()
                if p.closed and p.closed_at is not None and now - p.closed_at > retention
            ]
            dirty = [
                p
                for p in self.polls.values()
                if p.dirty and not p.closed and p not in expired
            ]

        # A recent 429 pauses all edits; state still gets saved below.
        if now < self._backoff_until:
            expired = dirty = []

        edits = 0
        backed_off = False

        # Close expired polls first, but never more than the per-tick cap;
        # the rest are picked up on the next pass. Polls rarely expire in
        # a batch, so this mainly guards restarts and coincidental clumps.
        for poll in expired:
            if edits >= EDITS_PER_TICK:
                break
            result = await self.close_poll(poll, "Voting period ended.")
            edits += 1
            if result == "ratelimited":
                backed_off = True
                break
            await asyncio.sleep(EDIT_SPACING)

        if not backed_off:
            for poll in dirty:
                if edits >= EDITS_PER_TICK:
                    break  # stays dirty; refreshed next tick
                poll.dirty = False
                ok = await self._refresh(poll)
                edits += 1
                if not ok:
                    backed_off = True
                    break
                await asyncio.sleep(EDIT_SPACING)

        if backed_off:
            self._backoff_until = _now() + RATE_LIMIT_BACKOFF
            log.warning(
                "Poll edits hit a rate limit - pausing edits for %.0fs",
                RATE_LIMIT_BACKOFF,
            )

        if stale:
            async with self._lock:
                for poll in stale:
                    log.info("Pruning closed poll %s after retention", poll.id)
                    self._discard(poll)

        if self._state_dirty:
            async with self._lock:
                await self._save_state()

    @flush_loop.before_loop
    async def _before_flush(self) -> None:
        await self.bot.wait_until_ready()

    @flush_loop.error
    async def _flush_error(self, error: BaseException) -> None:  # pragma: no cover
        log.exception("Poll flush loop error", exc_info=error)
        if not self.flush_loop.is_running():
            self.flush_loop.restart()

    # -- message rendering -----------------------------------------------

    def _partial_message(self, poll: Poll) -> Optional[discord.PartialMessage]:
        channel = self.bot.get_channel(poll.channel_id)
        if channel is None:
            channel = self.bot.get_partial_messageable(poll.channel_id)
        try:
            return channel.get_partial_message(poll.message_id)  # type: ignore[union-attr]
        except AttributeError:
            return None

    async def _refresh(self, poll: Poll) -> bool:
        """Edit a poll message to its current state.

        Returns ``False`` if the edit was rate limited (the caller should
        stop the batch and back off); ``True`` otherwise, including when
        the poll was dropped for a missing message or lost permissions.
        """
        message = self._partial_message(poll)
        if message is None:
            self._discard(poll)
            return True
        try:
            await message.edit(
                embed=self.build_public_embed(poll), allowed_mentions=NO_MENTIONS
            )
        except discord.NotFound:
            log.info("Poll %s message is gone - dropping", poll.id)
            self._discard(poll)
        except discord.Forbidden:
            log.warning("Missing permissions to edit poll %s - dropping", poll.id)
            self._discard(poll)
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                poll.dirty = True  # keep it queued for a later tick
                return False
            log.exception("Failed to refresh poll %s", poll.id)
        return True

    def _tally_lines(self, poll: Poll, *, highlight_winner: bool) -> List[str]:
        counts = poll.first_preferences()
        total = sum(counts) if poll.style == STYLE_MULTI else poll.voter_count
        # Bold every winning option once the poll is decided. For ranked
        # multi-winner this is the STV set, not simply the highest bars.
        winners = set(poll.winner_set()) if highlight_winner else set()
        lines: List[str] = []
        for index in range(min(len(poll.options), MAX_EMBED_OPTION_LINES)):
            crown = "\U0001f3c6 " if index in winners else ""
            marker = "**" if index in winners else ""
            lines.append(
                f"{marker}{crown}{poll.option_line(index)}{marker}\n"
                f"`{_bar(counts[index], total)}` **{counts[index]}**"
                f" ({_pct(counts[index], total):.0f}%)"
            )
        overflow = len(poll.options) - MAX_EMBED_OPTION_LINES
        if overflow > 0:
            lines.append(f"\u2026 and {overflow} more option(s).")
        return lines

    def _option_lines(self, poll: Poll) -> List[str]:
        lines = [
            poll.option_line(index)
            for index in range(min(len(poll.options), MAX_EMBED_OPTION_LINES))
        ]
        overflow = len(poll.options) - MAX_EMBED_OPTION_LINES
        if overflow > 0:
            lines.append(f"\u2026 and {overflow} more option(s).")
        return lines

    def build_public_embed(self, poll: Poll) -> discord.Embed:
        """The embed shown on the poll message itself."""
        embed = discord.Embed(
            title=_truncate(_clean(poll.question), 256),
            colour=COLOUR_CLOSED if poll.closed else COLOUR_OPEN,
        )

        show_tally = poll.published or not poll.hidden
        if show_tally:
            lines = self._tally_lines(poll, highlight_winner=poll.closed)
            if poll.style == STYLE_RANKED and (poll.closed or poll.published):
                embed.add_field(
                    name="Single transferable vote" if poll.multi_winner else "Instant runoff",
                    value=_truncate(self._render_rounds(poll), 1024),
                    inline=False,
                )
            elif poll.style == STYLE_RANKED:
                embed.set_footer(text="Showing first preferences only.")
        else:
            lines = self._option_lines(poll)
            lines.append("\n*Results are private to the poll author.*")

        embed.description = _truncate("\n".join(lines), 4000)

        meta = [STYLE_LABELS[poll.style], f"{poll.voter_count} voter(s)"]
        if poll.multi_winner:
            meta.append(f"{poll.winners} winners")
        if poll.anonymous:
            meta.append("anonymous")
        if poll.hidden:
            meta.append("hidden results")
        if poll.role_id:
            meta.append("role restricted")
        if poll.published:
            meta.append("results published")
        embed.add_field(name="\u200b", value=" \u2022 ".join(meta), inline=False)

        if poll.closed:
            embed.add_field(name="Status", value="Closed", inline=False)
        elif poll.ends_at is not None:
            when = datetime.fromtimestamp(poll.ends_at, tz=timezone.utc)
            embed.add_field(
                name="Closes", value=discord.utils.format_dt(when, "R"), inline=False
            )
        else:
            embed.add_field(
                name="Closes", value="When the author closes it", inline=False
            )

        return embed

    def build_results_embed(
        self, poll: Poll, *, include_voters: bool
    ) -> discord.Embed:
        """Ephemeral results embed for /poll results."""
        embed = discord.Embed(
            title=f"Results \u2014 {_truncate(_clean(poll.question), 230)}",
            colour=COLOUR_CLOSED if poll.closed else COLOUR_OPEN,
        )
        embed.description = _truncate(
            "\n".join(self._tally_lines(poll, highlight_winner=True)), 4000
        )

        if poll.style == STYLE_RANKED:
            if poll.multi_winner:
                label = "Single transferable vote"
            else:
                label = "Instant runoff" if poll.closed else "Projected runoff"
            embed.add_field(
                name=label,
                value=_truncate(self._render_rounds(poll), 1024),
                inline=False,
            )

        if include_voters:
            for name, value in self._voter_fields(poll):
                embed.add_field(name=name, value=_truncate(value, 1024), inline=False)
        elif poll.anonymous:
            embed.set_footer(text="This poll is anonymous - individual votes are not shown.")

        return embed

    def _voter_fields(self, poll: Poll) -> List[Tuple[str, str]]:
        """Per-user vote breakdown, capped to stay inside embed limits."""
        if not poll.ballots:
            return [("Voters", "No ballots have been cast.")]

        if poll.style == STYLE_RANKED:
            lines = []
            for uid, ballot in list(poll.ballots.items())[:MAX_RANKED_BALLOT_LINES]:
                ranking = " > ".join(poll.label(i) for i in ballot)
                lines.append(f"{poll.voter_name(uid)}: {ranking}")
            overflow = poll.voter_count - MAX_RANKED_BALLOT_LINES
            if overflow > 0:
                lines.append(f"\u2026 and {overflow} more ballot(s).")
            return [("Ballots", "\n".join(lines))]

        fields: List[Tuple[str, str]] = []
        for index in range(min(len(poll.options), MAX_EMBED_OPTION_LINES)):
            if poll.style == STYLE_SINGLE:
                voters = [uid for uid, choice in poll.ballots.items() if choice == index]
            else:
                voters = [uid for uid, mask in poll.ballots.items() if mask & (1 << index)]
            if not voters:
                continue
            shown = ", ".join(poll.voter_name(uid) for uid in voters[:MAX_VOTER_NAMES_PER_OPTION])
            overflow = len(voters) - MAX_VOTER_NAMES_PER_OPTION
            if overflow > 0:
                shown += f" \u2026 and {overflow} more"
            fields.append((poll.option_line(index, 60), shown))
        return fields or [("Voters", "No ballots have been cast.")]

    @staticmethod
    def _collapse_middle(lines: List[str], budget: int) -> List[str]:
        """Trim a list of lines to fit ``budget`` chars, dropping the middle.

        The decisive rounds of an instant-runoff count are the last ones,
        so the tail is kept preferentially over the head.
        """
        if sum(len(line) + 1 for line in lines) <= budget:
            return lines
        marker = "\u2026 middle rounds omitted \u2026"
        budget -= len(marker) + 1
        head: List[str] = []
        tail: List[str] = []
        i, j = 0, len(lines) - 1
        used = 0
        take_tail = True  # favour the closely contested final rounds
        while i <= j:
            if take_tail:
                need = len(lines[j]) + 1
                if used + need > budget:
                    break
                tail.append(lines[j])
                used += need
                j -= 1
            else:
                need = len(lines[i]) + 1
                if used + need > budget:
                    break
                head.append(lines[i])
                used += need
                i += 1
            take_tail = not take_tail
        return head + [marker] + list(reversed(tail))

    def _render_rounds(self, poll: Poll) -> str:
        if poll.multi_winner:
            return self._render_stv(poll)
        return self._render_irv(poll)

    def _fmt_frac(self, value: "Fraction") -> str:
        """Compact display of a Fraction weight: integer or 1-dp decimal."""
        if value.denominator == 1:
            return str(value.numerator)
        return f"{float(value):.1f}"

    def _render_stv(self, poll: Poll) -> str:
        rounds, winners = single_transferable_vote(
            poll.ballots.values(), len(poll.options), poll.winners
        )
        if not rounds:
            return "No ballots were cast."

        quota = rounds[-1]["quota"]
        log: List[str] = []
        for number, rnd in enumerate(rounds, start=1):
            if rnd["elected"]:
                who = ", ".join(poll.label(i) for i in rnd["elected"])
                log.append(f"R{number}: elected {who} (quota {self._fmt_frac(rnd['quota'])})")
            elif rnd["eliminated"] is not None:
                idx = rnd["eliminated"]
                log.append(
                    f"R{number}: {poll.label(idx)} out "
                    f"({self._fmt_frac(rnd['counts'][idx])} vote(s))"
                )
        log = self._collapse_middle(log, RUNOFF_CHAR_BUDGET)

        parts = [f"Electing {poll.winners} \u2022 Droop quota {self._fmt_frac(quota)}"]
        parts.extend(log)
        if winners:
            names = ", ".join(
                f"{poll.label(i)}. {_clean(_truncate(poll.options[i], 40))}"
                for i in winners
            )
            parts.append(f"**Elected ({len(winners)}):** {names}")
        else:
            parts.append("**Result:** no seats could be filled.")
        return _truncate("\n".join(parts), 1024)

    def _render_irv(self, poll: Poll) -> str:
        rounds, winner = instant_runoff(poll.ballots.values(), len(poll.options))
        if not rounds:
            return "No ballots were cast."

        # One short line per round records only what changed - who was
        # eliminated and on how many votes - rather than re-listing every
        # surviving candidate each round, which overflows the embed field.
        log: List[str] = []
        for number, (counts, exhausted, eliminated) in enumerate(rounds, start=1):
            if eliminated is None:
                continue
            note = f", {exhausted} exhausted" if exhausted else ""
            log.append(
                f"R{number}: {poll.label(eliminated)} out "
                f"({counts[eliminated]} vote(s){note})"
            )
        log = self._collapse_middle(log, RUNOFF_CHAR_BUDGET)

        # Final standings come from the last counted round.
        final_counts = rounds[-1][0]
        survivors = sorted(
            (
                i
                for i in range(len(poll.options))
                if final_counts[i] > 0 or i == winner
            ),
            key=lambda i: (-final_counts[i], i),
        )
        standings = " \u00b7 ".join(
            f"{poll.label(i)}:{final_counts[i]}" for i in survivors
        )

        parts = list(log)
        if standings:
            parts.append(f"**Final round:** {standings}")
        if winner is None:
            parts.append("**Result:** no majority could be resolved (tie).")
        else:
            parts.append(
                f"**Winner:** {poll.label(winner)}. "
                f"{_clean(_truncate(poll.options[winner], MAX_OPTION_LEN))}"
            )
        return _truncate("\n".join(parts), 1024)

    # -- permissions -----------------------------------------------------

    @staticmethod
    def _is_owner(user_id: int) -> bool:
        return user_id == OWNER_ID

    def _is_author_level(self, user_id: int, poll: Poll) -> bool:
        """Author-level commands: the poll author, or the bot owner always."""
        return user_id == poll.author_id or self._is_owner(user_id)

    # -- vote handling ---------------------------------------------------

    @staticmethod
    async def reject(interaction: discord.Interaction, message: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.debug("Could not deliver rejection notice", exc_info=True)

    def check_eligibility(
        self, interaction: discord.Interaction, poll: Poll
    ) -> Optional[str]:
        """Return an error string when the user may not vote, else ``None``."""
        if poll.closed:
            return "This poll is closed."
        if poll.role_id:
            member = interaction.user
            if not isinstance(member, discord.Member) or not any(
                role.id == poll.role_id for role in member.roles
            ):
                return "You do not have the role required to vote in this poll."
        already_voted = interaction.user.id in poll.ballots
        if already_voted and not poll.allow_change:
            return "This poll does not allow changing your vote."
        if not already_voted and len(poll.ballots) >= MAX_BALLOTS_PER_POLL:
            return "This poll has reached its maximum number of voters."
        return None

    @staticmethod
    def _remember_name(poll: Poll, user: discord.abc.User) -> None:
        poll.names[user.id] = getattr(user, "display_name", None) or str(user)

    async def handle_choice(
        self, interaction: discord.Interaction, poll_id: str, index: int
    ) -> None:
        poll = self.polls.get(poll_id)
        if poll is None:
            await self.reject(interaction, "This poll is no longer available.")
            return

        problem = self.check_eligibility(interaction, poll)
        if problem:
            await self.reject(interaction, problem)
            return
        if index >= len(poll.options):
            await self.reject(interaction, "That option no longer exists.")
            return

        user_id = interaction.user.id
        async with self._lock:
            if poll.style == STYLE_MULTI:
                mask = poll.ballots.get(user_id, 0)
                mask ^= 1 << index
                if mask:
                    poll.ballots[user_id] = mask
                    self._remember_name(poll, interaction.user)
                else:
                    poll.ballots.pop(user_id, None)
                    poll.names.pop(user_id, None)
                added = bool(mask & (1 << index))
                verb = "Added" if added else "Removed"
            else:
                if poll.ballots.get(user_id) == index:
                    await self.reject(interaction, "That is already your vote.")
                    return
                poll.ballots[user_id] = index
                self._remember_name(poll, interaction.user)
                verb = "Recorded"
            poll.dirty = True
            self._state_dirty = True

        await interaction.response.send_message(
            f"{verb}: **{poll.option_line(index)}**", ephemeral=True
        )

    async def handle_ranking(
        self, interaction: discord.Interaction, poll_id: str, raw: str
    ) -> None:
        poll = self.polls.get(poll_id)
        if poll is None:
            await self.reject(interaction, "This poll is no longer available.")
            return

        problem = self.check_eligibility(interaction, poll)
        if problem:
            await self.reject(interaction, problem)
            return

        count = len(poll.options)
        seen: List[int] = []
        for token in _RANK_TOKEN.findall(raw):
            number = int(token)
            if not 1 <= number <= count:
                await self.reject(
                    interaction, f"`{number}` is not an option (use 1-{count})."
                )
                return
            index = number - 1
            if index in seen:
                await self.reject(
                    interaction, f"Option `{number}` appears more than once."
                )
                return
            seen.append(index)

        if not seen:
            await self.reject(
                interaction,
                "Enter option numbers in order of preference, e.g. `2 1 3`.",
            )
            return

        async with self._lock:
            poll.ballots[interaction.user.id] = bytes(seen)
            self._remember_name(poll, interaction.user)
            poll.dirty = True
            self._state_dirty = True

        ordered = " > ".join(poll.option_line(i, 40) for i in seen[:10])
        if len(seen) > 10:
            ordered += f" \u2026 (+{len(seen) - 10} more)"
        await interaction.response.send_message(
            f"Ballot recorded:\n{ordered}", ephemeral=True
        )

    async def handle_retract(self, interaction: discord.Interaction, poll_id: str) -> None:
        poll = self.polls.get(poll_id)
        if poll is None:
            await self.reject(interaction, "This poll is no longer available.")
            return
        if poll.closed:
            await self.reject(interaction, "This poll is closed.")
            return
        if not poll.allow_change:
            await self.reject(interaction, "This poll does not allow changing your vote.")
            return

        async with self._lock:
            removed = poll.ballots.pop(interaction.user.id, None) is not None
            poll.names.pop(interaction.user.id, None)
            if removed:
                poll.dirty = True
                self._state_dirty = True

        await interaction.response.send_message(
            "Your vote has been retracted." if removed else "You have not voted yet.",
            ephemeral=True,
        )

    # -- state transitions -----------------------------------------------

    async def close_poll(self, poll: Poll, reason: str) -> Optional[str]:
        """Close a poll and edit its message.

        Returns ``"ratelimited"`` if the closing edit hit a 429 (the poll
        is still marked closed and will be pruned normally; only the
        message edit failed), else ``None``.
        """
        if poll.closed or poll.busy:
            return None
        poll.busy = True
        try:
            poll.closed = True
            poll.closed_at = _now()
            self._stop_view(poll.id)
            self._state_dirty = True

            embed = self.build_public_embed(poll)
            embed.set_footer(text=reason)
            message = self._partial_message(poll)
            if message is not None:
                try:
                    await message.edit(
                        embed=embed, view=None, allowed_mentions=NO_MENTIONS
                    )
                except discord.NotFound:
                    log.info("Poll %s message missing at close", poll.id)
                    self._discard(poll)
                except discord.Forbidden:
                    log.warning("Missing permissions to close poll %s", poll.id)
                except discord.HTTPException as exc:
                    if getattr(exc, "status", None) == 429:
                        return "ratelimited"
                    log.exception("Failed to edit poll %s at close", poll.id)
        finally:
            poll.busy = False
        return None

    async def reopen_poll(self, poll: Poll) -> Optional[str]:
        """Reopen a closed poll. Returns an error string on failure."""
        if not poll.closed:
            return "That poll is already open."
        if poll.busy:
            return "That poll is busy - try again in a moment."
        poll.busy = True
        try:
            poll.closed = False
            poll.closed_at = None
            # An expired deadline would immediately re-close it; reopened
            # polls stay open until closed manually.
            if poll.ends_at is not None and poll.ends_at <= _now():
                poll.ends_at = None
            self._register_view(poll)
            self._state_dirty = True

            message = self._partial_message(poll)
            if message is None:
                self._discard(poll)
                return "The poll message could not be found."
            try:
                await message.edit(
                    embed=self.build_public_embed(poll),
                    view=self._views[poll.id],
                    allowed_mentions=NO_MENTIONS,
                )
            except discord.NotFound:
                self._discard(poll)
                return "The poll message has been deleted."
            except discord.Forbidden:
                return "I no longer have permission to edit the poll message."
            except discord.HTTPException:
                log.exception("Failed to reopen poll %s", poll.id)
                return "Discord rejected the edit - try again."
            return None
        finally:
            poll.busy = False

    # -- lookup + autocomplete -------------------------------------------

    def _guild_polls(self, guild_id: int) -> List[Poll]:
        return [poll for poll in self.polls.values() if poll.guild_id == guild_id]

    def _resolve(
        self, interaction: discord.Interaction, message_id: Optional[str]
    ) -> Tuple[Optional[Poll], Optional[str]]:
        guild_id = interaction.guild_id or 0
        if message_id:
            cleaned = message_id.strip()
            if not cleaned.isdigit():
                return None, "That does not look like a valid poll."
            target = int(cleaned)
            for poll in self._guild_polls(guild_id):
                if poll.message_id == target:
                    return poll, None
            return None, "No poll found with that ID."

        candidates = [
            poll
            for poll in self._guild_polls(guild_id)
            if poll.channel_id == (interaction.channel_id or 0)
            and self._is_author_level(interaction.user.id, poll)
        ]
        if not candidates:
            return None, "You have no poll in this channel - pick one from the list."
        candidates.sort(key=lambda p: p.created_at, reverse=True)
        return candidates[0], None

    def _autocomplete_choices(
        self,
        interaction: discord.Interaction,
        current: str,
        *,
        want_closed: Optional[bool] = None,
        author_level_only: bool = False,
    ) -> List[app_commands.Choice[str]]:
        needle = current.casefold()
        polls = self._guild_polls(interaction.guild_id or 0)
        polls.sort(key=lambda p: p.created_at, reverse=True)

        choices: List[app_commands.Choice[str]] = []
        for poll in polls:
            if want_closed is not None and poll.closed is not want_closed:
                continue
            if author_level_only and not self._is_author_level(
                interaction.user.id, poll
            ):
                continue
            if needle and needle not in poll.question.casefold():
                continue
            status = "closed" if poll.closed else "open"
            label = _truncate(
                f"[{status}] {poll.question} ({poll.voter_count} votes)", 100
            )
            choices.append(
                app_commands.Choice(name=label, value=str(poll.message_id))
            )
            if len(choices) >= 25:
                break
        return choices

    async def ac_any_poll(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        return self._autocomplete_choices(interaction, current)

    async def ac_open_poll(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        return self._autocomplete_choices(interaction, current, want_closed=False)

    async def ac_closed_poll(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        return self._autocomplete_choices(interaction, current, want_closed=True)

    # -- commands --------------------------------------------------------

    @poll_group.command(name="create", description="Start a new poll.")
    @app_commands.describe(
        question="The question being asked.",
        options=(
            "Choices separated by commas, e.g. Red, Green, Blue "
            f"(use '{OPTION_SEPARATOR_ALT}' if an option contains a comma)."
        ),
        style="How votes are cast and counted.",
        duration=(
            f"Minutes until the poll closes ({MIN_DURATION}-{MAX_DURATION}). "
            "Leave empty to keep it open until you close it."
        ),
        anonymous="Nobody, including you, can see who voted for what.",
        hidden="Only you can see the results until you publish them.",
        lock_votes="Prevent voters from changing or retracting their vote.",
        role="Restrict voting to members with this role.",
        winners=(
            f"How many options win (1-{MAX_WINNERS}, default 1). "
            "Ranked polls use proportional STV; others take the top N."
        ),
    )
    @app_commands.choices(
        style=[
            app_commands.Choice(
                name="Single choice (first past the post)", value=STYLE_SINGLE
            ),
            app_commands.Choice(name="Multiple choice (approval)", value=STYLE_MULTI),
            app_commands.Choice(
                name="Ranked choice (instant runoff)", value=STYLE_RANKED
            ),
        ]
    )
    @app_commands.checks.cooldown(2, 30.0, key=lambda i: (i.guild_id, i.user.id))
    async def poll_create(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, MAX_QUESTION_LEN],
        options: str,
        style: Optional[app_commands.Choice[str]] = None,
        duration: Optional[app_commands.Range[int, MIN_DURATION, MAX_DURATION]] = None,
        anonymous: bool = False,
        hidden: bool = False,
        lock_votes: bool = False,
        role: Optional[discord.Role] = None,
        winners: app_commands.Range[int, 1, MAX_WINNERS] = 1,
    ) -> None:
        chosen_style = style.value if style else STYLE_SINGLE
        channel = interaction.channel
        if interaction.guild_id is None or channel is None:
            await self.reject(interaction, "Polls can only be created in a server.")
            return

        perms = interaction.app_permissions
        if not (perms.send_messages and perms.embed_links):
            await self.reject(
                interaction,
                "I need the Send Messages and Embed Links permissions here.",
            )
            return

        limit = (
            MAX_OPTIONS_RANKED if chosen_style == STYLE_RANKED else MAX_OPTIONS_BUTTONS
        )
        parsed = self._parse_options(options, limit, chosen_style)
        if isinstance(parsed, str):
            await self.reject(interaction, parsed)
            return

        if winners >= len(parsed):
            await self.reject(
                interaction,
                f"A poll with {len(parsed)} options can elect at most "
                f"{len(parsed) - 1} winner(s). Add more options or lower the "
                "winner count.",
            )
            return

        if len(self._guild_polls(interaction.guild_id)) >= MAX_POLLS_PER_GUILD:
            await self.reject(
                interaction,
                f"This server already has {MAX_POLLS_PER_GUILD} polls. "
                "Cancel an old one first.",
            )
            return

        normalised = question.strip().casefold()
        for existing in self._guild_polls(interaction.guild_id):
            if (
                not existing.closed
                and existing.channel_id == interaction.channel_id
                and existing.question.strip().casefold() == normalised
            ):
                await self.reject(
                    interaction, "An identical poll is already open in this channel."
                )
                return

        await interaction.response.defer(ephemeral=True, thinking=True)

        poll = Poll(
            id=secrets.token_hex(6),
            guild_id=interaction.guild_id,
            channel_id=channel.id,
            message_id=0,
            author_id=interaction.user.id,
            question=question.strip(),
            options=parsed,
            style=chosen_style,
            created_at=_now(),
            ends_at=_now() + duration * 60 if duration is not None else None,
            anonymous=anonymous,
            hidden=hidden,
            allow_change=not lock_votes,
            role_id=role.id if role else None,
            winners=winners,
        )

        view = PollView(self, poll)
        try:
            message = await channel.send(  # type: ignore[union-attr]
                embed=self.build_public_embed(poll),
                view=view,
                allowed_mentions=NO_MENTIONS,
            )
        except discord.Forbidden:
            view.stop()
            await interaction.followup.send(
                "I am not allowed to post in this channel.", ephemeral=True
            )
            return
        except discord.HTTPException:
            view.stop()
            log.exception("Failed to post poll message")
            await interaction.followup.send(
                "Discord rejected the poll message. Please try again.", ephemeral=True
            )
            return

        poll.message_id = message.id
        async with self._lock:
            self.polls[poll.id] = poll
            self._views[poll.id] = view
            self._state_dirty = True
            await self._save_state()

        notes = []
        if anonymous:
            notes.append("anonymous")
        if hidden:
            notes.append("hidden")
        suffix = f" ({', '.join(notes)})" if notes else ""
        await interaction.followup.send(
            f"Poll created{suffix}: {message.jump_url}", ephemeral=True
        )

    @staticmethod
    def _parse_options(raw: str, limit: int, style: str) -> Any:
        separator = (
            OPTION_SEPARATOR_ALT if OPTION_SEPARATOR_ALT in raw else OPTION_SEPARATOR
        )
        seen: Dict[str, None] = {}
        options: List[str] = []
        for chunk in raw.split(separator):
            option = " ".join(chunk.split())
            if not option:
                continue
            if len(option) > MAX_OPTION_LEN:
                return f"Options must be {MAX_OPTION_LEN} characters or fewer."
            key = option.casefold()
            if key in seen:
                return f"Duplicate option: {option}"
            seen[key] = None
            options.append(option)

        if len(options) < MIN_OPTIONS:
            return (
                f"Provide at least {MIN_OPTIONS} options separated by commas, "
                f"e.g. `Red, Green, Blue`. If an option contains a comma, "
                f"separate the whole list with '{OPTION_SEPARATOR_ALT}' instead."
            )
        if len(options) > limit:
            extra = (
                " Ranked polls allow up to "
                f"{MAX_OPTIONS_RANKED}."
                if style != STYLE_RANKED
                else ""
            )
            return f"A {STYLE_LABELS[style].lower()} poll allows at most {limit} options.{extra}"
        return options

    @poll_group.command(name="close", description="Close a poll (author or owner).")
    @app_commands.describe(poll="The poll to close (defaults to your latest here).")
    @app_commands.autocomplete(poll=ac_open_poll)
    async def poll_close(
        self, interaction: discord.Interaction, poll: Optional[str] = None
    ) -> None:
        found, error = self._resolve(interaction, poll)
        if found is None:
            await self.reject(interaction, error or "Poll not found.")
            return
        if not self._is_author_level(interaction.user.id, found):
            await self.reject(
                interaction, "Only the poll author or the bot owner can close this poll."
            )
            return
        if found.closed:
            await self.reject(interaction, "That poll is already closed.")
            return

        await interaction.response.send_message("Closing the poll\u2026", ephemeral=True)
        await self.close_poll(
            found, f"Closed by {interaction.user.display_name}."
        )

    @poll_group.command(name="reopen", description="Reopen a closed poll (author or owner).")
    @app_commands.describe(poll="The closed poll to reopen.")
    @app_commands.autocomplete(poll=ac_closed_poll)
    async def poll_reopen(
        self, interaction: discord.Interaction, poll: Optional[str] = None
    ) -> None:
        found, error = self._resolve(interaction, poll)
        if found is None:
            await self.reject(interaction, error or "Poll not found.")
            return
        if not self._is_author_level(interaction.user.id, found):
            await self.reject(
                interaction, "Only the poll author or the bot owner can reopen this poll."
            )
            return

        await interaction.response.defer(ephemeral=True)
        problem = await self.reopen_poll(found)
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return
        note = (
            " Its original deadline had passed, so it will now stay open until closed."
            if found.ends_at is None
            else ""
        )
        await interaction.followup.send(f"Poll reopened.{note}", ephemeral=True)

    @poll_group.command(
        name="cancel", description="Delete a poll and its message (author or owner)."
    )
    @app_commands.describe(poll="The poll to cancel (defaults to your latest here).")
    @app_commands.autocomplete(poll=ac_any_poll)
    async def poll_cancel(
        self, interaction: discord.Interaction, poll: Optional[str] = None
    ) -> None:
        found, error = self._resolve(interaction, poll)
        if found is None:
            await self.reject(interaction, error or "Poll not found.")
            return
        if not self._is_author_level(interaction.user.id, found):
            await self.reject(
                interaction, "Only the poll author or the bot owner can cancel this poll."
            )
            return

        found.busy = True
        message = self._partial_message(found)
        if message is not None:
            try:
                await message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
            except discord.HTTPException:
                log.exception("Failed to delete cancelled poll %s", found.id)

        async with self._lock:
            self._discard(found)
        await interaction.response.send_message("Poll cancelled and deleted.", ephemeral=True)

    @poll_group.command(
        name="publish",
        description="Publish tallies on the poll message, even for hidden polls (author or owner).",
    )
    @app_commands.describe(poll="The poll whose results to publish.")
    @app_commands.autocomplete(poll=ac_any_poll)
    async def poll_publish(
        self, interaction: discord.Interaction, poll: Optional[str] = None
    ) -> None:
        found, error = self._resolve(interaction, poll)
        if found is None:
            await self.reject(interaction, error or "Poll not found.")
            return
        if not self._is_author_level(interaction.user.id, found):
            await self.reject(
                interaction,
                "Only the poll author or the bot owner can publish results.",
            )
            return
        if found.published:
            await self.reject(interaction, "Results are already published.")
            return

        await interaction.response.defer(ephemeral=True)
        found.published = True
        self._state_dirty = True
        await self._refresh(found)
        if found.id not in self.polls:
            await interaction.followup.send(
                "The poll message could not be updated (it may have been deleted).",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "Results published on the poll message. Individual votes remain private.",
            ephemeral=True,
        )

    @poll_group.command(
        name="results", description="Privately view a poll's results."
    )
    @app_commands.describe(poll="The poll to inspect (defaults to your latest here).")
    @app_commands.autocomplete(poll=ac_any_poll)
    async def poll_results(
        self, interaction: discord.Interaction, poll: Optional[str] = None
    ) -> None:
        found, error = self._resolve(interaction, poll)
        if found is None:
            await self.reject(interaction, error or "Poll not found.")
            return

        viewer = interaction.user.id
        is_owner = self._is_owner(viewer)
        is_author = viewer == found.author_id

        # The owner sees everything, always.
        if is_owner:
            include_voters = True
        elif found.hidden and not is_author and not found.published:
            await self.reject(
                interaction, "Results for this poll are private to its author."
            )
            return
        elif found.anonymous:
            include_voters = False
        elif found.hidden:
            # Hidden, not anonymous: the author may see voters; others only
            # reach this branch once results are published, tallies only.
            include_voters = is_author
        else:
            include_voters = True  # regular mode: anyone may see who voted what

        embed = self.build_results_embed(found, include_voters=include_voters)
        if is_owner and (found.anonymous or found.hidden):
            embed.set_footer(text="Owner view - full details regardless of poll mode.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @poll_group.command(name="list", description="List polls in this server.")
    async def poll_list(self, interaction: discord.Interaction) -> None:
        polls = sorted(
            self._guild_polls(interaction.guild_id or 0), key=lambda p: p.created_at
        )
        if not polls:
            await interaction.response.send_message(
                "There are no polls in this server.", ephemeral=True
            )
            return

        embed = discord.Embed(title="Polls", colour=COLOUR_OPEN)
        for poll in polls[:25]:
            if poll.closed:
                timing = "closed"
            elif poll.ends_at:
                timing = "closes " + discord.utils.format_dt(
                    datetime.fromtimestamp(poll.ends_at, tz=timezone.utc), "R"
                )
            else:
                timing = "open until closed"
            flags = "".join(
                f" \u2022 {flag}"
                for flag, on in (
                    ("anonymous", poll.anonymous),
                    ("hidden", poll.hidden),
                    ("published", poll.published),
                )
                if on
            )
            embed.add_field(
                name=_truncate(_clean(poll.question), 200),
                value=(
                    f"{STYLE_LABELS[poll.style]} \u2022 {poll.voter_count} voter(s) "
                    f"\u2022 {timing}{flags}\n"
                    f"https://discord.com/channels/{poll.guild_id}/"
                    f"{poll.channel_id}/{poll.message_id}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- error handling --------------------------------------------------

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await self.reject(
                interaction, f"Slow down - try again in {error.retry_after:.0f}s."
            )
            return
        if isinstance(error, app_commands.MissingPermissions):
            await self.reject(interaction, "You do not have permission to do that.")
            return
        if isinstance(error, app_commands.CheckFailure):
            await self.reject(interaction, "That command is not available here.")
            return

        log.exception("Unhandled poll command error", exc_info=error)
        await self.reject(interaction, "Something went wrong running that command.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Polls(bot))