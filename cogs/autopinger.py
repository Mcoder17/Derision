import asyncio
import json
import os
import random
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from env import OWNER_ID


CONFIG_FILE = "data/random_ping_data.json"

CHECK_EMOJI = "✅"

TIMEOUT_DURATION = 10 * 60          # 10 minutes
PING_CHECK_INTERVAL = 3 * 60        # Re-check every 3 minutes
PING_THRESHOLD = 15                 # Run block probe every 15 successful pings

MIN_INTERVAL = 10
BLOCK_CONFIRMATION_PROBES = 2

# If a timeout's expiry changes by more than this amount from what this
# cog set, assume a moderator changed it and stop touching that timeout.
TIMEOUT_OWNERSHIP_TOLERANCE = 45


class AutoRandomPinger(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = self._load_data()

        # uid -> monotonic timestamp for next scheduled ping
        self.next_ping_at: dict[str, float] = {}

        # uid -> number of successful pings since last block probe
        self.ping_counts: dict[str, int] = {}

        # (guild_id, user_id) -> block monitoring task
        self.blocked_tasks: dict[tuple[int, int], asyncio.Task] = {}

        # uid -> currently running scheduled ping task
        self.active_ping_tasks: dict[str, asyncio.Task] = {}

        # (guild_id, user_id) -> timeout expiry that THIS cog set
        self.auto_timeout_expiry: dict[
            tuple[int, int], datetime
        ] = {}

        # If a moderator-owned timeout is detected, this cog will not
        # alter that user's timeout for the remainder of that monitoring session.
        self.timeout_suppressed: set[tuple[int, int]] = set()

        self.random_ping_task.start()

    # ================================================================
    # Lifecycle
    # ================================================================

    def cog_unload(self):
        self.random_ping_task.cancel()

        for task in list(self.active_ping_tasks.values()):
            task.cancel()

        self.active_ping_tasks.clear()

        for task in list(self.blocked_tasks.values()):
            task.cancel()

        self.blocked_tasks.clear()

    @tasks.loop(seconds=1)
    async def random_ping_task(self):
        """
        Scheduler.

        Uses the event loop's monotonic clock instead of decrementing a
        counter every iteration, so API delays do not distort intervals.
        """
        loop = asyncio.get_running_loop()
        now = loop.time()

        users = list(self.data.get("users", {}).items())

        for uid, entry in users:
            try:
                if not entry.get("running", False):
                    self.next_ping_at.pop(uid, None)
                    continue

                interval = self._safe_interval(entry.get("interval", 300))

                next_time = self.next_ping_at.get(uid)

                if next_time is None:
                    # First ping occurs after the configured interval.
                    self.next_ping_at[uid] = now + interval
                    continue

                if now < next_time:
                    continue

                # Don't accumulate missed pings if Discord/API work runs slowly.
                self.next_ping_at[uid] = now + interval

                existing = self.active_ping_tasks.get(uid)
                if existing and not existing.done():
                    continue

                task = asyncio.create_task(
                    self._scheduled_ping_runner(uid)
                )

                self.active_ping_tasks[uid] = task

                task.add_done_callback(
                    lambda finished, user_id=uid:
                    self._scheduled_task_finished(user_id, finished)
                )

            except Exception as e:
                print(
                    f"[AutoRandomPinger] Scheduler error for {uid}: {e}"
                )

    @random_ping_task.before_loop
    async def before_random_ping_task(self):
        await self.bot.wait_until_ready()

    @random_ping_task.error
    async def random_ping_task_error(self, error: Exception):
        print(
            f"[AutoRandomPinger] random_ping_task crashed: {error}"
        )

    def _scheduled_task_finished(
        self,
        uid: str,
        task: asyncio.Task
    ):
        current = self.active_ping_tasks.get(uid)

        if current is task:
            self.active_ping_tasks.pop(uid, None)

    async def _scheduled_ping_runner(self, uid: str):
        try:
            entry = self.data.get("users", {}).get(uid)

            if not entry or not entry.get("running", False):
                return

            await self._perform_ping(uid, entry)

        except asyncio.CancelledError:
            raise

        except Exception as e:
            print(
                f"[AutoRandomPinger] Scheduled ping failure "
                f"for {uid}: {e}"
            )

    # ================================================================
    # Persistence
    # ================================================================

    def _load_data(self) -> dict:
        default = {"users": {}}

        if not os.path.exists(CONFIG_FILE):
            self._save_data(default)
            return default

        try:
            with open(
                CONFIG_FILE,
                "r",
                encoding="utf-8"
            ) as f:
                data = json.load(f)

            if not isinstance(data, dict):
                raise ValueError("Root JSON object must be a dictionary.")

            users = data.get("users")

            if not isinstance(users, dict):
                data["users"] = {}

            return data

        except Exception as e:
            print(
                f"[AutoRandomPinger] Failed to load data file: {e}"
            )
            return default

    def _save_data(self, data: dict | None = None) -> None:
        if data is None:
            data = self.data

        try:
            directory = os.path.dirname(CONFIG_FILE)

            if directory:
                os.makedirs(directory, exist_ok=True)

            temp_file = f"{CONFIG_FILE}.tmp"

            with open(
                temp_file,
                "w",
                encoding="utf-8"
            ) as f:
                json.dump(
                    data,
                    f,
                    indent=2,
                    ensure_ascii=False
                )

            # Atomic replacement prevents a partially-written config
            # from being left behind if something goes wrong.
            os.replace(temp_file, CONFIG_FILE)

        except Exception as e:
            print(
                f"[AutoRandomPinger] Failed to save data file: {e}"
            )

    def _update_last_channel(
        self,
        user_id: str,
        channel_id: int,
        guild_id: int | None = None
    ):
        users = self.data.setdefault("users", {})

        entry = users.get(user_id, {})

        entry["last_channel"] = channel_id

        if guild_id is not None:
            entry["guild_id"] = guild_id

        users[user_id] = entry
        self._save_data()

    # ================================================================
    # General helpers
    # ================================================================

    @staticmethod
    def _safe_interval(value) -> int:
        try:
            return max(MIN_INTERVAL, int(value))
        except (TypeError, ValueError):
            return 300

    def _bot_member(
        self,
        guild: discord.Guild
    ) -> discord.Member | None:
        if guild.me:
            return guild.me

        if self.bot.user:
            return guild.get_member(self.bot.user.id)

        return None

    def _get_valid_text_channels(
        self,
        guild: discord.Guild,
        member: discord.Member
    ) -> list[discord.TextChannel]:
        bot_member = self._bot_member(guild)

        if not bot_member:
            return []

        valid: list[discord.TextChannel] = []

        for channel in guild.text_channels:
            bot_perms = channel.permissions_for(bot_member)

            if not (
                bot_perms.view_channel
                and bot_perms.send_messages
                and bot_perms.add_reactions
                and bot_perms.read_message_history
            ):
                continue

            user_perms = channel.permissions_for(member)

            # Do not ping the user somewhere they cannot see.
            if not user_perms.view_channel:
                continue

            valid.append(channel)

        return valid

    def _can_probe_channel(
        self,
        channel: discord.TextChannel
    ) -> bool:
        bot_member = self._bot_member(channel.guild)

        if not bot_member:
            return False

        perms = channel.permissions_for(bot_member)

        return (
            perms.view_channel
            and perms.read_message_history
            and perms.add_reactions
        )

    def _has_block_monitor(self, user_id: int) -> bool:
        for (guild_id, uid), task in self.blocked_tasks.items():
            if uid == user_id and not task.done():
                return True

        return False

    # ================================================================
    # Message / history helpers
    # ================================================================

    async def _find_recent_user_messages_in_channel(
        self,
        channel: discord.TextChannel,
        user_id: int,
        *,
        history_limit: int = 200,
        max_results: int = 1,
        exclude_ids: set[int] | None = None
    ) -> list[discord.Message]:
        exclude_ids = exclude_ids or set()

        found: list[discord.Message] = []

        try:
            async for message in channel.history(
                limit=history_limit
            ):
                if message.id in exclude_ids:
                    continue

                if message.author.id != user_id:
                    continue

                found.append(message)

                if len(found) >= max_results:
                    break

        except (
            discord.Forbidden,
            discord.HTTPException
        ):
            pass

        return found

    async def _find_probe_candidates(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        preferred_channel: discord.TextChannel | None = None,
        max_results: int = BLOCK_CONFIRMATION_PROBES
    ) -> list[discord.Message]:
        """
        Locate multiple distinct messages authored by the target user.

        We intentionally try to use more than one message for initial
        block confirmation so one random 403 doesn't immediately cause
        a moderation action.
        """
        channels: list[discord.TextChannel] = []

        if (
            preferred_channel
            and preferred_channel.guild.id == guild.id
            and self._can_probe_channel(preferred_channel)
        ):
            channels.append(preferred_channel)

        remaining = [
            channel
            for channel in guild.text_channels
            if channel not in channels
            and self._can_probe_channel(channel)
        ]

        random.shuffle(remaining)
        channels.extend(remaining)

        results: list[discord.Message] = []
        seen_ids: set[int] = set()

        for channel in channels:
            if len(results) >= max_results:
                break

            needed = max_results - len(results)

            found = await self._find_recent_user_messages_in_channel(
                channel,
                user_id,
                history_limit=200,
                max_results=needed,
                exclude_ids=seen_ids
            )

            for message in found:
                seen_ids.add(message.id)
                results.append(message)

                if len(results) >= max_results:
                    break

        return results

    # ================================================================
    # Reaction probe
    # ================================================================

    async def _probe_message_reaction(
        self,
        message: discord.Message
    ) -> str:
        """
        Attempt to add CHECK_EMOJI to a user's message.

        Returns:
            "clear"
                Reaction succeeded.

            "forbidden"
                Discord returned Forbidden even though the bot's local
                channel permissions still appear sufficient.

            "unknown"
                The test is inconclusive due to permissions, deletion,
                network/API errors, etc.

        IMPORTANT:
        A Forbidden response is not treated as absolute proof by itself.
        Initial detection requires multiple independent forbidden probes.
        """
        if not message.guild:
            return "unknown"

        if not isinstance(message.channel, discord.TextChannel):
            return "unknown"

        channel = message.channel

        if not self._can_probe_channel(channel):
            return "unknown"

        bot_member = self._bot_member(message.guild)

        if not bot_member:
            return "unknown"

        # If our probe emoji is somehow already present from this bot,
        # remove it first so the next add is a genuine API operation.
        for reaction in message.reactions:
            if (
                str(reaction.emoji) == CHECK_EMOJI
                and reaction.me
            ):
                try:
                    await message.remove_reaction(
                        CHECK_EMOJI,
                        bot_member
                    )
                except (
                    discord.Forbidden,
                    discord.HTTPException
                ):
                    return "unknown"

                break

        try:
            await message.add_reaction(CHECK_EMOJI)

        except discord.Forbidden:
            # Permissions may have changed between our initial check
            # and the API call. Re-check before considering this a
            # probable block signal.
            if not self._can_probe_channel(channel):
                return "unknown"

            return "forbidden"

        except (
            discord.NotFound,
            discord.HTTPException
        ):
            return "unknown"

        # Successful probe. Clean it up immediately.
        try:
            await message.remove_reaction(
                CHECK_EMOJI,
                bot_member
            )
        except (
            discord.Forbidden,
            discord.HTTPException
        ):
            pass

        return "clear"

    async def _confirm_probable_block(
        self,
        guild: discord.Guild,
        user_id: int,
        preferred_channel: discord.TextChannel | None
    ) -> tuple[str, discord.Message | None]:
        """
        Require multiple forbidden reaction probes before calling the
        result a probable block.

        Returns:
            ("blocked", message)
            ("clear", message)
            ("unknown", message_or_none)
        """
        candidates = await self._find_probe_candidates(
            guild,
            user_id,
            preferred_channel=preferred_channel,
            max_results=BLOCK_CONFIRMATION_PROBES
        )

        # Don't punish somebody based on only one questionable API call.
        if len(candidates) < BLOCK_CONFIRMATION_PROBES:
            return "unknown", candidates[0] if candidates else None

        forbidden_count = 0

        for message in candidates:
            result = await self._probe_message_reaction(message)

            if result == "clear":
                return "clear", message

            if result == "unknown":
                return "unknown", message

            if result == "forbidden":
                forbidden_count += 1

        if forbidden_count >= BLOCK_CONFIRMATION_PROBES:
            return "blocked", candidates[0]

        return "unknown", candidates[0]

    # ================================================================
    # Timeout helpers
    # ================================================================

    def _can_timeout_member(
        self,
        member: discord.Member
    ) -> bool:
        guild = member.guild
        bot_member = self._bot_member(guild)

        if not bot_member:
            return False

        if member.id == guild.owner_id:
            return False

        if not bot_member.guild_permissions.moderate_members:
            return False

        # Guild owner bypasses normal role hierarchy.
        if bot_member.id == guild.owner_id:
            return True

        if bot_member.top_role <= member.top_role:
            return False

        return True

    async def _apply_timeout(
        self,
        member: discord.Member
    ) -> bool:
        """
        Apply/extend an auto-timeout.

        This deliberately avoids replacing a timeout that appears to
        belong to a human moderator.
        """
        key = (member.guild.id, member.id)

        if key in self.timeout_suppressed:
            return False

        if not self._can_timeout_member(member):
            print(
                f"[AutoRandomPinger] Cannot timeout "
                f"{member} ({member.id}) in {member.guild.id}: "
                f"missing permission or role hierarchy."
            )
            return False

        now = datetime.now(timezone.utc)
        desired_until = now + timedelta(
            seconds=TIMEOUT_DURATION
        )

        current_until = member.timed_out_until
        owned_until = self.auto_timeout_expiry.get(key)

        if current_until:
            if current_until.tzinfo is None:
                current_until = current_until.replace(
                    tzinfo=timezone.utc
                )

            if current_until > now:
                if owned_until is None:
                    # User was already timed out by something else.
                    self.timeout_suppressed.add(key)

                    print(
                        f"[AutoRandomPinger] {member.id} already has "
                        f"an external timeout; auto-timeout suppressed."
                    )
                    return False

                difference = abs(
                    (
                        current_until - owned_until
                    ).total_seconds()
                )

                if difference > TIMEOUT_OWNERSHIP_TOLERANCE:
                    # A moderator probably changed our timeout.
                    self.auto_timeout_expiry.pop(key, None)
                    self.timeout_suppressed.add(key)

                    print(
                        f"[AutoRandomPinger] Timeout for {member.id} "
                        f"was modified externally; no longer touching it."
                    )
                    return False

        # If our timeout should still be active but disappeared,
        # assume a moderator manually removed it.
        if (
            owned_until
            and owned_until > now
            and (
                current_until is None
                or current_until <= now
            )
        ):
            self.auto_timeout_expiry.pop(key, None)
            self.timeout_suppressed.add(key)

            print(
                f"[AutoRandomPinger] Timeout for {member.id} appears "
                f"to have been manually removed; auto-timeout suppressed."
            )
            return False

        try:
            # "until" is positional-only in modern discord.py.
            await member.timeout(
                desired_until,
                reason="AutoRandomPinger: probable bot block detected"
            )

            self.auto_timeout_expiry[key] = desired_until

            return True

        except discord.Forbidden as e:
            print(
                f"[AutoRandomPinger] Failed to timeout "
                f"{member} ({member.id}): {e}"
            )

        except discord.HTTPException as e:
            print(
                f"[AutoRandomPinger] Timeout HTTP failure for "
                f"{member.id}: {e}"
            )

        return False

    async def _remove_timeout(
        self,
        member: discord.Member
    ) -> bool:
        """
        Remove a timeout only if this cog still appears to own it.
        """
        key = (member.guild.id, member.id)

        owned_until = self.auto_timeout_expiry.get(key)

        if owned_until is None:
            return False

        current_until = member.timed_out_until
        now = datetime.now(timezone.utc)

        if current_until is None or current_until <= now:
            self.auto_timeout_expiry.pop(key, None)
            return True

        if current_until.tzinfo is None:
            current_until = current_until.replace(
                tzinfo=timezone.utc
            )

        difference = abs(
            (
                current_until - owned_until
            ).total_seconds()
        )

        if difference > TIMEOUT_OWNERSHIP_TOLERANCE:
            # Someone modified the timeout after us.
            self.auto_timeout_expiry.pop(key, None)
            self.timeout_suppressed.add(key)

            print(
                f"[AutoRandomPinger] Refusing to remove timeout "
                f"for {member.id}; it appears externally modified."
            )

            return False

        if not self._can_timeout_member(member):
            return False

        try:
            await member.timeout(
                None,
                reason="AutoRandomPinger: block probe succeeded"
            )

            self.auto_timeout_expiry.pop(key, None)

            return True

        except discord.Forbidden as e:
            print(
                f"[AutoRandomPinger] Failed removing timeout "
                f"from {member.id}: {e}"
            )

        except discord.HTTPException as e:
            print(
                f"[AutoRandomPinger] Timeout removal HTTP failure "
                f"for {member.id}: {e}"
            )

        return False

    async def _remove_owned_timeouts_for_user(
        self,
        user_id: int
    ):
        keys = [
            key
            for key in self.auto_timeout_expiry
            if key[1] == user_id
        ]

        for guild_id, uid in keys:
            guild = self.bot.get_guild(guild_id)

            if not guild:
                self.auto_timeout_expiry.pop(
                    (guild_id, uid),
                    None
                )
                continue

            member = guild.get_member(uid)

            if member:
                await self._remove_timeout(member)

    # ================================================================
    # Block monitor
    # ================================================================

    def _start_block_monitor(
        self,
        guild: discord.Guild,
        member: discord.Member,
        channel: discord.TextChannel,
        last_message_id: int | None
    ):
        key = (guild.id, member.id)

        existing = self.blocked_tasks.get(key)

        if existing and not existing.done():
            return

        task = asyncio.create_task(
            self._periodic_block_check(
                guild,
                member.id,
                channel,
                last_message_id
            )
        )

        self.blocked_tasks[key] = task

    async def _cancel_block_monitors_for_user(
        self,
        user_id: int
    ):
        keys = [
            key
            for key in self.blocked_tasks
            if key[1] == user_id
        ]

        for key in keys:
            task = self.blocked_tasks.pop(key, None)

            if task:
                task.cancel()

        await self._remove_owned_timeouts_for_user(user_id)

        for key in list(self.timeout_suppressed):
            if key[1] == user_id:
                self.timeout_suppressed.discard(key)

    async def _periodic_block_check(
        self,
        guild: discord.Guild,
        user_id: int,
        channel: discord.TextChannel,
        last_message_id: int | None
    ):
        key = (guild.id, user_id)

        try:
            while True:
                await asyncio.sleep(PING_CHECK_INTERVAL)

                member = guild.get_member(user_id)

                if not member:
                    return

                check_message: discord.Message | None = None

                # Prefer the original confirmed message.
                if last_message_id:
                    try:
                        fetched = await channel.fetch_message(
                            last_message_id
                        )

                        if fetched.author.id == user_id:
                            check_message = fetched

                    except (
                        discord.Forbidden,
                        discord.NotFound,
                        discord.HTTPException
                    ):
                        check_message = None

                # Otherwise locate another recent message.
                if check_message is None:
                    candidates = await self._find_probe_candidates(
                        guild,
                        user_id,
                        preferred_channel=channel,
                        max_results=1
                    )

                    if candidates:
                        check_message = candidates[0]
                        channel = check_message.channel

                        if isinstance(
                            check_message.channel,
                            discord.TextChannel
                        ):
                            channel = check_message.channel

                        last_message_id = check_message.id

                if check_message is None:
                    continue

                result = await self._probe_message_reaction(
                    check_message
                )

                if result == "clear":
                    await self._remove_timeout(member)

                    # Monitoring session is finished, so reset
                    # moderator-interference suppression for next time.
                    self.timeout_suppressed.discard(key)

                    try:
                        bot_member = self._bot_member(guild)

                        if bot_member:
                            perms = channel.permissions_for(
                                bot_member
                            )

                            if perms.send_messages:
                                await channel.send(
                                    f"🟢 {member.mention} can interact "
                                    f"with the bot again. Monitoring stopped.",
                                    allowed_mentions=discord.AllowedMentions(
                                        users=True
                                    )
                                )

                    except discord.HTTPException:
                        pass

                    return

                if result == "forbidden":
                    # Initial monitoring was only started after multiple
                    # independent forbidden probes. One forbidden result
                    # is therefore sufficient for later re-checks.
                    await self._apply_timeout(member)

                # "unknown" simply waits for the next check.

        except asyncio.CancelledError:
            raise

        except Exception as e:
            print(
                f"[AutoRandomPinger] Block monitor error for "
                f"{user_id} in guild {guild.id}: {e}"
            )

        finally:
            current = self.blocked_tasks.get(key)

            if current is asyncio.current_task():
                self.blocked_tasks.pop(key, None)

    # ================================================================
    # Ping execution
    # ================================================================

    async def _send_ping_message(
        self,
        channel: discord.TextChannel,
        member: discord.Member
    ) -> bool:
        """
        Send and delete a mention.

        IMPORTANT:
        Forbidden here is a CHANNEL/API permission problem.
        It is NOT interpreted as proof that the target blocked the bot.
        """
        try:
            message = await channel.send(
                member.mention,
                allowed_mentions=discord.AllowedMentions(
                    users=True
                )
            )

        except discord.Forbidden as e:
            print(
                f"[AutoRandomPinger] Cannot send ping in "
                f"{channel.guild.id}/{channel.id}: {e}"
            )
            return False

        except discord.HTTPException as e:
            print(
                f"[AutoRandomPinger] Ping send HTTP failure in "
                f"{channel.guild.id}/{channel.id}: {e}"
            )
            return False

        try:
            await message.delete()

        except discord.HTTPException as e:
            # Sending succeeded, so still count this as a successful ping.
            print(
                f"[AutoRandomPinger] Could not delete ping message "
                f"{message.id}: {e}"
            )

        return True

    async def _after_successful_ping(
        self,
        uid: str,
        guild: discord.Guild,
        member: discord.Member,
        channel: discord.TextChannel
    ):
        self._update_last_channel(
            uid,
            channel.id,
            guild.id
        )

        self.ping_counts[uid] = (
            self.ping_counts.get(uid, 0) + 1
        )

        if self.ping_counts[uid] < PING_THRESHOLD:
            return

        self.ping_counts[uid] = 0

        status, probe_message = await self._confirm_probable_block(
            guild,
            member.id,
            channel
        )

        if status != "blocked":
            return

        print(
            f"[AutoRandomPinger] Multiple reaction probes were "
            f"forbidden for {member.id}; treating as probable block."
        )

        await self._apply_timeout(member)

        if probe_message is not None:
            probe_channel = probe_message.channel

            if isinstance(
                probe_channel,
                discord.TextChannel
            ):
                self._start_block_monitor(
                    guild,
                    member,
                    probe_channel,
                    probe_message.id
                )

    async def _perform_ping(
        self,
        uid: str,
        entry: dict
    ) -> bool:
        user_id = int(uid)

        # While probable-block monitoring is active, stop sending new
        # pings to that account.
        if self._has_block_monitor(user_id):
            return False

        scope = entry.get("scope", "single")

        # ------------------------------------------------------------
        # Single guild
        # ------------------------------------------------------------

        if scope == "single":
            guild_id = entry.get("guild_id")

            if not guild_id:
                return False

            guild = self.bot.get_guild(int(guild_id))

            if not guild:
                return False

            member = guild.get_member(user_id)

            if not member:
                return False

            valid_channels = self._get_valid_text_channels(
                guild,
                member
            )

            if not valid_channels:
                return False

            channel: discord.TextChannel | None = None

            last_channel_id = entry.get("last_channel")

            if last_channel_id:
                candidate = guild.get_channel(
                    int(last_channel_id)
                )

                if (
                    isinstance(
                        candidate,
                        discord.TextChannel
                    )
                    and candidate in valid_channels
                ):
                    channel = candidate

            if channel is None:
                channel = random.choice(valid_channels)

            success = await self._send_ping_message(
                channel,
                member
            )

            if success:
                await self._after_successful_ping(
                    uid,
                    guild,
                    member,
                    channel
                )

            return success

        # ------------------------------------------------------------
        # All mutual guilds
        #
        # "all" preserves the old behavior: each interval picks one
        # random mutual guild rather than pinging every mutual guild.
        # ------------------------------------------------------------

        mutuals: list[
            tuple[
                discord.Guild,
                discord.Member
            ]
        ] = []

        for guild in self.bot.guilds:
            member = guild.get_member(user_id)

            if member:
                mutuals.append((guild, member))

        if not mutuals:
            return False

        random.shuffle(mutuals)

        for guild, member in mutuals:
            channels = self._get_valid_text_channels(
                guild,
                member
            )

            if not channels:
                continue

            random.shuffle(channels)

            # Try a few channels in case permissions changed between
            # our local permission check and the API request.
            for channel in channels[:3]:
                success = await self._send_ping_message(
                    channel,
                    member
                )

                if not success:
                    continue

                await self._after_successful_ping(
                    uid,
                    guild,
                    member,
                    channel
                )

                return True

        return False

    # ================================================================
    # Command permission check
    # ================================================================

    async def cog_check(
        self,
        ctx: commands.Context
    ) -> bool:
        return ctx.author.id == int(OWNER_ID)

    # ================================================================
    # Commands
    # ================================================================

    @commands.command(name="pingstart")
    @commands.guild_only()
    async def cmd_ping_start(
        self,
        ctx: commands.Context,
        user: discord.Member,
        interval: int = 300
    ):
        """
        Start pinging a member in the current guild.
        """
        if interval < MIN_INTERVAL:
            return await ctx.send(
                f"⚠ Interval must be at least "
                f"{MIN_INTERVAL} seconds."
            )

        assert ctx.guild is not None

        uid = str(user.id)

        entry = self.data["users"].get(uid, {})

        entry["interval"] = interval
        entry["running"] = True
        entry["scope"] = "single"
        entry["guild_id"] = ctx.guild.id

        self.data["users"][uid] = entry
        self._save_data()

        # Reset the schedule so the new interval starts now.
        self.next_ping_at.pop(uid, None)
        self.ping_counts.pop(uid, None)

        await ctx.send(
            f"✅ Started auto-pinging {user.mention} "
            f"every {interval}s in this server.",
            allowed_mentions=discord.AllowedMentions(
                users=True
            )
        )

    @commands.command(name="pingstop")
    async def cmd_ping_stop(
        self,
        ctx: commands.Context,
        user: discord.User
    ):
        """
        Stop pinging a user and cancel any block monitor created for them.
        """
        uid = str(user.id)

        entry = self.data["users"].get(uid)

        if not entry or not entry.get("running", False):
            return await ctx.send(
                "⚠ That user is not currently being pinged."
            )

        entry["running"] = False

        self.data["users"][uid] = entry
        self._save_data()

        self.next_ping_at.pop(uid, None)
        self.ping_counts.pop(uid, None)

        running_task = self.active_ping_tasks.pop(
            uid,
            None
        )

        if running_task:
            running_task.cancel()

        await self._cancel_block_monitors_for_user(
            user.id
        )

        await ctx.send(
            f"🛑 Stopped auto-pinging {user.mention}.",
            allowed_mentions=discord.AllowedMentions.none()
        )

    @commands.command(name="setinterval")
    async def cmd_set_interval(
        self,
        ctx: commands.Context,
        user: discord.User,
        seconds: int
    ):
        """
        Set a user's ping interval.
        """
        if seconds < MIN_INTERVAL:
            return await ctx.send(
                f"⚠ Interval must be at least "
                f"{MIN_INTERVAL} seconds."
            )

        uid = str(user.id)

        entry = self.data["users"].get(uid, {})

        entry["interval"] = seconds

        self.data["users"][uid] = entry
        self._save_data()

        # Restart the interval from the current time.
        self.next_ping_at.pop(uid, None)

        await ctx.send(
            f"⏱ Interval for {user.mention} "
            f"set to {seconds}s.",
            allowed_mentions=discord.AllowedMentions.none()
        )

    @commands.command(name="setscope")
    @commands.guild_only()
    async def cmd_set_scope(
        self,
        ctx: commands.Context,
        user: discord.User,
        scope: str
    ):
        """
        Set user scope to:
          single - current guild only
          all    - randomly choose from mutual guilds each ping
        """
        assert ctx.guild is not None

        scope = scope.lower().strip()

        if scope not in ("single", "all"):
            return await ctx.send(
                "⚠ Scope must be `single` or `all`."
            )

        uid = str(user.id)

        entry = self.data["users"].get(uid, {})

        entry["scope"] = scope

        if scope == "single":
            entry["guild_id"] = ctx.guild.id
        else:
            entry.pop("guild_id", None)

        self.data["users"][uid] = entry
        self._save_data()

        self.next_ping_at.pop(uid, None)

        await ctx.send(
            f"✅ {user} scope set to `{scope}`."
        )

    @commands.command(name="listpings")
    async def cmd_list_pings(
        self,
        ctx: commands.Context
    ):
        """
        List configured users.
        """
        lines: list[str] = []

        for uid, entry in self.data.get(
            "users",
            {}
        ).items():
            try:
                user = self.bot.get_user(int(uid))

                if user is None:
                    user = await self.bot.fetch_user(
                        int(uid)
                    )

                name = str(user)

            except discord.HTTPException:
                name = f"Unknown({uid})"

            running = entry.get("running", False)
            interval = self._safe_interval(
                entry.get("interval", 300)
            )
            scope = entry.get("scope", "single")
            guild_id = entry.get("guild_id")

            monitoring = self._has_block_monitor(
                int(uid)
            )

            lines.append(
                f"{name} ({uid}) | "
                f"running={running} | "
                f"interval={interval}s | "
                f"scope={scope} | "
                f"guild_id={guild_id} | "
                f"monitoring={monitoring}"
            )

        if not lines:
            return await ctx.send(
                "No pings configured."
            )

        # Discord's message limit is 2000 chars.
        chunks: list[str] = []
        current = ""

        for line in lines:
            candidate = (
                f"{current}\n{line}"
                if current
                else line
            )

            if len(candidate) > 1850:
                chunks.append(current)
                current = line
            else:
                current = candidate

        if current:
            chunks.append(current)

        for chunk in chunks:
            await ctx.send(
                f"```text\n{chunk}\n```"
            )

    @commands.command(name="pingall")
    async def cmd_ping_all(
        self,
        ctx: commands.Context
    ):
        """
        Immediately attempt one ping for every running configured user.
        """
        sent = 0

        for uid, entry in list(
            self.data.get("users", {}).items()
        ):
            if not entry.get("running", False):
                continue

            try:
                success = await self._perform_ping(
                    uid,
                    entry
                )

                if success:
                    sent += 1

            except Exception as e:
                print(
                    f"[AutoRandomPinger] Immediate ping "
                    f"failure for {uid}: {e}"
                )

        await ctx.send(
            f"✅ Successfully pinged {sent} configured user(s)."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(
        AutoRandomPinger(bot)
    )