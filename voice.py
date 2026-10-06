"""Everything that talks to ElevenLabs lives here: speech to text and text to speech.

The rest of the bot only calls transcribe() and speak(), and asks can_transcribe() /
can_speak() whether voice is set up. Both calls wait for the network, so bot.py runs
them in a thread. If anything goes wrong they raise VoiceError, and bot.py falls back
to plain text, so a voice problem can never stop the bot from answering.
"""

import logging
import re

import config

logger = logging.getLogger("voice")

MAX_VOICE_SECONDS = 60    # longer voice notes are refused, to protect the free credits
MAX_SPOKEN_CHARS = 600    # longer replies are sent as text only (every spoken character costs credits)
# An Ogg/Opus file. Telegram shows that as a proper voice message (checked against the live API).
OUTPUT_FORMAT = "opus_48000_32"

# Written for the eye, so said differently out loud.
SPOKEN_WORDS = {"WFH": "work from home"}

# Replies show dates as "Fri 2 Oct". Left like that, a voice model says "Free" and "Ock-t".
# So the exact shape the bot writes (day, number, month) is spelled out before speaking.
# Only that shape is expanded, so a person called "Jan" or "Mar" is never turned into a month.
DAY_NAMES = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday",
             "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"}
MONTH_NAMES = {"Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April", "May": "May", "Jun": "June",
               "Jul": "July", "Aug": "August", "Sep": "September", "Oct": "October", "Nov": "November",
               "Dec": "December"}
SHORT_DATE = re.compile(r"\b(" + "|".join(DAY_NAMES) + r") (\d{1,2}) (" + "|".join(MONTH_NAMES) + r")\b")


class VoiceError(Exception):
    """Speech to text or text to speech failed (no network, no credits left, bad reply...)."""


def can_transcribe():
    """True if voice notes can be turned into text."""
    return bool(config.ELEVENLABS_API_KEY)


def can_speak():
    """True if replies can be spoken. Needs a voice as well as the key."""
    return bool(config.ELEVENLABS_API_KEY and config.ELEVENLABS_VOICE_ID)


def _client():
    # Imported here so the bot still starts, and the tests still run, without ElevenLabs.
    from elevenlabs.client import ElevenLabs

    return ElevenLabs(api_key=config.ELEVENLABS_API_KEY)


def _describe(error):
    """A short, safe description of what went wrong (the HTTP status when there is one)."""
    status = getattr(error, "status_code", None)
    detail = getattr(error, "body", None) or str(error)
    if isinstance(detail, dict):
        inner = detail.get("detail", detail)
        detail = inner.get("message") if isinstance(inner, dict) and inner.get("message") else inner
    prefix = f"HTTP {status}: " if status else f"{type(error).__name__}: "
    return prefix + str(detail)[:200]


def transcribe(audio):
    """Turn a voice note (the bytes of an audio file) into text with ElevenLabs Speech to Text."""
    try:
        response = _client().speech_to_text.convert(
            model_id=config.ELEVENLABS_STT_MODEL,
            file=("voice.ogg", audio, "audio/ogg"),
        )
    except Exception as e:  # no network, bad key, no credits left...
        raise VoiceError(f"Speech to text failed: {_describe(e)}") from e

    text = (getattr(response, "text", "") or "").strip()
    if not text:
        raise VoiceError("Speech to text heard nothing")
    return text


def to_spoken(text):
    """The words to say out loud for a text reply.

    "Fri 2 Oct" becomes "Friday 2 October", "WFH" becomes "work from home", and lines become sentences.
    """
    text = SHORT_DATE.sub(lambda m: f"{DAY_NAMES[m[1]]} {m[2]} {MONTH_NAMES[m[3]]}", text)
    for written, spoken in SPOKEN_WORDS.items():
        text = text.replace(written, spoken)
    lines = [line.strip().rstrip(".:") for line in text.splitlines() if line.strip()]
    return ". ".join(lines)


def speak(text):
    """Turn a text reply into speech with ElevenLabs Text to Speech. Returns an Ogg/Opus file as bytes."""
    spoken = to_spoken(text)
    if not spoken:
        raise VoiceError("Nothing to say")
    if len(spoken) > MAX_SPOKEN_CHARS:
        raise VoiceError(f"Reply is too long to speak ({len(spoken)} characters, limit {MAX_SPOKEN_CHARS})")
    try:
        chunks = _client().text_to_speech.convert(
            voice_id=config.ELEVENLABS_VOICE_ID,
            text=spoken,
            model_id=config.ELEVENLABS_TTS_MODEL,
            output_format=OUTPUT_FORMAT,
        )
        return b"".join(chunks)
    except Exception as e:
        raise VoiceError(f"Text to speech failed: {_describe(e)}") from e
