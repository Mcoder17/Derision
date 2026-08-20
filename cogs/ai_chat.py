import asyncio
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from cogs.ai.memory import clear_history, clear_all_history
from cogs.ai.notes import clear_notes, clear_all_notes
from cogs.ai.ai_handler import chat_with_ai
from cogs.ai.blacklist import (
    is_blacklisted,
    add_to_blacklist,
    remove_from_blacklist,
    get_blacklist,
)
from env import OWNER_ID


class NotOwner(app_commands.CheckFailure):
    pass


class Blacklisted(app_commands.CheckFailure):
    pass


def owner_only():
    async def predicate(interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            raise NotOwner()
        return True
    return app_commands.check(predicate)


def not_blacklisted():
    async def predicate(interaction: discord.Interaction):
        if is_blacklisted(interaction.user.id):
            raise Blacklisted()
        return True
    return app_commands.check(predicate)


def _split(text, size=2000):
    return [text[i:i + size] for i in range(0, len(text), size)] or [text]


class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, Blacklisted):
            message = "You're blacklisted."
        elif isinstance(error, NotOwner):
            message = "Only the owner can use this."
        else:
            print("SLASH ERROR:", error)
            traceback.print_exc()
            message = "Something went wrong."

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not self.bot.user:
            return
        if is_blacklisted(message.author.id):
            return
        if self.bot.user not in message.mentions:
            return

        cleaned = message.content
        cleaned = cleaned.replace(f"<@{self.bot.user.id}>", "")
        cleaned = cleaned.replace(f"<@!{self.bot.user.id}>", "")
        cleaned = cleaned.strip()
        if not cleaned:
            return

        try:
            async with message.channel.typing():
                reply = await asyncio.to_thread(
                    chat_with_ai,
                    message.author.id,
                    cleaned,
                    message.author.display_name,
                )

            chunks = _split(reply)
            await message.reply(chunks[0])
            for chunk in chunks[1:]:
                await message.channel.send(chunk)

        except Exception as e:
            print("MENTION ERROR:", e)
            traceback.print_exc()
            await message.reply(
                "Something went wrong. Contact the developer if this keeps happening."
            )

    @app_commands.command(name="chat", description="Chat with AI")
    @not_blacklisted()
    async def chat(self, interaction: discord.Interaction, message: str):
        await interaction.response.defer()
        reply = await asyncio.to_thread(
            chat_with_ai,
            interaction.user.id,
            message,
            interaction.user.display_name,
        )
        for chunk in _split(reply):
            await interaction.followup.send(chunk)

    @app_commands.command(name="resetmemory", description="Clear your AI conversation memory")
    @not_blacklisted()
    async def resetmemory(self, interaction: discord.Interaction):
        clear_history(interaction.user.id)
        await interaction.response.send_message("Your memory cleared.")

    @app_commands.command(name="resetallmemory", description="Clear ALL AI memory")
    @owner_only()
    async def resetallmemory(self, interaction: discord.Interaction):
        clear_all_history()
        await interaction.response.send_message("All memory wiped.")

    @app_commands.command(name="resetnotes", description="Clear stored notes about you")
    @not_blacklisted()
    async def resetnotes(self, interaction: discord.Interaction):
        clear_notes(interaction.user.id)
        await interaction.response.send_message("Your notes cleared.")

    @app_commands.command(name="blacklist_add", description="Blacklist a user")
    @owner_only()
    async def blacklist_add(self, interaction: discord.Interaction, user: discord.User):
        add_to_blacklist(user.id)
        await interaction.response.send_message(f"{user.mention} has been blacklisted.")

    @app_commands.command(name="blacklist_remove", description="Remove user from blacklist")
    @owner_only()
    async def blacklist_remove(self, interaction: discord.Interaction, user: discord.User):
        remove_from_blacklist(user.id)
        await interaction.response.send_message(f"{user.mention} removed from blacklist.")

    @app_commands.command(name="blacklist_list", description="View blacklisted users")
    @owner_only()
    async def blacklist_list(self, interaction: discord.Interaction):
        users = get_blacklist()
        if not users:
            await interaction.response.send_message("Blacklist is empty.")
            return
        formatted = "\n".join(f"<@{uid}>" for uid in users)
        await interaction.response.send_message(f"Blacklisted users:\n{formatted}")

    @app_commands.command(name="resetallnotes", description="Clear ALL stored notes")
    @owner_only()
    async def resetallnotes(self, interaction: discord.Interaction):
        clear_all_notes()
        await interaction.response.send_message("All notes wiped.")


async def setup(bot):
    await bot.add_cog(AIChat(bot))