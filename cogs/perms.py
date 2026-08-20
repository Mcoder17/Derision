import discord
from discord.ext import commands
from env import OWNER_ID 

class Perms(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def is_owner(self, user_id):
        return user_id == OWNER_ID

    @commands.command(name="perms")
    async def perms(self, ctx):
        # Owner check
        if not self.is_owner(ctx.author.id):
            return await ctx.send("❌ You are not authorized to use this command.")

        if ctx.guild is not None:
            return await ctx.send("❌ Use this command in DMs.")

        # Loop through servers safely
        for guild in self.bot.guilds:
            bot_member = guild.me

            if bot_member is None:
                perms_list = "❌ Could not fetch member info."
            else:
                perms = bot_member.guild_permissions
                perms_list = "\n".join(
                    f"• {name.replace('_', ' ').title()}"
                    for name, value in perms if value
                )

            embed = discord.Embed(
                title=f"🌐 {guild.name}",
                description=perms_list[:4000] or "No permissions?",
                color=discord.Color.blue()
            )

            try:
                await ctx.send(embed=embed)
            except discord.HTTPException:
                await ctx.send(f"⚠️ Couldn't send perms for {guild.name} (too large).")


async def setup(bot):
    await bot.add_cog(Perms(bot))