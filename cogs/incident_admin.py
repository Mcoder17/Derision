import discord
from discord.ext import commands

from env import OWNER_ID
from utils.public_stats import (
    create_incident,
    list_incidents,
    set_public_state,
    status_label_to_state,
    update_incident,
)

def owner_only():
    async def predicate(ctx: commands.Context) -> bool:
        return ctx.author.id == OWNER_ID
    return commands.check(predicate)


class IncidentAdmin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.group(name="incident", invoke_without_command=True)
    @owner_only()
    async def incident(self, ctx: commands.Context):
        await ctx.send(
            "Use: `!incident create title | severity | status | body` or "
            "`!incident update id | status | body` or "
            "`!incident resolve id | body` or "
            "`!incident list`"
        )

    @incident.command(name="create")
    @owner_only()
    async def incident_create(self, ctx: commands.Context, *, raw: str):
        parts = [part.strip() for part in raw.split("|", 3)]
        if len(parts) != 4:
            await ctx.send("Format: `!incident create title | severity | status | body`")
            return

        title, severity, status, body = parts
        incident_id = create_incident(title, body, severity, status)
        set_public_state(status_label_to_state(status), title)
        await ctx.send(f"Created incident #{incident_id}.")

    @incident.command(name="update")
    @owner_only()
    async def incident_update(self, ctx: commands.Context, *, raw: str):
        parts = [part.strip() for part in raw.split("|", 2)]
        if len(parts) != 3:
            await ctx.send("Format: `!incident update id | status | body`")
            return

        incident_id = int(parts[0])
        status = parts[1]
        body = parts[2]
        update_incident(incident_id, status=status, body=body)
        set_public_state(status_label_to_state(status), body)
        await ctx.send(f"Updated incident #{incident_id}.")

    @incident.command(name="resolve")
    @owner_only()
    async def incident_resolve(self, ctx: commands.Context, *, raw: str):
        parts = [part.strip() for part in raw.split("|", 1)]
        if not parts[0]:
            await ctx.send("Format: `!incident resolve id | body`")
            return

        incident_id = int(parts[0])
        body = parts[1] if len(parts) > 1 else "Resolved."
        update_incident(incident_id, status="Resolved", body=body)
        set_public_state("healthy", "")
        await ctx.send(f"Resolved incident #{incident_id}.")

    @incident.command(name="list")
    @owner_only()
    async def incident_list(self, ctx: commands.Context):
        incidents = list_incidents(limit=5)
        if not incidents:
            await ctx.send("No incidents yet.")
            return

        embed = discord.Embed(title="Recent incidents", color=discord.Color.blurple())
        for item in incidents:
            embed.add_field(
                name=f"#{item['id']} • {item['title']}",
                value=(
                    f"Severity: {item['severity']}\n"
                    f"Status: {item['status']}\n"
                    f"Body: {item['body'][:300]}"
                ),
                inline=False,
            )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(IncidentAdmin(bot))