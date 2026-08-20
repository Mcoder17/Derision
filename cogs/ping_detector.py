import discord
from discord.ext import commands

from env import OWNER_ID


class GhostPingDetector(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def notify_owner(self, embed: discord.Embed):
        user = self.bot.get_user(OWNER_ID)
        if user is None:
            try:
                user = await self.bot.fetch_user(OWNER_ID)
            except Exception:
                return
        try:
            await user.send(embed=embed)
        except discord.Forbidden:
            print("[GhostPing] Couldn't DM the owner (DMs closed).")

    # ---------- DELETED MESSAGE DETECTION ----------
    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author.bot:
            return

        # Check for personal ping or @everyone/@here
        mentioned_you = any(u.id == OWNER_ID for u in message.mentions)
        pinged_everyone = message.mention_everyone

        if mentioned_you or pinged_everyone:
            embed = discord.Embed(
                title="Ghost Ping Detected (Deleted Message)",
                color=discord.Color.red()
            )

            server_name = message.guild.name if message.guild else "DM"
            channel_name = f"#{message.channel}" if message.guild else "DM"
            ping_type = (
                "@everyone/@here"
                if pinged_everyone and not mentioned_you
                else ("You & @everyone/@here" if pinged_everyone else "You")
            )

            embed.add_field(name="Server", value=server_name, inline=False)
            embed.add_field(name="Channel", value=channel_name, inline=False)
            embed.add_field(name="User", value=f"{message.author} ({message.author.mention})", inline=False)
            embed.add_field(name="Ping Type", value=ping_type, inline=False)
            embed.add_field(name="Message Content", value=message.content or "*No text content*", inline=False)

            await self.notify_owner(embed)

    # ---------- EDITED MESSAGE DETECTION ----------
    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if before.author.bot:
            return

        # Check if you or everyone was pinged before but not after
        you_before = any(u.id == OWNER_ID for u in before.mentions)
        you_after = any(u.id == OWNER_ID for u in after.mentions)

        everyone_before = before.mention_everyone
        everyone_after = after.mention_everyone

        removed_you = you_before and not you_after
        removed_everyone = everyone_before and not everyone_after

        if removed_you or removed_everyone:
            embed = discord.Embed(
                title="Ghost Ping Detected (Edited Message)",
                color=discord.Color.orange()
            )

            server_name = before.guild.name if before.guild else "DM"
            channel_name = f"#{before.channel}" if before.guild else "DM"
            ping_type = (
                "@everyone/@here"
                if removed_everyone and not removed_you
                else ("You & @everyone/@here" if removed_you and removed_everyone else "You")
            )

            embed.add_field(name="Server", value=server_name, inline=False)
            embed.add_field(name="Channel", value=channel_name, inline=False)
            embed.add_field(name="User", value=f"{before.author} ({before.author.mention})", inline=False)
            embed.add_field(name="Ping Type", value=ping_type, inline=False)
            embed.add_field(name="Original Message", value=before.content or "*No text content*", inline=False)
            embed.add_field(name="Edited To", value=after.content or "*No text content*", inline=False)

            await self.notify_owner(embed)


async def setup(bot):
    await bot.add_cog(GhostPingDetector(bot))