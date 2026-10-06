"""Loads settings and the list of users, so no secrets and no personal data live in the code.

- Secrets and settings come from environment variables, or from a local .env file.
- The people who may use the bot come from users.json (copy users.example.json to start).

Nothing is read when this file is imported. bot.py calls load() at startup.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Relative paths (the env file, USERS_FILE, DATA_DIR) are worked out from this folder,
# so the bot behaves the same no matter where you start it from.
PROJECT_DIR = Path(__file__).parent

# These are filled in by load().
TELEGRAM_BOT_TOKEN = ""
GEMINI_API_KEY = ""
GEMINI_MODEL = "gemini-3.1-flash-lite"
# Optional. Voice notes need ELEVENLABS_API_KEY; spoken replies also need ELEVENLABS_VOICE_ID.
# On the free ElevenLabs plan the voice must be one of the built-in ("premade") voices.
ELEVENLABS_API_KEY = ""
ELEVENLABS_VOICE_ID = ""
ELEVENLABS_STT_MODEL = "scribe_v2"
ELEVENLABS_TTS_MODEL = "eleven_flash_v2_5"
# Where the bot keeps what it writes: the database and the conversation log. Point this
# outside the project folder and nothing private can end up next to the code.
DATA_DIR = PROJECT_DIR
DB_PATH = PROJECT_DIR / "schedule.db"
ENV_FILE = None
# Everyone in users.json may use the bot (the allowlist), and each has a name.
ALLOWED_USER_IDS = set()
USER_NAMES = {}  # Telegram user ID -> name, e.g. {100000001: "Alex"}


def load_users(path):
    """Read users.json and return {telegram_id: name}.

    The file looks like {"users": [{"telegram_id": 100000001, "name": "Alex"}, ...]}.
    Anything wrong with it stops the bot with a message saying what to fix.
    """
    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"Users file not found: {path}. Copy users.example.json to users.json and edit it.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SystemExit(f"{path.name} is not valid JSON: {e}")

    entries = data.get("users") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise SystemExit(f'{path.name} needs a non-empty "users" list.')

    names = {}
    for entry in entries:
        user_id = entry.get("telegram_id") if isinstance(entry, dict) else None
        name = " ".join(str(entry.get("name", "")).split()).title() if isinstance(entry, dict) else ""
        if not isinstance(user_id, int) or isinstance(user_id, bool) or not name:
            raise SystemExit(f'{path.name}: each user needs a numeric "telegram_id" and a "name". Problem entry: {entry!r}')
        if user_id in names:
            raise SystemExit(f"{path.name}: telegram_id {user_id} is listed twice.")
        if name.lower() in {n.lower() for n in names.values()}:
            raise SystemExit(f"{path.name}: the name {name!r} is listed twice.")
        names[user_id] = name
    return names


def load(env_file=".env"):
    """Read the given env file (and users.json) and fill in the settings above.

    On a host there is no .env file: you type the settings into the host's dashboard and
    they arrive as environment variables. So if the default .env doesn't exist but
    TELEGRAM_BOT_TOKEN is already in the environment, use the environment.
    (Only for the default .env. A file you asked for by name must exist, so a typo in
    --env can't quietly pick up some other token.)
    """
    global TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, GEMINI_MODEL, DATA_DIR, DB_PATH, ENV_FILE
    global ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ELEVENLABS_STT_MODEL, ELEVENLABS_TTS_MODEL
    global ALLOWED_USER_IDS, USER_NAMES

    env_path = PROJECT_DIR / env_file  # an absolute env_file is used as-is
    if env_path.is_file():
        # override=True so the values in this file win over anything already set on your PC.
        load_dotenv(env_path, override=True)
        source = env_path.name
    elif env_file == ".env" and os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        env_path = None
        source = "the environment variables"
    else:
        raise SystemExit(
            f"Env file not found: {env_path}. Copy .env.example to .env and fill it in "
            "(or, on a host, set TELEGRAM_BOT_TOKEN as an environment variable and run without --env)."
        )

    def setting(name, default=""):
        return os.getenv(name, "").strip() or default

    TELEGRAM_BOT_TOKEN = setting("TELEGRAM_BOT_TOKEN")
    GEMINI_API_KEY = setting("GEMINI_API_KEY")
    GEMINI_MODEL = setting("GEMINI_MODEL", "gemini-3.1-flash-lite")
    ELEVENLABS_API_KEY = setting("ELEVENLABS_API_KEY")
    ELEVENLABS_VOICE_ID = setting("ELEVENLABS_VOICE_ID")
    ELEVENLABS_STT_MODEL = setting("ELEVENLABS_STT_MODEL", "scribe_v2")
    ELEVENLABS_TTS_MODEL = setting("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")
    ENV_FILE = env_path

    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit(f"TELEGRAM_BOT_TOKEN is missing in {source}.")
    if not GEMINI_API_KEY:
        raise SystemExit(f"GEMINI_API_KEY is missing in {source}.")

    # Relative paths start from the project folder; absolute paths are used as they are.
    DATA_DIR = PROJECT_DIR / setting("DATA_DIR", ".")
    DB_PATH = DATA_DIR / setting("DB_PATH", "schedule.db")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    USER_NAMES = load_users(PROJECT_DIR / setting("USERS_FILE", "users.json"))
    ALLOWED_USER_IDS = set(USER_NAMES)
