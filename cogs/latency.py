import discord
from discord.ext import commands
import time

class Ping(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="ping")
    async def ping(self, ctx):
        """Check bot latency"""
        
        start = time.perf_counter()
        message = await ctx.send("Pinging...")
        end = time.perf_counter()

        round_trip = (end - start) * 1000  # ms
        ws_latency = self.bot.latency * 1000  # ms

        # Decide color based on latency
        avg_latency = (round_trip + ws_latency) / 2

        if avg_latency < 100:
            color = discord.Color.green()
            status = "🟢 Excellent"
        elif avg_latency < 200:
            color = discord.Color.yellow()
            status = "🟡 Decent"
        else:
            color = discord.Color.red()
            status = "🔴 Poor"

        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"Status: {status}",
            color=color
        )

        embed.add_field(
            name="WebSocket Latency",
            value=f"{ws_latency:.2f} ms",
            inline=False
        )
        embed.add_field(
            name="Round-trip Latency",
            value=f"{round_trip:.2f} ms",
            inline=False
        )

        await message.edit(content=None, embed=embed)

async def setup(bot):
    await bot.add_cog(Ping(bot))