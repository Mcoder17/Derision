from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from env import OWNER_ID


CONFIG_FILE = Path("data/reaction_config.json")
CHECK_EMOJI = "✔️"

TIMEOUT_DURATION = 10 * 60      
RECHECK_INTERVAL = 5 * 60     
HISTORY_SEARCH_LIMIT = 100
REACTION_DELAY = 0.15            

MESSAGE_CHUNK_LIMIT = 1900


class ReactionAttempt(Enum):
    SUCCESS = auto()
    FORBIDDEN = auto()
    NOT_FOUND = auto()
    FAILED = auto()


class BlockProbe(Enum):
    CLEAR = auto()
    TARGET_FORBIDDEN = auto()
    CHANNEL_FORBIDDEN = auto()
    UNKNOWN = auto()


class Reactor(commands.Cog):
    """
    Automatic per-user reactions with optional AutoRandomPinger integration.

    Block detection:
      Discord does not expose an official "this user blocked this bot" flag.

      Reactor therefore only treats a reaction failure as target-specific when:
        1. reacting to the target user's message raises Forbidden, AND
        2. reacting to a bot-created control message in the same channel works.

      Ordinary channel permission failures therefore do not trigger punishment.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = self._load_config()

        # (guild_id, user_id) -> punishment task
        self._active_punishers: dict[
            tuple[int, int],
            asyncio.Task[None],
        ] = {}

        # Recent target messages kept in memory.
        # (guild_id, user_id) -> Message
        self._last_messages: dict[
            tuple[int, int],
            discord.Message,
        ] = {}

        self._config_lock = asyncio.Lock()

        print("[Reactor] Cog loaded.")

    # ================================================================
    # CONFIG
    # ================================================================

    @staticmethod
    def _default_config() -> dict[str, Any]:
        return {
            "users": {},
            "self_assign_allowed": [],
        }

    def _load_config(self) -> dict[str, Any]:
        """
        Load and normalize configuration.

        Old `custom_emoji_users` data is intentionally ignored and removed
        when the normalized config is saved.
        """
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

        default = self._default_config()

        if not CONFIG_FILE.exists():
            self._write_config(default)
            return default

        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                raw = json.load(f)

        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"[Reactor] Config load failed "
                f"({type(exc).__name__}); resetting."
            )

            self._write_config(default)
            return default

        if not isinstance(raw, dict):
            print(
                "[Reactor] Config root is not an object; resetting."
            )

            self._write_config(default)
            return default

        # ---------------- USERS ----------------

        users_raw = raw.get("users", {})

        if not isinstance(users_raw, dict):
            users_raw = {}

        users: dict[str, list[str]] = {}

        for user_id, emojis in users_raw.items():
            uid = str(user_id)

            if not isinstance(emojis, list):
                continue

            cleaned: list[str] = []

            for emoji in emojis:
                if not isinstance(emoji, str):
                    continue

                emoji = emoji.strip()

                if not emoji:
                    continue

                if emoji not in cleaned:
                    cleaned.append(emoji)

            users[uid] = cleaned

        # ---------------- SELF ASSIGN ----------------

        self_assign_raw = raw.get(
            "self_assign_allowed",
            [],
        )

        if not isinstance(self_assign_raw, list):
            self_assign_raw = []

        self_assign_allowed = list(
            dict.fromkeys(
                str(user_id)
                for user_id in self_assign_raw
            )
        )

        config = {
            "users": users,
            "self_assign_allowed": self_assign_allowed,
        }

        # Persist normalized config.
        # This automatically removes old custom_emoji_users.
        self._write_config(config)

        return config

    def _write_config(
        self,
        config: dict[str, Any] | None = None,
    ) -> None:
        data = self.config if config is None else config

        CONFIG_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temp_file = CONFIG_FILE.with_suffix(
            CONFIG_FILE.suffix + ".tmp"
        )

        try:
            with temp_file.open(
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(
                    data,
                    f,
                    indent=2,
                    ensure_ascii=False,
                )

                f.flush()
                os.fsync(f.fileno())

            os.replace(
                temp_file,
                CONFIG_FILE,
            )

        except OSError as exc:
            print(
                f"[Reactor] Could not save config: "
                f"{type(exc).__name__}: {exc}"
            )

            try:
                temp_file.unlink(
                    missing_ok=True,
                )
            except OSError:
                pass

            raise

    async def _save_config(self) -> None:
        """
        Serialize config writes.

        The actual disk operation is moved off the Discord event loop.
        """
        async with self._config_lock:
            await asyncio.to_thread(
                self._write_config
            )

    # ================================================================
    # COG CLEANUP
    # ================================================================

    def cog_unload(self) -> None:
        for task in tuple(
            self._active_punishers.values()
        ):
            task.cancel()

        self._active_punishers.clear()
        self._last_messages.clear()

        print(
            "[Reactor] Cog unloaded; "
            "active punishment tasks cancelled."
        )

    # ================================================================
    # EMOJI HELPERS
    # ================================================================

    @staticmethod
    def _is_custom_emoji(value: str) -> bool:
        """
        Return True for Discord custom emoji strings such as:

            <:name:123456789>
            <a:name:123456789>
        """
        return (
            value.startswith("<")
            and value.endswith(">")
            and value.count(":") >= 2
        )

    def _resolve_emoji(
        self,
        configured: str,
    ) -> discord.Emoji | discord.PartialEmoji | str:
        """
        Resolve an emoji from config.

        There is NO custom-emoji permission check.

        Any configured user may have Unicode or Discord custom emojis.
        """
        if not self._is_custom_emoji(configured):
            return configured

        try:
            partial = discord.PartialEmoji.from_str(
                configured
            )
        except Exception:
            return configured

        if partial.id is None:
            return configured

        # Prefer cached full Emoji when the bot knows it.
        cached = self.bot.get_emoji(
            partial.id
        )

        return cached or partial

    # ================================================================
    # REACTION HELPERS
    # ================================================================

    @staticmethod
    def _channel_permissions_allow_reactions(
        message: discord.Message,
    ) -> bool:
        guild = message.guild

        if guild is None or guild.me is None:
            return False

        channel = message.channel

        if not hasattr(
            channel,
            "permissions_for",
        ):
            return False

        try:
            perms = channel.permissions_for(
                guild.me
            )
        except Exception:
            return False

        return bool(
            getattr(
                perms,
                "view_channel",
                False,
            )
            and getattr(
                perms,
                "read_message_history",
                False,
            )
            and getattr(
                perms,
                "add_reactions",
                False,
            )
        )

    async def _attempt_reaction(
        self,
        message: discord.Message,
        emoji: discord.Emoji
        | discord.PartialEmoji
        | str,
    ) -> ReactionAttempt:
        try:
            await message.add_reaction(
                emoji
            )

            return ReactionAttempt.SUCCESS

        except discord.Forbidden:
            return ReactionAttempt.FORBIDDEN

        except discord.NotFound:
            return ReactionAttempt.NOT_FOUND

        except (
            discord.HTTPException,
            TypeError,
        ) as exc:
            print(
                f"[Reactor] Reaction failed on "
                f"message {message.id}: "
                f"{type(exc).__name__}: {exc}"
            )

            return ReactionAttempt.FAILED

    def _bot_already_reacted(
        self,
        message: discord.Message,
        emoji: str,
    ) -> bool:
        for reaction in message.reactions:
            try:
                if (
                    str(reaction.emoji) == emoji
                    and reaction.me
                ):
                    return True
            except Exception:
                continue

        return False

    async def _remove_probe_reaction(
        self,
        message: discord.Message,
        emoji: str,
    ) -> None:
        if self.bot.user is None:
            return

        try:
            await message.remove_reaction(
                emoji,
                self.bot.user,
            )

        except (
            discord.HTTPException,
            discord.Forbidden,
            discord.NotFound,
        ):
            pass

    # ================================================================
    # CHANNEL HELPERS
    # ================================================================

    async def _safe_send(
        self,
        channel: Any,
        content: str,
    ) -> None:
        try:
            await channel.send(
                content
            )

        except (
            discord.HTTPException,
            discord.Forbidden,
            AttributeError,
        ):
            pass

    async def _send_chunked(
        self,
        ctx: commands.Context,
        content: str,
    ) -> None:
        """
        Send text while remaining safely below Discord's
        2000-character message limit.
        """
        if len(content) <= MESSAGE_CHUNK_LIMIT:
            await ctx.send(content)
            return

        lines = content.splitlines()

        chunks: list[str] = []
        current = ""

        for line in lines:
            # Handle pathological individual lines that are too large.
            if len(line) > MESSAGE_CHUNK_LIMIT:
                if current:
                    chunks.append(current)
                    current = ""

                while len(line) > MESSAGE_CHUNK_LIMIT:
                    chunks.append(
                        line[:MESSAGE_CHUNK_LIMIT]
                    )

                    line = line[
                        MESSAGE_CHUNK_LIMIT:
                    ]

                if line:
                    current = line

                continue

            candidate = (
                f"{current}\n{line}"
                if current
                else line
            )

            if len(candidate) > MESSAGE_CHUNK_LIMIT:
                if current:
                    chunks.append(current)

                current = line

            else:
                current = candidate

        if current:
            chunks.append(current)

        for chunk in chunks:
            await ctx.send(chunk)

    # ================================================================
    # CONTROL / BLOCK PROBE
    # ================================================================

    async def _control_probe(
        self,
        message: discord.Message,
    ) -> bool:
        """
        Test whether Reactor can react in the SAME channel.

        A short bot-created control message is used instead of touching
        another user's message.

        The control message is deleted immediately afterwards.
        """
        channel = message.channel

        temp: discord.Message | None = None

        try:
            temp = await channel.send(
                "Reactor permission check…",
                silent=True,
            )

            result = await self._attempt_reaction(
                temp,
                CHECK_EMOJI,
            )

            return (
                result
                is ReactionAttempt.SUCCESS
            )

        except (
            discord.Forbidden,
            discord.HTTPException,
            AttributeError,
            TypeError,
        ):
            return False

        finally:
            if temp is not None:
                try:
                    await temp.delete()

                except (
                    discord.Forbidden,
                    discord.HTTPException,
                    discord.NotFound,
                ):
                    pass

    async def _probe_block_state(
        self,
        message: discord.Message,
    ) -> BlockProbe:
        """
        Determine whether a reaction denial appears target-specific.

        Discord does not provide an official blocked-user API flag,
        therefore this remains deliberately conservative.
        """
        if message.guild is None:
            return BlockProbe.UNKNOWN

        if not self._channel_permissions_allow_reactions(
            message
        ):
            return BlockProbe.CHANNEL_FORBIDDEN

        had_reaction = self._bot_already_reacted(
            message,
            CHECK_EMOJI,
        )

        result = await self._attempt_reaction(
            message,
            CHECK_EMOJI,
        )

        if result is ReactionAttempt.SUCCESS:
            if not had_reaction:
                await self._remove_probe_reaction(
                    message,
                    CHECK_EMOJI,
                )

            return BlockProbe.CLEAR

        if result is ReactionAttempt.FORBIDDEN:
            control_worked = await self._control_probe(
                message
            )

            if control_worked:
                return BlockProbe.TARGET_FORBIDDEN

            return BlockProbe.CHANNEL_FORBIDDEN

        return BlockProbe.UNKNOWN

    # ================================================================
    # MESSAGE CACHE / LOOKUP
    # ================================================================

    async def _find_recent_user_message(
        self,
        channel: Any,
        user_id: int,
        *,
        limit: int = HISTORY_SEARCH_LIMIT,
    ) -> discord.Message | None:
        try:
            async for msg in channel.history(
                limit=limit
            ):
                if msg.author.id == user_id:
                    return msg

        except (
            discord.Forbidden,
            discord.HTTPException,
            AttributeError,
        ):
            return None

        return None

    def _remember_message(
        self,
        message: discord.Message,
    ) -> None:
        if message.guild is None:
            return

        self._last_messages[
            (
                message.guild.id,
                message.author.id,
            )
        ] = message

    # ================================================================
    # AUTORANDOMPINGER INTEGRATION
    # ================================================================

    def _pinger_last_channel_id(
        self,
        user_id: int,
    ) -> int | None:
        """
        Read AutoRandomPinger state if that cog exists.

        Reactor remains fully usable without AutoRandomPinger.
        """
        ping_cog = self.bot.get_cog(
            "AutoRandomPinger"
        )

        if ping_cog is None:
            return None

        data = getattr(
            ping_cog,
            "data",
            None,
        )

        if not isinstance(data, dict):
            return None

        users = data.get(
            "users",
            {},
        )

        if not isinstance(users, dict):
            return None

        entry = users.get(
            str(user_id),
            {},
        )

        if not isinstance(entry, dict):
            return None

        channel_id = entry.get(
            "last_channel"
        )

        try:
            return (
                int(channel_id)
                if channel_id is not None
                else None
            )

        except (
            TypeError,
            ValueError,
        ):
            return None

    async def _resolve_member(
        self,
        user: discord.User | discord.Member,
        preferred_channel_id: int | None = None,
    ) -> discord.Member | None:
        if isinstance(
            user,
            discord.Member,
        ):
            return user

        # Prefer the guild belonging to the pinger's
        # last known channel.
        if preferred_channel_id is not None:
            channel = self.bot.get_channel(
                preferred_channel_id
            )

            guild = getattr(
                channel,
                "guild",
                None,
            )

            if isinstance(
                guild,
                discord.Guild,
            ):
                member = guild.get_member(
                    user.id
                )

                if member is not None:
                    return member

                try:
                    return await guild.fetch_member(
                        user.id
                    )

                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    pass

        # Cheap guild cache lookup only.
        # No guild-wide history crawling.
        for guild in self.bot.guilds:
            member = guild.get_member(
                user.id
            )

            if member is not None:
                return member

        return None

    async def _find_anchor(
        self,
        user: discord.User | discord.Member,
        member: discord.Member,
        preferred_channel_id: int | None,
        supplied_message: discord.Message | None = None,
    ) -> discord.Message | None:
        if (
            supplied_message is not None
            and supplied_message.guild is not None
            and supplied_message.guild.id
            == member.guild.id
            and supplied_message.author.id
            == user.id
        ):
            return supplied_message

        cached = self._last_messages.get(
            (
                member.guild.id,
                user.id,
            )
        )

        if cached is not None:
            return cached

        if preferred_channel_id is not None:
            channel = member.guild.get_channel(
                preferred_channel_id
            )

            if channel is None:
                channel = member.guild.get_thread(
                    preferred_channel_id
                )

            if channel is not None:
                return await self._find_recent_user_message(
                    channel,
                    user.id,
                )

        return None

    # ================================================================
    # TIMEOUT HELPERS
    # ================================================================

    @staticmethod
    def _active_timeout(
        member: discord.Member,
    ) -> datetime | None:
        until = member.timed_out_until

        if until is None:
            return None

        if until <= discord.utils.utcnow():
            return None

        return until

    @staticmethod
    def _timeouts_match(
        a: datetime | None,
        b: datetime | None,
        tolerance: float = 5.0,
    ) -> bool:
        if a is None or b is None:
            return a is b

        return (
            abs(
                (a - b).total_seconds()
            )
            <= tolerance
        )

    async def _fresh_member(
        self,
        guild: discord.Guild,
        user_id: int,
    ) -> discord.Member | None:
        try:
            return await guild.fetch_member(
                user_id
            )

        except discord.NotFound:
            return None

        except (
            discord.Forbidden,
            discord.HTTPException,
        ):
            return guild.get_member(
                user_id
            )

    async def _apply_timeout(
        self,
        member: discord.Member,
    ) -> datetime | None:
        desired_until = (
            discord.utils.utcnow()
            + timedelta(
                seconds=TIMEOUT_DURATION
            )
        )

        try:
            await member.timeout(
                desired_until,
                reason=(
                    "Reactor: target-specific "
                    "reaction denial detected"
                ),
            )

            return desired_until

        except (
            discord.Forbidden,
            discord.HTTPException,
        ):
            return None

    async def _remove_timeout_if_owned(
        self,
        member: discord.Member,
        expected_until: datetime | None,
    ) -> bool:
        """
        Remove only a timeout that still matches the timeout
        Reactor last applied.

        Moderator changes are never overwritten.
        """
        if expected_until is None:
            return False

        fresh = await self._fresh_member(
            member.guild,
            member.id,
        )

        if fresh is None:
            return False

        current_until = self._active_timeout(
            fresh
        )

        if not self._timeouts_match(
            current_until,
            expected_until,
        ):
            return False

        try:
            await fresh.timeout(
                None,
                reason=(
                    "Reactor: reaction access restored"
                ),
            )

            return True

        except (
            discord.Forbidden,
            discord.HTTPException,
        ):
            return False

    # ================================================================
    # PUNISHMENT
    # ================================================================

    async def _start_punishment(
        self,
        member: discord.Member,
        anchor_message: discord.Message,
    ) -> None:
        key = (
            member.guild.id,
            member.id,
        )

        existing = self._active_punishers.get(
            key
        )

        if (
            existing is not None
            and not existing.done()
        ):
            return

        task = asyncio.create_task(
            self._punishment_loop(
                member,
                anchor_message,
            ),
            name=(
                f"reactor-punish-"
                f"{member.guild.id}-"
                f"{member.id}"
            ),
        )

        self._active_punishers[
            key
        ] = task

        def done_callback(
            done_task: asyncio.Task[None],
        ) -> None:
            if (
                self._active_punishers.get(key)
                is done_task
            ):
                self._active_punishers.pop(
                    key,
                    None,
                )

            if done_task.cancelled():
                return

            try:
                exc = done_task.exception()

            except asyncio.CancelledError:
                return

            if exc is not None:
                print(
                    "[Reactor] Punishment task "
                    f"crashed for guild={key[0]} "
                    f"user={key[1]}: "
                    f"{type(exc).__name__}: {exc}"
                )

        task.add_done_callback(
            done_callback
        )

    async def _punishment_loop(
        self,
        member: discord.Member,
        anchor_message: discord.Message,
    ) -> None:
        guild = member.guild
        channel = anchor_message.channel
        current_anchor = anchor_message

        fresh = await self._fresh_member(
            guild,
            member.id,
        )

        if fresh is None:
            return

        # Protect existing moderator timeout.
        preexisting_timeout = self._active_timeout(
            fresh
        )

        owns_timeout = (
            preexisting_timeout is None
        )

        expected_timeout: datetime | None = None

        if owns_timeout:
            expected_timeout = await self._apply_timeout(
                fresh
            )

            if expected_timeout is None:
                await self._safe_send(
                    channel,
                    (
                        f"⚠️ I couldn't timeout "
                        f"{fresh.mention}. Check my "
                        "**Moderate Members** permission "
                        "and role position."
                    ),
                )

                return

            await self._safe_send(
                channel,
                (
                    f"🔴 {fresh.mention} was timed out "
                    "for 10 minutes after a "
                    "target-specific reaction denial. "
                    "Rechecking in 5 minutes."
                ),
            )

        else:
            await self._safe_send(
                channel,
                (
                    f"🔴 {fresh.mention} failed the "
                    "target-specific reaction check. "
                    "They already have a moderator "
                    "timeout, so Reactor will not "
                    "change it. Rechecking in 5 minutes."
                ),
            )

        while True:
            await asyncio.sleep(
                RECHECK_INTERVAL
            )

            fresh = await self._fresh_member(
                guild,
                member.id,
            )

            if fresh is None:
                return

            # If Reactor owned the timeout but something
            # changed it manually, stop managing it.
            if owns_timeout:
                current_until = self._active_timeout(
                    fresh
                )

                if not self._timeouts_match(
                    current_until,
                    expected_timeout,
                ):
                    await self._safe_send(
                        channel,
                        (
                            f"ℹ️ {fresh.mention}'s "
                            "timeout was changed manually. "
                            "Reactor has stopped managing it."
                        ),
                    )

                    return

            probe = await self._probe_block_state(
                current_anchor
            )

            if probe is BlockProbe.UNKNOWN:
                replacement = await self._find_recent_user_message(
                    channel,
                    fresh.id,
                )

                if replacement is not None:
                    current_anchor = replacement

                    self._remember_message(
                        replacement
                    )

                    probe = await self._probe_block_state(
                        current_anchor
                    )

            # ---------------- CLEAR ----------------

            if probe is BlockProbe.CLEAR:
                if owns_timeout:
                    removed = await self._remove_timeout_if_owned(
                        fresh,
                        expected_timeout,
                    )

                    if removed:
                        await self._safe_send(
                            channel,
                            (
                                f"🟢 {fresh.mention} passed "
                                "the reaction recheck. Their "
                                "Reactor timeout was removed."
                            ),
                        )

                    else:
                        await self._safe_send(
                            channel,
                            (
                                f"🟢 {fresh.mention} passed "
                                "the reaction recheck. Their "
                                "timeout was not changed because "
                                "it no longer matched Reactor's "
                                "last timeout."
                            ),
                        )

                else:
                    await self._safe_send(
                        channel,
                        (
                            f"🟢 {fresh.mention} passed "
                            "the reaction recheck. Their "
                            "existing moderator timeout "
                            "was left untouched."
                        ),
                    )

                return

            # ---------------- STILL FAILING ----------------

            if probe is BlockProbe.TARGET_FORBIDDEN:
                if owns_timeout:
                    expected_timeout = await self._apply_timeout(
                        fresh
                    )

                    if expected_timeout is None:
                        await self._safe_send(
                            channel,
                            (
                                f"⚠️ I could not extend "
                                f"{fresh.mention}'s Reactor "
                                "timeout. Automatic punishment "
                                "has stopped."
                            ),
                        )

                        return

                    await self._safe_send(
                        channel,
                        (
                            f"🔴 {fresh.mention} still fails "
                            "the target-specific reaction check. "
                            "Their timeout was extended by "
                            "another 10 minutes."
                        ),
                    )

                # If they already had a moderator timeout,
                # Reactor checks again later without modifying it.
                continue

            # ---------------- INCONCLUSIVE ----------------

            await self._safe_send(
                channel,
                (
                    "⚠️ I can no longer verify the "
                    f"reaction state for {fresh.mention}. "
                    "Automatic checks have stopped."
                ),
            )

            return

    # ================================================================
    # MAIN AUTOMATIC REACTIONS
    # ================================================================

    @commands.Cog.listener()
    async def on_message(
        self,
        message: discord.Message,
    ) -> None:
        if (
            message.author.bot
            or message.guild is None
        ):
            return

        user_id = str(
            message.author.id
        )

        emojis = tuple(
            self.config
            .get("users", {})
            .get(user_id, ())
        )

        if not emojis:
            return

        self._remember_message(
            message
        )

        for index, configured in enumerate(
            emojis
        ):
            emoji = self._resolve_emoji(
                configured
            )

            result = await self._attempt_reaction(
                message,
                emoji,
            )

            if result is ReactionAttempt.SUCCESS:
                if (
                    REACTION_DELAY
                    and index < len(emojis) - 1
                ):
                    await asyncio.sleep(
                        REACTION_DELAY
                    )

                continue

            # Message vanished. Nothing else can be done.
            if result is ReactionAttempt.NOT_FOUND:
                return

            # Invalid/unavailable custom emoji or another
            # HTTP error must never punish the user.
            #
            # Continue so one bad configured emoji does not
            # prevent the user's remaining reactions.
            if result is ReactionAttempt.FAILED:
                continue

            if result is ReactionAttempt.FORBIDDEN:
                probe = await self._probe_block_state(
                    message
                )

                if probe is BlockProbe.TARGET_FORBIDDEN:
                    member = message.author

                    if isinstance(
                        member,
                        discord.Member,
                    ):
                        await self._start_punishment(
                            member,
                            message,
                        )

                    return

                print(
                    "[Reactor] Forbidden reaction was "
                    "not target-specific: "
                    f"guild={message.guild.id} "
                    f"channel={message.channel.id} "
                    f"user={message.author.id} "
                    f"probe={probe.name}"
                )

                return

    # ================================================================
    # EVENTS FROM AUTORANDOMPINGER
    # ================================================================

    @commands.Cog.listener()
    async def on_user_blocked_bot(
        self,
        user: discord.User | discord.Member,
        *args: Any,
    ) -> None:
        """
        Compatible with:

            bot.dispatch("user_blocked_bot", user)

        AutoRandomPinger is optional.
        Reactor independently verifies the reaction state before
        taking moderation action.
        """
        supplied_message = next(
            (
                arg
                for arg in args
                if isinstance(
                    arg,
                    discord.Message,
                )
            ),
            None,
        )

        preferred_channel_id = self._pinger_last_channel_id(
            user.id
        )

        member = await self._resolve_member(
            user,
            preferred_channel_id,
        )

        if member is None:
            return

        anchor = await self._find_anchor(
            user,
            member,
            preferred_channel_id,
            supplied_message,
        )

        if anchor is None:
            return

        probe = await self._probe_block_state(
            anchor
        )

        if probe is BlockProbe.TARGET_FORBIDDEN:
            await self._start_punishment(
                member,
                anchor,
            )

    @commands.Cog.listener()
    async def on_check_unblocked(
        self,
        user: discord.User | discord.Member,
        *args: Any,
    ) -> None:
        """
        Compatible with:

            bot.dispatch("check_unblocked", user)

        If Reactor already has an active punishment loop for the
        member, that loop performs the rechecks.
        """
        supplied_message = next(
            (
                arg
                for arg in args
                if isinstance(
                    arg,
                    discord.Message,
                )
            ),
            None,
        )

        preferred_channel_id = self._pinger_last_channel_id(
            user.id
        )

        member = await self._resolve_member(
            user,
            preferred_channel_id,
        )

        if member is None:
            return

        key = (
            member.guild.id,
            member.id,
        )

        active = self._active_punishers.get(
            key
        )

        if (
            active is not None
            and not active.done()
        ):
            return

        anchor = await self._find_anchor(
            user,
            member,
            preferred_channel_id,
            supplied_message,
        )

        if anchor is None:
            return

        probe = await self._probe_block_state(
            anchor
        )

        if probe is BlockProbe.TARGET_FORBIDDEN:
            await self._start_punishment(
                member,
                anchor,
            )

    # ================================================================
    # COMMAND PERMISSION HELPERS
    # ================================================================

    @staticmethod
    def _owner(
        ctx: commands.Context,
    ) -> bool:
        return (
            ctx.author.id
            == OWNER_ID
        )

    def _can_manage_user(
        self,
        ctx: commands.Context,
        user: discord.User,
    ) -> bool:
        """
        OWNER:
            Can modify anyone.

        self_assign_allowed:
            Can modify themselves only.
        """
        if self._owner(ctx):
            return True

        author_id = str(
            ctx.author.id
        )

        return (
            author_id
            in self.config.get(
                "self_assign_allowed",
                [],
            )
            and author_id
            == str(user.id)
        )

    def _can_view_reactions(
        self,
        ctx: commands.Context,
    ) -> bool:
        if self._owner(ctx):
            return True

        return (
            str(ctx.author.id)
            in self.config.get(
                "self_assign_allowed",
                [],
            )
        )

    def _display_config_user(
        self,
        ctx: commands.Context,
        user_id: str,
    ) -> str:
        """
        Resolve a readable username without pinging users in !rl.
        """
        try:
            uid = int(user_id)
        except ValueError:
            return f"User ID `{user_id}`"

        if ctx.guild is not None:
            member = ctx.guild.get_member(
                uid
            )

            if member is not None:
                return (
                    f"{discord.utils.escape_markdown(str(member))} "
                    f"(`{uid}`)"
                )

        user = self.bot.get_user(
            uid
        )

        if user is not None:
            return (
                f"{discord.utils.escape_markdown(str(user))} "
                f"(`{uid}`)"
            )

        return f"User ID `{uid}`"

    # ================================================================
    # COMMANDS
    # ================================================================

    @commands.command(
        name="removereactuser",
        aliases=["rru"],
    )
    async def remove_react_user(
        self,
        ctx: commands.Context,
        user: discord.User,
    ) -> None:
        """
        Completely remove a user from automatic reaction tracking.

        This does not remove their self-assign permission.
        Use dsa separately if that permission should also be revoked.
        """
        if not self._owner(ctx):
            await ctx.send(
                "❌ You are not allowed to use this command."
            )
            return

        user_id = str(
            user.id
        )

        if user_id not in self.config["users"]:
            await ctx.send(
                "⚠️ That user isn't being tracked."
            )
            return

        del self.config["users"][
            user_id
        ]

        await self._save_config()

        await ctx.send(
            f"🗑️ Removed **{user}** from the reaction list."
        )

    # ----------------------------------------------------------------

    @commands.command(
        name="adduseremoji",
        aliases=["aue"],
    )
    async def add_user_emoji(
        self,
        ctx: commands.Context,
        user: discord.User,
        emoji: str,
    ) -> None:
        """
        Add a reaction to a user.

        Owner:
            Can modify anyone.

        Self-assign users:
            Can modify only themselves.

        Unicode and Discord custom emojis are both allowed.
        """
        if not self._can_manage_user(
            ctx,
            user,
        ):
            author_id = str(
                ctx.author.id
            )

            if (
                author_id
                in self.config.get(
                    "self_assign_allowed",
                    [],
                )
            ):
                await ctx.send(
                    "❌ You can only modify "
                    "**your own** reactions."
                )

            else:
                await ctx.send(
                    "❌ You are not allowed "
                    "to modify reactions."
                )

            return

        emoji = emoji.strip()

        if not emoji:
            await ctx.send(
                "❌ Please provide an emoji."
            )
            return

        user_id = str(
            user.id
        )

        # aue automatically starts tracking new users.
        automatically_added = (
            user_id
            not in self.config["users"]
        )

        if automatically_added:
            self.config["users"][
                user_id
            ] = []

        if (
            emoji
            in self.config["users"][user_id]
        ):
            await ctx.send(
                f"⚠️ {user.name} already has that emoji."
            )
            return

        self.config["users"][
            user_id
        ].append(
            emoji
        )

        await self._save_config()

        if automatically_added:
            await ctx.send(
                f"✅ {user.name} was automatically "
                "added to the reaction list."
            )

        await ctx.send(
            f"✅ Added {emoji} to "
            f"{user.name}'s reactions."
        )

    # ----------------------------------------------------------------

    @commands.command(
        name="removeuseremoji",
        aliases=["rue"],
    )
    async def remove_user_emoji(
        self,
        ctx: commands.Context,
        user: discord.User,
        emoji: str,
    ) -> None:
        """
        Remove one configured reaction from a user.
        """
        if not self._can_manage_user(
            ctx,
            user,
        ):
            author_id = str(
                ctx.author.id
            )

            if (
                author_id
                in self.config.get(
                    "self_assign_allowed",
                    [],
                )
            ):
                await ctx.send(
                    "❌ You can only modify "
                    "**your own** reactions."
                )

            else:
                await ctx.send(
                    "❌ You are not allowed "
                    "to use this command."
                )

            return

        user_id = str(
            user.id
        )

        emojis = self.config[
            "users"
        ].get(
            user_id
        )

        if (
            not emojis
            or emoji not in emojis
        ):
            await ctx.send(
                "⚠️ Emoji not found for that user."
            )
            return

        emojis.remove(
            emoji
        )

        await self._save_config()

        await ctx.send(
            f"🗑️ Removed {emoji} from **{user}**."
        )

    # ----------------------------------------------------------------

    @commands.command(
        name="allowselfassign",
        aliases=["asa"],
    )
    async def allow_self_assign(
        self,
        ctx: commands.Context,
        user: discord.User,
    ) -> None:
        """
        Allow a user to manage their OWN reaction list.

        This does NOT grant special custom Discord emoji permissions.
        Unicode and custom Discord emojis are always treated equally.
        """
        if not self._owner(ctx):
            await ctx.send(
                "❌ You are not allowed to use this command."
            )
            return

        user_id = str(
            user.id
        )

        if (
            user_id
            in self.config["self_assign_allowed"]
        ):
            await ctx.send(
                "⚠️ That user can already manage "
                "their own reactions."
            )
            return

        self.config[
            "self_assign_allowed"
        ].append(
            user_id
        )

        await self._save_config()

        await ctx.send(
            f"✅ {user.mention} can now manage "
            "their own reaction list."
        )

    # ----------------------------------------------------------------

    @commands.command(
        name="disallowselfassign",
        aliases=["dsa"],
    )
    async def disallow_self_assign(
        self,
        ctx: commands.Context,
        user: discord.User,
    ) -> None:
        """
        Revoke a user's ability to manage their own reaction list.

        Existing configured reactions remain untouched.
        """
        if not self._owner(ctx):
            await ctx.send(
                "❌ You are not allowed to use this command."
            )
            return

        user_id = str(
            user.id
        )

        if (
            user_id
            not in self.config["self_assign_allowed"]
        ):
            await ctx.send(
                "⚠️ That user doesn't have "
                "self-assign permission."
            )
            return

        self.config[
            "self_assign_allowed"
        ].remove(
            user_id
        )

        await self._save_config()

        await ctx.send(
            f"🚫 {user.mention} can no longer "
            "manage their own reaction list."
        )

    # ----------------------------------------------------------------

    @commands.command(
        name="reactlist",
        aliases=["rl"],
    )
    async def react_list(
        self,
        ctx: commands.Context,
        user: discord.User | None = None,
    ) -> None:
        """
        !rl
            Show every currently configured user and their reactions.

        !rl @user
            Show only that user's configured reactions.
        """
        if not self._can_view_reactions(
            ctx
        ):
            await ctx.send(
                "❌ You are not allowed to use this command."
            )
            return

        # ============================================================
        # SPECIFIC USER
        # ============================================================

        if user is not None:
            emojis = self.config[
                "users"
            ].get(
                str(user.id),
                [],
            )

            if not emojis:
                await ctx.send(
                    f"**{user}** has no reactions assigned."
                )
                return

            await self._send_chunked(
                ctx,
                (
                    f"**Reactions for {user}:**\n"
                    + " ".join(emojis)
                ),
            )

            return

        # ============================================================
        # ALL USERS
        # ============================================================

        users = self.config.get(
            "users",
            {},
        )

        if not users:
            await ctx.send(
                "⚠️ No reactions are currently configured."
            )
            return

        lines = [
            "**Configured reactions:**"
        ]

        for user_id, emojis in users.items():
            display_name = self._display_config_user(
                ctx,
                user_id,
            )

            if emojis:
                reaction_text = " ".join(
                    emojis
                )
            else:
                reaction_text = "*No reactions*"

            lines.append(
                f"• **{display_name}** — {reaction_text}"
            )

        await self._send_chunked(
            ctx,
            "\n".join(lines),
        )


async def setup(
    bot: commands.Bot,
) -> None:
    await bot.add_cog(
        Reactor(bot)
    )