import discord
from discord.ext import commands
import asyncio
import json
import os
from datetime import datetime, timedelta

from env import OWNER_ID


class Nuker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data_file = "data/nuke_data.json"
        self.countdowns = {}  # guild_id: {"message": msg, "end_time": datetime}
        self.load_data()

    # ---------- JSON HANDLING ----------
    def load_data(self):
        if not os.path.exists(self.data_file):
            with open(self.data_file, "w") as f:
                json.dump({}, f)
            print("[Nuker] Created new nuke_data.json")
        with open(self.data_file, "r") as f:
            self.data = json.load(f)

    def save_data(self):
        with open(self.data_file, "w") as f:
            json.dump(self.data, f, indent=4)

    # ---------- NUKE COMMAND ----------
    @commands.command(name="nuke", aliases=["confessionunban"])
    @commands.guild_only()
    async def nuke_server(self, ctx):
        guild = ctx.guild
        user = ctx.author

        if guild.id in self.countdowns:
            await ctx.send("🚨 A process is already running here!")
            return

        # initialize countdown
        end_time = datetime.utcnow() + timedelta(seconds=10)
        msg = await ctx.send(
            f"💣 **Server-wide action initialized by {user.mention}!**\n"
            f"Server will be nuked in **1 minute** unless someone types `!abort nuke`.\n"
            f"⏱️ Time remaining: **1:00**"
        )

        # store data
        self.countdowns[guild.id] = {"message": msg, "end_time": end_time}
        self.data.setdefault(str(guild.id), [])
        if user.id not in self.data[str(guild.id)]:
            self.data[str(guild.id)].append(user.id)
            self.save_data()

        # --- DM Owner about command trigger ---
        try:
            owner = await self.bot.fetch_user(OWNER_ID)
            message_link = f"https://discord.com/channels/{guild.id}/{ctx.channel.id}/{ctx.message.id}"
            embed = discord.Embed(
                title="🚨 Nuke Command Initiated",
                description=(
                    f"**User:** {user} (`{user.id}`)\n"
                    f"**Server:** {guild.name} (`{guild.id}`)\n"
                    f"**Channel:** {ctx.channel.mention} (`{ctx.channel.id}`)\n"
                    f"**Message Link:** [Jump to Command]({message_link})\n"
                    f"**Time:** {discord.utils.format_dt(datetime.utcnow(), style='F')}"
                ),
                color=discord.Color.red()
            )
            await owner.send(embed=embed)
        except Exception as e:
            print(f"[Nuker] Failed to DM owner (trigger): {e}")

        # start countdown updater
        self.bot.loop.create_task(self._countdown_task(ctx, guild, user, msg, end_time))

    # ---------- COUNTDOWN HANDLER ----------
    async def _countdown_task(self, ctx, guild, user, msg, end_time):
        while datetime.utcnow() < end_time:
            remaining = int((end_time - datetime.utcnow()).total_seconds())
            minutes, seconds = divmod(remaining, 60)
            if guild.id not in self.countdowns:
                return  # aborted

            await msg.edit(
                content=(
                    f"💣 **Nuke activated by {user.mention}!**\n"
                    f"Server will be nuked in **{minutes:02d}:{seconds:02d}**.\n"
                    f"Type `!abort nuke` to cancel!"
                )
            )
            await asyncio.sleep(30)  # update every 30 seconds

        if guild.id in self.countdowns:
            await self.hide_all_channels(guild, user)
            del self.countdowns[guild.id]
            await msg.edit(content=f"💥 **Server nuking completed for {user.mention}!**")

            # --- DM Owner: Countdown Complete ---
            try:
                owner = await self.bot.fetch_user(OWNER_ID)
                embed = discord.Embed(
                    title="✅ Countdown Complete",
                    description=(
                        f"**Server:** {guild.name} (`{guild.id}`)\n"
                        f"**Triggered by:** {user} (`{user.id}`)\n"
                        f"**Status:** Completed\n"
                        f"**Time:** {discord.utils.format_dt(datetime.utcnow(), style='F')}"
                    ),
                    color=discord.Color.green()
                )
                await owner.send(embed=embed)
            except Exception as e:
                print(f"[Nuker] Failed to DM owner (complete): {e}")

    # ---------- ABORT COMMAND ----------
    @commands.command(name="abort")
    @commands.guild_only()
    async def abort_nuke(self, ctx, arg=None):
        if arg != "nuke":
            await ctx.send("⚠️ Usage: `!abort nuke` to stop the process.")
            return

        guild = ctx.guild
        user = ctx.author

        if guild.id not in self.countdowns:
            await ctx.send("❌ No process is currently active!")
            return

        del self.countdowns[guild.id]
        await ctx.send(f"🛑 **Operation aborted by {user.mention}.** Server is safe (for now).")

        # --- DM Owner: Abort ---
        try:
            owner = await self.bot.fetch_user(OWNER_ID)
            message_link = f"https://discord.com/channels/{guild.id}/{ctx.channel.id}/{ctx.message.id}"
            embed = discord.Embed(
                title="⛔ Countdown Aborted",
                description=(
                    f"**User:** {user} (`{user.id}`)\n"
                    f"**Server:** {guild.name} (`{guild.id}`)\n"
                    f"**Channel:** {ctx.channel.mention} (`{ctx.channel.id}`)\n"
                    f"**Message Link:** [Jump to Message]({message_link})\n"
                    f"**Time:** {discord.utils.format_dt(datetime.utcnow(), style='F')}"
                ),
                color=discord.Color.orange()
            )
            await owner.send(embed=embed)
        except Exception as e:
            print(f"[Nuker] Failed to DM owner (abort): {e}")

    # ---------- UNHIDE COMMAND ----------
    @commands.command(name="unhide")
    async def unhide(self, ctx):
        user = ctx.author
        restored_total = 0

        for guild in self.bot.guilds:
            if str(guild.id) in self.data and user.id in self.data[str(guild.id)]:
                for channel in guild.channels:
                    try:
                        await channel.set_permissions(user, overwrite=None)
                        restored_total += 1
                    except Exception as e:
                        print(f"[Nuker] Error unhiding {channel.name}: {e}")
                self.data[str(guild.id)].remove(user.id)
                if not self.data[str(guild.id)]:
                    del self.data[str(guild.id)]
                self.save_data()

        await ctx.send(f"🔓 Restored visibility for {user.mention} ({restored_total} channels).")

    # ---------- HIDE ALL CHANNELS ----------
    async def hide_all_channels(self, guild, user):
        hidden_count = 0
        for channel in guild.channels:
            try:
                await channel.set_permissions(user, view_channel=False)
                hidden_count += 1
            except Exception as e:
                print(f"[Nuker] Error hiding {channel.name}: {e}")
        print(f"[Nuker] Hid {hidden_count} channels for {user} in {guild.name}")


async def setup(bot):
    await bot.add_cog(Nuker(bot))