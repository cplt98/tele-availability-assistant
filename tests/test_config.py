"""Tests for config.py: settings from environment variables or a .env file, and the
list of users from users.json. Everything happens in temporary folders."""

import json
from pathlib import Path

import pytest

import config

SETTING_NAMES = (
    "TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY", "GEMINI_MODEL", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID",
    "ELEVENLABS_STT_MODEL", "ELEVENLABS_TTS_MODEL", "DATA_DIR", "DB_PATH", "USERS_FILE",
)
PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture
def clean_settings(tmp_path, monkeypatch):
    """No settings in the environment, an empty project folder, and every setting put back afterwards."""
    for name in SETTING_NAMES:
        monkeypatch.setenv(name, "")  # registers the original state so it is restored afterwards
        monkeypatch.delenv(name)
    for name in ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY", "GEMINI_MODEL", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID",
                 "ELEVENLABS_STT_MODEL", "ELEVENLABS_TTS_MODEL", "DATA_DIR", "DB_PATH", "ENV_FILE",
                 "ALLOWED_USER_IDS", "USER_NAMES"):
        monkeypatch.setattr(config, name, getattr(config, name))
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    return tmp_path


def write_users(folder, users, name="users.json"):
    path = folder / name
    path.write_text(json.dumps({"users": users}), encoding="utf-8")
    return path


def minimal_settings(folder, monkeypatch):
    """The settings every start needs: a bot token, a Gemini key and one user."""
    write_users(folder, [{"telegram_id": 1, "name": "Alex"}])
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:abc")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")


# ---------- users.json ----------

def test_the_example_users_file_is_valid():
    assert config.load_users(PROJECT_ROOT / "users.example.json") == {
        100000001: "Alex", 100000002: "Sam", 100000003: "Jordan",
    }


def test_names_are_tidied(tmp_path):
    path = write_users(tmp_path, [{"telegram_id": 1, "name": "  sam   lee "}])
    assert config.load_users(path) == {1: "Sam Lee"}


def test_a_missing_users_file_says_what_to_do(tmp_path):
    with pytest.raises(SystemExit) as error:
        config.load_users(tmp_path / "users.json")
    assert "users.example.json" in str(error.value)


def test_a_users_file_that_is_not_json_is_reported(tmp_path):
    path = tmp_path / "users.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        config.load_users(path)
    assert "not valid JSON" in str(error.value)


@pytest.mark.parametrize("content", [{"users": []}, {"users": "Alex"}, {"people": []}, [1, 2]])
def test_a_users_file_needs_a_non_empty_users_list(tmp_path, content):
    path = tmp_path / "users.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        config.load_users(path)
    assert '"users"' in str(error.value)


@pytest.mark.parametrize("entry", [
    {"name": "Alex"},                                  # no ID
    {"telegram_id": 1},                                # no name
    {"telegram_id": 1, "name": "  "},                  # blank name
    {"telegram_id": "100000001", "name": "Alex"},      # ID as text
    {"telegram_id": 1.5, "name": "Alex"},              # ID not a whole number
    {"telegram_id": True, "name": "Alex"},             # JSON true is not an ID
    "Alex",                                            # not an object
])
def test_a_bad_user_entry_is_reported(tmp_path, entry):
    with pytest.raises(SystemExit) as error:
        config.load_users(write_users(tmp_path, [entry]))
    assert "telegram_id" in str(error.value)


def test_the_same_id_twice_is_reported(tmp_path):
    path = write_users(tmp_path, [{"telegram_id": 1, "name": "Alex"}, {"telegram_id": 1, "name": "Sam"}])
    with pytest.raises(SystemExit) as error:
        config.load_users(path)
    assert "listed twice" in str(error.value)


def test_the_same_name_twice_is_reported_whatever_the_case(tmp_path):
    path = write_users(tmp_path, [{"telegram_id": 1, "name": "Alex"}, {"telegram_id": 2, "name": "ALEX"}])
    with pytest.raises(SystemExit) as error:
        config.load_users(path)
    assert "listed twice" in str(error.value)


# ---------- load(): environment variables, .env, users ----------

def test_settings_come_from_environment_variables_when_there_is_no_env_file(clean_settings, monkeypatch):
    write_users(clean_settings, [{"telegram_id": 111, "name": "Alex"}, {"telegram_id": 222, "name": "sam"}])
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:abc")
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setenv("GEMINI_MODEL", "some-model")
    monkeypatch.setenv("DB_PATH", "team.db")

    config.load()   # there is no .env in the project folder

    assert config.TELEGRAM_BOT_TOKEN == "1:abc" and config.GEMINI_API_KEY == "key"
    assert config.GEMINI_MODEL == "some-model"
    assert config.USER_NAMES == {111: "Alex", 222: "Sam"}
    assert config.ALLOWED_USER_IDS == {111, 222}
    assert config.DB_PATH == clean_settings / "team.db"
    assert config.ENV_FILE is None


def test_defaults_when_optional_settings_are_missing(clean_settings, monkeypatch):
    minimal_settings(clean_settings, monkeypatch)
    config.load()
    assert config.GEMINI_MODEL == "gemini-3.1-flash-lite"
    assert config.DATA_DIR == clean_settings
    assert config.DB_PATH == clean_settings / "schedule.db"
    # voice is off until a key is given, and the models have working defaults
    assert config.ELEVENLABS_API_KEY == "" and config.ELEVENLABS_VOICE_ID == ""
    assert config.ELEVENLABS_STT_MODEL == "scribe_v2"
    assert config.ELEVENLABS_TTS_MODEL == "eleven_flash_v2_5"


def test_elevenlabs_settings_are_read(clean_settings, monkeypatch):
    minimal_settings(clean_settings, monkeypatch)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-123")
    monkeypatch.setenv("ELEVENLABS_STT_MODEL", "scribe_next")
    monkeypatch.setenv("ELEVENLABS_TTS_MODEL", "eleven_v4_turbo")
    config.load()
    assert config.ELEVENLABS_API_KEY == "el-key" and config.ELEVENLABS_VOICE_ID == "voice-123"
    assert config.ELEVENLABS_STT_MODEL == "scribe_next"
    assert config.ELEVENLABS_TTS_MODEL == "eleven_v4_turbo"


def test_a_gemini_key_is_required(clean_settings, monkeypatch):
    write_users(clean_settings, [{"telegram_id": 1, "name": "Alex"}])
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:abc")
    with pytest.raises(SystemExit) as error:
        config.load()
    assert "GEMINI_API_KEY is missing" in str(error.value)


def test_data_dir_defaults_to_the_project_folder_and_holds_the_database(clean_settings, monkeypatch):
    minimal_settings(clean_settings, monkeypatch)
    monkeypatch.setenv("DB_PATH", "team.db")
    config.load()
    assert config.DATA_DIR == clean_settings
    assert config.DB_PATH == clean_settings / "team.db"


def test_a_relative_data_dir_starts_from_the_project_folder_and_is_created(clean_settings, monkeypatch):
    minimal_settings(clean_settings, monkeypatch)
    monkeypatch.setenv("DATA_DIR", "var/data")
    config.load()
    assert config.DATA_DIR == clean_settings / "var" / "data"
    assert config.DB_PATH == clean_settings / "var" / "data" / "schedule.db"
    assert config.DB_PATH.parent.is_dir()   # ready for the database to be created


def test_an_absolute_data_dir_can_be_outside_the_project_folder(clean_settings, monkeypatch, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("outside") / "bot-data"
    minimal_settings(clean_settings, monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(elsewhere))
    config.load()
    assert config.DATA_DIR == elsewhere
    assert config.DB_PATH == elsewhere / "schedule.db"
    assert elsewhere.is_dir()


def test_an_absolute_db_path_from_the_environment_is_used_as_it_is(clean_settings, monkeypatch, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("persistent") / "schedule.db"   # e.g. a host's persistent disk
    minimal_settings(clean_settings, monkeypatch)
    monkeypatch.setenv("DB_PATH", str(elsewhere))
    config.load()
    assert config.DB_PATH == elsewhere


def test_an_env_file_is_read_and_wins_over_the_environment(clean_settings, monkeypatch):
    write_users(clean_settings, [{"telegram_id": 1, "name": "Alex"}])
    (clean_settings / ".env").write_text("TELEGRAM_BOT_TOKEN=1:fromfile\nGEMINI_API_KEY=key\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "9:fromenvironment")
    config.load()
    assert config.TELEGRAM_BOT_TOKEN == "1:fromfile"
    assert config.ENV_FILE == clean_settings / ".env"


def test_the_users_file_can_be_chosen_with_users_file(clean_settings, monkeypatch):
    write_users(clean_settings, [{"telegram_id": 5, "name": "Jordan"}], name="team.json")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:abc")
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setenv("USERS_FILE", "team.json")
    config.load()
    assert config.USER_NAMES == {5: "Jordan"}


def test_no_env_file_and_no_token_is_a_clear_error(clean_settings):
    with pytest.raises(SystemExit) as error:
        config.load()
    assert "Env file not found" in str(error.value) and "environment variable" in str(error.value)


def test_a_named_env_file_must_exist_even_if_the_environment_has_a_token(clean_settings, monkeypatch):
    minimal_settings(clean_settings, monkeypatch)
    with pytest.raises(SystemExit) as error:
        config.load(".env.tset")   # a typo must not quietly fall back to some other token
    assert "Env file not found" in str(error.value)


def test_a_missing_token_in_the_env_file_is_reported(clean_settings):
    write_users(clean_settings, [{"telegram_id": 1, "name": "Alex"}])
    (clean_settings / ".env").write_text("GEMINI_API_KEY=abc\n", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        config.load()
    assert "TELEGRAM_BOT_TOKEN is missing in .env" in str(error.value)


def test_a_missing_users_file_stops_startup(clean_settings, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:abc")
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    with pytest.raises(SystemExit) as error:
        config.load()
    assert "Users file not found" in str(error.value)
