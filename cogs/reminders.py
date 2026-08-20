import asyncio
import difflib
import os
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo, available_timezones

import aiosqlite
import discord
from discord.ext import commands, tasks

from env import OWNER_ID

DB_PATH = "db/reminders.db"

PAGE_SIZE = 6
LIST_VIEW_TIMEOUT = 180
NOTIFY_VIEW_TIMEOUT = 3600
CHECK_INTERVAL_SECONDS = 20
DEFAULT_HOUR = 9  # used when a day is given without a time, e.g. "friday buy milk"

# Locale -> timezone heuristic, used only when the user has not set a timezone.
LOCALE_TZ_MAP = {
    "en-us": "America/New_York",
    "en-gb": "Europe/London",
    "en-ca": "America/Toronto",
    "en-au": "Australia/Sydney",
    "ms": "Asia/Kuala_Lumpur",
    "ms-my": "Asia/Kuala_Lumpur",
    "zh-cn": "Asia/Shanghai",
    "zh-tw": "Asia/Taipei",
    "ja": "Asia/Tokyo",
    "ko": "Asia/Seoul",
    "fr": "Europe/Paris",
    "de": "Europe/Berlin",
    "es-es": "Europe/Madrid",
    "es": "America/Mexico_City",
}

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

REPEAT_KEYWORDS = {
    "minute": timedelta(minutes=1), "minutes": timedelta(minutes=1),
    "min": timedelta(minutes=1), "mins": timedelta(minutes=1),
    "minutely": timedelta(minutes=1),
    "hour": timedelta(hours=1), "hours": timedelta(hours=1),
    "hr": timedelta(hours=1), "hrs": timedelta(hours=1),
    "hourly": timedelta(hours=1),
    "day": timedelta(days=1), "days": timedelta(days=1), "daily": timedelta(days=1),
    "week": timedelta(weeks=1), "weeks": timedelta(weeks=1), "weekly": timedelta(weeks=1),
    "month": timedelta(days=30), "months": timedelta(days=30), "monthly": timedelta(days=30),
    "year": timedelta(days=365), "years": timedelta(days=365), "yearly": timedelta(days=365),
}

DURATION_PART_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>weeks?|w|days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)",
    re.IGNORECASE,
)

UNIT_SECONDS = {
    "w": 604800, "week": 604800, "weeks": 604800,
    "d": 86400, "day": 86400, "days": 86400,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
}

TARGET_PREFIX_RE = re.compile(
    r"^(?:<@!?(?P<mention_id>\d+)>|(?:user|target|to)\s*:\s*(?P<typed_id>\d+))\s+(?P<rest>.+)$",
    re.IGNORECASE | re.DOTALL,
)

# Time must have :MM (am/pm optional) OR am/pm directly, so bare numbers
# like the "25" in "dec 25" never get mistaken for a time.
TIME_RE = re.compile(
    r"\b(?:"
    # compact form: 630pm, 1230am (am/pm required so bare numbers aren't times)
    r"(?P<ch>\d{1,2})(?P<cm>\d{2})\s*(?P<cap>am|pm)"
    r"|"
    # standard form: 6:30, 6:30:15 pm, 9pm
    r"(?P<h>\d{1,2})"
    r"(?::(?P<m>\d{2})(?::(?P<s>\d{2}))?\s*(?P<ap>am|pm)?|\s*(?P<ap2>am|pm))"
    r")\b",
    re.IGNORECASE,
)

DATE_ISO_RE = re.compile(r"\b(?P<y>\d{4})-(?P<mo>\d{1,2})-(?P<d>\d{1,2})\b")
DATE_NUM_RE = re.compile(r"\b(?P<a>\d{1,2})[/.](?P<b>\d{1,2})(?:[/.](?P<y>\d{2,4}))?\b")
DATE_MON_RE = re.compile(
    r"\b(?:"
    r"(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+(?P<mon1>jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*"
    r"|(?P<mon2>jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?"
    r")\b",
    re.IGNORECASE,
)
DAY_WORD_RE = re.compile(
    r"\b(?:(?P<next>next)\s+)?"
    r"(?P<word>today|tomorrow|tmrw|tmr|tonight|noon|midnight|"
    r"monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu|"
    r"friday|fri|saturday|sat|sunday|sun)\b",
    re.IGNORECASE,
)

FILLER_RES = [
    re.compile(r"^(?:please\s+)?remind\s+me\s+to\s+", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?remind\s+me\s+", re.IGNORECASE),
    re.compile(r"^(?:set\s+a\s+)?reminder\s+to\s+", re.IGNORECASE),
    re.compile(r"^(?:set\s+)?reminder\s+", re.IGNORECASE),
    re.compile(r"^(?:at|on|in|after|for)\s+", re.IGNORECASE),
]


def _clean_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _truncate(text: str, limit: int = 120) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _ts(dt_utc: datetime, style: str = "f") -> str:
    """Discord native timestamp: renders in each viewer's own local time."""
    return f"<t:{int(dt_utc.timestamp())}:{style}>"


def _format_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total <= 0:
        return "0s"
    parts = []
    for label, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        qty, total = divmod(total, size)
        if qty:
            parts.append(f"{qty}{label}")
    if total:
        parts.append(f"{total}s")
    return " ".join(parts)


# ----------------------------------------------------------------------
# Lightweight natural-language time parsing (replaces dateparser)
# ----------------------------------------------------------------------

def parse_duration(text: str) -> Optional[timedelta]:
    """Parses '30 seconds', '1m30s', '1 hour 20 minutes', '1d 3h 10m', etc."""
    if not text:
        return None
    cleaned = _clean_spaces(text).lower().replace("-", " ").replace(",", " ")
    cleaned = re.sub(r"^(?:in|after|for|every|repeat|about|around)\s+", "", cleaned)
    cleaned = cleaned.replace(" and ", " ")

    matches = list(DURATION_PART_RE.finditer(cleaned))
    if not matches:
        return None

    total = 0.0
    remainder = list(cleaned)
    for m in matches:
        total += float(m.group("value")) * UNIT_SECONDS[m.group("unit").lower()]
        for i in range(m.start(), m.end()):
            remainder[i] = " "

    leftover = re.sub(r"[\s,]+|and", "", "".join(remainder))
    if leftover or total <= 0:
        return None
    return timedelta(seconds=int(total))


def _extend_span_over_glue(text: str, start: int) -> int:
    """Pull a preceding 'at '/'on ' into the span so it's removed with the phrase."""
    m = re.search(r"(?:\bat|\bon)\s+$", text[:start], re.IGNORECASE)
    return m.start() if m else start


def parse_absolute(text: str, tz: ZoneInfo) -> Tuple[Optional[datetime], Optional[str], Optional[str]]:
    """
    Finds a day and/or time phrase anywhere in `text`.
    Returns (utc_datetime, leftover_message, error) — error is set only for
    'that time is in the past' style problems; (None, None, None) means
    'no time phrase found at all'.
    """
    now = datetime.now(tz)
    low = text.lower()
    spans: List[Tuple[int, int]] = []

    day: Optional[date] = None
    day_is_explicit_date = False   # a calendar date the user typed out
    weekday_target: Optional[int] = None
    weekday_next = False
    fixed_time: Optional[Tuple[int, int, int]] = None  # from noon/midnight/tonight

    m = DATE_ISO_RE.search(low)
    if m:
        try:
            day = date(int(m["y"]), int(m["mo"]), int(m["d"]))
            day_is_explicit_date = True
            spans.append((_extend_span_over_glue(text, m.start()), m.end()))
        except ValueError:
            pass

    if day is None:
        m = DATE_MON_RE.search(low)
        if m:
            mon = MONTHS[(m["mon1"] or m["mon2"])[:3].lower()]
            d = int(m["d1"] or m["d2"])
            try:
                day = date(now.year, mon, d)
                if day < now.date():
                    day = date(now.year + 1, mon, d)
                day_is_explicit_date = True
                spans.append((_extend_span_over_glue(text, m.start()), m.end()))
            except ValueError:
                day = None

    if day is None:
        m = DATE_NUM_RE.search(low)
        if m:
            a, b = int(m["a"]), int(m["b"])
            y = m["y"]
            year = now.year if not y else (int(y) + 2000 if len(y) == 2 else int(y))
            d, mo = (a, b) if b <= 12 else (b, a)  # day-first, auto-swap
            try:
                day = date(year, mo, d)
                if not y and day < now.date():
                    day = date(year + 1, mo, d)
                day_is_explicit_date = True
                spans.append((_extend_span_over_glue(text, m.start()), m.end()))
            except ValueError:
                day = None

    if day is None and weekday_target is None:
        m = DAY_WORD_RE.search(low)
        if m:
            word = m["word"].lower()
            spans.append((_extend_span_over_glue(text, m.start()), m.end()))
            if word in ("today",):
                day = now.date()
            elif word in ("tomorrow", "tmr", "tmrw"):
                day = now.date() + timedelta(days=1)
            elif word == "tonight":
                day = now.date()
                fixed_time = (20, 0, 0)
            elif word == "noon":
                day = now.date()
                fixed_time = (12, 0, 0)
            elif word == "midnight":
                day = now.date() + timedelta(days=1)
                fixed_time = (0, 0, 0)
            else:
                weekday_target = WEEKDAYS[word]
                weekday_next = bool(m["next"])

    # --- time ---
    hour = minute = second = None
    for m in TIME_RE.finditer(low):
        if any(s <= m.start() < e or s < m.end() <= e for s, e in spans):
            continue  # overlaps a date match (e.g. 2026-08-05)
        if m["ch"]:  # compact form: 630pm
            h, mi, se = int(m["ch"]), int(m["cm"]), 0
            ap = m["cap"].lower()
        else:
            h = int(m["h"])
            mi = int(m["m"] or 0)
            se = int(m["s"] or 0)
            ap = (m["ap"] or m["ap2"] or "").lower()
        if ap:
            if not 1 <= h <= 12:
                continue
            h = h % 12 + (12 if ap == "pm" else 0)
        if h > 23 or mi > 59 or se > 59:
            continue
        hour, minute, second = h, mi, se
        spans.append((_extend_span_over_glue(text, m.start()), m.end()))
        break

    if fixed_time and hour is None:
        hour, minute, second = fixed_time

    if day is None and weekday_target is None and hour is None:
        return None, None, None  # nothing time-like found

    if hour is None:
        hour, minute, second = DEFAULT_HOUR, 0, 0

    t = dtime(hour, minute, second)

    if weekday_target is not None:
        delta = (weekday_target - now.weekday()) % 7
        if weekday_next and delta == 0:
            delta = 7
        candidate = datetime.combine(now.date() + timedelta(days=delta), t, tzinfo=tz)
        if candidate <= now:
            candidate += timedelta(days=7)
    elif day is not None:
        candidate = datetime.combine(day, t, tzinfo=tz)
        if candidate <= now:
            if day_is_explicit_date:
                return None, None, "That time is in the past."
            candidate += timedelta(days=1)  # "today 5pm" typed at 6pm -> tomorrow
    else:
        candidate = datetime.combine(now.date(), t, tzinfo=tz)
        if candidate <= now:
            candidate += timedelta(days=1)  # bare "8:05 pm" rolls to tomorrow

    # Build the leftover message by cutting matched spans out.
    chars = list(text)
    for s, e in spans:
        for i in range(s, e):
            chars[i] = " "
    message = _clean_spaces("".join(chars)).strip(" -–—,;:|")

    return candidate.astimezone(timezone.utc), message, None


# ----------------------------------------------------------------------
# Views
# ----------------------------------------------------------------------

class ReminderListView(discord.ui.View):
    """Paginated list. Embeds are rendered lazily; a select deletes reminders."""

    def __init__(self, cog: "Reminders", author_id: int, rows: List[dict], tz_name: str):
        super().__init__(timeout=LIST_VIEW_TIMEOUT)
        self.cog = cog
        self.author_id = author_id
        self.rows = rows
        self.tz_name = tz_name
        self.index = 0
        self.message: Optional[discord.Message] = None
        self._rebuild()

    # -- helpers --

    @property
    def page_count(self) -> int:
        return max(1, (len(self.rows) + PAGE_SIZE - 1) // PAGE_SIZE)

    def _page_rows(self) -> List[dict]:
        start = self.index * PAGE_SIZE
        return self.rows[start:start + PAGE_SIZE]

    def _embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"⏰ Active reminders ({len(self.rows)})",
            color=discord.Color.blurple(),
        )
        lines = []
        for row in self._page_rows():
            due = datetime.fromisoformat(row["remind_at"])
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            msg = discord.utils.escape_markdown(_truncate(row["message"], 60))
            repeat = f" 🔁 `{_format_duration(timedelta(seconds=int(row['repeat_interval'])))}`" if row["repeat_interval"] else ""
            target = f" → <@{row['target_id']}>" if row["target_id"] != row["owner_id"] else ""
            lines.append(f"`#{row['id']}` {_ts(due, 'f')} ({_ts(due, 'R')}){repeat}{target}\n> {msg}")
        embed.description = "\n".join(lines) or "No reminders."
        embed.set_footer(text=f"Page {self.index + 1}/{self.page_count} • Times shown in your local time")
        return embed

    def _rebuild(self):
        self.index = min(self.index, self.page_count - 1)
        self.prev_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= self.page_count - 1
        self.delete_select.options = [
            discord.SelectOption(
                label=f"#{row['id']} — {_truncate(row['message'], 40)}",
                value=str(row["id"]),
            )
            for row in self._page_rows()
        ] or [discord.SelectOption(label="Nothing to delete", value="0")]
        self.delete_select.disabled = not self.rows

    async def _refresh(self, interaction: discord.Interaction):
        self._rebuild()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("That reminder list isn't yours.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    # -- components --

    @discord.ui.select(placeholder="🗑️ Delete a reminder on this page…", min_values=1, max_values=1, row=0)
    async def delete_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        reminder_id = int(select.values[0])
        if reminder_id:
            await self.cog.delete_reminder(reminder_id, self.author_id)
            self.rows = [r for r in self.rows if r["id"] != reminder_id]
        if not self.rows:
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(
                embed=discord.Embed(title="⏰ No active reminders", color=discord.Color.blurple()),
                view=self,
            )
            self.stop()
            return
        await self._refresh(interaction)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary, emoji="⬅️", row=1)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        await self._refresh(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, emoji="➡️", row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(self.page_count - 1, self.index + 1)
        await self._refresh(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="✖️", row=1)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


class ConfirmClearView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="Yes, delete everything", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


class ReminderNotifyView(discord.ui.View):
    def __init__(self, cog: "Reminders", row: dict):
        super().__init__(timeout=NOTIFY_VIEW_TIMEOUT)
        self.cog = cog
        self.row = row
        self.message: Optional[discord.Message] = None
        if not row["repeat_interval"]:
            self.remove_item(self.stop_repeat)

    def _disable_all(self):
        for item in self.children:
            item.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.row["target_id"]:
            await interaction.response.send_message("This reminder isn't yours.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        self._disable_all()
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _finish(self, interaction: discord.Interaction, note: str):
        self._disable_all()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send(note, ephemeral=True)
        self.stop()

    async def _snooze(self, interaction: discord.Interaction, delta: timedelta, label: str):
        due = datetime.now(timezone.utc) + delta
        await self.cog.create_reminder(
            owner_id=self.row["target_id"],
            target_id=self.row["target_id"],
            message=self.row["message"],
            remind_at=due,
        )
        await self._finish(interaction, f"⏰ Snoozed — I'll remind you again {_ts(due, 'R')} ({label}).")

    @discord.ui.button(label="10m", style=discord.ButtonStyle.primary, emoji="⏰")
    async def snooze_10m(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._snooze(interaction, timedelta(minutes=10), "10 minutes")

    @discord.ui.button(label="1h", style=discord.ButtonStyle.primary, emoji="🕐")
    async def snooze_1h(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._snooze(interaction, timedelta(hours=1), "1 hour")

    @discord.ui.button(label="Tomorrow", style=discord.ButtonStyle.primary, emoji="🌅")
    async def snooze_1d(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._snooze(interaction, timedelta(days=1), "1 day")

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, emoji="✅")
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, "✅ Dismissed.")

    @discord.ui.button(label="Stop repeating", style=discord.ButtonStyle.danger, emoji="🛑")
    async def stop_repeat(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.delete_reminder(self.row["id"], None)
        await self._finish(interaction, "🛑 Repeating reminder stopped.")


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------

class Reminders(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: Optional[aiosqlite.Connection] = None
        self.db_lock = asyncio.Lock()
        self._tz_names: Optional[List[str]] = None  # lazy cache

    # -------------------------
    # Lifecycle / DB
    # -------------------------

    async def cog_load(self):
        os.makedirs("db", exist_ok=True)
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL;")
        await self.db.execute("PRAGMA synchronous=NORMAL;")
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                repeat_interval INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS timezones (
                user_id INTEGER PRIMARY KEY,
                timezone TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(remind_at);
            CREATE INDEX IF NOT EXISTS idx_reminders_owner ON reminders(owner_id);
            """
        )
        await self.db.commit()
        if not self.check_reminders.is_running():
            self.check_reminders.start()

    def cog_unload(self):
        # Must stay synchronous: discord.py invokes cog_unload() without awaiting,
        # so an `async def` here leaks an un-awaited coroutine (the RuntimeWarning).
        if self.check_reminders.is_running():
            self.check_reminders.cancel()
        db, self.db = self.db, None
        if db is not None:
            try:
                # aiosqlite.close() is async; hand it to the loop instead of awaiting.
                self.bot.loop.create_task(db.close())
            except RuntimeError:
                pass  # loop already closed on full shutdown; the fd is released anyway

    def _utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    # -------------------------
    # Timezone handling
    # -------------------------

    def _all_tz_names(self) -> List[str]:
        if self._tz_names is None:
            self._tz_names = sorted(available_timezones())
        return self._tz_names

    def _normalize_timezone_name(self, tz: str) -> Optional[str]:
        if not tz:
            return None
        tz = tz.strip().replace(" ", "_")
        names = self._all_tz_names()
        lowered = {name.lower(): name for name in names}
        hit = lowered.get(tz.lower())
        if hit:
            return hit
        matches = difflib.get_close_matches(tz.lower(), list(lowered), n=1, cutoff=0.85)
        return lowered[matches[0]] if matches else None

    def _locale_candidates(self, ctx) -> List[str]:
        out = []
        interaction = getattr(ctx, "interaction", None)
        if interaction:
            for attr in ("locale", "guild_locale"):
                val = getattr(interaction, attr, None)
                if val:
                    out.append(str(getattr(val, "value", val)).replace("_", "-").lower())
        guild = getattr(ctx, "guild", None)
        if guild and getattr(guild, "preferred_locale", None):
            val = guild.preferred_locale
            out.append(str(getattr(val, "value", val)).replace("_", "-").lower())
        return out

    async def get_effective_timezone(self, user_id: int, ctx=None) -> Tuple[str, str]:
        async with self.db_lock:
            cursor = await self.db.execute(
                "SELECT timezone FROM timezones WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
        if row:
            stored = self._normalize_timezone_name(row["timezone"])
            if stored:
                return stored, "your setting"
        for loc in (self._locale_candidates(ctx) if ctx else []):
            if loc in LOCALE_TZ_MAP:
                return LOCALE_TZ_MAP[loc], "guessed from Discord locale"
        return "UTC", "default"

    async def set_user_timezone(self, user_id: int, tz: str) -> Optional[str]:
        canonical = self._normalize_timezone_name(tz)
        if not canonical:
            return None
        async with self.db_lock:
            await self.db.execute(
                """
                INSERT INTO timezones (user_id, timezone) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET timezone = excluded.timezone
                """,
                (user_id, canonical),
            )
            await self.db.commit()
        return canonical

    async def clear_user_timezone(self, user_id: int):
        async with self.db_lock:
            await self.db.execute("DELETE FROM timezones WHERE user_id = ?", (user_id,))
            await self.db.commit()

    # -------------------------
    # Parsing (top level)
    # -------------------------

    def _strip_fillers(self, text: str) -> str:
        text = _clean_spaces(text)
        for pat in FILLER_RES:
            text = pat.sub("", text)
        return _clean_spaces(text)

    def _split_duration_prefix(self, text: str) -> Tuple[Optional[timedelta], str]:
        candidate = self._strip_fillers(text)
        if not candidate:
            return None, text
        tokens = candidate.split()
        best, best_idx = None, 0
        for end in range(1, min(len(tokens), 8) + 1):
            d = parse_duration(" ".join(tokens[:end]))
            if d:
                best, best_idx = d, end
        if best is None:
            return None, text
        return best, " ".join(tokens[best_idx:]).strip()

    def _parse_repeat(self, text: str) -> Tuple[Optional[timedelta], str]:
        cleaned = self._strip_fillers(text)
        if not cleaned.lower().startswith(("every ", "repeat ")):
            return None, text
        remainder = re.sub(r"^(?:every|repeat)\s+", "", cleaned, flags=re.IGNORECASE).strip()
        if not remainder:
            return None, text
        first = remainder.split(maxsplit=1)[0].lower()
        if first in REPEAT_KEYWORDS:
            return REPEAT_KEYWORDS[first], remainder[len(first):].strip()
        duration, rest = self._split_duration_prefix(remainder)
        if duration:
            return duration, rest
        return None, text

    def parse_input(self, raw: str, tz_name: str):
        """
        Returns (remind_at_utc, message, repeat_interval, target_id, error).
        """
        text = _clean_spaces(raw)
        if not text:
            return None, None, None, None, "Please provide reminder text."

        target_id = None
        m = TARGET_PREFIX_RE.match(text)
        if m:
            target_id = int(m["mention_id"] or m["typed_id"])
            text = _clean_spaces(m["rest"])

        tz = ZoneInfo(tz_name)

        # 1) repeating
        repeat, remainder = self._parse_repeat(text)
        if repeat:
            now = self._utcnow()
            first_due = now + repeat
            anchored, msg, err = parse_absolute(remainder, tz)
            if anchored:
                first_due = anchored
                while first_due <= now:
                    first_due += repeat
                message = msg or "Reminder"
            else:
                message = _clean_spaces(remainder).strip(" -–—,;:|") or "Reminder"
            return first_due, message, repeat, target_id, None

        # 2) plain duration: "30 seconds check oven", "1h30m drink water"
        duration, remainder = self._split_duration_prefix(text)
        if duration:
            message = _clean_spaces(remainder).strip(" -–—,;:|") or "Reminder"
            return self._utcnow() + duration, message, None, target_id, None

        # 3) natural datetime: "tomorrow 5pm submit report", "friday buy milk"
        dt, msg, err = parse_absolute(self._strip_fillers(text), tz)
        if err:
            return None, None, None, target_id, err
        if dt:
            return dt, (msg or "Reminder"), None, target_id, None

        return None, None, None, target_id, (
            "I couldn't find a time in that. Try:\n"
            "`30 seconds` · `1h30m` · `tomorrow 5pm` · `8:05 pm` · `friday` · "
            "`25 dec 9am` · `every 2h` · `every day at 9pm`"
        )

    # -------------------------
    # DB operations
    # -------------------------

    async def create_reminder(self, owner_id: int, target_id: int, message: str,
                              remind_at: datetime, repeat_interval: Optional[timedelta] = None) -> int:
        async with self.db_lock:
            cursor = await self.db.execute(
                """
                INSERT INTO reminders (owner_id, target_id, message, remind_at, repeat_interval, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id, target_id, message,
                    remind_at.astimezone(timezone.utc).isoformat(),
                    int(repeat_interval.total_seconds()) if repeat_interval else None,
                    self._utcnow().isoformat(),
                ),
            )
            await self.db.commit()
            return cursor.lastrowid

    async def fetch_owned(self, owner_id: int) -> List[dict]:
        async with self.db_lock:
            cursor = await self.db.execute(
                "SELECT * FROM reminders WHERE owner_id = ? ORDER BY remind_at ASC",
                (owner_id,),
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def fetch_actionable(self, reminder_id: int, author_id: int) -> Optional[dict]:
        """
        Returns the reminder only if `author_id` may act on it: i.e. they own it,
        or they are the bot owner. Returns None for both 'does not exist' and
        'not yours', so callers give one identical reply and sequential IDs can't
        be enumerated by strangers.
        """
        async with self.db_lock:
            if author_id == OWNER_ID:
                cursor = await self.db.execute(
                    "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
                )
            else:
                cursor = await self.db.execute(
                    "SELECT * FROM reminders WHERE id = ? AND owner_id = ?",
                    (reminder_id, author_id),
                )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def delete_reminder(self, reminder_id: int, owner_id: Optional[int]) -> bool:
        async with self.db_lock:
            if owner_id is None:
                cursor = await self.db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            else:
                cursor = await self.db.execute(
                    "DELETE FROM reminders WHERE id = ? AND owner_id = ?", (reminder_id, owner_id)
                )
            await self.db.commit()
            return cursor.rowcount > 0

    async def clear_owned(self, owner_id: int) -> int:
        async with self.db_lock:
            cursor = await self.db.execute("DELETE FROM reminders WHERE owner_id = ?", (owner_id,))
            await self.db.commit()
            return cursor.rowcount

    async def update_reminder(self, reminder_id: int, owner_id: int, *,
                              remind_at: Optional[datetime] = None,
                              message: Optional[str] = None,
                              repeat_interval: Optional[timedelta] = None,
                              clear_repeat: bool = False) -> bool:
        fields, values = [], []
        if remind_at is not None:
            fields.append("remind_at = ?")
            values.append(remind_at.astimezone(timezone.utc).isoformat())
        if message is not None:
            fields.append("message = ?")
            values.append(message)
        if repeat_interval is not None:
            fields.append("repeat_interval = ?")
            values.append(int(repeat_interval.total_seconds()))
        elif clear_repeat:
            fields.append("repeat_interval = NULL")
        if not fields:
            return False
        values.extend([reminder_id, owner_id])
        async with self.db_lock:
            cursor = await self.db.execute(
                f"UPDATE reminders SET {', '.join(fields)} WHERE id = ? AND owner_id = ?",
                tuple(values),
            )
            await self.db.commit()
            return cursor.rowcount > 0

    # -------------------------
    # Delivery loop
    # -------------------------

    async def _collect_and_advance_due(self) -> List[dict]:
        """DB work only, done under the lock; DM sending happens outside it."""
        now = self._utcnow()
        async with self.db_lock:
            cursor = await self.db.execute(
                "SELECT * FROM reminders WHERE remind_at <= ? ORDER BY remind_at ASC LIMIT 50",
                (now.isoformat(),),
            )
            due = [dict(r) for r in await cursor.fetchall()]
            for row in due:
                if row["repeat_interval"]:
                    step = int(row["repeat_interval"])
                    current = datetime.fromisoformat(row["remind_at"])
                    if current.tzinfo is None:
                        current = current.replace(tzinfo=timezone.utc)
                    missed = int((now - current).total_seconds() // step) + 1
                    next_due = current + timedelta(seconds=step * max(1, missed))
                    await self.db.execute(
                        "UPDATE reminders SET remind_at = ? WHERE id = ?",
                        (next_due.isoformat(), row["id"]),
                    )
                else:
                    await self.db.execute("DELETE FROM reminders WHERE id = ?", (row["id"],))
            if due:
                await self.db.commit()
        return due

    async def _send_reminder_dm(self, row: dict):
        user = self.bot.get_user(row["target_id"])
        if user is None:
            try:
                user = await self.bot.fetch_user(row["target_id"])
            except discord.HTTPException:
                return
        due = datetime.fromisoformat(row["remind_at"])
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)

        embed = discord.Embed(
            title="⏰ Reminder",
            description=_truncate(row["message"], 3500),
            color=discord.Color.green(),
        )
        embed.add_field(name="Was due", value=f"{_ts(due, 'f')} ({_ts(due, 'R')})", inline=True)
        if row["repeat_interval"]:
            embed.add_field(
                name="Repeats",
                value=f"every `{_format_duration(timedelta(seconds=int(row['repeat_interval'])))}`",
                inline=True,
            )
        if row["owner_id"] != row["target_id"]:
            embed.set_footer(text=f"Set by user ID {row['owner_id']} • #{row['id']}")
        else:
            embed.set_footer(text=f"Reminder #{row['id']}")

        view = ReminderNotifyView(self, row)
        try:
            view.message = await user.send(embed=embed, view=view)
        except discord.HTTPException:
            pass  # DMs closed; reminder was already consumed so it won't loop

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def check_reminders(self):
        due = await self._collect_and_advance_due()
        for row in due:
            await self._send_reminder_dm(row)

    @check_reminders.before_loop
    async def before_check_reminders(self):
        await self.bot.wait_until_ready()

    # -------------------------
    # Commands
    # -------------------------

    @commands.hybrid_command(name="remind", aliases=["r"])
    async def remind(self, ctx: commands.Context, *, text: Optional[str] = None):
        """Set a reminder: !remind 30 seconds check oven"""
        await self._cmd_add(ctx, text)

    @commands.hybrid_command(name="timezone")
    async def timezone_cmd(self, ctx: commands.Context, tz: Optional[str] = None):
        """View or set your timezone: !timezone Asia/Kuala_Lumpur"""
        await self._cmd_timezone(ctx, tz)

    @commands.hybrid_group(name="reminders", invoke_without_command=True, aliases=["reminder"])
    async def reminders(self, ctx: commands.Context):
        await self._cmd_list(ctx)

    @reminders.command(name="add", aliases=["create", "set"])
    async def reminders_add(self, ctx: commands.Context, *, text: Optional[str] = None):
        await self._cmd_add(ctx, text)

    @reminders.command(name="list", aliases=["show", "ls"])
    async def reminders_list(self, ctx: commands.Context):
        await self._cmd_list(ctx)

    @reminders.command(name="delete", aliases=["del", "remove", "rm", "cancel"])
    async def reminders_delete(self, ctx: commands.Context, reminder_id: int):
        row = await self.fetch_actionable(reminder_id, ctx.author.id)
        if not row:
            await ctx.send(f"Reminder `#{reminder_id}` not found, or it isn't yours.")
            return
        ok = await self.delete_reminder(reminder_id, None)
        await ctx.send(f"🗑️ Deleted reminder `#{reminder_id}`." if ok else f"Could not delete `#{reminder_id}`.")

    @reminders.command(name="clear", aliases=["purge"])
    async def reminders_clear(self, ctx: commands.Context):
        rows = await self.fetch_owned(ctx.author.id)
        if not rows:
            await ctx.send("You have no reminders to clear.")
            return
        view = ConfirmClearView(ctx.author.id)
        msg = await ctx.send(f"Delete all **{len(rows)}** of your reminders?", view=view)
        await view.wait()
        if view.confirmed:
            removed = await self.clear_owned(ctx.author.id)
            await msg.edit(content=f"🗑️ Cleared `{removed}` reminder(s).", view=None)
        else:
            await msg.edit(content="Cancelled.", view=None)

    @reminders.command(name="edit")
    async def reminders_edit(self, ctx: commands.Context, reminder_id: int, *, text: Optional[str] = None):
        row = await self.fetch_actionable(reminder_id, ctx.author.id)
        if not row:
            await ctx.send(f"Reminder `#{reminder_id}` not found, or it isn't yours.")
            return
        if not text:
            await ctx.send("Provide a new time and/or message, e.g. `!reminders edit 12 tomorrow 6pm new text`.")
            return

        tz_name, _ = await self.get_effective_timezone(ctx.author.id, ctx)
        dt, message, repeat, _, err = self.parse_input(text, tz_name)

        if dt is None:
            # message-only edit
            new_msg = _clean_spaces(text)
            ok = await self.update_reminder(reminder_id, row["owner_id"], message=new_msg)
            await ctx.send(f"✏️ Updated message of `#{reminder_id}`." if ok else "Update failed.")
            return

        if repeat is None and row["repeat_interval"]:
            repeat = timedelta(seconds=int(row["repeat_interval"]))
        ok = await self.update_reminder(
            reminder_id, row["owner_id"],
            remind_at=dt,
            message=message if message and message != "Reminder" else row["message"],
            repeat_interval=repeat,
        )
        if ok:
            extra = f" 🔁 `{_format_duration(repeat)}`" if repeat else ""
            await ctx.send(f"✏️ Updated `#{reminder_id}` → {_ts(dt, 'f')} ({_ts(dt, 'R')}){extra}")
        else:
            await ctx.send("Update failed.")

    @reminders.command(name="snooze")
    async def reminders_snooze(self, ctx: commands.Context, reminder_id: int, *, duration: Optional[str] = None):
        row = await self.fetch_actionable(reminder_id, ctx.author.id)
        if not row:
            await ctx.send(f"Reminder `#{reminder_id}` not found, or it isn't yours.")
            return
        td = parse_duration(duration or "")
        if not td:
            await ctx.send("Give me a duration, e.g. `10m` or `1 hour`.")
            return
        new_due = self._utcnow() + td
        ok = await self.update_reminder(reminder_id, row["owner_id"], remind_at=new_due)
        if ok:
            await ctx.send(f"😴 Snoozed `#{reminder_id}` → {_ts(new_due, 'f')} ({_ts(new_due, 'R')})")
        else:
            await ctx.send("Snooze failed.")

    @reminders.command(name="timezone")
    async def reminders_timezone(self, ctx: commands.Context, tz: Optional[str] = None):
        await self._cmd_timezone(ctx, tz)

    @reminders.command(name="help")
    async def reminders_help(self, ctx: commands.Context):
        tz_name, tz_source = await self.get_effective_timezone(ctx.author.id, ctx)
        await ctx.send(embed=self._help_embed(tz_name, tz_source, getattr(ctx, "clean_prefix", "!") or "!"))

    # -------------------------
    # Command implementations
    # -------------------------

    def _help_embed(self, tz_name: str, tz_source: str, prefix: str = "!") -> discord.Embed:
        p = prefix
        embed = discord.Embed(
            title="⏰ Reminders",
            color=discord.Color.blurple(),
            description=(
                f"Set a reminder with **`{p}remind <when> <what>`** and I'll DM you when it's due.\n"
                f"Times are read in your timezone and shown to everyone in their own local time."
            ),
        )
        embed.add_field(
            name="🕑 Relative time",
            value=(
                "`30s` · `10m` · `1h30m` · `2h` · `1d 3h`\n"
                f"↳ *{p}remind 1h30m take out the trash*"
            ),
            inline=False,
        )
        embed.add_field(
            name="📅 Specific time",
            value=(
                "`5pm` · `8:05pm` · `tomorrow 9am` · `friday` · `next mon 7pm`\n"
                "`25 dec 9am` · `2026-08-15 14:00` · `tonight` · `noon`\n"
                f"↳ *{p}remind tomorrow 5pm submit report*"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔁 Repeating",
            value=(
                "`every 2h` · `daily` · `weekly` · `every day at 9pm`\n"
                f"↳ *{p}remind every day at 9pm drink water*"
            ),
            inline=False,
        )
        embed.add_field(
            name="🛠️ Manage",
            value=(
                f"`{p}reminders` — list yours (with buttons to delete)\n"
                f"`{p}reminders edit <id> <new time/text>`\n"
                f"`{p}reminders snooze <id> <duration>`\n"
                f"`{p}reminders delete <id>` · `{p}reminders clear`"
            ),
            inline=False,
        )
        embed.add_field(
            name="🌍 Timezone",
            value=(
                f"`{p}timezone Asia/Kuala_Lumpur` to set · `{p}timezone auto` to reset\n"
                f"Currently: **{tz_name}** ({tz_source})"
            ),
            inline=False,
        )
        embed.set_footer(
            text=f"Tip: a date with no time defaults to {DEFAULT_HOUR}:00 AM • "
                 f"owner can target others with  <@user> in 10m do thing"
        )
        return embed

    async def _cmd_timezone(self, ctx: commands.Context, tz: Optional[str]):
        if not tz:
            current, source = await self.get_effective_timezone(ctx.author.id, ctx)
            await ctx.send(
                f"🌍 Timezone: `{current}` ({source})\n"
                f"Set with `!timezone Asia/Kuala_Lumpur`, clear with `!timezone auto`."
            )
            return
        if tz.strip().lower() in {"auto", "reset", "clear", "default"}:
            await self.clear_user_timezone(ctx.author.id)
            current, source = await self.get_effective_timezone(ctx.author.id, ctx)
            await ctx.send(f"🌍 Timezone cleared. Now using `{current}` ({source}).")
            return
        canonical = await self.set_user_timezone(ctx.author.id, tz)
        if canonical:
            await ctx.send(f"🌍 Timezone set to `{canonical}`.")
        else:
            await ctx.send("Invalid timezone. Example: `Asia/Kuala_Lumpur`.")

    async def _cmd_add(self, ctx: commands.Context, text: Optional[str]):
        tz_name, tz_source = await self.get_effective_timezone(ctx.author.id, ctx)
        if not text:
            await ctx.send(embed=self._help_embed(tz_name, tz_source, getattr(ctx, "clean_prefix", "!") or "!"))
            return

        remind_at, message, repeat, target_id, err = self.parse_input(text, tz_name)
        if err:
            await ctx.send(f"❌ {err}")
            return

        if target_id is not None and target_id != ctx.author.id and ctx.author.id != OWNER_ID:
            await ctx.send("Only the bot owner can set reminders for other users.")
            return
        target_id = target_id or ctx.author.id

        reminder_id = await self.create_reminder(
            owner_id=ctx.author.id,
            target_id=target_id,
            message=message or "Reminder",
            remind_at=remind_at,
            repeat_interval=repeat,
        )

        extra = f" 🔁 every `{_format_duration(repeat)}`" if repeat else ""
        target_note = f" for <@{target_id}>" if target_id != ctx.author.id else ""
        await ctx.send(
            f"✅ Reminder `#{reminder_id}`{target_note}: "
            f"{_ts(remind_at, 'f')} ({_ts(remind_at, 'R')}){extra}\n"
            f"> {discord.utils.escape_markdown(_truncate(message, 100))}"
        )

    async def _cmd_list(self, ctx: commands.Context):
        tz_name, tz_source = await self.get_effective_timezone(ctx.author.id, ctx)
        rows = await self.fetch_owned(ctx.author.id)
        if not rows:
            await ctx.send(embed=self._help_embed(tz_name, tz_source, getattr(ctx, "clean_prefix", "!") or "!"))
            return
        view = ReminderListView(self, ctx.author.id, rows, tz_name)
        view.message = await ctx.send(embed=view._embed(), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Reminders(bot))