import discord
from discord.ext import commands
import random
import json
import os
from env import OWNER_ID

EXCLUDE_FILE = "data/picker_exclusions.json"


def load_exclusions():
    if not os.path.exists(EXCLUDE_FILE):
        return {}
    with open(EXCLUDE_FILE, "r") as f:
        return json.load(f)


def save_exclusions(data):
    with open(EXCLUDE_FILE, "w") as f:
        json.dump(data, f, indent=2)


class RandomPicker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.exclusions = load_exclusions()
        self.processed_messages = set()

    def get_guild_exclusions(self, guild_id):
        return set(self.exclusions.get(str(guild_id), []))

    def add_exclusion(self, guild_id, user_id):
        gid = str(guild_id)
        self.exclusions.setdefault(gid, [])
        if user_id not in self.exclusions[gid]:
            self.exclusions[gid].append(user_id)
            save_exclusions(self.exclusions)

    def remove_exclusion(self, guild_id, user_id):
        gid = str(guild_id)
        if gid in self.exclusions and user_id in self.exclusions[gid]:
            self.exclusions[gid].remove(user_id)
            save_exclusions(self.exclusions)

    def is_owner(self, ctx):
        return ctx.author.id == OWNER_ID

    @commands.command(name="pick")
    async def pick(self, ctx):
        if ctx.message.id in self.processed_messages:
            return

        self.processed_messages.add(ctx.message.id)

        excluded = self.get_guild_exclusions(ctx.guild.id)

        members = [
            m for m in ctx.channel.members
            if not m.bot and m.id not in excluded
        ]

        if not members:
            await ctx.send("No eligible users found.")
            return

        chosen = random.choice(members)
        await ctx.send(f"🎯 **Chosen:** {chosen.mention}")

    @commands.command(name="exclude")
    async def exclude(self, ctx, member: discord.Member):
        if not self.is_owner(ctx):
            return

        self.add_exclusion(ctx.guild.id, member.id)
        await ctx.send(f"🚫 Excluded **{member}** from future picks.")

    @commands.command(name="include")
    async def include(self, ctx, member: discord.Member):
        if not self.is_owner(ctx):
            return

        self.remove_exclusion(ctx.guild.id, member.id)
        await ctx.send(f"✅ Included **{member}** back into picks.")

    @commands.command(name="excluded")
    async def excluded(self, ctx):
        if not self.is_owner(ctx):
            return

        excluded = self.get_guild_exclusions(ctx.guild.id)

        if not excluded:
            await ctx.send("No users are excluded.")
            return

        names = []
        for uid in excluded:
            member = ctx.guild.get_member(uid)
            names.append(member.name if member else f"Unknown ({uid})")

        await ctx.send("🚫 **Excluded users:**\n" + ", ".join(names))


async def setup(bot):
    await bot.add_cog(RandomPicker(bot))
