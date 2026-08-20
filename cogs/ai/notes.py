import re

from cogs.ai.storage import load_json, update_json

FILE = "data/notes.json"
MAX_NOTES = 5

# Reject anything that looks like an attempt to inject instructions.
_INJECTION = re.compile(
    r"(?:^|\b)(?:system|assistant|user|developer)\s*:|"
    r"ignore\s+(?:all|the|any|your|previous|prior)|"
    r"disregard\s+(?:all|the|previous|prior)|"
    r"you\s+are\s+now|new\s+instructions?|"
    r"</?\s*(?:system|prompt|instructions?)\b|"
    r"prompt\s+injection|override\s+(?:the|your)",
    re.IGNORECASE,
)

# Keep only letters, digits, spaces, apostrophes, hyphens. Strips brackets,
# colons, backticks, braces, etc. that could be used to fake structure.
_DISALLOWED = re.compile(r"[^a-zA-Z0-9 '\-]")

# Word-boundary triggers, mapped to how the note should read.
_TRIGGERS = [
    (re.compile(r"\bi (?:really |absolutely )?love\b", re.I), "loves"),
    (re.compile(r"\bi (?:really |absolutely )?like\b", re.I), "likes"),
    (re.compile(r"\bi (?:really |absolutely )?hate\b", re.I), "hates"),
    (re.compile(r"\bmy name is\b|\bcall me\b", re.I), "name is"),
    (re.compile(r"\bi'm\b|\bi am\b", re.I), "is"),
]

# Where to cut the captured fragment off.
_STOP = re.compile(
    r"[.,!?;:]|\bbut\b|\band\b|\bbecause\b|\bso\b|\bwhen\b|\bif\b|\bthough\b",
    re.I,
)


def _clean_note(text):
    if not text:
        return None
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if _INJECTION.search(text):
        return None
    text = _DISALLOWED.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 3 or not re.search(r"[a-zA-Z]", text):
        return None
    return text[:60].strip()


def _fragment_after(message, match):
    tail = message[match.end():]
    tail = _STOP.split(tail, 1)[0]
    return " ".join(tail.split()[:6]).strip()


def get_notes(user_id):
    data = load_json(FILE, dict)
    return data.get(str(user_id), {}).get("notes", [])


def add_note(user_id, note):
    note = _clean_note(note)
    if not note:
        return

    uid = str(user_id)

    def mutate(data):
        entry = data.setdefault(uid, {"notes": []})
        if note not in entry["notes"]:
            entry["notes"].append(note)
        entry["notes"] = entry["notes"][-MAX_NOTES:]

    update_json(FILE, mutate, dict)


def extract_notes(user_id, message, username):
    # Always remember the display name (sanitized).
    add_note(user_id, f"name is {username}")

    for pattern, label in _TRIGGERS:
        match = pattern.search(message)
        if not match:
            continue
        fragment = _fragment_after(message, match)
        if fragment:
            add_note(user_id, f"{label} {fragment}")


def clear_notes(user_id):
    uid = str(user_id)

    def mutate(data):
        if uid in data:
            data[uid]["notes"] = []

    update_json(FILE, mutate, dict)


def clear_all_notes():
    update_json(FILE, lambda data: data.clear(), dict)