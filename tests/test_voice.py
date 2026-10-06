"""Tests for voice.py. ElevenLabs is replaced by a fake, so no test calls the real
service or spends any credits."""

import re
from types import SimpleNamespace as NS

import pytest

import config
import voice


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "el-key")
    monkeypatch.setattr(config, "ELEVENLABS_VOICE_ID", "voice-123")
    monkeypatch.setattr(config, "ELEVENLABS_STT_MODEL", "stt-model")
    monkeypatch.setattr(config, "ELEVENLABS_TTS_MODEL", "tts-model")


class FakeApiError(Exception):
    """Looks like the SDK's error: it carries an HTTP status and a body."""

    def __init__(self, status_code, body):
        super().__init__(f"status_code: {status_code}, body: {body}")
        self.status_code = status_code
        self.body = body


def fake_elevenlabs(monkeypatch, stt=None, tts=None):
    """Replace the ElevenLabs client. `stt` is what speech to text returns (or an exception to raise),
    `tts` is the list of audio chunks text to speech returns (or an exception to raise)."""
    calls = {}

    class SpeechToText:
        def convert(self, **kwargs):
            calls["stt"] = kwargs
            if isinstance(stt, Exception):
                raise stt
            return stt

    class TextToSpeech:
        def convert(self, **kwargs):
            calls["tts"] = kwargs
            if isinstance(tts, Exception):
                raise tts
            return iter(tts)

    monkeypatch.setattr(voice, "_client", lambda: NS(speech_to_text=SpeechToText(), text_to_speech=TextToSpeech()))
    return calls


# ---------- is voice set up? ----------

def test_voice_is_off_without_a_key(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "")
    monkeypatch.setattr(config, "ELEVENLABS_VOICE_ID", "voice-123")
    assert not voice.can_transcribe() and not voice.can_speak()


def test_a_key_alone_allows_listening_but_not_speaking(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "el-key")
    monkeypatch.setattr(config, "ELEVENLABS_VOICE_ID", "")
    assert voice.can_transcribe() and not voice.can_speak()


def test_a_key_and_a_voice_allow_both(settings):
    assert voice.can_transcribe() and voice.can_speak()


# ---------- speech to text ----------

def test_transcribe_returns_the_text_and_sends_the_audio_and_model(settings, monkeypatch):
    calls = fake_elevenlabs(monkeypatch, stt=NS(text="  Is Sam in the office on Wednesday  ", language_code="eng"))
    assert voice.transcribe(b"audio-bytes") == "Is Sam in the office on Wednesday"
    assert calls["stt"]["model_id"] == "stt-model"
    assert calls["stt"]["file"] == ("voice.ogg", b"audio-bytes", "audio/ogg")


@pytest.mark.parametrize("heard", ["", "   ", None])
def test_transcribe_treats_silence_as_a_failure(settings, monkeypatch, heard):
    fake_elevenlabs(monkeypatch, stt=NS(text=heard))
    with pytest.raises(voice.VoiceError, match="heard nothing"):
        voice.transcribe(b"audio")


def test_transcribe_turns_service_errors_into_voice_errors(settings, monkeypatch):
    body = {"detail": {"status": "quota_exceeded", "message": "You have run out of credits."}}
    fake_elevenlabs(monkeypatch, stt=FakeApiError(401, body))
    with pytest.raises(voice.VoiceError) as error:
        voice.transcribe(b"audio")
    assert "HTTP 401" in str(error.value) and "run out of credits" in str(error.value)


def test_transcribe_turns_any_other_failure_into_a_voice_error(settings, monkeypatch):
    fake_elevenlabs(monkeypatch, stt=ConnectionError("no network"))
    with pytest.raises(voice.VoiceError, match="ConnectionError"):
        voice.transcribe(b"audio")


# ---------- text to speech ----------

def test_speak_joins_the_chunks_and_uses_the_configured_voice_and_model(settings, monkeypatch):
    calls = fake_elevenlabs(monkeypatch, tts=[b"one", b"two", b"three"])
    assert voice.speak("Sam on Wed 30 Sep: Office") == b"onetwothree"
    assert calls["tts"]["voice_id"] == "voice-123"
    assert calls["tts"]["model_id"] == "tts-model"
    assert calls["tts"]["text"] == "Sam on Wednesday 30 September: Office"
    assert calls["tts"]["output_format"] == voice.OUTPUT_FORMAT


def test_abbreviated_dates_never_reach_the_voice_model(settings, monkeypatch):
    # The reported bug: "Fri" was read as "free" and "Oct" was mangled, because the model was
    # sent the abbreviations. It must be sent the full words.
    calls = fake_elevenlabs(monkeypatch, tts=[b"audio"])
    voice.speak("Sam, Mon 28 Sep to Fri 2 Oct:\nMon 28 Sep: Office\nFri 2 Oct: WFH")
    sent = calls["tts"]["text"]
    assert "Friday 2 October" in sent and "Monday 28 September" in sent
    abbreviations = [*voice.DAY_NAMES, *voice.MONTH_NAMES, "WFH"]
    for abbreviation in abbreviations:
        if abbreviation != "May":                      # "May" is the same word in full
            assert not re.search(rf"\b{abbreviation}\b", sent), abbreviation


def test_the_output_format_is_ogg_opus_so_telegram_shows_a_voice_message():
    # Checked against the live API: the "opus_*" formats come back as an Ogg file ("OggS").
    assert voice.OUTPUT_FORMAT.startswith("opus_")


def test_speak_refuses_replies_that_are_too_long(settings, monkeypatch):
    calls = fake_elevenlabs(monkeypatch, tts=[b"audio"])
    with pytest.raises(voice.VoiceError, match="too long"):
        voice.speak("word " * 200)
    assert "tts" not in calls   # no request made, so no credits spent


def test_speak_refuses_when_there_is_nothing_to_say(settings, monkeypatch):
    fake_elevenlabs(monkeypatch, tts=[b"audio"])
    with pytest.raises(voice.VoiceError, match="Nothing to say"):
        voice.speak("  \n ")


def test_speak_turns_service_errors_into_voice_errors(settings, monkeypatch):
    body = {"detail": {"code": "paid_plan_required", "message": "Free users cannot use library voices via the API."}}
    fake_elevenlabs(monkeypatch, tts=FakeApiError(402, body))
    with pytest.raises(voice.VoiceError) as error:
        voice.speak("hello")
    assert "HTTP 402" in str(error.value) and "library voices" in str(error.value)


def test_speak_turns_failures_while_streaming_into_voice_errors(settings, monkeypatch):
    def broken_stream():
        yield b"first part"
        raise TimeoutError("connection dropped")

    monkeypatch.setattr(
        voice, "_client",
        lambda: NS(text_to_speech=NS(convert=lambda **kwargs: broken_stream())),
    )
    with pytest.raises(voice.VoiceError, match="TimeoutError"):
        voice.speak("hello")


# ---------- what is said out loud ----------

@pytest.mark.parametrize("written, said", [
    ("Sam on Wed 30 Sep: Office", "Sam on Wednesday 30 September: Office"),
    ("Sam:\nThu 1 Oct: WFH\nCorrect?", "Sam. Thursday 1 October: work from home. Correct?"),
    ("Sam, Mon 28 Sep to Fri 2 Oct:\nMon 28 Sep: Office\nTue 29 Sep: WFH",
     "Sam, Monday 28 September to Friday 2 October. Monday 28 September: Office. Tuesday 29 September: work from home"),
    ("Done, I've asked Sam to update.", "Done, I've asked Sam to update"),
    ("Nothing from Sam for Thu 1 Oct yet.\n\n", "Nothing from Sam for Thursday 1 October yet"),
    ("Fri 2 Oct is a public holiday (Test Day), so nothing is needed from Sam.",
     "Friday 2 October is a public holiday (Test Day), so nothing is needed from Sam"),
])
def test_what_is_said_out_loud(written, said):
    assert voice.to_spoken(written) == said


@pytest.mark.parametrize("short, full", list(voice.DAY_NAMES.items()))
def test_every_day_name_is_spelled_out(short, full):
    assert voice.to_spoken(f"{short} 5 Oct") == f"{full} 5 October"


@pytest.mark.parametrize("short, full", list(voice.MONTH_NAMES.items()))
def test_every_month_name_is_spelled_out(short, full):
    assert voice.to_spoken(f"Mon 5 {short}") == f"Monday 5 {full}"


@pytest.mark.parametrize("text", [
    "Jan on Mon 5 Oct: Office",          # a person called Jan is not turned into January
    "Mar and Sun are in the office",     # names, and a day with no date, stay as they are
    "Is Sat or Sun better?",             # a day on its own is left alone
    "Oct is a long month",               # a month on its own is left alone
])
def test_only_the_date_shape_is_expanded(text):
    spoken = voice.to_spoken(text)
    assert spoken.replace("Monday 5 October", "Mon 5 Oct") == text
