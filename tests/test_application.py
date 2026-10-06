"""Tests for how the bot is wired together: which handlers exist and in what order.
Nothing connects to Telegram; building the application only registers handlers."""

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, TypeHandler, filters

import bot
import config


def build(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:not-a-real-token")
    return bot.build_application()


def test_the_allowlist_gate_runs_before_every_other_handler(monkeypatch):
    app = build(monkeypatch)
    assert min(app.handlers) == -1                       # the earliest group is the gate
    [gate] = app.handlers[-1]
    assert isinstance(gate, TypeHandler) and gate.type is Update and gate.callback is bot.check_allowed
    assert app.handlers[0], "the normal handlers are in group 0, after the gate"


def test_every_kind_of_message_has_a_handler(monkeypatch):
    app = build(monkeypatch)
    handlers = app.handlers[0]
    commands = [h for h in handlers if isinstance(h, CommandHandler)]
    assert [sorted(c.commands) for c in commands] == [["start"]]            # only /start: no /set or /show any more
    assert any(isinstance(h, MessageHandler) and h.callback is bot.handle_voice and h.filters is filters.VOICE
               for h in handlers), "voice notes must be handled, or they are silently ignored"
    assert any(isinstance(h, MessageHandler) and h.callback is bot.handle_text for h in handlers)
    patterns = {h.pattern.pattern for h in handlers if isinstance(h, CallbackQueryHandler)}
    assert patterns == {"^confirm:", "^nudge:"}


def test_the_bot_is_more_patient_than_the_library_default(monkeypatch):
    # python-telegram-bot's own default is 5 seconds, which one slow moment can miss.
    assert bot.NETWORK_TIMEOUT > 5 and bot.POLL_TIMEOUT > 5
    assert bot.STARTUP_RETRIES >= 1
    request = build(monkeypatch).bot.request
    assert request._client.timeout.connect == bot.NETWORK_TIMEOUT
    assert request._client.timeout.read == bot.NETWORK_TIMEOUT
