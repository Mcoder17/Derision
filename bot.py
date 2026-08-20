import asyncio
import os
import threading

import discord
import uvicorn
from discord.ext import commands, tasks

from env import SERVER_PORT, TOKEN
from utils.public_stats import (
    app as api_app,
    get_public_state,
    init_db,
    mark_startup,
    record_interaction,
    record_ping,
    sample_availability,
    set_public_state,
)

# --- Discord Intents ---
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True

# --- Bot Setup ---
bot = commands.Bot(command_prefix=["!", "d", "D"], intents=intents)

_background_started = False
_disconnect_watchdog: asyncio.Task | None = None


def start_api_server() -> None:
    def run() -> None:
        uvicorn.run(
            api_app,
            host="0.0.0.0",
            port=SERVER_PORT,
            log_level="info",
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    print(f"[Web] FastAPI listening on 0.0.0.0:{SERVER_PORT}")


async def load_all_cogs():
    cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
    if not os.path.isdir(cogs_dir):
        print(f"[Boot] ❌ Cogs directory not found: {cogs_dir}")
        return

    # Load regular .py cogs
    for filename in os.listdir(cogs_dir):
        path = os.path.join(cogs_dir, filename)

        if os.path.isfile(path) and filename.endswith(".py") and not filename.startswith("_"):
            module_name = f"cogs.{filename[:-3]}"
            try:
                await bot.load_extension(module_name)
                print(f"[Boot] ✅ Loaded cog: {module_name}")
            except Exception as e:
                print(f"[Boot] ⚠️ Failed to load {module_name}: {type(e).__name__}: {e}")

    # Load only approved package-style cogs
    package_cogs = [""]  # add more here only if needed

    for package_name in package_cogs:
        package_path = os.path.join(cogs_dir, package_name)
        init_path = os.path.join(package_path, "__init__.py")

        if os.path.isdir(package_path) and os.path.isfile(init_path):
            module_name = f"cogs.{package_name}"
            try:
                await bot.load_extension(module_name)
                print(f"[Boot] ✅ Loaded cog: {module_name}")
            except Exception as e:
                print(f"[Boot] ⚠️ Failed to load {module_name}: {type(e).__name__}: {e}")


@tasks.loop(minutes=5)
async def ping_sampler():
    if bot.is_ready():
        record_ping(max(bot.latency * 1000, 0.0))


@tasks.loop(minutes=5)
async def availability_sampler():
    sample_availability()


@ping_sampler.before_loop
async def before_ping_sampler():
    await bot.wait_until_ready()


@availability_sampler.before_loop
async def before_availability_sampler():
    await bot.wait_until_ready()


async def _offline_watchdog():
    await asyncio.sleep(60)
    if not bot.is_closed() and not bot.is_ready():
        set_public_state("offline", "Disconnected from Discord")


@bot.event
async def on_ready():
    global _background_started, _disconnect_watchdog

    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print("Guilds:", ", ".join(g.name for g in bot.guilds))
    print("Bot is now ready and listening for commands")

    if _disconnect_watchdog and not _disconnect_watchdog.done():
        _disconnect_watchdog.cancel()
        _disconnect_watchdog = None

    current = get_public_state()
    if current["public_state"] == "offline":
        set_public_state("healthy", "")

    if not _background_started:
        ping_sampler.start()
        availability_sampler.start()
        sample_availability()
        _background_started = True

    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"[Boot] ⚠️ Slash command sync failed: {type(e).__name__}: {e}")


@bot.event
async def on_resumed():
    global _disconnect_watchdog
    if _disconnect_watchdog and not _disconnect_watchdog.done():
        _disconnect_watchdog.cancel()
        _disconnect_watchdog = None

    current = get_public_state()
    if current["public_state"] == "offline":
        set_public_state("healthy", "")


@bot.event
async def on_disconnect():
    global _disconnect_watchdog
    if _disconnect_watchdog and not _disconnect_watchdog.done():
        _disconnect_watchdog.cancel()
    _disconnect_watchdog = asyncio.create_task(_offline_watchdog())


@bot.event
async def on_command_completion(ctx):
    record_interaction()


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type in (
        discord.InteractionType.application_command,
        discord.InteractionType.component,
        discord.InteractionType.modal_submit,
    ):
        record_interaction()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if bot.user and bot.user.mentioned_in(message):
        record_interaction()

    await bot.process_commands(message)


async def main():
    if not TOKEN:
        raise RuntimeError("Missing TOKEN in environment (.env or OS environment).")

    init_db()
    mark_startup()
    start_api_server()

    async with bot:
        await load_all_cogs()
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot shutdown via KeyboardInterrupt")
    except Exception as e:
        print(f"Fatal Error: {type(e).__name__}: {e}")