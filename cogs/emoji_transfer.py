import discord
from discord.ext import commands
import aiohttp
import asyncio
import re
import logging
from io import BytesIO
from typing import Optional

log = logging.getLogger(__name__)

EMOJI_REGEX = re.compile(r"<a?:(\w+):(\d+)>")
NAME_CLEAN_REGEX = re.compile(r"\W+")

MAX_EMOJI_BYTES = 256 * 1024
MAX_STICKER_BYTES = 512 * 1024
CREATE_DELAY = 1.5


def has_expression_perm(perms: discord.Permissions) -> bool:
    """Works across discord.py versions (manage_expressions is newer,
    manage_emojis_and_stickers is the deprecated alias)."""
    val = getattr(perms, "manage_expressions", None)
    if val is None:
        val = getattr(perms, "manage_emojis_and_stickers", False)
    return bool(val)


class EmojiTransfer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MaxConcurrencyReached):
            await ctx.send(
                "A transfer is already running for this server — "
                "let it finish before starting another."
            )
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                f"Missing argument: `{error.param.name}`. See `!emoji` for usage."
            )
        else:
            raise error

    # ---------- helpers ----------

    async def download(self, url, limit=MAX_EMOJI_BYTES):
        """Returns (data, None) on success or (None, reason) on failure."""
        try:
            async with self.session.get(url) as r:
                if r.status != 200:
                    return None, f"HTTP {r.status}"
                data = await r.read()
        except aiohttp.ClientError as e:
            return None, str(e)
        if limit and len(data) > limit:
            return None, f"too large ({len(data) // 1024} KB > {limit // 1024} KB)"
        return data, None

    def sanitize_name(self, name: str) -> str:
        """Discord emoji names must be 2-32 chars, alphanumeric + underscore."""
        name = NAME_CLEAN_REGEX.sub("_", name or "").strip("_")
        if not name:
            name = "emoji"
        if len(name) < 2:
            name = f"{name}_e"
        return name[:32]

    def resolve_name(self, used: set, name: str) -> str:
        """Rename on collision (name -> name_1 -> name_2 ...), keeping <=32 chars."""
        name = self.sanitize_name(name)
        if name not in used:
            return name
        i = 1
        while True:
            suffix = f"_{i}"
            candidate = f"{name[:32 - len(suffix)]}{suffix}"
            if candidate not in used:
                return candidate
            i += 1

    def slot_counts(self, guild):
        static = sum(1 for e in guild.emojis if not e.animated)
        animated = sum(1 for e in guild.emojis if e.animated)
        return static, animated

    def parse_emoji_input(self, arg):
        arg = str(arg)
        if arg.isdigit():
            return int(arg)
        m = EMOJI_REGEX.match(arg)
        if m:
            return int(m.group(2))
        return None

    async def resolve_target(self, ctx, target_id):
        target = self.bot.get_guild(target_id) if target_id else ctx.guild
        if not target:
            return None, "Target server not found (is the bot in it?)."
        return target, None

    async def check_perms(self, ctx, target):
        """Verify BOTH the bot and the invoking user can manage expressions."""
        me = target.me
        if me is None or not has_expression_perm(me.guild_permissions):
            return f"I'm missing **Manage Expressions** in **{target.name}**."

        member = target.get_member(ctx.author.id)
        if member is None: 
            try:
                member = await target.fetch_member(ctx.author.id)
            except discord.HTTPException:
                member = None
        if member is None:
            return f"You don't appear to be a member of **{target.name}**."
        if not has_expression_perm(member.guild_permissions):
            return f"You need **Manage Expressions** in **{target.name}**."
        return None

    async def fetch_reference(self, ctx):
        ref = ctx.message.reference
        if not ref:
            return None
        if ref.resolved and isinstance(ref.resolved, discord.Message):
            return ref.resolved
        if ref.message_id is None:
            return None
        try:
            return await ctx.channel.fetch_message(ref.message_id)
        except discord.HTTPException:
            return None

    async def _transfer_emojis(self, ctx, target, emojis):
        """Shared bulk-emoji worker for sendall / send / clone.
        Policy: always copy, renaming on name collisions."""
        limit = target.emoji_limit
        static_used, animated_used = self.slot_counts(target)
        used_names = {e.name for e in target.emojis}

        added = skipped = errors = 0
        last_error = None
        total = len(emojis)
        progress = await ctx.send(
            f"Starting transfer of {total} emoji(s) to **{target.name}**..."
        )

        for i, emoji in enumerate(emojis, start=1):
            if emoji.animated and animated_used >= limit:
                skipped += 1
                continue
            if not emoji.animated and static_used >= limit:
                skipped += 1
                continue

            data, derr = await self.download(str(emoji.url))
            if data is None:
                errors += 1
                last_error = derr
                continue

            name = self.resolve_name(used_names, emoji.name)
            try:
                await target.create_custom_emoji(
                    name=name, image=data, reason=f"Transferred by {ctx.author}"
                )
                added += 1
                used_names.add(name)
                if emoji.animated:
                    animated_used += 1
                else:
                    static_used += 1
                await asyncio.sleep(CREATE_DELAY)
            except discord.HTTPException as e:
                errors += 1
                last_error = str(e)
                log.warning("Emoji create failed (%s): %s", name, e)

            if i % 5 == 0 or i == total:
                await progress.edit(
                    content=(
                        f"{i}/{total} | added {added} | "
                        f"skipped {skipped} | errors {errors}"
                    )
                )

        summary = (
            f"**Done** → {target.name}\n"
            f"Added: {added}\n"
            f"Skipped (slots full): {skipped}\n"
            f"Errors: {errors}"
        )
        if last_error:
            summary += f"\nLast error: `{last_error[:150]}`"
        await progress.edit(content=summary)

    # ---------- command group ----------

    @commands.guild_only()
    @commands.group(name="emoji", invoke_without_command=True)
    async def emoji(self, ctx):
        await ctx.send(
            "**Emoji / sticker tools** (TARGET defaults to this server):\n"
            "`!emoji info [TARGET]` — show slot usage\n"
            "`!emoji sendall [TARGET]` — copy all emojis from here\n"
            "`!emoji send [TARGET] <ids/emojis>` — copy selected emojis\n"
            "`!emoji clone <SOURCE_ID> [TARGET]` — copy all emojis from another server\n"
            "`!emoji fromurl [TARGET] <url> [name]` — add an emoji from a link\n"
            "`!emoji add [TARGET] [name]` — reply to an image\n"
            "`!emoji delete [TARGET] <name/emoji>` — remove an emoji\n"
            "`!emoji sticker [TARGET] [name]` — reply to a sticker/image\n"
            "`!emoji stickersall [TARGET]` — copy all stickers from here"
        )

    @emoji.command()
    async def info(self, ctx, target_id: Optional[int] = None):
        target, err = await self.resolve_target(ctx, target_id)
        if err:
            return await ctx.send(err)
        static_used, animated_used = self.slot_counts(target)
        limit = target.emoji_limit
        await ctx.send(
            f"**{target.name}** expression slots:\n"
            f"Static emojis: {static_used}/{limit}\n"
            f"Animated emojis: {animated_used}/{limit}\n"
            f"Stickers: {len(target.stickers)}/{target.sticker_limit}"
        )

    @emoji.command()
    @commands.max_concurrency(1, commands.BucketType.guild, wait=False)
    async def sendall(self, ctx, target_id: Optional[int] = None):
        target, err = await self.resolve_target(ctx, target_id)
        if err:
            return await ctx.send(err)
        perm_err = await self.check_perms(ctx, target)
        if perm_err:
            return await ctx.send(perm_err)

        emojis = list(ctx.guild.emojis)
        if not emojis:
            return await ctx.send("This server has no emojis to copy.")
        await self._transfer_emojis(ctx, target, emojis)

    @emoji.command()
    @commands.max_concurrency(1, commands.BucketType.guild, wait=False)
    async def send(self, ctx, *args):
        if not args:
            return await ctx.send(
                "Give me emoji IDs or custom emojis, e.g. `!emoji send :blob: 123456789`.\n"
                "Optionally start with a target server ID. "
                "For a plain image, reply to it with `!emoji add`."
            )

        target = ctx.guild
        items = list(args)

        if items[0].isdigit():
            maybe = self.bot.get_guild(int(items[0]))
            if maybe:
                target = maybe
                items = items[1:]

        perm_err = await self.check_perms(ctx, target)
        if perm_err:
            return await ctx.send(perm_err)

        ids = [eid for eid in (self.parse_emoji_input(x) for x in items) if eid]
        if not ids:
            return await ctx.send("No valid emoji IDs or custom emojis found.")

        emojis, missing = [], 0
        for eid in ids:
            e = self.bot.get_emoji(eid)
            if e:
                emojis.append(e)
            else:
                missing += 1

        if not emojis:
            return await ctx.send(
                "I couldn't access any of those emojis "
                "(I need to share a server with them)."
            )

        await self._transfer_emojis(ctx, target, emojis)
        if missing:
            await ctx.send(f"Note: {missing} emoji ID(s) couldn't be resolved.")

    @emoji.command()
    @commands.max_concurrency(1, commands.BucketType.guild, wait=False)
    async def clone(self, ctx, source_id: int, target_id: Optional[int] = None):
        source = self.bot.get_guild(source_id)
        if not source:
            return await ctx.send(
                "I'm not in that source server, so I can't read its emojis."
            )
        target, err = await self.resolve_target(ctx, target_id)
        if err:
            return await ctx.send(err)
        perm_err = await self.check_perms(ctx, target)
        if perm_err:
            return await ctx.send(perm_err)

        emojis = list(source.emojis)
        if not emojis:
            return await ctx.send("That source server has no emojis.")
        await self._transfer_emojis(ctx, target, emojis)

    @emoji.command()
    async def fromurl(self, ctx, target_id: Optional[int], url: str, *, name: str = None):
        target, err = await self.resolve_target(ctx, target_id)
        if err:
            return await ctx.send(err)
        perm_err = await self.check_perms(ctx, target)
        if perm_err:
            return await ctx.send(perm_err)

        data, derr = await self.download(url)
        if data is None:
            return await ctx.send(f"Couldn't download that URL: {derr}.")

        used = {e.name for e in target.emojis}
        emoji_name = self.resolve_name(used, name or "emoji")
        try:
            created = await target.create_custom_emoji(
                name=emoji_name, image=data, reason=f"Added by {ctx.author}"
            )
            await ctx.send(f"Created emoji `{created.name}` {created}")
        except discord.HTTPException as e:
            await ctx.send(f"Failed to create emoji: `{e}`")

    @emoji.command()
    async def add(self, ctx, target_id: Optional[int] = None, *, name: str = None):
        target, err = await self.resolve_target(ctx, target_id)
        if err:
            return await ctx.send(err)
        perm_err = await self.check_perms(ctx, target)
        if perm_err:
            return await ctx.send(perm_err)

        replied = await self.fetch_reference(ctx)
        if not replied or not replied.attachments:
            return await ctx.send("Reply to a message that contains an image.")

        attachment = replied.attachments[0]
        if not (attachment.content_type or "").startswith("image"):
            return await ctx.send("That attachment isn't an image.")

        data, derr = await self.download(attachment.url)
        if data is None:
            return await ctx.send(f"Couldn't use that image: {derr}.")

        used = {e.name for e in target.emojis}
        base = name or attachment.filename.rsplit(".", 1)[0]
        emoji_name = self.resolve_name(used, base)
        try:
            created = await target.create_custom_emoji(
                name=emoji_name, image=data, reason=f"Added by {ctx.author}"
            )
            await ctx.send(f"Created emoji `{created.name}` {created}")
        except discord.HTTPException as e:
            await ctx.send(f"Failed to create emoji: `{e}`")

    @emoji.command()
    async def delete(self, ctx, target_id: Optional[int], name: str):
        target, err = await self.resolve_target(ctx, target_id)
        if err:
            return await ctx.send(err)
        perm_err = await self.check_perms(ctx, target)
        if perm_err:
            return await ctx.send(perm_err)

        wanted_id = self.parse_emoji_input(name)
        match = next(
            (
                e for e in target.emojis
                if (wanted_id and e.id == wanted_id) or e.name == name
            ),
            None,
        )
        if not match:
            return await ctx.send(f"No emoji matching `{name}` in **{target.name}**.")
        try:
            await match.delete(reason=f"Deleted by {ctx.author}")
            await ctx.send(f"Deleted `{match.name}`.")
        except discord.HTTPException as e:
            await ctx.send(f"Failed to delete: `{e}`")

    @emoji.command()
    async def sticker(self, ctx, target_id: Optional[int] = None, *, name: str = None):
        target, err = await self.resolve_target(ctx, target_id)
        if err:
            return await ctx.send(err)
        perm_err = await self.check_perms(ctx, target)
        if perm_err:
            return await ctx.send(perm_err)

        replied = await self.fetch_reference(ctx)
        if not replied:
            return await ctx.send("Reply to a sticker or an image message.")

        used = {s.name for s in target.stickers}

        if replied.stickers:
            src = replied.stickers[0]
            try:
                full = await src.fetch()
            except discord.HTTPException:
                full = None
            fmt = getattr(full, "format", getattr(src, "format", None))
            if fmt is discord.StickerFormatType.lottie:
                return await ctx.send(
                    "That's a Lottie sticker — Discord won't let me re-upload it."
                )
            data, derr = await self.download(str(src.url), limit=MAX_STICKER_BYTES)
            if data is None:
                return await ctx.send(f"Couldn't download the sticker: {derr}.")
            sticker_name = name or src.name
            description = getattr(full, "description", None) or "Cloned sticker"

        elif replied.attachments:
            attachment = replied.attachments[0]
            if not (attachment.content_type or "").startswith("image"):
                return await ctx.send("That attachment isn't an image.")
            data, derr = await self.download(attachment.url, limit=MAX_STICKER_BYTES)
            if data is None:
                return await ctx.send(f"Couldn't download the image: {derr}.")
            sticker_name = name or attachment.filename.rsplit(".", 1)[0]
            description = "Cloned sticker"

        else:
            return await ctx.send("Reply must contain a sticker or an image.")

        sticker_name = self.resolve_name(used, sticker_name)
        try:
            file = discord.File(BytesIO(data), filename="sticker.png")
            created = await target.create_sticker(
                name=sticker_name,
                description=description,
                emoji="🙂",
                file=file,
                reason=f"Cloned by {ctx.author}",
            )
            await ctx.send(f"Created sticker `{created.name}`.")
        except discord.HTTPException as e:
            await ctx.send(
                f"Failed to create sticker: `{e}`\n"
                "Stickers must be 320×320 PNG/APNG under 512 KB."
            )

    @emoji.command()
    @commands.max_concurrency(1, commands.BucketType.guild, wait=False)
    async def stickersall(self, ctx, target_id: Optional[int] = None):
        target, err = await self.resolve_target(ctx, target_id)
        if err:
            return await ctx.send(err)
        perm_err = await self.check_perms(ctx, target)
        if perm_err:
            return await ctx.send(perm_err)

        stickers = list(ctx.guild.stickers)
        if not stickers:
            return await ctx.send("This server has no stickers.")

        used = {s.name for s in target.stickers}
        sticker_count = len(target.stickers)
        limit = target.sticker_limit

        added = skipped = errors = 0
        last_error = None
        total = len(stickers)
        progress = await ctx.send(
            f"Transferring {total} sticker(s) to **{target.name}**..."
        )

        for i, sticker in enumerate(stickers, start=1):
            if sticker_count >= limit:
                skipped += 1
                continue
            if sticker.format is discord.StickerFormatType.lottie:
                skipped += 1
                last_error = "Lottie stickers can't be re-uploaded."
                continue

            data, derr = await self.download(str(sticker.url), limit=MAX_STICKER_BYTES)
            if data is None:
                errors += 1
                last_error = derr
                continue

            name = self.resolve_name(used, sticker.name)
            try:
                file = discord.File(BytesIO(data), filename="sticker.png")
                await target.create_sticker(
                    name=name,
                    description=sticker.description or "Cloned sticker",
                    emoji="🙂",
                    file=file,
                    reason=f"Cloned by {ctx.author}",
                )
                added += 1
                sticker_count += 1
                used.add(name)
                await asyncio.sleep(CREATE_DELAY)
            except discord.HTTPException as e:
                errors += 1
                last_error = str(e)
                log.warning("Sticker create failed (%s): %s", name, e)

            if i % 3 == 0 or i == total:
                await progress.edit(
                    content=(
                        f"{i}/{total} | added {added} | "
                        f"skipped {skipped} | errors {errors}"
                    )
                )

        summary = (
            f"**Done** → {target.name}\n"
            f"Added: {added}\nSkipped: {skipped}\nErrors: {errors}"
        )
        if last_error:
            summary += f"\nLast note: `{last_error[:150]}`"
        await progress.edit(content=summary)


async def setup(bot):
    await bot.add_cog(EmojiTransfer(bot))