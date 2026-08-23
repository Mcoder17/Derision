import asyncio
import json
import math
import os
import re
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
import discord
from discord.ext import commands

from env import (
    LINGUISTICS_REPORT_CHANNEL_NAME,
    LINGUISTICS_REPORT_GUILD_ID,
    OWNER_ID,
)
from utils.public_stats import record_messages_analyzed


DB_DIR = "./db"
DB_PATH = f"{DB_DIR}/linguistics.db"
SEND_COMPARISON_COPY_TO_REPORT_CHANNEL = True

MIN_MESSAGES = 150
MATTR_WINDOW = 50

BOT_COMMAND_PREFIXES = ("!", "?", ".", "$")
STRING_COMMAND_PREFIXES: Tuple[str, ...] = ("owo ", "owo")

PROFILE_COLUMNS: Tuple[str, ...] = (
    "total_messages", "total_chars", "total_words", "total_sentences",
    "message_word_count_sum", "message_word_count_sumsq",
    "message_sentence_len_sum", "message_sentence_len_sumsq",
    "unique_words_sum", "ttr_sum", "mattr_sum", "mattr_sumsq", "mattr_count", "hapax_ratio_sum",
    "uppercase_words", "emoji_count", "unicode_emoji_count", "custom_emoji_count",
    "digit_tokens", "url_count", "short_words", "long_words", "contraction_tokens",
    "repeated_char_tokens", "question_marks", "exclamation_marks", "ellipsis_count",
    "avg_word_len_sum", "avg_sentence_len_sum",
    "function_words_json", "punctuation_json", "emoji_json", "unicode_emojis_json",
    "custom_emojis_json", "char_ngrams_json", "common_words_json",
)

PROFILE_COLUMNS_SQL = ", ".join(PROFILE_COLUMNS)

FUNCTION_WORDS = {
    # Articles
    "a", "an", "the",

    # Conjunctions
    "and", "but", "or", "nor", "for", "yet", "so",
    "because", "since", "while", "although", "though",
    "unless", "until", "whereas", "whether",

    # Prepositions
    "of", "to", "in", "on", "at", "for", "with",
    "from", "by", "about", "into", "over", "after",
    "before", "between", "through", "during", "under",
    "around", "against", "without", "within",

    # Pronouns
    "i", "me", "my", "mine",
    "you", "your", "yours",
    "we", "us", "our", "ours",
    "they", "them", "their", "theirs",
    "he", "him", "his",
    "she", "her", "hers",
    "it", "its",

    # Demonstratives
    "this", "that", "these", "those",

    # Auxiliary verbs
    "is", "are", "am", "was", "were",
    "be", "been", "being",
    "do", "does", "did",
    "have", "has", "had",
    "can", "could",
    "will", "would",
    "shall", "should",
    "may", "might",
    "must",

    # Negations
    "not", "no", "never",
    "dont", "doesnt", "didnt",
    "cant", "couldnt",
    "wont", "wouldnt",
    "isnt", "arent",
    "wasnt", "werent",

    # Fillers / discourse markers
    "well", "like", "actually", "literally",
    "basically", "seriously", "honestly",
    "obviously", "apparently",
    "really", "very", "quite",
    "pretty", "rather",
    "just", "even", "still",
    "already", "anyway",
    "anyways",

    # Hedging
    "maybe", "perhaps", "probably",
    "possibly", "likely",
    "kinda", "sorta",
    "somewhat", "almost", "prolly",

    # Conversation markers
    "yeah", "yea", "yep", "yup",
    "nah", "nope",
    "okay", "ok", "alright",
    "sure", "fine",
    "right", "true", "yhh", "alr",

    # Informal address terms
    "bro", "bros",
    "bruh",
    "man",
    "dude",
    "homie",
    "fam",
    "gang",
    "mate",
    "buddy",
    "cuh",
    "gng",

    # Internet / Gen Z discourse markers
    "fr",
    "ngl",
    "tbh",
    "imo",
    "imho",
    "irl",
    "idk",
    "ik",
    "lmao",
    "lmfao",
    "lol",
    "rofl",
    "smh",
    "omg",
    "wtf",
    "btw",
    "fyi",

    # Modern filler words
    "lowkey",
    "highkey",
    "legit",
    "deadass",
    "frfr",
    "real",
    "facts",
    "honestly",
    "genuinely",
    "lwk",
    "hwk",
    "lowk",

    # Agreement markers
    "exactly",
    "definitely",
    "absolutely",
    "totally",
    "literally",

    # Common slang connectors
    "cuz",
    "cause",
    "bc",
    "tho",
    "thooo",
    "nvm",
    "yall",
    "ya",
    "nahh",
    "broo",
    "brooo",

    # Modern reaction words
    "based",
    "cringe",
    "valid",
    "wild",
    "crazy",
    "insane",
    "damn",
    "demn",
    "dem",

    # Question/follow-up markers
    "wait",
    "holdon",
    "listen",
    "look",
    "see",
    "mean",
}

PUNCT_TOKENS = [".", ",", "!", "?", ":", ";", "-", "...", "(", ")", "[", "]", "{", "}"]

# Letters only, but keeps contractions like don't / I'm.
WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|<#\d+>")
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
DISCORD_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")
WS_RE = re.compile(r"\s+")
REPEATED_CHARS_RE = re.compile(r"(.)\1{2,}", re.IGNORECASE)


class Linguistics(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending_public_message_count = 0
        self._public_stats_lock = asyncio.Lock()

        self.db: Optional[aiosqlite.Connection] = None
        self.db_lock: Optional[asyncio.Lock] = None
        self.ready = asyncio.Event()

    async def cog_load(self):
        await self.ensure_db_setup()
        self.ready.set()

    def cog_unload(self):
        if self.db is not None:
            try:
                loop = asyncio.get_event_loop()

                if loop.is_running():
                    loop.create_task(self._close_db())
            except Exception:
                pass

    async def _close_db(self):
        try:
            if self.db is not None:
                await self.db.commit()
                await self.db.close()
        except Exception as e:
            print(f"[DB Close Error] {e}")

    async def _flush_public_message_count(self):
        async with self._public_stats_lock:
            pending = self._pending_public_message_count
            if pending <= 0:
                return

            self._pending_public_message_count = 0

        try:
            record_messages_analyzed(pending)
        except Exception as e:
            print(f"[Public Stats] Failed to record {pending} analyzed messages: {e}")

    async def _wait_ready(self):
        await self.ready.wait()

    async def _rebuild_global_stats_if_needed(self):
        if self.db is None or self.db_lock is None:
            return

        async with self.db_lock:
            cur = await self.db.execute("SELECT COUNT(*) FROM global_stats")
            row = await cur.fetchone()
            await cur.close()

            if row and row[0] > 0:
                return

            cur = await self.db.execute("SELECT function_words_json FROM user_profiles")
            rows = await cur.fetchall()
            await cur.close()

            user_count = 0
            df: Counter = Counter()
            for (function_json,) in rows:
                user_count += 1
                try:
                    func = json.loads(function_json or "{}")
                except Exception:
                    func = {}
                for w in func.keys():
                    df[w] += 1

            await self.db.execute(
                """
                INSERT OR REPLACE INTO global_stats
                (id, user_count, function_words_df_json, updated_at)
                VALUES (1, ?, ?, ?)
                """,
                (user_count, json.dumps(dict(df), ensure_ascii=False), int(time.time())),
            )
            await self.db.commit()

    async def ensure_db_setup(self):
        if self.db is not None:
            return

        os.makedirs(DB_DIR, exist_ok=True)
        self.db_lock = asyncio.Lock()

        self.db = await aiosqlite.connect(DB_PATH)
        await self.db.execute("PRAGMA journal_mode=WAL;")
        await self.db.execute("PRAGMA synchronous=NORMAL;")
        await self.db.execute("PRAGMA temp_store=FILE;")
        await self.db.execute("PRAGMA cache_size=-500;")
        await self.db.execute("PRAGMA busy_timeout=5000;")
        await self.db.execute("PRAGMA wal_autocheckpoint=100;")

        # Tables/artifacts from earlier versions that the current code never
        # uses. Dropping them is idempotent and reclaims their space; the
        # profile schema itself is assumed to already be user_id-keyed.
        for legacy_table in (
            "metadata",
            "retention_rules",
            "seen_messages",
            "comparisons",
            "scope_stats",
            "user_profiles_legacy",
        ):
            await self.db.execute(f"DROP TABLE IF EXISTS {legacy_table}")

        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                total_messages INTEGER NOT NULL DEFAULT 0,
                total_chars INTEGER NOT NULL DEFAULT 0,
                total_words INTEGER NOT NULL DEFAULT 0,
                total_sentences INTEGER NOT NULL DEFAULT 0,

                message_word_count_sum REAL NOT NULL DEFAULT 0,
                message_word_count_sumsq REAL NOT NULL DEFAULT 0,
                message_sentence_len_sum REAL NOT NULL DEFAULT 0,
                message_sentence_len_sumsq REAL NOT NULL DEFAULT 0,

                unique_words_sum REAL NOT NULL DEFAULT 0,
                ttr_sum REAL NOT NULL DEFAULT 0,
                mattr_sum REAL NOT NULL DEFAULT 0,
                mattr_sumsq REAL NOT NULL DEFAULT 0,
                mattr_count INTEGER NOT NULL DEFAULT 0,
                hapax_ratio_sum REAL NOT NULL DEFAULT 0,

                uppercase_words INTEGER NOT NULL DEFAULT 0,
                emoji_count INTEGER NOT NULL DEFAULT 0,
                unicode_emoji_count INTEGER NOT NULL DEFAULT 0,
                custom_emoji_count INTEGER NOT NULL DEFAULT 0,
                digit_tokens INTEGER NOT NULL DEFAULT 0,
                url_count INTEGER NOT NULL DEFAULT 0,
                short_words INTEGER NOT NULL DEFAULT 0,
                long_words INTEGER NOT NULL DEFAULT 0,
                contraction_tokens INTEGER NOT NULL DEFAULT 0,
                repeated_char_tokens INTEGER NOT NULL DEFAULT 0,
                question_marks INTEGER NOT NULL DEFAULT 0,
                exclamation_marks INTEGER NOT NULL DEFAULT 0,
                ellipsis_count INTEGER NOT NULL DEFAULT 0,

                avg_word_len_sum REAL NOT NULL DEFAULT 0,
                avg_sentence_len_sum REAL NOT NULL DEFAULT 0,

                function_words_json TEXT NOT NULL DEFAULT '{}',
                punctuation_json TEXT NOT NULL DEFAULT '{}',
                emoji_json TEXT NOT NULL DEFAULT '{}',
                unicode_emojis_json TEXT NOT NULL DEFAULT '{}',
                custom_emojis_json TEXT NOT NULL DEFAULT '{}',
                char_ngrams_json TEXT NOT NULL DEFAULT '{}',
                common_words_json TEXT NOT NULL DEFAULT '{}',

                updated_at INTEGER NOT NULL
            )
            """
        )

        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS global_stats (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                user_count INTEGER NOT NULL DEFAULT 0,
                function_words_df_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            )
            """
        )

        await self.db.commit()

        await self._rebuild_global_stats_if_needed()

    async def _db_fetchone(self, query: str, params: Tuple[Any, ...] = (), wait_ready: bool = True):
        if wait_ready:
            await self._wait_ready()
        if self.db is None or self.db_lock is None:
            return None
        async with self.db_lock:
            cur = await self.db.execute(query, params)
            row = await cur.fetchone()
            await cur.close()
            return row

    def parse_user_token(self, token: str) -> Optional[str]:
        token = token.strip()
        m = re.fullmatch(r"<@!?(\d+)>", token)
        if m:
            return m.group(1)
        if token.isdigit():
            return token
        return None

    def _display_name_for(self, ctx, user_id: str) -> str:
        # Best-effort human-readable label; falls back to the raw id. Uses no
        # mention syntax so viewing someone's stats never pings them.
        try:
            uid_int = int(user_id)
        except (TypeError, ValueError):
            return str(user_id)

        user_obj = None
        guild = getattr(ctx, "guild", None)
        if guild is not None:
            getter = getattr(guild, "get_member", None)
            if callable(getter):
                user_obj = getter(uid_int)

        if user_obj is None:
            getter = getattr(self.bot, "get_user", None)
            if callable(getter):
                user_obj = getter(uid_int)

        if user_obj is None:
            return str(user_id)
        return getattr(user_obj, "display_name", None) or str(user_obj)

    def _remove_emoji_chars(self, text: str) -> str:
        out = []
        for ch in text or "":
            code = ord(ch)
            is_emoji = (
                0x1F300 <= code <= 0x1FAFF or
                0x1F1E6 <= code <= 0x1F1FF or
                0x2700 <= code <= 0x27BF
            )
            out.append(" " if is_emoji else ch)
        return "".join(out)

    def _normalize_text(self, text: str) -> str:
        text = text or ""
        text = URL_RE.sub(" ", text)
        text = MENTION_RE.sub(" ", text)
        text = CUSTOM_EMOJI_RE.sub(" ", text)
        text = self._remove_emoji_chars(text)
        text = re.sub(r"\b\d+\b", " ", text)
        text = WS_RE.sub(" ", text)
        return text.strip()

    def _tokenize(self, text: str) -> List[str]:
        words = WORD_RE.findall(text or "")
        return [w.lower() for w in words if len(w) > 1 or w.lower() in {"i", "a"}]

    def _extract_unicode_emoji_tokens(self, text: str) -> List[str]:
        tokens: List[str] = []
        current: List[str] = []
        for ch in text or "":
            code = ord(ch)
            is_emoji = (
                0x1F300 <= code <= 0x1FAFF or
                0x1F1E6 <= code <= 0x1F1FF or
                0x2700 <= code <= 0x27BF
            )
            if is_emoji:
                current.append(ch)
            else:
                if current:
                    tokens.append("".join(current))
                    current = []
        if current:
            tokens.append("".join(current))
        return tokens

    def _extract_custom_emoji_tokens(self, text: str) -> List[str]:
        tokens: List[str] = []
        for match in DISCORD_EMOJI_RE.finditer(text or ""):
            name = match.group(1).lower()
            tokens.append(f"discord:{name}")
        return tokens

    def _entropy_normalized(self, c: Counter) -> float:
        total = sum(c.values())
        if total <= 0:
            return 0.0
        entropy = 0.0
        for count in c.values():
            p = count / total
            entropy -= p * math.log2(p)
        max_entropy = math.log2(len(c)) if len(c) > 1 else 0.0
        if max_entropy <= 0:
            return 0.0
        return max(0.0, min(entropy / max_entropy, 1.0))

    def _variance_from_sums(self, n: int, s: float, ss: float) -> float:
        if n <= 1:
            return 0.0
        mean = s / n
        return max((ss / n) - (mean * mean), 0.0)

    def _cv_from_sums(self, n: int, s: float, ss: float) -> float:
        if n <= 1:
            return 0.0
        mean = s / n
        if mean <= 0:
            return 0.0
        var = self._variance_from_sums(n, s, ss)
        return math.sqrt(var) / mean

    def _extract_message_metrics(self, text: str) -> Dict[str, Any]:
        raw = text or ""
        norm = self._normalize_text(raw)
        words = self._tokenize(norm)

        word_count = len(words)
        char_count = len(norm)
        sentence_count = max(len([s for s in re.split(r"[.!?]+", norm) if s.strip()]), 1)

        word_counter = Counter(words)
        unique_words_count = len(word_counter)
        hapax_count = sum(1 for c in word_counter.values() if c == 1)

        punctuation = Counter()
        punctuation["..."] = raw.count("...")
        for p in PUNCT_TOKENS:
            if p != "...":
                punctuation[p] = raw.count(p)

        unicode_emojis = Counter(self._extract_unicode_emoji_tokens(raw))
        custom_emojis = Counter(self._extract_custom_emoji_tokens(raw))
        emojis = Counter(unicode_emojis)
        emojis.update(custom_emojis)

        function_words = Counter(w for w in words if w in FUNCTION_WORDS)
        common_words = Counter(words)

        upper_words = sum(1 for w in re.findall(r"\b\w+\b", raw) if len(w) > 1 and w.isupper())
        digit_tokens = sum(1 for w in words if any(ch.isdigit() for ch in w))
        url_count = len(URL_RE.findall(raw))
        short_words = sum(1 for w in words if len(w) <= 3)
        long_words = sum(1 for w in words if len(w) >= 8)
        contraction_tokens = sum(1 for w in words if "'" in w)
        repeated_char_tokens = len(REPEATED_CHARS_RE.findall(raw))
        question_marks = raw.count("?")
        exclamation_marks = raw.count("!")
        ellipsis_count = raw.count("...")
        avg_word_len = (sum(len(w) for w in words) / word_count) if word_count else 0.0
        avg_sentence_len = (word_count / sentence_count) if sentence_count else 0.0

        compact = re.sub(r"\s+", "", norm.lower())[:4000]
        char_ngrams = Counter()
        for i in range(max(len(compact) - 2, 0)):
            gram = compact[i:i + 3]
            if len(gram) == 3:
                char_ngrams[gram] += 1

        def _mattr(tokens: List[str], window: int = MATTR_WINDOW) -> float:
            if not tokens:
                return 0.0
            if len(tokens) <= 1:
                return len(set(tokens)) / max(len(tokens), 1)
            window = max(2, min(window, len(tokens)))
            if len(tokens) <= window:
                return len(set(tokens)) / len(tokens)

            counter = Counter(tokens[:window])
            total_windows = len(tokens) - window + 1
            total = len(counter) / window

            for i in range(1, total_windows):
                left = tokens[i - 1]
                right = tokens[i + window - 1]

                counter[left] -= 1
                if counter[left] <= 0:
                    del counter[left]
                counter[right] += 1
                total += len(counter) / window

            return total / total_windows

        mattr = _mattr(words)
        ttr_legacy = (unique_words_count / word_count) if word_count else 0.0
        hapax_ratio = (hapax_count / word_count) if word_count else 0.0
        digit_ratio = (digit_tokens / word_count) if word_count else 0.0
        short_word_ratio = (short_words / word_count) if word_count else 0.0
        long_word_ratio = (long_words / word_count) if word_count else 0.0
        contraction_ratio = (contraction_tokens / word_count) if word_count else 0.0
        repeated_ratio = repeated_char_tokens / max(char_count, 1)
        url_ratio = url_count / max(sentence_count, 1)
        question_rate = question_marks / max(sentence_count, 1)
        exclamation_rate = exclamation_marks / max(sentence_count, 1)
        ellipsis_rate = ellipsis_count / max(sentence_count, 1)
        emoji_density = sum(emojis.values()) / max(word_count, 1)
        unicode_emoji_density = sum(unicode_emojis.values()) / max(word_count, 1)
        custom_emoji_density = sum(custom_emojis.values()) / max(word_count, 1)
        uppercase_ratio = upper_words / max(word_count, 1)
        function_word_ratio = sum(function_words.values()) / max(word_count, 1)
        content_word_ratio = max(1.0 - function_word_ratio, 0.0)
        punctuation_variety = (len([k for k, v in punctuation.items() if v > 0]) / max(sum(punctuation.values()), 1))

        return {
            "word_count": word_count,
            "char_count": char_count,
            "sentence_count": sentence_count,
            "unique_words_count": unique_words_count,
            "hapax_count": hapax_count,
            "mattr": mattr,
            "ttr": ttr_legacy,
            "hapax_ratio": hapax_ratio,
            "emoji_count": sum(emojis.values()),
            "unicode_emoji_count": sum(unicode_emojis.values()),
            "custom_emoji_count": sum(custom_emojis.values()),
            "upper_words": upper_words,
            "digit_tokens": digit_tokens,
            "url_count": url_count,
            "short_words": short_words,
            "long_words": long_words,
            "contraction_tokens": contraction_tokens,
            "repeated_char_tokens": repeated_char_tokens,
            "question_marks": question_marks,
            "exclamation_marks": exclamation_marks,
            "ellipsis_count": ellipsis_count,
            "avg_word_len": avg_word_len,
            "avg_sentence_len": avg_sentence_len,
            "digit_ratio": digit_ratio,
            "short_word_ratio": short_word_ratio,
            "long_word_ratio": long_word_ratio,
            "contraction_ratio": contraction_ratio,
            "repeated_ratio": repeated_ratio,
            "url_ratio": url_ratio,
            "question_rate": question_rate,
            "exclamation_rate": exclamation_rate,
            "ellipsis_rate": ellipsis_rate,
            "emoji_density": emoji_density,
            "unicode_emoji_density": unicode_emoji_density,
            "custom_emoji_density": custom_emoji_density,
            "uppercase_ratio": uppercase_ratio,
            "function_word_ratio": function_word_ratio,
            "content_word_ratio": content_word_ratio,
            "punctuation_variety": punctuation_variety,
            "function_words": function_words,
            "punctuation": punctuation,
            "emojis": emojis,
            "unicode_emojis": unicode_emojis,
            "custom_emojis": custom_emojis,
            "char_ngrams": Counter(dict(char_ngrams.most_common(120))),
            "common_words": Counter(dict(common_words.most_common(150))),
        }

    def _merge_counter_json(self, existing_json: str, delta: Counter, keep_top: int) -> str:
        data = Counter(json.loads(existing_json or "{}"))
        data.update(delta)
        data = Counter(dict(data.most_common(keep_top)))
        return json.dumps(dict(data), ensure_ascii=False)

    async def _update_global_stats_locked(self, new_profile: bool, new_function_words: List[str]):
        if self.db is None:
            return

        now = int(time.time())
        cur = await self.db.execute(
            "SELECT user_count, function_words_df_json FROM global_stats WHERE id = 1"
        )
        row = await cur.fetchone()
        await cur.close()

        if row is None:
            user_count = 1 if new_profile else 0
            df = Counter()
        else:
            user_count = int(row[0]) + (1 if new_profile else 0)
            try:
                df = Counter(json.loads(row[1] or "{}"))
            except Exception:
                df = Counter()

        for w in new_function_words:
            df[w] += 1

        await self.db.execute(
            """
            INSERT OR REPLACE INTO global_stats
            (id, user_count, function_words_df_json, updated_at)
            VALUES (1, ?, ?, ?)
            """,
            (user_count, json.dumps(dict(df), ensure_ascii=False), now),
        )

    async def _apply_message_metrics(
            self,
            user_id: str,
            metrics: Dict[str, Any],
            commit: bool = False,
        ) -> bool:
        if self.db is None or self.db_lock is None:
            return False

        now = int(time.time())
        # user_id + data columns + updated_at
        insert_placeholders = ", ".join(["?"] * (len(PROFILE_COLUMNS) + 2))

        async with self.db_lock:
            try:
                async with self._public_stats_lock:
                    self._pending_public_message_count += 1

                cur = await self.db.execute(
                    f"""
                    SELECT {PROFILE_COLUMNS_SQL}
                    FROM user_profiles
                    WHERE user_id = ?
                    """,
                    (user_id,),
                )
                row = await cur.fetchone()
                await cur.close()

                mattr_value = float(metrics["mattr"])
                ttr_value = float(metrics.get("ttr", mattr_value))

                if row is None:
                    await self.db.execute(
                        f"""
                        INSERT INTO user_profiles (
                            user_id, {PROFILE_COLUMNS_SQL}, updated_at
                        ) VALUES ({insert_placeholders})
                        """,
                        (
                            user_id,
                            1,
                            int(metrics["char_count"]),
                            int(metrics["word_count"]),
                            int(metrics["sentence_count"]),
                            float(metrics["word_count"]),
                            float(metrics["word_count"] ** 2),
                            float(metrics["avg_sentence_len"]),
                            float(metrics["avg_sentence_len"] ** 2),
                            float(metrics["unique_words_count"]),
                            float(ttr_value),
                            mattr_value,
                            float(mattr_value ** 2),
                            1,
                            float(metrics["hapax_ratio"]),
                            int(metrics["upper_words"]),
                            int(metrics["emoji_count"]),
                            int(metrics["unicode_emoji_count"]),
                            int(metrics["custom_emoji_count"]),
                            int(metrics["digit_tokens"]),
                            int(metrics["url_count"]),
                            int(metrics["short_words"]),
                            int(metrics["long_words"]),
                            int(metrics["contraction_tokens"]),
                            int(metrics["repeated_char_tokens"]),
                            int(metrics["question_marks"]),
                            int(metrics["exclamation_marks"]),
                            int(metrics["ellipsis_count"]),
                            float(metrics["avg_word_len"]),
                            float(metrics["avg_sentence_len"]),
                            json.dumps(dict(metrics["function_words"]), ensure_ascii=False),
                            json.dumps(dict(metrics["punctuation"]), ensure_ascii=False),
                            json.dumps(dict(metrics["emojis"]), ensure_ascii=False),
                            json.dumps(dict(metrics["unicode_emojis"]), ensure_ascii=False),
                            json.dumps(dict(metrics["custom_emojis"]), ensure_ascii=False),
                            json.dumps(dict(metrics["char_ngrams"]), ensure_ascii=False),
                            json.dumps(dict(metrics["common_words"]), ensure_ascii=False),
                            now,
                        ),
                    )

                    await self._update_global_stats_locked(
                        True,
                        list(metrics["function_words"].keys()),
                    )

                else:
                    (
                        total_messages,
                        total_chars,
                        total_words,
                        total_sentences,
                        mw_sum,
                        mw_sumsq,
                        ms_sum,
                        ms_sumsq,
                        unique_words_sum,
                        ttr_sum,
                        mattr_sum,
                        mattr_sumsq,
                        mattr_count,
                        hapax_ratio_sum,
                        uppercase_words,
                        emoji_count,
                        unicode_emoji_count,
                        custom_emoji_count,
                        digit_tokens,
                        url_count,
                        short_words,
                        long_words,
                        contraction_tokens,
                        repeated_char_tokens,
                        question_marks,
                        exclamation_marks,
                        ellipsis_count,
                        avg_word_len_sum,
                        avg_sentence_len_sum,
                        function_json,
                        punct_json,
                        emoji_json,
                        unicode_emoji_json,
                        custom_emoji_json,
                        char_json,
                        common_json,
                    ) = row

                    try:
                        existing_function_words = set(
                            json.loads(function_json or "{}").keys()
                        )
                    except Exception:
                        existing_function_words = set()

                    new_function_words = [
                        w
                        for w in metrics["function_words"].keys()
                        if w not in existing_function_words
                    ]

                    if new_function_words:
                        await self._update_global_stats_locked(
                            False,
                            new_function_words,
                        )

                    await self.db.execute(
                        """
                        UPDATE user_profiles
                        SET total_messages = ?,
                            total_chars = ?,
                            total_words = ?,
                            total_sentences = ?,
                            message_word_count_sum = ?,
                            message_word_count_sumsq = ?,
                            message_sentence_len_sum = ?,
                            message_sentence_len_sumsq = ?,
                            unique_words_sum = ?,
                            ttr_sum = ?,
                            mattr_sum = ?,
                            mattr_sumsq = ?,
                            mattr_count = ?,
                            hapax_ratio_sum = ?,
                            uppercase_words = ?,
                            emoji_count = ?,
                            unicode_emoji_count = ?,
                            custom_emoji_count = ?,
                            digit_tokens = ?,
                            url_count = ?,
                            short_words = ?,
                            long_words = ?,
                            contraction_tokens = ?,
                            repeated_char_tokens = ?,
                            question_marks = ?,
                            exclamation_marks = ?,
                            ellipsis_count = ?,
                            avg_word_len_sum = ?,
                            avg_sentence_len_sum = ?,
                            function_words_json = ?,
                            punctuation_json = ?,
                            emoji_json = ?,
                            unicode_emojis_json = ?,
                            custom_emojis_json = ?,
                            char_ngrams_json = ?,
                            common_words_json = ?,
                            updated_at = ?
                        WHERE user_id = ?
                        """,
                        (
                          total_messages + 1,
                          total_chars + int(metrics["char_count"]),
                          total_words + int(metrics["word_count"]),
                          total_sentences + int(metrics["sentence_count"]),
                          mw_sum + float(metrics["word_count"]),
                          mw_sumsq + float(metrics["word_count"] ** 2),
                          ms_sum + float(metrics["avg_sentence_len"]),
                          ms_sumsq + float(metrics["avg_sentence_len"] ** 2),
                          unique_words_sum + float(metrics["unique_words_count"]),
                          ttr_sum + float(ttr_value),
                          mattr_sum + mattr_value,
                          mattr_sumsq + float(mattr_value ** 2),
                          int(mattr_count) + 1,
                          hapax_ratio_sum + float(metrics["hapax_ratio"]),
                          uppercase_words + int(metrics["upper_words"]),
                          emoji_count + int(metrics["emoji_count"]),
                          unicode_emoji_count + int(metrics["unicode_emoji_count"]),
                          custom_emoji_count + int(metrics["custom_emoji_count"]),
                          digit_tokens + int(metrics["digit_tokens"]),
                          url_count + int(metrics["url_count"]),
                          short_words + int(metrics["short_words"]),
                          long_words + int(metrics["long_words"]),
                          contraction_tokens + int(metrics["contraction_tokens"]),
                          repeated_char_tokens + int(metrics["repeated_char_tokens"]),
                          question_marks + int(metrics["question_marks"]),
                          exclamation_marks + int(metrics["exclamation_marks"]),
                          ellipsis_count + int(metrics["ellipsis_count"]),
                          avg_word_len_sum + float(metrics["avg_word_len"]),
                          avg_sentence_len_sum + float(metrics["avg_sentence_len"]),
                          self._merge_counter_json(function_json, metrics["function_words"], keep_top=75),
                          self._merge_counter_json(punct_json, metrics["punctuation"], keep_top=30),
                          self._merge_counter_json(emoji_json, metrics["emojis"], keep_top=60),
                          self._merge_counter_json(unicode_emoji_json, metrics["unicode_emojis"], keep_top=60),
                          self._merge_counter_json(custom_emoji_json, metrics["custom_emojis"], keep_top=60),
                          self._merge_counter_json(char_json, metrics["char_ngrams"], keep_top=120),
                          self._merge_counter_json(common_json, metrics["common_words"], keep_top=150),
                          now,
                          user_id,
                        ),
                    )

            except Exception as e:
                print(f"[Metrics Processing Error] {e}")
                try:
                    await self.db.rollback()
                except Exception:
                    pass
                raise

            if commit:
                await self.db.commit()
                await self._flush_public_message_count()

            return True

    @staticmethod
    def _looks_like_bot_command(content: str) -> bool:
        text = (content or "").lstrip()
        if len(text) < 2:
            return False

        # Single symbol immediately followed by a letter: "!ban", ".play".
        if text[0] in BOT_COMMAND_PREFIXES and text[1].isalpha():
            return True

        # Literal word / letter+symbol prefixes: "pls beg", "m!play".
        low = text.lower()
        for prefix in STRING_COMMAND_PREFIXES:
            pl = (prefix or "").lower()
            if len(pl) < 2 or not low.startswith(pl):
                continue
            # All-letters prefixes ("pls") only count at a word boundary, so
            # they can't fire inside a longer word ("plsomething"). Prefixes
            # ending in a space or symbol ("pls ", "m!") are already delimited.
            if pl[-1].isalpha():
                rest = low[len(pl):]
                if rest and rest[0].isalnum():
                    continue
            return True

        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return
        try:
            if message.author.bot or not message.content:
                return

            # Don't fold command invocations into linguistic profiles — the
            # prefix, command words, IDs and mentions are noise, not natural
            # style. First a cheap heuristic that also catches other bots'
            # commands (e.g. "?play", "$bal"); then an authoritative check for
            # THIS bot's own commands via get_context, which resolves its real
            # prefix(es) even if they aren't in BOT_COMMAND_PREFIXES.
            if self._looks_like_bot_command(message.content):
                return

            ctx = await self.bot.get_context(message)
            if ctx.valid:
                return

            metrics = self._extract_message_metrics(message.content)
            await self._apply_message_metrics(
                str(message.author.id),
                metrics,
                commit=True,
            )
        except Exception as e:
            print(f"[Stream Error] {e}")

    async def get_profile(self, user_id: str) -> Optional[dict]:
        row = await self._db_fetchone(
            f"""
            SELECT {PROFILE_COLUMNS_SQL}
            FROM user_profiles
            WHERE user_id = ?
            """,
            (user_id,),
        )
        if row is None:
            return None

        (
            total_messages,
            total_chars,
            total_words,
            total_sentences,
            mw_sum,
            mw_sumsq,
            ms_sum,
            ms_sumsq,
            unique_words_sum,
            ttr_sum,
            mattr_sum,
            mattr_sumsq,
            mattr_count,
            hapax_ratio_sum,
            uppercase_words,
            emoji_count,
            unicode_emoji_count,
            custom_emoji_count,
            digit_tokens,
            url_count,
            short_words,
            long_words,
            contraction_tokens,
            repeated_char_tokens,
            question_marks,
            exclamation_marks,
            ellipsis_count,
            avg_word_len_sum,
            avg_sentence_len_sum,
            function_json,
            punct_json,
            emoji_json,
            unicode_emoji_json,
            custom_emoji_json,
            char_json,
            common_json,
        ) = row

        denom = max(total_messages, 1)
        word_denom = max(total_words, 1)

        burstiness = self._cv_from_sums(total_messages, float(mw_sum), float(mw_sumsq))
        sentence_variance = self._cv_from_sums(total_messages, float(ms_sum), float(ms_sumsq))
        function_counter = Counter(json.loads(function_json or "{}"))
        punctuation_counter = Counter(json.loads(punct_json or "{}"))
        emoji_counter = Counter(json.loads(emoji_json or "{}"))
        unicode_emoji_counter = Counter(json.loads(unicode_emoji_json or "{}"))
        custom_emoji_counter = Counter(json.loads(custom_emoji_json or "{}"))

        function_word_total = sum(function_counter.values())
        punctuation_total = sum(punctuation_counter.values())
        unicode_total = sum(unicode_emoji_counter.values())
        custom_total = sum(custom_emoji_counter.values())

        mattr_denom = max(int(mattr_count), 1)
        if int(mattr_count) > 0:
            avg_mattr = float(mattr_sum) / mattr_denom
            mattr_variance = max((float(mattr_sumsq) / mattr_denom) - (avg_mattr * avg_mattr), 0.0) if mattr_denom > 1 else 0.0
        else:
            avg_mattr = float(ttr_sum) / denom
            mattr_variance = 0.0

        return {
            "total_messages": total_messages,
            "total_chars": total_chars,
            "total_words": total_words,
            "total_sentences": total_sentences,
            "avg_unique_words": unique_words_sum / denom,
            "avg_mattr": avg_mattr,
            "avg_ttr": avg_mattr,
            "mattr_variance": mattr_variance,
            "avg_hapax_ratio": hapax_ratio_sum / denom,
            "uppercase_words": uppercase_words,
            "emoji_count": emoji_count,
            "unicode_emoji_count": unicode_emoji_count,
            "custom_emoji_count": custom_emoji_count,
            "digit_tokens": digit_tokens,
            "url_count": url_count,
            "short_words": short_words,
            "long_words": long_words,
            "contraction_tokens": contraction_tokens,
            "repeated_char_tokens": repeated_char_tokens,
            "question_marks": question_marks,
            "exclamation_marks": exclamation_marks,
            "ellipsis_count": ellipsis_count,
            "avg_word_len": avg_word_len_sum / denom,
            "avg_sentence_len": avg_sentence_len_sum / denom,
            "uppercase_ratio": uppercase_words / word_denom,
            "emoji_density": emoji_count / denom,
            "unicode_emoji_density": unicode_total / word_denom,
            "custom_emoji_density": custom_total / word_denom,
            "digit_ratio": digit_tokens / word_denom,
            "url_ratio": url_count / denom,
            "short_word_ratio": short_words / word_denom,
            "long_word_ratio": long_words / word_denom,
            "contraction_ratio": contraction_tokens / word_denom,
            "repeated_ratio": repeated_char_tokens / denom,
            "question_rate": question_marks / denom,
            "exclamation_rate": exclamation_marks / denom,
            "ellipsis_rate": ellipsis_count / denom,
            "burstiness": burstiness,
            "sentence_variance": sentence_variance,
            "stopword_entropy": self._entropy_normalized(function_counter),
            "function_word_ratio": function_word_total / word_denom,
            "content_word_ratio": max(1.0 - (function_word_total / word_denom), 0.0),
            "punctuation_variety": (len([k for k, v in punctuation_counter.items() if v > 0]) / max(punctuation_total, 1)),
            "function_words": function_counter,
            "punctuation": punctuation_counter,
            "emojis": emoji_counter,
            "unicode_emojis": unicode_emoji_counter,
            "custom_emojis": custom_emoji_counter,
            "char_ngrams": Counter(json.loads(char_json or "{}")),
            "common_words": Counter(json.loads(common_json or "{}")),
        }

    def _pick_shared_markers(self, p1: dict, p2: dict) -> List[str]:
        markers = []

        def add_top_shared(title: str, c1: Counter, c2: Counter, limit: int = 5):
            shared = (c1 & c2).most_common(limit)
            if shared:
                items = ", ".join(f"`{k}`" for k, _ in shared[:limit])
                markers.append(f"{title}: {items}")

        add_top_shared("Shared function words", p1["function_words"], p2["function_words"])
        add_top_shared("Shared punctuation habits", p1["punctuation"], p2["punctuation"])
        add_top_shared("Shared common words", p1["common_words"], p2["common_words"])
        add_top_shared("Shared character n-grams", p1["char_ngrams"], p2["char_ngrams"])
        add_top_shared("Shared emojis", p1["emojis"], p2["emojis"])
        add_top_shared("Shared unicode emojis", p1["unicode_emojis"], p2["unicode_emojis"])
        add_top_shared("Shared custom emojis", p1["custom_emojis"], p2["custom_emojis"])
        return markers[:5]

    def _weighted_counter_similarity(self, a: Counter, b: Counter) -> float:
        keys = set(a) | set(b)
        if not keys:
            return 0.0
        num = sum(min(float(a.get(k, 0.0)), float(b.get(k, 0.0))) for k in keys)
        den = sum(max(float(a.get(k, 0.0)), float(b.get(k, 0.0))) for k in keys)
        return (num / den) if den else 0.0

    def _set_jaccard(self, a: Counter, b: Counter, limit: int) -> float:
        if limit <= 0:
            return 0.0
        sa = {k for k, _ in a.most_common(limit)}
        sb = {k for k, _ in b.most_common(limit)}
        union = sa | sb
        if not union:
            return 0.0
        return len(sa & sb) / len(union)

    @staticmethod
    def _normalized_diff_similarity(x: float, y: float, scale: float) -> float:
        if scale <= 0:
            return 0.0
        diff = abs(x - y)
        return max(0.0, 1.0 - min(diff / scale, 1.0))

    @staticmethod
    def _sigmoid(x: float) -> float:
        if x >= 0:
            z = math.exp(-x)
            return 1.0 / (1.0 + z)
        z = math.exp(x)
        return z / (1.0 + z)

    async def _get_global_stats(self) -> Dict[str, Any]:
        row = await self._db_fetchone(
            "SELECT user_count, function_words_df_json FROM global_stats WHERE id = 1"
        )
        if row is None:
            return {"user_count": 0, "function_words_df": Counter()}
        user_count, df_json = row
        try:
            df = Counter(json.loads(df_json or "{}"))
        except Exception:
            df = Counter()
        return {"user_count": int(user_count), "function_words_df": df}

    def _tfidf_vector(self, function_words: Counter, total_user_count: int, df_counts: Counter) -> Counter:
        vec = {}
        user_count = max(total_user_count, 1)
        for w, tf in function_words.items():
            df = int(df_counts.get(w, 0))
            idf = math.log((user_count + 1) / (df + 1)) + 1.0
            vec[w] = float(tf) * idf
        return Counter(vec)

    async def _profile_similarity(self, p1: dict, p2: dict) -> Tuple[float, float, float, Dict[str, float], List[str]]:
        global_stats = await self._get_global_stats()
        user_count = global_stats["user_count"]
        global_df = global_stats["function_words_df"]

        tfidf_1 = self._tfidf_vector(p1["function_words"], user_count, global_df)
        tfidf_2 = self._tfidf_vector(p2["function_words"], user_count, global_df)

        raw_scores: Dict[str, float] = {}

        raw_scores["weighted_common_words"] = self._weighted_counter_similarity(p1["common_words"], p2["common_words"])
        raw_scores["weighted_function_words"] = self._weighted_counter_similarity(p1["function_words"], p2["function_words"])
        raw_scores["weighted_punctuation"] = self._weighted_counter_similarity(p1["punctuation"], p2["punctuation"])
        raw_scores["weighted_unicode_emojis"] = self._weighted_counter_similarity(p1["unicode_emojis"], p2["unicode_emojis"])
        raw_scores["weighted_custom_emojis"] = self._weighted_counter_similarity(p1["custom_emojis"], p2["custom_emojis"])
        raw_scores["weighted_char_ngrams"] = self._weighted_counter_similarity(p1["char_ngrams"], p2["char_ngrams"])

        raw_scores["jaccard_common_words"] = self._set_jaccard(p1["common_words"], p2["common_words"], 100)
        raw_scores["jaccard_function_words"] = self._set_jaccard(p1["function_words"], p2["function_words"], 40)
        raw_scores["jaccard_punctuation"] = self._set_jaccard(p1["punctuation"], p2["punctuation"], 20)
        raw_scores["jaccard_unicode_emojis"] = self._set_jaccard(p1["unicode_emojis"], p2["unicode_emojis"], 40)
        raw_scores["jaccard_custom_emojis"] = self._set_jaccard(p1["custom_emojis"], p2["custom_emojis"], 40)
        raw_scores["jaccard_char_ngrams"] = self._set_jaccard(p1["char_ngrams"], p2["char_ngrams"], 120)

        raw_scores["tfidf_function_words"] = self._weighted_counter_similarity(tfidf_1, tfidf_2)

        raw_scores["burstiness"] = self._normalized_diff_similarity(p1["burstiness"], p2["burstiness"], scale=1.5)
        raw_scores["sentence_variance"] = self._normalized_diff_similarity(p1["sentence_variance"], p2["sentence_variance"], scale=1.5)
        raw_scores["stopword_entropy"] = self._normalized_diff_similarity(p1["stopword_entropy"], p2["stopword_entropy"], scale=1.0)

        raw_scores["avg_word_len"] = self._normalized_diff_similarity(p1["avg_word_len"], p2["avg_word_len"], scale=4.0)
        raw_scores["avg_sentence_len"] = self._normalized_diff_similarity(p1["avg_sentence_len"], p2["avg_sentence_len"], scale=20.0)
        raw_scores["unique_words"] = self._normalized_diff_similarity(p1["avg_unique_words"], p2["avg_unique_words"], scale=25.0)
        raw_scores["mattr"] = self._normalized_diff_similarity(p1["avg_mattr"], p2["avg_mattr"], scale=0.25)
        raw_scores["hapax_ratio"] = self._normalized_diff_similarity(p1["avg_hapax_ratio"], p2["avg_hapax_ratio"], scale=0.25)

        raw_scores["uppercase_ratio"] = self._normalized_diff_similarity(p1["uppercase_ratio"], p2["uppercase_ratio"], scale=0.25)
        raw_scores["emoji_density"] = self._normalized_diff_similarity(p1["emoji_density"], p2["emoji_density"], scale=1.5)
        raw_scores["unicode_emoji_density"] = self._normalized_diff_similarity(p1["unicode_emoji_density"], p2["unicode_emoji_density"], scale=1.0)
        raw_scores["custom_emoji_density"] = self._normalized_diff_similarity(p1["custom_emoji_density"], p2["custom_emoji_density"], scale=1.0)
        raw_scores["digit_ratio"] = self._normalized_diff_similarity(p1["digit_ratio"], p2["digit_ratio"], scale=0.2)
        raw_scores["url_ratio"] = self._normalized_diff_similarity(p1["url_ratio"], p2["url_ratio"], scale=0.5)
        raw_scores["short_word_ratio"] = self._normalized_diff_similarity(p1["short_word_ratio"], p2["short_word_ratio"], scale=0.35)
        raw_scores["long_word_ratio"] = self._normalized_diff_similarity(p1["long_word_ratio"], p2["long_word_ratio"], scale=0.35)
        raw_scores["contraction_ratio"] = self._normalized_diff_similarity(p1["contraction_ratio"], p2["contraction_ratio"], scale=0.2)
        raw_scores["repeated_ratio"] = self._normalized_diff_similarity(p1["repeated_ratio"], p2["repeated_ratio"], scale=0.08)
        raw_scores["question_rate"] = self._normalized_diff_similarity(p1["question_rate"], p2["question_rate"], scale=1.5)
        raw_scores["exclamation_rate"] = self._normalized_diff_similarity(p1["exclamation_rate"], p2["exclamation_rate"], scale=1.5)
        raw_scores["ellipsis_rate"] = self._normalized_diff_similarity(p1["ellipsis_rate"], p2["ellipsis_rate"], scale=1.0)
        raw_scores["function_word_ratio"] = self._normalized_diff_similarity(p1["function_word_ratio"], p2["function_word_ratio"], scale=0.30)
        raw_scores["content_word_ratio"] = self._normalized_diff_similarity(p1["content_word_ratio"], p2["content_word_ratio"], scale=0.30)
        raw_scores["punctuation_variety"] = self._normalized_diff_similarity(p1["punctuation_variety"], p2["punctuation_variety"], scale=0.35)
        raw_scores["mattr_stability"] = self._normalized_diff_similarity(
            1.0 / (1.0 + p1.get("mattr_variance", 0.0)),
            1.0 / (1.0 + p2.get("mattr_variance", 0.0)),
            scale=0.75,
        )

        families = {
            "lexical": {
                "weighted_common_words": 0.18,
                "weighted_function_words": 0.20,
                "jaccard_common_words": 0.12,
                "jaccard_function_words": 0.18,
                "tfidf_function_words": 0.32,
            },
            "character": {
                "weighted_char_ngrams": 0.58,
                "jaccard_char_ngrams": 0.42,
            },
            "punctuation": {
                "weighted_punctuation": 0.38,
                "jaccard_punctuation": 0.22,
                "question_rate": 0.12,
                "exclamation_rate": 0.12,
                "ellipsis_rate": 0.08,
                "punctuation_variety": 0.08,
            },
            "emoji": {
                "weighted_unicode_emojis": 0.18,
                "weighted_custom_emojis": 0.18,
                "jaccard_unicode_emojis": 0.16,
                "jaccard_custom_emojis": 0.16,
                "unicode_emoji_density": 0.16,
                "custom_emoji_density": 0.16,
            },
            "rhythm": {
                "burstiness": 0.28,
                "sentence_variance": 0.22,
                "avg_sentence_len": 0.18,
                "avg_word_len": 0.16,
                "contraction_ratio": 0.08,
                "mattr_stability": 0.08,
            },
            "richness": {
                "mattr": 0.30,
                "hapax_ratio": 0.20,
                "unique_words": 0.16,
                "stopword_entropy": 0.18,
                "short_word_ratio": 0.08,
                "long_word_ratio": 0.08,
            },
            "surface": {
                "uppercase_ratio": 0.24,
                "digit_ratio": 0.18,
                "url_ratio": 0.18,
                "repeated_ratio": 0.22,
                "function_word_ratio": 0.09,
                "content_word_ratio": 0.09,
            },
        }

        family_weights = {
            "lexical": 0.28,
            "character": 0.18,
            "punctuation": 0.10,
            "emoji": 0.08,
            "rhythm": 0.18,
            "richness": 0.14,
            "surface": 0.04,
        }

        family_scores: Dict[str, float] = {}
        for family_name, components in families.items():
            total = sum(components.values()) or 1.0
            family_scores[family_name] = sum(raw_scores[name] * weight for name, weight in components.items()) / total

        total_family_weight = sum(family_weights.values()) or 1.0
        raw_similarity = sum(family_scores[name] * family_weights[name] for name in family_weights) / total_family_weight

        family_values = list(family_scores.values())
        family_mean = raw_similarity
        family_dispersion = math.sqrt(
            sum((value - family_mean) ** 2 for value in family_values) / max(len(family_values), 1)
        ) if family_values else 0.0
        high_families = sum(1 for value in family_values if value >= 0.80)
        low_families = sum(1 for value in family_values if value <= 0.30)

        contradiction_penalty = min(
            1.0,
            (family_dispersion * 1.35)
            + (0.12 if high_families and low_families else 0.0)
            + max(0.0, abs(family_scores["lexical"] - family_scores["rhythm"]) - 0.18) * 0.80
            + max(0.0, abs(family_scores["character"] - family_scores["emoji"]) - 0.20) * 0.50
        )

        similarity_score = max(0.0, min(raw_similarity - (contradiction_penalty * 0.06), 1.0))

        min_messages = min(p1["total_messages"], p2["total_messages"])
        max_messages = max(p1["total_messages"], p2["total_messages"], 1)
        volume_support = max(0.0, min(min_messages / 250.0, 1.0))
        balance_support = max(0.0, min(min_messages / max_messages, 1.0))
        coherence_support = max(0.0, min(1.0 - min(family_dispersion * 1.55, 0.75), 1.0))

        confidence = max(
            0.0,
            min(
                (0.45 * volume_support + 0.20 * balance_support + 0.35 * coherence_support) * 100.0,
                100.0,
            ),
        )

        logit = (
            (similarity_score - 0.50) * 5.8
            + (confidence / 100.0 - 0.50) * 1.6
            - contradiction_penalty * 2.0
            - (1.0 - balance_support) * 0.35
        )
        same_authorship_probability = self._sigmoid(logit) * 100.0

        markers = self._pick_shared_markers(p1, p2)

        breakdown = {k: round(v * 100.0, 2) for k, v in raw_scores.items()}
        breakdown["family_dispersion"] = round(family_dispersion * 100.0, 2)
        breakdown["contradiction_penalty"] = round(contradiction_penalty * 100.0, 2)
        breakdown["lexical_family"] = round(family_scores["lexical"] * 100.0, 2)
        breakdown["character_family"] = round(family_scores["character"] * 100.0, 2)
        breakdown["punctuation_family"] = round(family_scores["punctuation"] * 100.0, 2)
        breakdown["emoji_family"] = round(family_scores["emoji"] * 100.0, 2)
        breakdown["rhythm_family"] = round(family_scores["rhythm"] * 100.0, 2)
        breakdown["richness_family"] = round(family_scores["richness"] * 100.0, 2)
        breakdown["surface_family"] = round(family_scores["surface"] * 100.0, 2)

        return similarity_score, confidence, same_authorship_probability, breakdown, markers

    def _confidence_tier(self, confidence: float) -> str:
        if confidence < 40:
            return "low"
        if confidence < 65:
            return "moderate"
        if confidence < 80:
            return "high"
        return "very high"

    def _format_report(self, user1: str, user2: str, result: dict) -> Tuple[str, str]:
        similarity = result.get("similarity_score", result.get("score", 0))
        probability = result.get("same_authorship_probability", 0)
        summary_lines = [
            "🧠 Linguistic Comparison Report:",
            f"Users: {user1} vs {user2}",
            f"Mode: {result['mode']}",
            f"Similarity score: {similarity}%",
            f"Same-authorship probability: {probability}%",
            f"Evidence confidence: {result['confidence']}% ({result['tier']})",
            f"Messages analyzed: {result['u1_count']} / {result['u2_count']}",
        ]

        if result.get("note"):
            summary_lines.append(f"Note: {result['note']}")

        markers = result.get("markers") or []
        if markers:
            summary_lines.extend(["", "Shared markers:"])
            for marker in markers:
                summary_lines.append(f"• {marker}")

        summary_report = "\n".join(summary_lines)

        breakdown_lines = []
        breakdown = result.get("breakdown") or {}
        if breakdown:
            breakdown_lines.extend([
                "📊 Feature Breakdown:",
                f"• Weighted common-word overlap: {breakdown.get('weighted_common_words', 0)}%",
                f"• Weighted function-word overlap: {breakdown.get('weighted_function_words', 0)}%",
                f"• Weighted punctuation overlap: {breakdown.get('weighted_punctuation', 0)}%",
                f"• Weighted Unicode emoji overlap: {breakdown.get('weighted_unicode_emojis', 0)}%",
                f"• Weighted custom emoji overlap: {breakdown.get('weighted_custom_emojis', 0)}%",
                f"• Weighted character n-gram overlap: {breakdown.get('weighted_char_ngrams', 0)}%",
                f"• Jaccard common words: {breakdown.get('jaccard_common_words', 0)}%",
                f"• Jaccard function words: {breakdown.get('jaccard_function_words', 0)}%",
                f"• Jaccard punctuation: {breakdown.get('jaccard_punctuation', 0)}%",
                f"• Jaccard Unicode emojis: {breakdown.get('jaccard_unicode_emojis', 0)}%",
                f"• Jaccard custom emojis: {breakdown.get('jaccard_custom_emojis', 0)}%",
                f"• Jaccard character n-grams: {breakdown.get('jaccard_char_ngrams', 0)}%",
                f"• TF-IDF function-word profile: {breakdown.get('tfidf_function_words', 0)}%",
                f"• Burstiness match: {breakdown.get('burstiness', 0)}%",
                f"• Sentence variance match: {breakdown.get('sentence_variance', 0)}%",
                f"• Stopword entropy match: {breakdown.get('stopword_entropy', 0)}%",
                f"• Avg word length match: {breakdown.get('avg_word_len', 0)}%",
                f"• Avg sentence length match: {breakdown.get('avg_sentence_len', 0)}%",
                f"• Vocabulary size match: {breakdown.get('unique_words', 0)}%",
                f"• MATTR match: {breakdown.get('mattr', 0)}%",
                f"• Hapax ratio match: {breakdown.get('hapax_ratio', 0)}%",
                f"• Capitalization match: {breakdown.get('uppercase_ratio', 0)}%",
                f"• Unicode emoji density match: {breakdown.get('unicode_emoji_density', 0)}%",
                f"• Custom emoji density match: {breakdown.get('custom_emoji_density', 0)}%",
                f"• Digit usage match: {breakdown.get('digit_ratio', 0)}%",
                f"• URL usage match: {breakdown.get('url_ratio', 0)}%",
                f"• Short-word ratio match: {breakdown.get('short_word_ratio', 0)}%",
                f"• Long-word ratio match: {breakdown.get('long_word_ratio', 0)}%",
                f"• Contraction ratio match: {breakdown.get('contraction_ratio', 0)}%",
                f"• Repeated-character ratio match: {breakdown.get('repeated_ratio', 0)}%",
                f"• Question-rate match: {breakdown.get('question_rate', 0)}%",
                f"• Exclamation-rate match: {breakdown.get('exclamation_rate', 0)}%",
                f"• Ellipsis-rate match: {breakdown.get('ellipsis_rate', 0)}%",
                f"• Function-word share match: {breakdown.get('function_word_ratio', 0)}%",
                f"• Content-word share match: {breakdown.get('content_word_ratio', 0)}%",
                f"• Punctuation variety match: {breakdown.get('punctuation_variety', 0)}%",
                f"• Family dispersion: {breakdown.get('family_dispersion', 0)}%",
                f"• Contradiction penalty: {breakdown.get('contradiction_penalty', 0)}%",
                f"• Lexical family score: {breakdown.get('lexical_family', 0)}%",
                f"• Character family score: {breakdown.get('character_family', 0)}%",
                f"• Punctuation family score: {breakdown.get('punctuation_family', 0)}%",
                f"• Emoji family score: {breakdown.get('emoji_family', 0)}%",
                f"• Rhythm family score: {breakdown.get('rhythm_family', 0)}%",
                f"• Richness family score: {breakdown.get('richness_family', 0)}%",
                f"• Surface family score: {breakdown.get('surface_family', 0)}%",
            ])

        breakdown_report = "\n".join(breakdown_lines) if breakdown_lines else ""

        return summary_report, breakdown_report

    def _format_profile_stats(self, label: str, profile: dict) -> str:
        total_messages = profile["total_messages"]
        total_words = profile["total_words"]
        words_per_msg = total_words / max(total_messages, 1)

        def top(counter, n):
            items = [k for k, c in counter.most_common(n) if c > 0]
            return ", ".join(f"`{k}`" for k in items) if items else "—"

        lines = [
            f"🧾 Linguistic Profile: {label}",
        ]

        if total_messages < MIN_MESSAGES:
            lines.append(
                f"⚠️ Small sample — under {MIN_MESSAGES} messages, so figures may be noisy."
            )

        lines += [
            "",
            "Overview",
            f"• Messages analyzed: {total_messages}",
            f"• Total words: {total_words:,} (chars: {profile['total_chars']:,}, sentences: {profile['total_sentences']:,})",
            f"• Words per message: {words_per_msg:.1f}",
            f"• Avg word length: {profile['avg_word_len']:.2f} chars",
            f"• Avg sentence length: {profile['avg_sentence_len']:.1f} words",
            "",
            "Vocabulary richness",
            f"• MATTR (lexical diversity): {profile['avg_mattr'] * 100:.1f}%",
            f"• Hapax ratio (once-off words): {profile['avg_hapax_ratio'] * 100:.1f}%",
            f"• Unique words per message: {profile['avg_unique_words']:.1f}",
            f"• Function-word share: {profile['function_word_ratio'] * 100:.1f}% "
            f"(content {profile['content_word_ratio'] * 100:.1f}%)",
            "",
            "Rhythm & structure",
            f"• Burstiness (msg-length variability): {profile['burstiness']:.2f}",
            f"• Sentence-length variability: {profile['sentence_variance']:.2f}",
            f"• Stopword entropy: {profile['stopword_entropy'] * 100:.1f}%",
            "",
            "Habits",
            f"• Uppercase ratio: {profile['uppercase_ratio'] * 100:.1f}%",
            f"• Emoji density: {profile['emoji_density']:.2f}/msg "
            f"(unicode {profile['unicode_emoji_count']}, custom {profile['custom_emoji_count']})",
            f"• Contraction ratio: {profile['contraction_ratio'] * 100:.1f}%",
            f"• Question / exclamation / ellipsis per msg: "
            f"{profile['question_rate']:.2f} / {profile['exclamation_rate']:.2f} / {profile['ellipsis_rate']:.2f}",
            f"• URLs per message: {profile['url_ratio']:.2f}",
            "",
            "Top markers",
            f"• Function words: {top(profile['function_words'], 6)}",
            f"• Common words: {top(profile['common_words'], 6)}",
            f"• Punctuation: {top(profile['punctuation'], 5)}",
            f"• Emojis: {top(profile['emojis'], 5)}",
        ]

        return "\n".join(lines)

    async def get_report_channel(self) -> Optional[discord.TextChannel]:
        guild = self.bot.get_guild(LINGUISTICS_REPORT_GUILD_ID)
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(LINGUISTICS_REPORT_GUILD_ID)
            except Exception:
                return None

        for channel in guild.text_channels:
            if channel.name == LINGUISTICS_REPORT_CHANNEL_NAME:
                return channel

        try:
            return await guild.create_text_channel(LINGUISTICS_REPORT_CHANNEL_NAME)
        except Exception as e:
            print(f"[Report Channel Error] {e}")
            return None

    async def _send_reports(self, ctx: commands.Context, user1: str, user2: str, result: dict):
        summary_report, breakdown_report = self._format_report(user1, user2, result)

        await ctx.send(summary_report[:1900])
        if breakdown_report:
            try:
                await ctx.send(breakdown_report[:3900])
            except Exception:
                pass

        if SEND_COMPARISON_COPY_TO_REPORT_CHANNEL:
            report_channel = await self.get_report_channel()
            if report_channel is not None:
                try:
                    await report_channel.send(summary_report[:1900])
                    if breakdown_report:
                        await report_channel.send(breakdown_report[:3900])
                except Exception:
                    pass

    async def _run_comparison(self, ctx: commands.Context, user1: str, user2: str):
        mode = "global"

        p1 = await self.get_profile(user1)
        p2 = await self.get_profile(user2)

        if p1 is None or p2 is None:
            final_result = {
                "score": 0.0,
                "similarity_score": 0.0,
                "same_authorship_probability": 0.0,
                "confidence": 0.0,
                "tier": "low",
                "mode": mode,
                "u1_count": 0 if p1 is None else p1["total_messages"],
                "u2_count": 0 if p2 is None else p2["total_messages"],
                "breakdown": {},
                "markers": [],
                "note": "Insufficient data available for one or both users.",
            }
            await self._send_reports(ctx, user1, user2, final_result)
            return

        if p1["total_messages"] < MIN_MESSAGES or p2["total_messages"] < MIN_MESSAGES:
            final_result = {
                "score": 0.0,
                "similarity_score": 0.0,
                "same_authorship_probability": 0.0,
                "confidence": 0.0,
                "tier": "low",
                "mode": mode,
                "u1_count": p1["total_messages"],
                "u2_count": p2["total_messages"],
                "breakdown": {},
                "markers": [],
                "note": "Not enough messages yet for a stable comparison.",
            }
            await self._send_reports(ctx, user1, user2, final_result)
            return

        score, confidence, authorship_prob, breakdown, markers = await self._profile_similarity(p1, p2)

        final_result = {
            "score": round(score * 100.0, 2),
            "similarity_score": round(score * 100.0, 2),
            "same_authorship_probability": round(authorship_prob, 2),
            "confidence": round(confidence, 2),
            "tier": self._confidence_tier(confidence),
            "mode": mode,
            "u1_count": p1["total_messages"],
            "u2_count": p2["total_messages"],
            "breakdown": breakdown,
            "markers": markers,
            "note": "",
        }

        await self._send_reports(ctx, user1, user2, final_result)

    @commands.command()
    async def compare(self, ctx, user1: str, user2: str, mode: str = "global"):
        if mode != "global":
            return await ctx.send("Only global mode is supported now.")

        if isinstance(ctx.channel, discord.DMChannel) and ctx.author.id != OWNER_ID:
            return await ctx.send("Not allowed.")

        uid1 = self.parse_user_token(user1)
        uid2 = self.parse_user_token(user2)
        if uid1 is None or uid2 is None:
            return await ctx.send("Pass two valid user IDs or mentions.")

        await self._run_comparison(ctx, uid1, uid2)

    @commands.command()
    async def stats(self, ctx, user: str = None):
        if isinstance(ctx.channel, discord.DMChannel) and ctx.author.id != OWNER_ID:
            return await ctx.send("Not allowed.")

        if user is None:
            uid = str(ctx.author.id)
        else:
            uid = self.parse_user_token(user)
            if uid is None:
                return await ctx.send(
                    "Pass a valid user ID or mention, or nothing to see your own stats."
                )

        profile = await self.get_profile(uid)
        if profile is None or profile["total_messages"] == 0:
            return await ctx.send(f"No linguistic data recorded for `{uid}` yet.")

        label = self._display_name_for(ctx, uid)
        report = self._format_profile_stats(label, profile)
        await ctx.send(report[:1900])


async def setup(bot: commands.Bot):
    await bot.add_cog(Linguistics(bot))