import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _require_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _require_int(name: str) -> int:
    value = _require_value(name)
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer.") from exc


TOKEN = _require_value("TOKEN")
OPENROUTER_API_KEY = _require_value("OPENROUTER_API_KEY")
OWNER_ID = _require_int("OWNER_ID")
LINGUISTICS_REPORT_GUILD_ID = _require_int("LINGUISTICS_REPORT_GUILD_ID")
LINGUISTICS_REPORT_CHANNEL_NAME = _require_value("LINGUISTICS_REPORT_CHANNEL_NAME")
SERVER_PORT = _require_int("SERVER_PORT")