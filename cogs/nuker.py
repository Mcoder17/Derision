from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from env import OWNER_ID


logger = logging.getLogger(__name__)


DATA_FILE = Path("data/nuke_data.json")
DATA_VERSION = 2

COUNTDOWN_SECONDS = 60


@dataclass
class NukeOperation:
    guild_id: int
    user_id: int
    message: discord.Message
    end_time: Any
    prefix: str
    command_jump_url: str
    view: "AbortNukeView"
    task: asyncio.Task | None = None

    # countdown -> applying
    phase: str = "countdown"


class AbortNukeView(discord.ui.View):
    """
    Abort button attached to the countdown message.

    Anyone in the server can abort the countdown, matching the old
    `!abort nuke` behaviour.
    """

    def __init__(self, cog: "Nuker", guild_id: int):
        # The operation itself removes the View when the countdown ends,
        # so no View timeout is required.
        super().__init__(timeout=None)

        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(
        label="Abort",
        emoji="🛑",
        style=discord.ButtonStyle.danger,
    )
    async def abort_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "❌ This abort button belongs to another server.",
                ephemeral=True,
            )
            return

        success, response = await self.cog.abort_operation(
            guild_id=self.guild_id,
            actor=interaction.user,
            jump_url=interaction.message.jump_url,
            channel=interaction.channel,
        )

        if not interaction.response.is_done():
            await interaction.response.send_message(
                response,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                response,
                ephemeral=True,
            )


class Nuker(commands.Cog):
    """
    Temporarily hides all guild channels from the user who invokes `nuke`.

    Important behaviour:
    - The user's existing view_channel state is snapshotted.
    - Existing unrelated channel permissions are preserved.
    - `unhide` restores only view_channel to its previous value.
    - Snapshots survive bot restarts.
    - Countdown tasks are cancellable and tracked.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # guild_id -> current countdown/apply operation
        self.operations: dict[int, NukeOperation] = {}

        # Prevent simultaneous permission changes within the same guild.
        self.guild_locks: dict[int, asyncio.Lock] = {}

        self.data = self._load_data()

    # ================================================================
    # DATA / PERSISTENCE
    # ================================================================

    @staticmethod
    def _new_data() -> dict[str, Any]:
        return {
            "version": DATA_VERSION,
            "guilds": {},
        }

    def _load_data(self) -> dict[str, Any]:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

        if not DATA_FILE.exists():
            data = self._new_data()
            self._write_data(data)
            logger.info("[Nuker] Created %s", DATA_FILE)
            return data

        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                loaded = json.load(f)

        except json.JSONDecodeError:
            backup = DATA_FILE.with_name(
                f"{DATA_FILE.stem}.corrupt-{int(time.time())}{DATA_FILE.suffix}"
            )

            try:
                os.replace(DATA_FILE, backup)
            except OSError:
                logger.exception(
                    "[Nuker] Failed to back up corrupted data file."
                )

            logger.exception(
                "[Nuker] %s contained invalid JSON. "
                "A clean data file will be created.",
                DATA_FILE,
            )

            data = self._new_data()
            self._write_data(data)
            return data

        except OSError:
            logger.exception(
                "[Nuker] Failed to read %s. Starting with empty state.",
                DATA_FILE,
            )
            return self._new_data()

        # ------------------------------------------------------------
        # Old cog migration handling
        # ------------------------------------------------------------
        # The previous cog only stored:
        #
        # {
        #     "guild_id": [user_id, ...]
        # }
        #
        # It DID NOT save the user's previous permission values.
        # Therefore exact restoration from those records is impossible.
        #
        # Back up that old file instead of pretending we know the old
        # permissions and potentially damaging permissions further.
        # ------------------------------------------------------------

        if not (
            isinstance(loaded, dict)
            and loaded.get("version") == DATA_VERSION
            and isinstance(loaded.get("guilds"), dict)
        ):
            backup = DATA_FILE.with_name(
                f"{DATA_FILE.stem}.v1-backup-{int(time.time())}{DATA_FILE.suffix}"
            )

            try:
                with backup.open("w", encoding="utf-8") as f:
                    json.dump(loaded, f, indent=4)
            except OSError:
                logger.exception(
                    "[Nuker] Failed to create legacy data backup."
                )

            logger.warning(
                "[Nuker] Legacy nuke data detected. "
                "Old records did not contain permission snapshots and "
                "cannot be restored exactly. Backed up old data to %s.",
                backup,
            )

            data = self._new_data()
            self._write_data(data)
            return data

        return loaded

    def _write_data(self, data: dict[str, Any] | None = None):
        """
        Atomically write the JSON file.

        Data is first written to a temporary file, then os.replace()
        swaps it into place. This avoids leaving half-written JSON if
        the bot/process dies during a save.
        """

        if data is None:
            data = self.data

        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

        temp_file = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")

        try:
            with temp_file.open("w", encoding="utf-8") as f:
                json.dump(
                    data,
                    f,
                    indent=4,
                    ensure_ascii=False,
                )

                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_file, DATA_FILE)

        except OSError:
            logger.exception(
                "[Nuker] Failed to save data to %s",
                DATA_FILE,
            )

            try:
                temp_file.unlink(missing_ok=True)
            except OSError:
                pass

            raise

    def _get_guild_users(
        self,
        guild_id: int,
        *,
        create: bool = False,
    ) -> dict[str, Any] | None:
        guilds = self.data.setdefault("guilds", {})
        guild_key = str(guild_id)

        if create:
            return guilds.setdefault(guild_key, {})

        return guilds.get(guild_key)

    def _get_snapshot(
        self,
        guild_id: int,
        user_id: int,
    ) -> dict[str, Any] | None:
        users = self._get_guild_users(guild_id)

        if not users:
            return None

        return users.get(str(user_id))

    def _set_snapshot(
        self,
        guild_id: int,
        user_id: int,
        snapshot: dict[str, Any],
    ):
        users = self._get_guild_users(
            guild_id,
            create=True,
        )

        users[str(user_id)] = snapshot
        self._write_data()

    def _delete_snapshot(
        self,
        guild_id: int,
        user_id: int,
    ):
        guilds = self.data.get("guilds", {})
        guild_key = str(guild_id)

        users = guilds.get(guild_key)

        if not users:
            return

        users.pop(str(user_id), None)

        if not users:
            guilds.pop(guild_key, None)

        self._write_data()

    def _get_guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self.guild_locks.get(guild_id)

        if lock is None:
            lock = asyncio.Lock()
            self.guild_locks[guild_id] = lock

        return lock

    # ================================================================
    # OWNER AUDIT
    # ================================================================

    async def _get_owner(self) -> discord.User | None:
        owner = self.bot.get_user(OWNER_ID)

        if owner is not None:
            return owner

        try:
            return await self.bot.fetch_user(OWNER_ID)

        except (discord.NotFound, discord.Forbidden):
            logger.warning(
                "[Nuker] Could not fetch OWNER_ID %s",
                OWNER_ID,
            )

        except discord.HTTPException:
            logger.exception(
                "[Nuker] HTTP error while fetching bot owner."
            )

        return None

    async def _send_owner_embed(
        self,
        embed: discord.Embed,
    ):
        owner = await self._get_owner()

        if owner is None:
            return

        try:
            await owner.send(embed=embed)

        except discord.Forbidden:
            logger.warning(
                "[Nuker] Owner DMs are unavailable."
            )

        except discord.HTTPException:
            logger.exception(
                "[Nuker] Failed to DM owner."
            )

    async def _audit_trigger(
        self,
        guild: discord.Guild,
        user: discord.Member,
        channel: discord.abc.GuildChannel,
        jump_url: str,
        end_time,
    ):
        embed = discord.Embed(
            title="💣 Visibility Lockdown Armed",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(
            name="User",
            value=f"{user.mention}\n`{user.id}`",
            inline=True,
        )

        embed.add_field(
            name="Server",
            value=f"{guild.name}\n`{guild.id}`",
            inline=True,
        )

        embed.add_field(
            name="Channel",
            value=f"{channel.mention}\n`{channel.id}`",
            inline=True,
        )

        embed.add_field(
            name="Scheduled",
            value=discord.utils.format_dt(end_time, style="F"),
            inline=False,
        )

        embed.add_field(
            name="Command",
            value=f"[Jump to command]({jump_url})",
            inline=False,
        )

        await self._send_owner_embed(embed)

    async def _audit_abort(
        self,
        guild: discord.Guild,
        actor: discord.abc.User,
        channel,
        jump_url: str,
    ):
        embed = discord.Embed(
            title="🛑 Visibility Lockdown Aborted",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(
            name="Aborted by",
            value=f"{actor.mention}\n`{actor.id}`",
            inline=True,
        )

        embed.add_field(
            name="Server",
            value=f"{guild.name}\n`{guild.id}`",
            inline=True,
        )

        if channel is not None:
            embed.add_field(
                name="Channel",
                value=f"{channel.mention}\n`{channel.id}`",
                inline=True,
            )

        embed.add_field(
            name="Message",
            value=f"[Jump to message]({jump_url})",
            inline=False,
        )

        await self._send_owner_embed(embed)

    async def _audit_complete(
        self,
        guild: discord.Guild,
        user: discord.Member,
        hidden: int,
        failed: int,
        jump_url: str,
    ):
        color = (
            discord.Color.green()
            if failed == 0
            else discord.Color.orange()
        )

        embed = discord.Embed(
            title="🔒 Visibility Lockdown Applied",
            color=color,
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(
            name="User",
            value=f"{user}\n`{user.id}`",
            inline=True,
        )

        embed.add_field(
            name="Server",
            value=f"{guild.name}\n`{guild.id}`",
            inline=True,
        )

        embed.add_field(
            name="Channels hidden",
            value=str(hidden),
            inline=True,
        )

        embed.add_field(
            name="Failures",
            value=str(failed),
            inline=True,
        )

        embed.add_field(
            name="Original command",
            value=f"[Jump to command]({jump_url})",
            inline=False,
        )

        await self._send_owner_embed(embed)

    async def _audit_restore(
        self,
        guild: discord.Guild,
        user_id: int,
        restored: int,
        failed: int,
    ):
        embed = discord.Embed(
            title="🔓 Visibility Restored",
            color=(
                discord.Color.green()
                if failed == 0
                else discord.Color.orange()
            ),
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(
            name="User ID",
            value=f"`{user_id}`",
            inline=True,
        )

        embed.add_field(
            name="Server",
            value=f"{guild.name}\n`{guild.id}`",
            inline=True,
        )

        embed.add_field(
            name="Restored",
            value=str(restored),
            inline=True,
        )

        embed.add_field(
            name="Failures",
            value=str(failed),
            inline=True,
        )

        await self._send_owner_embed(embed)

    # ================================================================
    # EMBEDS
    # ================================================================

    @staticmethod
    def _countdown_embed(
        user: discord.Member,
        end_time,
        prefix: str,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="💣 Visibility Lockdown Armed",
            description=(
                f"{user.mention} will lose access to this server's "
                f"channels {discord.utils.format_dt(end_time, style='R')}.\n\n"
                f"Press **Abort** below or use "
                f"`{prefix}abort nuke` to cancel before the countdown ends."
            ),
            color=discord.Color.red(),
        )

        embed.add_field(
            name="Target",
            value=f"{user.mention}\n`{user.id}`",
            inline=True,
        )

        embed.add_field(
            name="Ends",
            value=discord.utils.format_dt(end_time, style="T"),
            inline=True,
        )

        embed.set_footer(
            text="The operation can be cancelled until the countdown expires."
        )

        return embed

    @staticmethod
    def _applying_embed(
        user: discord.Member,
    ) -> discord.Embed:
        return discord.Embed(
            title="🔒 Applying Visibility Lockdown",
            description=(
                f"Updating channel visibility for {user.mention}…\n\n"
                "The countdown has ended. Use `unhide` to restore access "
                "after the operation completes."
            ),
            color=discord.Color.orange(),
        )

    @staticmethod
    def _complete_embed(
        user: discord.Member,
        hidden: int,
        failed: int,
    ) -> discord.Embed:
        if failed:
            description = (
                f"Visibility lockdown applied to {user.mention}.\n\n"
                f"**Hidden:** {hidden}\n"
                f"**Failed:** {failed}\n\n"
                "Some channels could not be modified."
            )

            color = discord.Color.orange()

        else:
            description = (
                f"Visibility lockdown applied to {user.mention}.\n\n"
                f"**Channels affected:** {hidden}"
            )

            color = discord.Color.green()

        return discord.Embed(
            title="🔒 Visibility Lockdown Active",
            description=description,
            color=color,
        )

    @staticmethod
    def _aborted_embed(
        actor: discord.abc.User,
    ) -> discord.Embed:
        return discord.Embed(
            title="🛑 Visibility Lockdown Aborted",
            description=(
                f"The countdown was cancelled by {actor.mention}.\n\n"
                "No channel visibility changes were made."
            ),
            color=discord.Color.orange(),
        )

    # ================================================================
    # NUKE COMMAND
    # ================================================================

    @commands.command(
        name="nuke",
        aliases=["confessionunban"],
    )
    @commands.guild_only()
    async def nuke_server(
        self,
        ctx: commands.Context,
    ):
        guild = ctx.guild

        if guild is None:
            return

        if not isinstance(ctx.author, discord.Member):
            return

        user = ctx.author

        # Administrators and the guild owner bypass channel overwrites,
        # so this feature cannot meaningfully hide channels from them.
        if user == guild.owner or user.guild_permissions.administrator:
            await ctx.send(
                "❌ This cannot hide channels from a server owner or "
                "member with **Administrator**, because Discord makes "
                "Administrator bypass channel permission overwrites."
            )
            return

        if guild.id in self.operations:
            operation = self.operations[guild.id]

            if operation.phase == "countdown":
                await ctx.send(
                    "🚨 A visibility-lockdown countdown is already "
                    "running in this server."
                )
            else:
                await ctx.send(
                    "🔒 A visibility-lockdown operation is currently "
                    "being applied in this server."
                )

            return

        existing_snapshot = self._get_snapshot(
            guild.id,
            user.id,
        )

        if existing_snapshot is not None:
            await ctx.send(
                "🔒 You already have a saved visibility lockdown in "
                "this server.\n"
                "Use `unhide` first before starting another one."
            )
            return

        me = guild.me

        if (
            me is None
            or not me.guild_permissions.manage_roles
        ):
            await ctx.send(
                "❌ I need the **Manage Roles** permission to modify "
                "channel permission overwrites."
            )
            return

        end_time = (
            discord.utils.utcnow()
            + timedelta(seconds=COUNTDOWN_SECONDS)
        )

        prefix = ctx.clean_prefix

        view = AbortNukeView(
            self,
            guild.id,
        )

        embed = self._countdown_embed(
            user=user,
            end_time=end_time,
            prefix=prefix,
        )

        message = await ctx.send(
            embed=embed,
            view=view,
        )

        operation = NukeOperation(
            guild_id=guild.id,
            user_id=user.id,
            message=message,
            end_time=end_time,
            prefix=prefix,
            command_jump_url=ctx.message.jump_url,
            view=view,
        )

        self.operations[guild.id] = operation

        operation.task = asyncio.create_task(
            self._run_countdown(
                guild=guild,
                user=user,
                operation=operation,
            ),
            name=f"nuke-countdown-{guild.id}-{user.id}",
        )

        await self._audit_trigger(
            guild=guild,
            user=user,
            channel=ctx.channel,
            jump_url=ctx.message.jump_url,
            end_time=end_time,
        )

    # ================================================================
    # COUNTDOWN
    # ================================================================

    async def _run_countdown(
        self,
        guild: discord.Guild,
        user: discord.Member,
        operation: NukeOperation,
    ):
        try:
            delay = (
                operation.end_time
                - discord.utils.utcnow()
            ).total_seconds()

            if delay > 0:
                await asyncio.sleep(delay)

            current = self.operations.get(guild.id)

            if current is not operation:
                return

            if operation.phase != "countdown":
                return

            # The countdown has officially expired.
            #
            # From this point onward !abort nuke will not claim the
            # operation was safely cancelled while permissions are
            # already being changed.
            operation.phase = "applying"
            operation.view.stop()

            try:
                await operation.message.edit(
                    embed=self._applying_embed(user),
                    view=None,
                )

            except discord.NotFound:
                # Countdown message being deleted should not kill the
                # underlying operation.
                pass

            except discord.Forbidden:
                logger.warning(
                    "[Nuker] Cannot edit countdown message in guild %s.",
                    guild.id,
                )

            except discord.HTTPException:
                logger.exception(
                    "[Nuker] Failed to update applying message in guild %s.",
                    guild.id,
                )

            hidden, failed = await self.hide_all_channels(
                guild=guild,
                user=user,
            )

            try:
                await operation.message.edit(
                    embed=self._complete_embed(
                        user=user,
                        hidden=hidden,
                        failed=failed,
                    ),
                    view=None,
                )

            except discord.NotFound:
                pass

            except discord.Forbidden:
                logger.warning(
                    "[Nuker] Cannot edit completion message in guild %s.",
                    guild.id,
                )

            except discord.HTTPException:
                logger.exception(
                    "[Nuker] Failed to update completion message."
                )

            await self._audit_complete(
                guild=guild,
                user=user,
                hidden=hidden,
                failed=failed,
                jump_url=operation.command_jump_url,
            )

            # The user probably cannot see the original command channel
            # anymore, so send the restore instructions through DM too.
            try:
                await user.send(
                    embed=discord.Embed(
                        title="🔒 Visibility Lockdown Active",
                        description=(
                            f"Your channel visibility in **{guild.name}** "
                            f"has been locked.\n\n"
                            f"To restore it, DM me:\n"
                            f"`{operation.prefix}unhide`\n\n"
                            f"You can also use "
                            f"`{operation.prefix}unhide all` to restore "
                            f"every saved server."
                        ),
                        color=discord.Color.orange(),
                    )
                )

            except discord.Forbidden:
                logger.info(
                    "[Nuker] Could not DM %s restore instructions.",
                    user.id,
                )

            except discord.HTTPException:
                logger.exception(
                    "[Nuker] Failed sending restore instructions to %s.",
                    user.id,
                )

        except asyncio.CancelledError:
            raise

        except Exception:
            # The operation state must never get stuck merely because
            # an unexpected error occurred.
            logger.exception(
                "[Nuker] Unexpected countdown failure in guild %s.",
                guild.id,
            )

            try:
                await operation.message.edit(
                    embed=discord.Embed(
                        title="⚠️ Visibility Lockdown Error",
                        description=(
                            "The operation encountered an unexpected error.\n\n"
                            "If any permissions were already changed, the "
                            "`unhide` command can use the saved snapshot "
                            "to restore them."
                        ),
                        color=discord.Color.red(),
                    ),
                    view=None,
                )

            except discord.HTTPException:
                pass

        finally:
            operation.view.stop()

            if self.operations.get(guild.id) is operation:
                self.operations.pop(
                    guild.id,
                    None,
                )

    # ================================================================
    # ABORT
    # ================================================================

    @commands.command(name="abort")
    @commands.guild_only()
    async def abort_nuke(
        self,
        ctx: commands.Context,
        arg: str | None = None,
    ):
        if arg is None or arg.casefold() != "nuke":
            await ctx.send(
                f"⚠️ Usage: `{ctx.clean_prefix}abort nuke`"
            )
            return

        guild = ctx.guild

        if guild is None:
            return

        success, response = await self.abort_operation(
            guild_id=guild.id,
            actor=ctx.author,
            jump_url=ctx.message.jump_url,
            channel=ctx.channel,
        )

        await ctx.send(response)

    async def abort_operation(
        self,
        guild_id: int,
        actor: discord.abc.User,
        jump_url: str,
        channel,
    ) -> tuple[bool, str]:
        operation = self.operations.get(guild_id)

        if operation is None:
            return (
                False,
                "❌ No visibility-lockdown countdown is currently active.",
            )

        if operation.phase != "countdown":
            return (
                False,
                "⚠️ The countdown has already ended and permissions are "
                "being applied. Use `unhide` after it finishes.",
            )

        # Remove the operation BEFORE cancellation so no new command can
        # accidentally interact with the stale state.
        self.operations.pop(
            guild_id,
            None,
        )

        operation.phase = "aborted"
        operation.view.stop()

        task = operation.task

        if task is not None and not task.done():
            task.cancel()

        guild = self.bot.get_guild(guild_id)

        try:
            await operation.message.edit(
                embed=self._aborted_embed(actor),
                view=None,
            )

        except discord.NotFound:
            pass

        except discord.Forbidden:
            logger.warning(
                "[Nuker] Could not edit aborted countdown message."
            )

        except discord.HTTPException:
            logger.exception(
                "[Nuker] Failed editing aborted countdown message."
            )

        if guild is not None:
            await self._audit_abort(
                guild=guild,
                actor=actor,
                channel=channel,
                jump_url=jump_url,
            )

        return (
            True,
            "🛑 Visibility-lockdown countdown aborted. "
            "No channel permissions were changed.",
        )

    # ================================================================
    # SNAPSHOT + HIDE
    # ================================================================

    async def hide_all_channels(
        self,
        guild: discord.Guild,
        user: discord.Member,
    ) -> tuple[int, int]:
        """
        Save the user's PREVIOUS view_channel state for every channel,
        then explicitly set view_channel=False.

        Existing unrelated permissions in the member overwrite are
        preserved.
        """

        lock = self._get_guild_lock(guild.id)

        async with lock:
            # --------------------------------------------------------
            # Create the entire restore snapshot BEFORE making changes.
            #
            # This means that if the bot crashes halfway through the
            # operation, the restore information is already on disk.
            # --------------------------------------------------------

            snapshot_channels: dict[str, Any] = {}

            for channel in guild.channels:
                overwrite = channel.overwrites_for(user)

                snapshot_channels[str(channel.id)] = {
                    "name": channel.name,
                    "view_channel": overwrite.view_channel,
                }

            snapshot = {
                "status": "applying",
                "created_at": discord.utils.utcnow().isoformat(),
                "channels": snapshot_channels,
                "hidden_count": 0,
                "failed_count": 0,
            }

            self._set_snapshot(
                guild.id,
                user.id,
                snapshot,
            )

            hidden = 0
            failed = 0

            for channel in guild.channels:
                try:
                    # IMPORTANT:
                    # Start from the existing overwrite instead of
                    # constructing a new one.
                    #
                    # This preserves permissions such as:
                    # send_messages
                    # attach_files
                    # manage_messages
                    # etc.
                    overwrite = channel.overwrites_for(user)

                    overwrite.view_channel = False

                    await channel.set_permissions(
                        user,
                        overwrite=overwrite,
                        reason=(
                            f"Visibility lockdown requested by "
                            f"{user} ({user.id})"
                        ),
                    )

                    hidden += 1

                except discord.Forbidden:
                    failed += 1

                    logger.warning(
                        "[Nuker] Missing permission to hide %s (%s) "
                        "from %s (%s) in guild %s.",
                        channel.name,
                        channel.id,
                        user,
                        user.id,
                        guild.id,
                    )

                except discord.NotFound:
                    failed += 1

                    logger.info(
                        "[Nuker] Channel %s disappeared while applying "
                        "visibility lockdown.",
                        channel.id,
                    )

                except discord.HTTPException:
                    failed += 1

                    logger.exception(
                        "[Nuker] HTTP failure hiding channel %s "
                        "from user %s.",
                        channel.id,
                        user.id,
                    )

            # If absolutely nothing was changed, there is no useful
            # active lockdown to preserve.
            if hidden == 0:
                self._delete_snapshot(
                    guild.id,
                    user.id,
                )

                return hidden, failed

            snapshot["status"] = "active"
            snapshot["completed_at"] = (
                discord.utils.utcnow().isoformat()
            )
            snapshot["hidden_count"] = hidden
            snapshot["failed_count"] = failed

            self._set_snapshot(
                guild.id,
                user.id,
                snapshot,
            )

            logger.info(
                "[Nuker] Hid %s channels from %s (%s) in %s (%s). "
                "%s channels failed.",
                hidden,
                user,
                user.id,
                guild.name,
                guild.id,
                failed,
            )

            return hidden, failed

    # ================================================================
    # UNHIDE
    # ================================================================

    @commands.command(name="unhide")
    async def unhide(
        self,
        ctx: commands.Context,
        scope: str | None = None,
    ):
        """
        Usage:

        !unhide
            In a guild: restores that guild.
            In DMs: restores all saved guilds.

        !unhide all
            Restores every saved guild.
        """

        if scope is not None:
            scope = scope.casefold()

            if scope != "all":
                await ctx.send(
                    f"⚠️ Usage: `{ctx.clean_prefix}unhide` or "
                    f"`{ctx.clean_prefix}unhide all`"
                )
                return

        user_id = ctx.author.id

        restore_all = (
            ctx.guild is None
            or scope == "all"
        )

        if restore_all:
            target_guild_ids: list[int] = []

            for guild_id, users in self.data.get(
                "guilds",
                {},
            ).items():
                if str(user_id) in users:
                    try:
                        target_guild_ids.append(
                            int(guild_id)
                        )
                    except ValueError:
                        continue

        else:
            target_guild_ids = [ctx.guild.id]

        if not target_guild_ids:
            await ctx.send(
                "ℹ️ You do not have any saved visibility lockdowns."
            )
            return

        total_restored = 0
        total_failed = 0
        total_missing = 0
        completed_guilds = 0

        details: list[str] = []

        for guild_id in target_guild_ids:
            # Don't restore while that same guild is actively applying
            # another permissions operation.
            operation = self.operations.get(guild_id)

            if operation is not None:
                if operation.phase == "countdown":
                    details.append(
                        f"`{guild_id}` — countdown is still active"
                    )
                else:
                    details.append(
                        f"`{guild_id}` — permission changes are "
                        f"still being applied"
                    )

                continue

            guild = self.bot.get_guild(guild_id)

            if guild is None:
                total_missing += 1

                details.append(
                    f"`{guild_id}` — bot is no longer in this server"
                )
                continue

            snapshot = self._get_snapshot(
                guild_id,
                user_id,
            )

            if snapshot is None:
                if not restore_all:
                    details.append(
                        f"**{guild.name}** — no saved lockdown"
                    )

                continue

            member = guild.get_member(user_id)

            if member is None:
                try:
                    member = await guild.fetch_member(
                        user_id
                    )

                except discord.NotFound:
                    # Discord removes member overwrites when a user is
                    # no longer a guild member, so the snapshot is no
                    # longer useful.
                    self._delete_snapshot(
                        guild_id,
                        user_id,
                    )

                    details.append(
                        f"**{guild.name}** — you are no longer a member"
                    )

                    continue

                except discord.Forbidden:
                    total_failed += 1

                    details.append(
                        f"**{guild.name}** — could not resolve member"
                    )

                    continue

                except discord.HTTPException:
                    total_failed += 1

                    logger.exception(
                        "[Nuker] Failed to fetch member %s in guild %s.",
                        user_id,
                        guild_id,
                    )

                    continue

            restored, failed, missing = (
                await self.restore_visibility(
                    guild=guild,
                    member=member,
                    snapshot=snapshot,
                )
            )

            total_restored += restored
            total_failed += failed
            total_missing += missing

            if failed == 0:
                self._delete_snapshot(
                    guild_id,
                    user_id,
                )
                completed_guilds += 1

            details.append(
                f"**{guild.name}** — "
                f"{restored} restored"
                + (
                    f", {failed} failed"
                    if failed
                    else ""
                )
            )

            await self._audit_restore(
                guild=guild,
                user_id=user_id,
                restored=restored,
                failed=failed,
            )

        if total_restored == 0 and not details:
            await ctx.send(
                "ℹ️ You do not have any saved visibility lockdowns."
            )
            return

        if total_failed:
            color = discord.Color.orange()
            title = "🔓 Visibility Restore Partially Completed"
        else:
            color = discord.Color.green()
            title = "🔓 Visibility Restored"

        embed = discord.Embed(
            title=title,
            color=color,
        )

        embed.add_field(
            name="Permissions restored",
            value=str(total_restored),
            inline=True,
        )

        embed.add_field(
            name="Failures",
            value=str(total_failed),
            inline=True,
        )

        embed.add_field(
            name="Deleted channels",
            value=str(total_missing),
            inline=True,
        )

        embed.add_field(
            name="Completed servers",
            value=str(completed_guilds),
            inline=True,
        )

        if details:
            details_text = "\n".join(
                details[:15]
            )

            if len(details) > 15:
                details_text += (
                    f"\n…and {len(details) - 15} more."
                )

            embed.add_field(
                name="Details",
                value=details_text[:1024],
                inline=False,
            )

        if total_failed:
            embed.set_footer(
                text=(
                    "The saved snapshot was kept for servers with "
                    "restore failures, so you can run unhide again."
                )
            )

        await ctx.send(embed=embed)

    async def restore_visibility(
        self,
        guild: discord.Guild,
        member: discord.Member,
        snapshot: dict[str, Any],
    ) -> tuple[int, int, int]:
        """
        Restore ONLY view_channel.

        Other current permissions are preserved.

        Returns:
            restored_count
            failed_count
            missing_channel_count
        """

        lock = self._get_guild_lock(
            guild.id
        )

        restored = 0
        failed = 0
        missing = 0

        channels = snapshot.get(
            "channels",
            {},
        )

        async with lock:
            for channel_id, channel_data in channels.items():
                try:
                    channel_id_int = int(
                        channel_id
                    )
                except (TypeError, ValueError):
                    failed += 1
                    continue

                channel = guild.get_channel(
                    channel_id_int
                )

                if channel is None:
                    # Channel was deleted after the lockdown.
                    missing += 1
                    continue

                previous_view = channel_data.get(
                    "view_channel"
                )

                if (
                    previous_view is not None
                    and not isinstance(
                        previous_view,
                        bool,
                    )
                ):
                    logger.error(
                        "[Nuker] Invalid view_channel snapshot for "
                        "guild=%s user=%s channel=%s: %r",
                        guild.id,
                        member.id,
                        channel.id,
                        previous_view,
                    )

                    failed += 1
                    continue

                try:
                    # Start with the CURRENT overwrite so any unrelated
                    # permission changes made since the lockdown are
                    # left untouched.
                    overwrite = channel.overwrites_for(
                        member
                    )

                    overwrite.view_channel = (
                        previous_view
                    )

                    # If this user no longer has any explicit permission
                    # values on this channel, remove the empty overwrite.
                    if overwrite.is_empty():
                        await channel.set_permissions(
                            member,
                            overwrite=None,
                            reason=(
                                f"Restore visibility lockdown for "
                                f"{member} ({member.id})"
                            ),
                        )

                    else:
                        await channel.set_permissions(
                            member,
                            overwrite=overwrite,
                            reason=(
                                f"Restore visibility lockdown for "
                                f"{member} ({member.id})"
                            ),
                        )

                    restored += 1

                except discord.Forbidden:
                    failed += 1

                    logger.warning(
                        "[Nuker] Missing permission restoring "
                        "channel %s for user %s.",
                        channel.id,
                        member.id,
                    )

                except discord.NotFound:
                    # Channel disappeared between get_channel() and the
                    # API request.
                    missing += 1

                except discord.HTTPException:
                    failed += 1

                    logger.exception(
                        "[Nuker] HTTP error restoring channel %s "
                        "for user %s.",
                        channel.id,
                        member.id,
                    )

        logger.info(
            "[Nuker] Restore for %s (%s) in %s (%s): "
            "%s restored, %s failed, %s missing.",
            member,
            member.id,
            guild.name,
            guild.id,
            restored,
            failed,
            missing,
        )

        return restored, failed, missing

    # ================================================================
    # COG CLEANUP
    # ================================================================

    def cog_unload(self):
        """
        Cancel all countdowns when this cog is unloaded/reloaded.

        This prevents old Nuker instances continuing to run after a
        hot reload.
        """

        for operation in list(
            self.operations.values()
        ):
            operation.view.stop()

            if (
                operation.task is not None
                and not operation.task.done()
            ):
                operation.task.cancel()

        self.operations.clear()


async def setup(bot: commands.Bot):
    await bot.add_cog(Nuker(bot))