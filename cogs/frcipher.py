import zlib
from typing import Literal

import discord
from discord.ext import commands


class FRCog(commands.Cog):
    """
    Compact F/R binary encoder/decoder.

    Encoding alphabet:
        f = 0
        r = 1

    Every encoded chunk ALWAYS:
        - starts with f
        - ends with r

    Format:
        f [compression flag] [payload] r

    Compression flag:
        f = uncompressed UTF-8
        r = raw DEFLATE compressed UTF-8

    Large messages are split into independent encoded chunks so that
    every Discord message still starts with f and ends with r.
    """

    DISCORD_MESSAGE_LIMIT = 2000
    MAX_DECODED_BYTES = 100_000

    # ------------------------------------------------------------------
    # Low-level conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _bytes_to_bits(data: bytes) -> str:
        """Convert bytes to an 8-bit binary string."""
        return "".join(f"{byte:08b}" for byte in data)

    @staticmethod
    def _bits_to_bytes(bits: str) -> bytes:
        """Convert a byte-aligned binary string back into bytes."""
        if not bits:
            return b""

        if len(bits) % 8 != 0:
            raise ValueError("Invalid bitstream length.")

        return bytes(
            int(bits[i:i + 8], 2)
            for i in range(0, len(bits), 8)
        )

    @staticmethod
    def _compress(data: bytes) -> bytes:
        """
        Raw DEFLATE compression.

        A raw DEFLATE stream has less overhead than normal zlib
        compression, which is useful for short encoded messages.
        """
        compressor = zlib.compressobj(
            level=9,
            method=zlib.DEFLATED,
            wbits=-15,
        )

        return compressor.compress(data) + compressor.flush()

    @staticmethod
    def _decompress(data: bytes) -> bytes:
        """Decompress a raw DEFLATE stream."""
        return zlib.decompress(data, wbits=-15)

    # ------------------------------------------------------------------
    # F/R conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _bits_to_fr(bits: str) -> str:
        """Convert 0/1 bits into f/r characters."""
        return bits.translate(
            str.maketrans({
                "0": "f",
                "1": "r",
            })
        )

    @staticmethod
    def _fr_to_bits(data: str) -> str:
        """Convert f/r characters into 0/1 bits."""
        return data.translate(
            str.maketrans({
                "f": "0",
                "r": "1",
            })
        )

    # ------------------------------------------------------------------
    # Encoding
    # ------------------------------------------------------------------

    @classmethod
    def encode(cls, text: str) -> str:
        """
        Encode one piece of text.

        Format:

            f + compression flag + payload + r

        The outer f/r characters are fixed markers.

        Examples:

            ff........r = uncompressed
            fr........r = compressed
        """

        if not isinstance(text, str):
            raise TypeError("text must be a string.")

        raw = text.encode("utf-8")

        if not raw:
            # Empty text still gets a valid f...r structure.
            # No payload bytes are needed.
            return "ffr"

        compressed = cls._compress(raw)

        # Only use compression when it actually produces a smaller
        # payload.
        use_compression = len(compressed) < len(raw)

        if use_compression:
            payload = compressed
            compression_flag = "1"  # r
        else:
            payload = raw
            compression_flag = "0"  # f

        payload_bits = cls._bytes_to_bits(payload)

        # Fixed:
        #   0 = f start marker
        #   flag
        #   payload
        #   1 = r end marker
        bits = (
            "0"
            + compression_flag
            + payload_bits
            + "1"
        )

        return cls._bits_to_fr(bits)

    # ------------------------------------------------------------------
    # Decoding
    # ------------------------------------------------------------------

    @classmethod
    def decode(cls, encoded: str) -> str:
        """
        Decode one complete f/r chunk.
        """

        if not isinstance(encoded, str):
            raise TypeError("encoded must be a string.")

        # Remove whitespace so copied/multiline encoded data can still
        # be decoded.
        encoded = "".join(encoded.split())

        if not encoded:
            raise ValueError("Encoded message is empty.")

        # Only f and r are legal.
        invalid = set(encoded) - {"f", "r"}

        if invalid:
            raise ValueError(
                "Encoded data may only contain the letters 'f' and 'r'."
            )

        # Every encoded chunk MUST start with f.
        if not encoded.startswith("f"):
            raise ValueError(
                "Invalid F/R data: message must start with f."
            )

        # Every encoded chunk MUST end with r.
        if not encoded.endswith("r"):
            raise ValueError(
                "Invalid F/R data: message must end with r."
            )

        # A valid encoding is:
        #
        # f [flag] [payload bits] r
        #
        # Therefore:
        #   first char = start marker
        #   second char = compression flag
        #   last char = end marker
        #
        if len(encoded) < 3:
            raise ValueError(
                "Invalid F/R data: message is too short."
            )

        inner = encoded[1:-1]

        # First bit of the inner section is the compression flag.
        if not inner:
            raise ValueError(
                "Invalid F/R data: missing compression flag."
            )

        # Everything after the flag represents complete bytes.
        payload_fr = inner[1:]

        if not payload_fr:
            # This is only valid for our empty-text representation.
            if inner != "f":
                raise ValueError(
                    "Invalid F/R data: malformed empty payload."
                )

            return ""

        # Payload must contain complete bytes.
        if len(payload_fr) % 8 != 0:
            raise ValueError(
                "Invalid F/R data: malformed payload length."
            )

        bits = cls._fr_to_bits(inner)

        compressed = bits[0] == "1"

        payload_bits = bits[1:]

        payload = cls._bits_to_bytes(payload_bits)

        try:
            if compressed:
                raw = cls._decompress(payload)

                if len(raw) > cls.MAX_DECODED_BYTES:
                    raise ValueError(
                        "Decoded message exceeds the allowed size."
                    )
            else:
                raw = payload

                if len(raw) > cls.MAX_DECODED_BYTES:
                    raise ValueError(
                        "Decoded message exceeds the allowed size."
                    )

            return raw.decode("utf-8")

        except zlib.error as exc:
            raise ValueError(
                "Compressed F/R data is corrupted."
            ) from exc

        except UnicodeDecodeError as exc:
            raise ValueError(
                "Decoded F/R data is not valid UTF-8."
            ) from exc

    # ------------------------------------------------------------------
    # Chunking
    # ------------------------------------------------------------------

    @classmethod
    def encode_chunks(cls, text: str) -> list[str]:
        """
        Encode text into independent chunks.

        Unlike simply slicing one encoded stream, every chunk is a
        completely valid F/R message beginning with f and ending with r.

        The algorithm finds the largest UTF-8 text chunk that fits within
        Discord's 2,000-character message limit after encoding.
        """

        if not text:
            return [cls.encode("")]

        chunks: list[str] = []
        position = 0

        while position < len(text):
            low = position + 1
            high = len(text)
            best_end = None

            # Binary search for the largest text chunk that can be
            # encoded into <= 2000 characters.
            while low <= high:
                mid = (low + high) // 2
                candidate_text = text[position:mid]

                try:
                    candidate_encoded = cls.encode(candidate_text)
                except Exception:
                    candidate_encoded = ""

                if (
                    candidate_encoded
                    and len(candidate_encoded) <= cls.DISCORD_MESSAGE_LIMIT
                ):
                    best_end = mid
                    low = mid + 1
                else:
                    high = mid - 1

            if best_end is None:
                # This should practically never happen because a single
                # Unicode character can normally be encoded well below
                # Discord's limit.
                raise ValueError(
                    "A single character cannot fit into an encoded Discord message."
                )

            chunk_text = text[position:best_end]
            encoded_chunk = cls.encode(chunk_text)

            # Defensive guarantees.
            if not encoded_chunk.startswith("f"):
                raise RuntimeError(
                    "Internal error: encoded chunk does not start with f."
                )

            if not encoded_chunk.endswith("r"):
                raise RuntimeError(
                    "Internal error: encoded chunk does not end with r."
                )

            if len(encoded_chunk) > cls.DISCORD_MESSAGE_LIMIT:
                raise RuntimeError(
                    "Internal error: encoded chunk exceeds Discord's limit."
                )

            chunks.append(encoded_chunk)
            position = best_end

        return chunks

    # ------------------------------------------------------------------
    # Sending encoded data
    # ------------------------------------------------------------------

    async def _send_encoded(
        self,
        ctx: commands.Context,
        text: str,
    ):
        """Encode and send text as one or more valid F/R messages."""

        chunks = self.encode_chunks(text)

        if len(chunks) > 1:
            await ctx.send(
                f"Encoded into {len(chunks)} parts. "
                f"Each part starts with `f` and ends with `r`."
            )

        for chunk in chunks:
            await ctx.send(chunk)

    # ------------------------------------------------------------------
    # Sending decoded data
    # ------------------------------------------------------------------

    async def _send_decoded(
        self,
        ctx: commands.Context,
        text: str,
    ):
        """Send decoded text, splitting it when Discord requires it."""

        if not text:
            await ctx.send("")
            return

        for position in range(
            0,
            len(text),
            self.DISCORD_MESSAGE_LIMIT,
        ):
            await ctx.send(
                text[
                    position:
                    position + self.DISCORD_MESSAGE_LIMIT
                ]
            )

    # ------------------------------------------------------------------
    # Hybrid command
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="fr",
        description="Encode or decode text using compact F/R binary.",
    )
    @commands.cooldown(
        3,
        10,
        commands.BucketType.user,
    )
    async def fr(
        self,
        ctx: commands.Context,
        mode: Literal["encode", "decode"],
        *,
        text: str,
    ):
        """
        Prefix:

            !fr encode Hello world
            !fr decode ffr...

        Slash:

            /fr encode text:Hello world
            /fr decode text:ffr...
        """

        if mode == "encode":
            try:
                await self._send_encoded(
                    ctx,
                    text,
                )

            except UnicodeError:
                await ctx.send(
                    "The supplied text could not be encoded."
                )

            except ValueError as exc:
                await ctx.send(
                    f"Encoding failed: {exc}"
                )

        elif mode == "decode":
            try:
                # Normally this is one complete chunk. If someone gives
                # multiple valid chunks concatenated together, decode them
                # individually.
                decoded_parts = self._decode_multiple_chunks(text)

                await self._send_decoded(
                    ctx,
                    "".join(decoded_parts),
                )

            except ValueError as exc:
                await ctx.send(
                    f"Invalid F/R data: {exc}"
                )

    # ------------------------------------------------------------------
    # Multi-chunk decoding
    # ------------------------------------------------------------------

    @classmethod
    def _decode_multiple_chunks(
        cls,
        encoded: str,
    ) -> list[str]:
        """
        Decode either:
            - one F/R chunk, or
            - multiple concatenated F/R chunks.

        Because every chunk begins with f and ends with r, boundaries
        can be identified without whitespace.
        """

        encoded = "".join(encoded.split())

        if not encoded:
            raise ValueError("Encoded message is empty.")

        if any(char not in "fr" for char in encoded):
            raise ValueError(
                "Encoded data may only contain the letters 'f' and 'r'."
            )

        if not encoded.startswith("f"):
            raise ValueError(
                "Encoded data must start with f."
            )

        # --------------------------------------------------------------
        # A single chunk is by far the common case.
        #
        # Detecting arbitrary concatenated chunks is not always possible
        # from the f/r stream alone because r can occur inside the binary
        # payload. Therefore, when one valid complete chunk is supplied,
        # decode it directly.
        # --------------------------------------------------------------

        try:
            return [cls.decode(encoded)]
        except ValueError:
            pass

        # --------------------------------------------------------------
        # Try splitting at every possible end marker.
        #
        # At each position after the first character, a candidate ending
        # in r is tested with decode(). If successful, continue decoding
        # the remaining data.
        # --------------------------------------------------------------

        result: list[str] = []
        position = 0

        while position < len(encoded):
            if encoded[position] != "f":
                raise ValueError(
                    "Invalid concatenated F/R data: expected f at chunk start."
                )

            found = False

            # The shortest meaningful chunk is 3 characters: ffr.
            for end in range(
                position + 3,
                len(encoded) + 1,
            ):
                if encoded[end - 1] != "r":
                    continue

                candidate = encoded[position:end]

                try:
                    decoded = cls.decode(candidate)
                except ValueError:
                    continue

                result.append(decoded)
                position = end
                found = True
                break

            if not found:
                raise ValueError(
                    "Could not identify a valid F/R chunk."
                )

        return result

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    @fr.error
    async def fr_error(
        self,
        ctx: commands.Context,
        error: commands.CommandError,
    ):
        if isinstance(
            error,
            commands.CommandOnCooldown,
        ):
            await ctx.send(
                f"You're using this too quickly. "
                f"Try again in {error.retry_after:.1f}s."
            )
            return

        if isinstance(
            error,
            commands.MissingRequiredArgument,
        ):
            await ctx.send(
                "Usage: `!fr encode <text>` or "
                "`!fr decode <f/r data>`"
            )
            return

        if isinstance(
            error,
            commands.BadLiteralArgument,
        ):
            await ctx.send(
                "Mode must be either `encode` or `decode`."
            )
            return

        raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(FRCog(bot))