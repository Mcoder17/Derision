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
CREATE_DELAY = 1.5  # seconds between creates; emoji/sticker creation is rate limited

CDN_EMOJI = "https://cdn.discordapp.com/emojis/{id}.{ext}"


def has_expression_perm(perms: discord.Permissions) -> bool:
    """Works across discord.py versions (manage_expressions is newer,
    manage_emojis_and_stickers is the deprecated alias)."""
    val = getattr(perms, "manage_expressions", None)
    if val is None:
        val = getattr(perms, "manage_emojis_and_stickers", False)
    return bool(val)


class EmojiJob:
    """One emoji to create. `fetch` -> (data, animated, error)."""

    __slots__ = ("name", "fetch")

    def __init__(self, name, fetch):
        self.name = name
        self.fetch = fetch


class StickerJob:
    """One sticker to create. `fetch` -> (data, name, description, error)."""

    __slots__ = ("fetch",)

    def __init__(self, fetch):
        self.fetch = fetch


class EmojiTransfer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        # one session for the whole cog rather than one per download
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
            raise error  # hand off to the bot's global error handler / logging

    # ---------- download helpers ----------

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

    async def download_emoji_by_id(self, eid):
        """Fetch an emoji straight from the CDN by ID, regardless of whether the
        bot shares a server with it. Tries animated first, then static.
        Returns (data, animated, error)."""
        last = None
        for ext, animated in (("gif", True), ("png", False)):
            data, err = await self.download(CDN_EMOJI.format(id=eid, ext=ext))
            if data is not None:
                return data, animated, None
            last = err
        return None, False, last or "no emoji found for that ID"

    async def fetch_sticker_by_id(self, sid):
        """Resolve a sticker by ID via the API — works for any sticker, not just
        ones on servers the bot shares. Returns a Sticker or None."""
        try:
            return await self.bot.fetch_sticker(sid)
        except discord.HTTPException:
            return None

    # ---------- name helpers ----------

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

    def parse_emoji_input(self, arg):
        arg = str(arg)
        if arg.isdigit():
            return int(arg)
        m = EMOJI_REGEX.match(arg)
        if m:
            return int(m.group(2))
        return None

    def parse_id_token(self, token, allow_emoji=False):
        """Parse a token like '123', '<:x:123>', or '123=name' into
        (id_or_None, name_or_None)."""
        name = None
        if "=" in token:
            token, name = token.split("=", 1)
            name = name.strip() or None
        token = token.strip()
        if allow_emoji:
            eid = self.parse_emoji_input(token)
        else:
            eid = int(token) if token.isdigit() else None
        return eid, name

    # ---------- job builders ----------

    def job_from_emoji(self, e, name=None):
        """Job from a cached emoji object; optional name override."""
        async def fetch():
            data, err = await self.download(str(e.url))
            return data, e.animated, err
        return EmojiJob(name or e.name, fetch)

    def job_from_id(self, eid, name=None):
        """Job from a raw emoji ID (resolved via the CDN); optional name."""
        async def fetch():
            return await self.download_emoji_by_id(eid)
        return EmojiJob(name or f"emoji_{eid}", fetch)

    def sticker_job_from_obj(self, s, name=None):
        """Job from a sticker object we already hold (e.g. from stickersall)."""
        async def fetch():
            if s.format is discord.StickerFormatType.lottie:
                return None, None, None, "Lottie stickers can't be re-uploaded."
            data, err = await self.download(str(s.url), limit=MAX_STICKER_BYTES)
            if data is None:
                return None, None, None, err
            return data, name or s.name, s.description or "Cloned sticker", None
        return StickerJob(fetch)

    def sticker_job_from_id(self, sid, name=None):
        """Job from a raw sticker ID (resolved via the API); optional name."""
        async def fetch():
            s = await self.fetch_sticker_by_id(sid)
            if s is None:
                return None, None, None, f"sticker {sid} not found"
            if s.format is discord.StickerFormatType.lottie:
                return None, None, None, "Lottie stickers can't be re-uploaded."
            data, err = await self.download(str(s.url), limit=MAX_STICKER_BYTES)
            if data is None:
                return None, None, None, err
            return data, name or s.name, s.description or "Cloned sticker", None
        return StickerJob(fetch)

    # ---------- target / permission helpers ----------

    def slot_counts(self, guild):
        static = sum(1 for e in guild.emojis if not e.animated)
        animated = sum(1 for e in guild.emojis if e.animated)
        return static, animated

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
        if member is None:  # not cached; fall back to an API fetch
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

    def split_target(self, ctx, items):
        """If the first token is a plain ID resolving to a guild the bot is in,
        treat it as the target and strip it. Returns (target, remaining_items)."""
        target = ctx.guild
        if items and items[0].isdigit():
            maybe = self.bot.get_guild(int(items[0]))
            if maybe:
                target = maybe
                items = items[1:]
        return target, list(items)

    # ---------- shared workers ----------

    async def _transfer_emojis(self, ctx, target, jobs):
        """Bulk-emoji worker. Always copy, renaming on collision. Animated-ness is
        read from each job after its data is fetched (raw IDs behave like cached)."""
        limit = target.emoji_limit
        static_used, animated_used = self.slot_counts(target)
        used_names = {e.name for e in target.emojis}

        added = skipped = errors = 0
        last_error = None
        total = len(jobs)
        progress = await ctx.send(
            f"Starting transfer of {total} emoji(s) to **{target.name}**..."
        )

        for i, job in enumerate(jobs, start=1):
            data, animated, derr = await job.fetch()
            if data is None:
                errors += 1
                last_error = derr
                continue

            if animated and animated_used >= limit:
                skipped += 1
                continue
            if not animated and static_used >= limit:
                skipped += 1
                continue

            name = self.resolve_name(used_names, job.name)
            try:
                await target.create_custom_emoji(
                    name=name, image=data, reason=f"Transferred by {ctx.author}"
                )
                added += 1
                used_names.add(name)
                if animated:
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
            f"**Done** -> {target.name}\n"
            f"Added: {added}\n"
            f"Skipped (slots full): {skipped}\n"
            f"Errors: {errors}"
        )
        if last_error:
            summary += f"\nLast error: `{last_error[:150]}`"
        await progress.edit(content=summary)

    async def _transfer_stickers(self, ctx, target, jobs):
        """Bulk-sticker worker mirroring the emoji one."""
        used = {s.name for s in target.stickers}
        sticker_count = len(target.stickers)
        limit = target.sticker_limit

        added = skipped = errors = 0
        last_error = None
        total = len(jobs)
        progress = await ctx.send(
            f"Transferring {total} sticker(s) to **{target.name}**..."
        )

        for i, job in enumerate(jobs, start=1):
            if sticker_count >= limit:
                skipped += 1
                continue

            data, name, description, err = await job.fetch()
            if data is None:
                # Lottie / unsupported counts as skipped, real failures as errors
                if err and "Lottie" in err:
                    skipped += 1
                else:
                    errors += 1
                last_error = err
                continue

            name = self.resolve_name(used, name)
            try:
                file = discord.File(BytesIO(data), filename="sticker.png")
                await target.create_sticker(
                    name=name,
                    description=description,
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
            f"**Done** -> {target.name}\n"
            f"Added: {added}\nSkipped: {skipped}\nErrors: {errors}"
        )
        if last_error:
            summary += f"\nLast note: `{last_error[:150]}`"
        await progress.edit(content=summary)

    # ---------- command group ----------

    @commands.guild_only()
    @commands.group(name="emoji", invoke_without_command=True)
    async def emoji(self, ctx):
        await ctx.send(
            "**Emoji / sticker tools** (TARGET defaults to this server).\n"
            "By-ID commands accept `ID=name` to name each item.\n\n"
            "__Emojis__\n"
            "`!emoji info [TARGET]` — show slot usage\n"
            "`!emoji sendall [TARGET]` — copy all emojis from here\n"
            "`!emoji send [TARGET] <ids/emojis>` — copy emojis (works on any ID)\n"
            "`!emoji grab [TARGET] <ids/emojis>` — copy emojis by ID from anywhere\n"
            "`!emoji clone <SOURCE_ID> [TARGET]` — copy all emojis from another server\n"
            "`!emoji fromurl [TARGET] <url> [name]` — add an emoji from a link\n"
            "`!emoji add [TARGET] [name]` — reply to an image\n"
            "`!emoji delete [TARGET] <name/emoji>` — remove an emoji\n\n"
            "__Stickers__\n"
            "`!emoji sticker [TARGET] [name]` — reply to a sticker/image\n"
            "`!emoji stickergrab [TARGET] <ids>` — copy stickers by ID from anywhere\n"
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
        await self._transfer_emojis(ctx, target, [self.job_from_emoji(e) for e in emojis])

    @emoji.command()
    @commands.max_concurrency(1, commands.BucketType.guild, wait=False)
    async def send(self, ctx, *args):
        if not args:
            return await ctx.send(
                "Give me emoji IDs or custom emojis, e.g. `!emoji send :blob: 123456789`.\n"
                "Use `ID=name` to rename. Optionally start with a target server ID.\n"
                "For a plain image, reply to it with `!emoji add`."
            )

        target, items = self.split_target(ctx, list(args))
        perm_err = await self.check_perms(ctx, target)
        if perm_err:
            return await ctx.send(perm_err)

        jobs = []
        for token in items:
            eid, name = self.parse_id_token(token, allow_emoji=True)
            if not eid:
                continue
            cached = self.bot.get_emoji(eid)
            jobs.append(
                self.job_from_emoji(cached, name) if cached else self.job_from_id(eid, name)
            )

        if not jobs:
            return await ctx.send("No valid emoji IDs or custom emojis found.")
        await self._transfer_emojis(ctx, target, jobs)

    @emoji.command()
    @commands.max_concurrency(1, commands.BucketType.guild, wait=False)
    async def grab(self, ctx, *args):
        """Copy emojis purely by ID, straight from the CDN — no shared server
        required. Accepts raw IDs, pasted custom emojis, or `ID=name`."""
        if not args:
            return await ctx.send(
                "Give me emoji IDs, e.g. `!emoji grab 123456789 987654321=cool`.\n"
                "Use `ID=name` to rename. Optionally start with a target server ID."
            )

        target, items = self.split_target(ctx, list(args))
        perm_err = await self.check_perms(ctx, target)
        if perm_err:
            return await ctx.send(perm_err)

        jobs = []
        for token in items:
            eid, name = self.parse_id_token(token, allow_emoji=True)
            if eid:
                jobs.append(self.job_from_id(eid, name))

        if not jobs:
            return await ctx.send("No valid emoji IDs found.")
        await self._transfer_emojis(ctx, target, jobs)

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
        await self._transfer_emojis(ctx, target, [self.job_from_emoji(e) for e in emojis])

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
                "Stickers must be 320x320 PNG/APNG under 512 KB."
            )

    @emoji.command()
    @commands.max_concurrency(1, commands.BucketType.guild, wait=False)
    async def stickergrab(self, ctx, *args):
        """Copy stickers by ID from anywhere (resolved via the API). Accepts raw
        IDs or `ID=name`."""
        if not args:
            return await ctx.send(
                "Give me sticker IDs, e.g. `!emoji stickergrab 123456789 987=name`.\n"
                "Use `ID=name` to rename. Optionally start with a target server ID."
            )

        target, items = self.split_target(ctx, list(args))
        perm_err = await self.check_perms(ctx, target)
        if perm_err:
            return await ctx.send(perm_err)

        jobs = []
        for token in items:
            sid, name = self.parse_id_token(token)  # stickers: plain IDs only
            if sid:
                jobs.append(self.sticker_job_from_id(sid, name))

        if not jobs:
            return await ctx.send("No valid sticker IDs found.")
        await self._transfer_stickers(ctx, target, jobs)

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
        await self._transfer_stickers(
            ctx, target, [self.sticker_job_from_obj(s) for s in stickers]
        )


async def setup(bot):
    await bot.add_cog(EmojiTransfer(bot))