import asyncio
import json
import os

import discord
from discord.ext import commands

from env import OWNER_ID

DATA_FILE = "data/clones.json"


class UserClone(commands.Cog):
    """Mirror a target member's nickname and roles onto one or more clones.

    Roles that can't be handed out directly -- managed integration/booster
    roles, or roles positioned above the bot in the hierarchy -- are reproduced
    as assignable copies that match the original's appearance (name, colour,
    hoist, mentionable) but carry NO permissions, and the clone receives those.
    """

    def __init__(self, bot):
        self.bot = bot
        self.data = self.load_data()
        self.clones = self.data["clones"]            # {gid: {target_id: [clone_id, ...]}}
        self.role_copies = self.data["role_copies"]  # {gid: {original_role_id: copy_role_id}}
        self._locks = {}                             # clone_id -> asyncio.Lock
        self._synced_once = False

    # ----------------------------------------------------------------- data --

    def load_data(self):
        if not os.path.exists(DATA_FILE):
            return {"clones": {}, "role_copies": {}}
        with open(DATA_FILE, "r") as f:
            raw = json.load(f)
        if "clones" not in raw and "role_copies" not in raw:
            raw = {"clones": raw, "role_copies": {}}
        raw.setdefault("clones", {})
        raw.setdefault("role_copies", {})
        return raw

    def save_data(self):
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with open(DATA_FILE, "w") as f:
            json.dump(self.data, f, indent=4)

    def _lock(self, clone_id):
        lock = self._locks.get(clone_id)
        if lock is None:
            lock = self._locks[clone_id] = asyncio.Lock()
        return lock

    # ---------------------------------------------------------- role copies --

    async def _match_role(self, copy, original):
        """Keep an existing copy visually identical to the role it mirrors."""
        fields = {}
        if copy.name != original.name:
            fields["name"] = original.name
        if copy.colour != original.colour:
            fields["colour"] = original.colour
        if copy.hoist != original.hoist:
            fields["hoist"] = original.hoist
        if copy.mentionable != original.mentionable:
            fields["mentionable"] = original.mentionable
        if fields:
            try:
                await copy.edit(reason="Sync copy role with original", **fields)
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def get_or_create_copy(self, guild, role):
        """Return an assignable copy of `role`, creating one if needed.

        Copies are keyed by the original role id per guild, so multiple clones
        (and multiple targets) sharing the same unassignable role reuse a single
        copy instead of spawning duplicates.
        """
        gid = str(guild.id)
        copies = self.role_copies.setdefault(gid, {})
        rid = str(role.id)

        copy_id = copies.get(rid)
        if copy_id:
            existing = guild.get_role(int(copy_id))
            if existing:
                await self._match_role(existing, role)
                return existing
            copies.pop(rid, None)  # stale mapping -> fall through and recreate

        try:
            new_role = await guild.create_role(
                name=role.name,
                colour=role.colour,
                hoist=role.hoist,
                mentionable=role.mentionable,
                # appearance only -- copies deliberately carry no permissions
                reason=f"Assignable copy of unassignable role '{role.name}'",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            print("Copy role creation failed:", e)
            return None

        # best-effort: place the copy just under the bot's top role
        try:
            pos = max(1, min(role.position, guild.me.top_role.position - 1))
            await new_role.edit(position=pos)
        except (discord.Forbidden, discord.HTTPException):
            pass

        copies[rid] = str(new_role.id)
        self.save_data()
        return new_role

    # ----------------------------------------------------------------- sync --

    async def sync_member(self, target: discord.Member, clone: discord.Member):
        guild = target.guild
        me = guild.me

        async with self._lock(clone.id):
            try:
                # ---- desired roles (copy anything not directly assignable)
                desired = set()
                for role in target.roles:
                    if role == guild.default_role:
                        continue
                    if not role.managed and role < me.top_role:
                        desired.add(role)
                    else:
                        copy = await self.get_or_create_copy(guild, role)
                        if copy and copy < me.top_role:
                            desired.add(copy)

                current = {r for r in clone.roles if r != guild.default_role}

                # ---- apply nick + roles in a single edit where possible
                edit_kwargs = {}
                if clone.nick != target.nick:
                    edit_kwargs["nick"] = target.nick
                if current != desired:
                    edit_kwargs["roles"] = desired
                if edit_kwargs:
                    await clone.edit(reason="Clone sync", **edit_kwargs)

            except discord.Forbidden:
                print(f"Missing perms / hierarchy blocks clone in: {guild.name}")
            except Exception as e:
                print("Clone error:", e)

    async def _resync_guild(self, guild):
        count = 0
        for target_id, clone_ids in self.clones.get(str(guild.id), {}).items():
            target = guild.get_member(int(target_id))
            if not target:
                continue
            for clone_id in clone_ids:
                clone = guild.get_member(int(clone_id))
                if clone:
                    await self.sync_member(target, clone)
                    count += 1
        return count

    # -------------------------------------------------------------- checks --

    async def cog_check(self, ctx):
        # every command in this cog is owner-only
        return ctx.author.id == OWNER_ID

    # -------------------------------------------------------------- command --

    @commands.guild_only()
    @commands.group(invoke_without_command=True)
    async def clone(self, ctx):
        await ctx.send(
            "Clone Commands:\n"
            "`clone add @target @clone`\n"
            "`clone remove @target @clone`\n"
            "`clone list`\n"
            "`clone clear`\n"
            "`clone cleanup`\n"
            "`clone resync`\n"
            "`clone force @target`"
        )

    @clone.command()
    async def add(self, ctx, target: discord.Member, clone: discord.Member):
        targets = self.clones.setdefault(str(ctx.guild.id), {})
        clist = targets.setdefault(str(target.id), [])
        if clone.id not in clist:
            clist.append(clone.id)
            self.save_data()
        await self.sync_member(target, clone)
        await ctx.send(f"{clone.mention} now mirrors {target.mention}")

    @clone.command()
    async def remove(self, ctx, target: discord.Member, clone: discord.Member):
        targets = self.clones.get(str(ctx.guild.id), {})
        clist = targets.get(str(target.id))
        if clist and clone.id in clist:
            clist.remove(clone.id)
            if not clist:
                del targets[str(target.id)]
            self.save_data()
            await ctx.send("Clone removed.")
        else:
            await ctx.send("Clone pair not found.")

    @clone.command()
    async def list(self, ctx):
        targets = self.clones.get(str(ctx.guild.id))
        if not targets:
            await ctx.send("No clones configured.")
            return
        lines = []
        for target_id, clone_ids in targets.items():
            target = ctx.guild.get_member(int(target_id))
            t = target.mention if target else f"`{target_id}`"
            for clone_id in clone_ids:
                clone = ctx.guild.get_member(int(clone_id))
                c = clone.mention if clone else f"`{clone_id}`"
                lines.append(f"{t} ➜ {c}")
        await ctx.send("**Clone Pairs:**\n" + "\n".join(lines))

    @clone.command()
    async def clear(self, ctx):
        self.clones.pop(str(ctx.guild.id), None)
        self.save_data()
        await ctx.send(
            "All clones cleared. Copy roles left intact — use "
            "`clone cleanup` to delete them."
        )

    @clone.command()
    async def cleanup(self, ctx):
        """Delete every copy role this cog created in the guild."""
        copies = self.role_copies.get(str(ctx.guild.id), {})
        deleted = 0
        for copy_id in list(copies.values()):
            role = ctx.guild.get_role(int(copy_id))
            if role:
                try:
                    await role.delete(reason="Clone copy cleanup")
                    deleted += 1
                except (discord.Forbidden, discord.HTTPException):
                    pass
        self.role_copies.pop(str(ctx.guild.id), None)
        self.save_data()
        await ctx.send(f"Deleted {deleted} copy role(s).")

    @clone.command()
    async def resync(self, ctx):
        if not self.clones.get(str(ctx.guild.id)):
            await ctx.send("No clones.")
            return
        count = await self._resync_guild(ctx.guild)
        await ctx.send(f"Resynced {count} clones.")

    @clone.command()
    async def force(self, ctx, target: discord.Member):
        clone_ids = self.clones.get(str(ctx.guild.id), {}).get(str(target.id))
        if not clone_ids:
            await ctx.send("No clone for that user.")
            return
        for clone_id in clone_ids:
            clone = ctx.guild.get_member(int(clone_id))
            if clone:
                await self.sync_member(target, clone)
        await ctx.send("Forced resync complete.")

    # ------------------------------------------------------------ listeners --

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        targets = self.clones.get(str(after.guild.id))
        if not targets:
            return

        # updated member is a target -> push changes to its clones
        if str(after.id) in targets:
            if before.roles != after.roles or before.nick != after.nick:
                for clone_id in targets[str(after.id)]:
                    clone = after.guild.get_member(int(clone_id))
                    if clone:
                        await self.sync_member(after, clone)

        # updated member is a clone -> re-pull from its target (drift protection)
        for target_id, clone_ids in targets.items():
            if after.id in clone_ids:
                target = after.guild.get_member(int(target_id))
                if target:
                    await self.sync_member(target, after)

    @commands.Cog.listener()
    async def on_ready(self):
        if self._synced_once:  # on_ready can fire on every reconnect
            return
        self._synced_once = True
        for gid in list(self.clones.keys()):
            guild = self.bot.get_guild(int(gid))
            if guild:
                await self._resync_guild(guild)


async def setup(bot):
    await bot.add_cog(UserClone(bot))