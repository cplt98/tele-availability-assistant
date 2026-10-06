"""Tests for the voice flow in bot.py: a voice note in, text (and, for answers, speech) out.
Telegram, Gemini and ElevenLabs are all replaced by fakes."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardMarkup, InputFile
from telegram.error import TelegramError

import bot
import config
import db
import llm
import voice

ALEX = NS(id=111, first_name="Alexandra")   # Telegram's own first name differs from the configured name
HEARD = 'I heard: "is sam wfh today"\n\n'    # the lead-in every reply to a voice note starts with


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A bot with voice fully set up, a temporary database and log, and two users."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake")
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "el-key")
    monkeypatch.setattr(config, "ELEVENLABS_VOICE_ID", "voice-123")
    monkeypatch.setattr(config, "USER_NAMES", {111: "Alex", 222: "Sam"})
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {111, 222})
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()

    calls = NS(transcribe=[], speak=[])

    def fake_transcribe(audio):
        calls.transcribe.append(audio)
        return "is sam wfh today"

    def fake_speak(text):
        calls.speak.append(text)
        return b"spoken:" + text.encode()

    monkeypatch.setattr(voice, "transcribe", fake_transcribe)
    monkeypatch.setattr(voice, "speak", fake_speak)
    return NS(dir=tmp_path, calls=calls)


def model_says(monkeypatch, reply):
    monkeypatch.setattr(llm, "_call_model", lambda text, today, sender: json.dumps(reply))


def asks_about_sam_today():
    day = llm.today_sg().isoformat()
    return {"intent": "query", "entries": [], "question": None,
            "queries": [{"person": "Sam", "start_date": day, "end_date": day}]}


def tells_wfh_today():
    day = llm.today_sg().isoformat()
    return {"intent": "update", "queries": [], "question": None,
            "entries": [{"person": None, "date": day, "status": "wfh"}]}


def make_message(text="", voice_note=None):
    """A pretend Telegram message. Everything the bot sends is recorded in `sent`, in order."""
    sent = []

    async def reply_text(t, reply_markup=None):
        sent.append(("text", t, reply_markup))

    async def reply_voice(voice, **kwargs):
        sent.append(("voice", voice))

    return NS(text=text, voice=voice_note, reply_text=reply_text, reply_voice=reply_voice, sent=sent)


def make_context(download=b"audio-bytes"):
    get_file = AsyncMock(return_value=NS(download_as_bytearray=AsyncMock(return_value=bytearray(download))))
    return NS(user_data={}, bot_data={}, bot=NS(get_file=get_file, send_message=AsyncMock()))


def send_voice_note(message, context, user=ALEX):
    asyncio.run(bot.handle_voice(NS(effective_user=user, message=message), context))


def voice_note(duration=5):
    return NS(duration=duration, file_id="file-1")


def kinds(message):
    return [item[0] for item in message.sent]


def read_log(env):
    path = env.dir / "eval_log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# ---------- an answer: one text message, plus the spoken answer ----------

def test_a_voice_question_gets_one_text_message_and_a_spoken_answer(env, monkeypatch):
    model_says(monkeypatch, asks_about_sam_today())
    message = make_message(voice_note=voice_note())
    context = make_context(download=b"the-voice-note")

    send_voice_note(message, context)

    assert kinds(message) == ["text", "voice"]            # not three messages: the transcript is inside the text
    text = message.sent[0][1]
    assert text.startswith(HEARD)                         # what was heard, then the answer, in one message
    answer = text[len(HEARD):]
    assert answer.startswith("Nothing from Sam for")
    assert env.calls.transcribe == [b"the-voice-note"]    # the downloaded bytes went to speech to text
    assert env.calls.speak == [answer]                    # only the answer is spoken, not the "I heard" line
    spoken = message.sent[1][1]
    assert isinstance(spoken, InputFile) and spoken.filename == "reply.ogg"
    assert spoken.input_file_content == b"spoken:" + answer.encode()


def test_the_transcript_goes_through_the_same_parsing_as_typed_text(env, monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "_call_model", lambda text, today, sender: (seen.append((text, sender)), json.dumps(asks_about_sam_today()))[1])
    send_voice_note(make_message(voice_note=voice_note()), make_context())
    assert seen == [("is sam wfh today", "Alex")]         # the transcript, and the sender's configured name


# ---------- everything else stays text: the user has to read and tap it anyway ----------

def test_a_voice_update_is_one_text_message_with_buttons_and_is_not_spoken(env, monkeypatch):
    model_says(monkeypatch, tells_wfh_today())
    message = make_message(voice_note=voice_note())

    send_voice_note(message, make_context())

    assert kinds(message) == ["text"]
    _, text, markup = message.sent[0]
    assert text.startswith(HEARD) and text.endswith("Correct?")
    assert isinstance(markup, InlineKeyboardMarkup)       # Yes / No buttons are on that one message
    assert env.calls.speak == []                          # a summary you must read and tap is not spoken
    assert db.get_statuses_for_date(llm.today_sg().isoformat()) == []   # nothing saved until someone presses Yes


def test_a_question_back_to_the_user_is_text_only(env, monkeypatch):
    model_says(monkeypatch, {"intent": "update", "entries": [], "queries": [], "question": "Which Thursday do you mean?"})
    message = make_message(voice_note=voice_note())
    send_voice_note(message, make_context())
    assert kinds(message) == ["text"]
    assert message.sent[0][1] == HEARD + "Which Thursday do you mean?"
    assert env.calls.speak == []


def test_a_failed_parse_says_sorry_in_text_after_the_heard_line(env, monkeypatch):
    def down(text, today, sender):
        raise llm.LLMError("429 rate limit")

    monkeypatch.setattr(llm, "_call_model", down)
    message = make_message(voice_note=voice_note())
    send_voice_note(message, make_context())
    assert message.sent == [("text", HEARD + "Sorry, try again.", None)]
    assert env.calls.speak == []


def test_replies_to_button_presses_stay_text_only(env, monkeypatch):
    model_says(monkeypatch, tells_wfh_today())
    context = make_context()
    send_voice_note(make_message(voice_note=voice_note()), context)

    pressed_message = make_message()
    pressed = NS(data="confirm:yes", from_user=ALEX, message=pressed_message,
                 edit_message_reply_markup=AsyncMock(), answer=AsyncMock())
    asyncio.run(bot.confirm_callback(NS(callback_query=pressed), context))

    assert pressed_message.sent == [("text", "Saved!", None)]
    assert env.calls.speak == []
    assert db.get_statuses_for_date(llm.today_sg().isoformat()) == [("Alex", "wfh")]


def test_typed_messages_only_ever_get_text_back(env, monkeypatch):
    model_says(monkeypatch, asks_about_sam_today())
    message = make_message("is sam wfh today")
    asyncio.run(bot.handle_text(NS(effective_user=ALEX, message=message), make_context()))
    assert kinds(message) == ["text"]
    assert not message.sent[0][1].startswith("I heard")   # no lead-in for typed text
    assert env.calls.transcribe == [] and env.calls.speak == []


def test_voice_in_without_a_voice_id_replies_in_text_only(env, monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_VOICE_ID", "")
    model_says(monkeypatch, asks_about_sam_today())
    message = make_message(voice_note=voice_note())
    send_voice_note(message, make_context())
    assert kinds(message) == ["text"]
    assert env.calls.transcribe and env.calls.speak == []


# ---------- limits and set-up ----------

def test_voice_notes_are_refused_when_voice_is_not_set_up(env, monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "")
    message = make_message(voice_note=voice_note())
    context = make_context()
    send_voice_note(message, context)
    assert message.sent == [("text", "Voice notes aren't set up yet. Please type your message.", None)]
    context.bot.get_file.assert_not_called()              # nothing downloaded


@pytest.mark.parametrize("duration", [61, 600, timedelta(seconds=61)])
def test_voice_notes_that_are_too_long_are_refused_before_anything_is_spent(env, duration):
    message = make_message(voice_note=voice_note(duration))
    context = make_context()
    send_voice_note(message, context)
    assert "too long" in message.sent[0][1] and len(message.sent) == 1
    context.bot.get_file.assert_not_called()
    assert env.calls.transcribe == []


@pytest.mark.parametrize("duration", [1, 60, timedelta(seconds=10)])
def test_voice_notes_within_the_limit_are_accepted(env, monkeypatch, duration):
    model_says(monkeypatch, asks_about_sam_today())
    send_voice_note(make_message(voice_note=voice_note(duration)), make_context())
    assert len(env.calls.transcribe) == 1


# ---------- when things go wrong, the bot carries on in text ----------

def test_a_speech_to_text_failure_falls_back_to_text_and_is_logged(env, monkeypatch):
    def broken(audio):
        raise voice.VoiceError("Speech to text failed: HTTP 402: out of credits")

    monkeypatch.setattr(voice, "transcribe", broken)
    message = make_message(voice_note=voice_note())

    send_voice_note(message, make_context())     # must not raise

    assert message.sent == [("text", "Sorry, I couldn't understand that voice note. Please try again, or type it.", None)]
    [record] = [r for r in read_log(env) if r["kind"] == "voice_error"]
    assert record["stage"] == "stt" and record["user_id"] == 111 and "out of credits" in record["error"]


def test_a_failed_download_from_telegram_falls_back_to_text(env):
    message = make_message(voice_note=voice_note())
    context = make_context()
    context.bot.get_file = AsyncMock(side_effect=TelegramError("file is gone"))

    send_voice_note(message, context)

    assert kinds(message) == ["text"] and "couldn't understand" in message.sent[0][1]
    assert env.calls.transcribe == []
    assert [r["stage"] for r in read_log(env) if r["kind"] == "voice_error"] == ["stt"]


def test_a_text_to_speech_failure_still_delivers_the_text_answer(env, monkeypatch):
    def broken(text):
        raise voice.VoiceError("Text to speech failed: HTTP 429: too many requests")

    monkeypatch.setattr(voice, "speak", broken)
    model_says(monkeypatch, asks_about_sam_today())
    message = make_message(voice_note=voice_note())

    send_voice_note(message, make_context())     # must not raise

    assert kinds(message) == ["text"]            # the answer arrived; only the voice copy is missing
    assert message.sent[0][1].startswith(HEARD + "Nothing from Sam for")
    [record] = [r for r in read_log(env) if r["kind"] == "voice_error"]
    assert record["stage"] == "tts" and "too many requests" in record["error"]


def test_a_failure_sending_the_voice_message_is_logged_not_raised(env, monkeypatch):
    model_says(monkeypatch, asks_about_sam_today())
    message = make_message(voice_note=voice_note())

    async def broken_reply_voice(voice, **kwargs):
        raise TelegramError("cannot send voice")

    message.reply_voice = broken_reply_voice
    send_voice_note(message, make_context())     # must not raise

    assert kinds(message) == ["text"]
    assert [r["stage"] for r in read_log(env) if r["kind"] == "voice_error"] == ["send"]


def test_the_api_key_never_reaches_the_log(env, monkeypatch):
    def broken(audio):
        raise voice.VoiceError("rejected the key el-key")

    monkeypatch.setattr(voice, "transcribe", broken)
    send_voice_note(make_message(voice_note=voice_note()), make_context())
    raw = (env.dir / "eval_log.jsonl").read_text(encoding="utf-8")
    assert "el-key" not in raw and "[redacted]" in raw


def test_the_log_holds_text_only_never_audio(env, monkeypatch):
    model_says(monkeypatch, asks_about_sam_today())
    send_voice_note(make_message(voice_note=voice_note()), make_context(download=b"SECRET-AUDIO"))
    raw = (env.dir / "eval_log.jsonl").read_text(encoding="utf-8")
    assert "SECRET-AUDIO" not in raw and "spoken:" not in raw


# ---------- /start ----------

def test_start_greets_people_by_their_configured_name_not_their_telegram_name(env):
    message = make_message()
    asyncio.run(bot.start(NS(effective_user=ALEX, message=message), NS()))
    greeting = message.sent[0][1]
    assert greeting.startswith("Hi Alex!") and "Alexandra" not in greeting
    assert "voice note" in greeting
