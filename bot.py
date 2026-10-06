"""The Telegram bot: the allowlist check, the command handlers, the plain-language
schedule flow (parse -> confirm -> save), and startup."""

import argparse
import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import config
import db
import holidays_sg
import llm
import voice

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
# httpx logs every request URL at INFO level, and those URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("bot")


def sender_name(user):
    """The name from users.json for this Telegram user, otherwise their Telegram first name."""
    name = config.USER_NAMES.get(user.id) or user.first_name
    return llm.normalize_person(name) or name


async def check_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs before every other handler. Stops updates from anyone not on the allowlist."""
    user = update.effective_user
    if user is None or user.id not in config.ALLOWED_USER_IDS:
        # Log the ID only, never the message content.
        logger.warning("Ignored update from user ID %s", user.id if user else "unknown")
        log_ignored(user.id if user else None)
        raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = sender_name(update.effective_user)
    await send_reply(
        update.message, update.effective_user.id,
        f"Hi {name}! I'm the availability assistant. Tell me where you'll be "
        "(e.g. 'WFH tomorrow') or ask where someone is (e.g. 'is Sam in on Wednesday?'). "
        "You can type it or send a voice note.",
    )


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update.message, update.effective_user.id, "Got it, I can't understand that yet.")


# ---------- Plain-language schedules ----------
#
# Each user has a "pending" dict in context.user_data that remembers where the
# conversation is. Its "stage" is one of:
#   "answer"  - the bot asked a question and is waiting for the reply
#   "confirm" - the bot showed a summary and is waiting for Yes / No
#   "change"  - the user pressed No and the bot is waiting for what to change

STATUS_LABELS = {
    "office": "Office",
    "wfh": "WFH",
    "half_day": "Half day",
    "leave": "On leave",
    llm.CLEAR: "cleared (back to not entered)",
}

# Don't ask the same person to update more than once in this many seconds.
NUDGE_COOLDOWN = 30 * 60


def fmt_date(iso):
    """"2026-10-01" -> "Thu 1 Oct"."""
    day = datetime.strptime(iso, "%Y-%m-%d")
    return f"{day:%a} {day.day} {day:%b}"


def format_summary(entries):
    """Group entries by person, e.g.

    Sam:
    Fri 3 Oct: WFH
    """
    lines = []
    last_person = None
    for entry in entries:  # already sorted by person, then date
        if entry["person"] != last_person:
            lines.append(f"{entry['person']}:")
            last_person = entry["person"]
        lines.append(f"{fmt_date(entry['date'])}: {STATUS_LABELS[entry['status']]}")
    return "\n".join(lines)


def format_date_range(start, end):
    """"Mon 1 Dec" for one day, "Mon 1 Dec to Fri 5 Dec" for a range (ISO dates in)."""
    return fmt_date(start) if start == end else f"{fmt_date(start)} to {fmt_date(end)}"


# ---------- The conversation log (eval_log.jsonl) ----------
#
# One JSON line per event. The "kind" field says which:
#   "parse"   - a message we asked the AI to read, what it understood, and whether the user confirmed
#   "reply"   - something the bot sent to a user
#   "ignored" - someone not on the allowlist (their ID only, never what they wrote)
#   "voice_error" - speech to text or text to speech failed (the bot carried on in text)

def _write_log(record):
    """Append one record to eval_log.jsonl (in DATA_DIR).

    The bot token and the API keys are scrubbed from whatever is written, and a problem
    with the log file never stops the bot from working.
    """
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "env": config.ENV_FILE.name if config.ENV_FILE else "environment",
        **record,
    }
    line = json.dumps(record, ensure_ascii=False)
    for secret in (config.TELEGRAM_BOT_TOKEN, config.GEMINI_API_KEY, config.ELEVENLABS_API_KEY):
        if secret:
            line = line.replace(secret, "[redacted]")
    try:
        with open(config.DATA_DIR / "eval_log.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        logger.warning("Could not write eval_log.jsonl: %s", type(e).__name__)


def log_eval(user_id, message, result, confirmed, error=None):
    """Log a message the AI read, so we can check its accuracy later.

    confirmed: True (pressed Yes), False (pressed No), None (never got that far).
    """
    _write_log({
        "kind": "parse",
        "user_id": user_id,
        "message": message,
        "result": result,
        "confirmed": confirmed,
        "error": error,
    })


def log_reply(user_id, text):
    """Log something the bot sent. user_id is who received it."""
    _write_log({"kind": "reply", "user_id": user_id, "text": text})


def log_ignored(user_id):
    """Log that the allowlist blocked someone. Only their ID is kept, never their message."""
    _write_log({"kind": "ignored", "user_id": user_id})


def log_voice_error(stage, user_id, error):
    """Log a voice problem. stage is "stt" (listening), "tts" (speaking) or "send"."""
    logger.error("Voice problem (%s): %s", stage, error)
    _write_log({"kind": "voice_error", "stage": stage, "user_id": user_id, "error": str(error)})


async def send_reply(message, user_id, text, reply_markup=None, speak=False, spoken=None):
    """Reply to a message and log what was sent (after it has actually been sent).

    With speak=True the reply is also sent as a voice message, saying `spoken` if given
    (for example the answer without a lead-in line) or else `text`. The text always goes first.
    """
    sent = await message.reply_text(text, reply_markup=reply_markup)
    log_reply(user_id, text)
    if speak:
        await speak_reply(message, user_id, text if spoken is None else spoken)
    return sent


async def speak_reply(message, user_id, text):
    """Send `text` as a spoken voice message too.

    The text reply has already been sent, so if speaking fails (no credits, no network,
    voice not set up) it is only logged. The bot never stops answering because of voice.
    """
    if not voice.can_speak():
        return
    try:
        audio = await asyncio.to_thread(voice.speak, text)
        await message.reply_voice(voice=InputFile(audio, filename="reply.ogg"))
    except voice.VoiceError as e:
        log_voice_error("tts", user_id, e)
    except TelegramError as e:
        log_voice_error("send", user_id, e)


def _callback_user_id(query):
    """The ID of whoever pressed a button, or None if Telegram didn't say."""
    return getattr(getattr(query, "from_user", None), "id", None)


def _seconds(duration):
    """Telegram's voice-note length as a number of seconds (it may arrive as a timedelta)."""
    return duration.total_seconds() if hasattr(duration, "total_seconds") else duration


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A voice note: turn it into text, then handle it exactly like a typed message.

    The reply starts with what was heard, so a mishearing is obvious. Answers to questions
    are also spoken (when voice output is set up).
    """
    user_id = update.effective_user.id
    note = update.message.voice

    if not voice.can_transcribe():
        await send_reply(update.message, user_id, "Voice notes aren't set up yet. Please type your message.")
        return
    if _seconds(note.duration) > voice.MAX_VOICE_SECONDS:
        await send_reply(
            update.message, user_id,
            f"That voice note is too long (the limit is {voice.MAX_VOICE_SECONDS} seconds). "
            "Please send a shorter one, or type it.",
        )
        return

    try:
        telegram_file = await context.bot.get_file(note.file_id)
        audio = bytes(await telegram_file.download_as_bytearray())
        transcript = await asyncio.to_thread(voice.transcribe, audio)
    except (voice.VoiceError, TelegramError) as e:
        log_voice_error("stt", user_id, e)
        await send_reply(update.message, user_id, "Sorry, I couldn't understand that voice note. Please try again, or type it.")
        return

    await process_text(update, context, transcript, heard=transcript)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any non-command text: ask the model what schedule the user means."""
    await process_text(update, context, update.message.text)


async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, message, heard=None):
    """Work out what `message` means and reply. Typed text and voice transcripts both come here.

    heard is the transcript when the message came as a voice note. The reply then starts with
    `I heard: "..."`, in the same message, and an answer to a question is also spoken. Everything
    else (summaries with Yes/No buttons, questions back, "Saved!") stays text, because the user
    has to read and tap those anyway.
    """
    user_id = update.effective_user.id
    by_voice = heard is not None
    lead_in = f'I heard: "{heard}"\n\n' if by_voice else ""
    pending = context.user_data.get("pending")

    # If this message answers a question or a "what to change?", include the earlier
    # conversation so the model sees the whole picture.
    if pending and pending["stage"] == "answer":
        text = f"{pending['context']}\nQuestion asked: {pending['question']}\nUser's answer: {message}"
    elif pending and pending["stage"] == "change":
        summary = format_summary(pending["result"]["entries"])
        text = f"{pending['context']}\nProposed schedule:\n{summary}\nUser wants this changed: {message}"
    else:
        text = message
        if pending:  # a summary was shown but the user never pressed Yes or No
            log_eval(user_id, pending["context"], pending["result"], None)

    sender = sender_name(update.effective_user)
    try:
        result = await asyncio.to_thread(llm.parse_schedule, text, llm.today_sg(), sender)
    except llm.LLMError as e:
        logger.error("Model call failed: %s", e)
        log_eval(user_id, text, None, None, error=str(e))
        await send_reply(update.message, user_id, lead_in + "Sorry, try again.")
        return

    if result["question"]:
        context.user_data["pending"] = {
            "stage": "answer",
            "context": text,
            "question": result["question"],
            "result": result,
        }
        log_eval(user_id, text, result, None)
        await send_reply(update.message, user_id, lead_in + result["question"])
        return

    if result["intent"] == "query":
        # Someone is asking, not telling. Answer from the database (nothing to confirm or save).
        context.user_data.pop("pending", None)
        log_eval(user_id, text, result, None)
        await answer_queries(update, context, result, sender, lead_in=lead_in, speak=by_voice)
        return

    context.user_data["pending"] = {"stage": "confirm", "context": text, "result": result}
    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Yes", callback_data="confirm:yes"),
          InlineKeyboardButton("No", callback_data="confirm:no")]]
    )
    await send_reply(update.message, user_id, lead_in + f"{format_summary(result['entries'])}\nCorrect?", reply_markup=buttons)


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The user pressed Yes or No under a summary."""
    query = update.callback_query
    await query.answer()  # stops the button's loading spinner
    await query.edit_message_reply_markup(reply_markup=None)  # remove the buttons

    pending = context.user_data.get("pending")
    if not pending or pending["stage"] != "confirm":
        await send_reply(query.message, query.from_user.id, "That request has expired. Please send your schedule again.")
        return

    user = query.from_user
    if query.data == "confirm:yes":
        # Each entry says whose schedule it is (e.g. Jordan typing "sam is WFH Friday" saves for Sam).
        for entry in pending["result"]["entries"]:
            if entry["status"] == llm.CLEAR:  # changed their mind: back to "not entered"
                db.delete_status(entry["person"], entry["date"])
            else:  # a new status simply replaces any earlier one
                db.set_status(entry["person"], entry["date"], entry["status"], source="llm")
        log_eval(user.id, pending["context"], pending["result"], True)
        del context.user_data["pending"]
        await send_reply(query.message, user.id, "Saved!")
    else:
        log_eval(user.id, pending["context"], pending["result"], False)
        pending["stage"] = "change"
        await send_reply(query.message, user.id, "No problem. What should I change?")


# ---------- Answering questions ("is sam wfh today?") ----------

def find_user_id(person):
    """The Telegram ID for a name in users.json, or None if we can't message them."""
    for user_id, name in config.USER_NAMES.items():
        if name.lower() == person.lower() and user_id in config.ALLOWED_USER_IDS:
            return user_id
    return None


def format_answer(query, rows, people, today, public=None):
    """Turn database rows into a reply. The AI plays no part in this.

    query: {"start_date", "end_date"}; rows: (person, date, status) from the database;
    people: the names to report on; today: a date.
    public: {iso date: name} of public holidays.
    Returns (text, missing) where missing maps each person to the upcoming work
    days (ISO dates, today or later) they haven't entered yet. Public holidays never
    count as missing, because there is nothing to enter.
    """
    public = public or {}
    start = date.fromisoformat(query["start_date"])
    end = date.fromisoformat(query["end_date"])
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    single = len(days) == 1
    span = format_date_range(start.isoformat(), end.isoformat())

    status = {(person.lower(), day): s for person, day, s in rows}

    def label(person, day):
        s = status.get((person.lower(), day.isoformat()))
        return STATUS_LABELS[s] if s else None

    lines = []
    if not people:
        lines.append(f"Nobody has entered a schedule for {span} yet.")
    else:
        if len(people) > 1:
            lines.append(f"{span}:")
        for person in people:
            if single:
                found = label(person, days[0])
                holiday = public.get(days[0].isoformat())
                if len(people) > 1:
                    lines.append(f"{person}: {found or ('public holiday' if holiday else 'not entered yet')}")
                elif found:
                    lines.append(f"{person} on {span}: {found}" + (f" (public holiday: {holiday})" if holiday else ""))
                elif holiday:
                    lines.append(f"{span} is a public holiday ({holiday}), so nothing is needed from {person}.")
                else:
                    lines.append(f"Nothing from {person} for {span} yet.")
            else:
                lines.append(f"{person}, {span}:" if len(people) == 1 else f"{person}:")
                for day in days:
                    iso = day.isoformat()
                    found = label(person, day)
                    if found:
                        lines.append(f"{fmt_date(iso)}: {found}" + (f" (public holiday: {public[iso]})" if iso in public else ""))
                    elif iso in public:
                        lines.append(f"{fmt_date(iso)}: Public holiday ({public[iso]})")
                    elif day.weekday() < 5 and day >= today:  # skip weekends and days already gone
                        lines.append(f"{fmt_date(iso)}: not entered yet")

    missing = {}
    for person in people:
        dates = [
            d.isoformat() for d in days
            if d >= today and d.weekday() < 5 and d.isoformat() not in public and label(person, d) is None
        ]
        if dates:
            missing[person] = dates
    return "\n".join(lines), missing


def describe_dates(dates):
    """["2026-10-01", "2026-10-02"] -> "Thu 1 Oct and Fri 2 Oct"."""
    if len(dates) <= 2:
        return " and ".join(fmt_date(d) for d in dates)
    return f"{fmt_date(dates[0])} to {fmt_date(dates[-1])}"


async def answer_queries(update: Update, context: ContextTypes.DEFAULT_TYPE, result, sender, lead_in="", speak=False):
    """Reply with what's in the database, plus "Ask X to update" buttons for people with gaps."""
    today = llm.today_sg()
    texts = []
    missing_all = {}  # person -> set of missing dates
    for q in result["queries"]:
        rows = db.get_statuses_between(q["start_date"], q["end_date"], q["person"])
        if q["person"]:
            people = [q["person"]]
        else:  # everyone: whoever has entered something, plus everyone listed in users.json
            people = sorted({r[0] for r in rows} | set(config.USER_NAMES.values()))
        start, end = date.fromisoformat(q["start_date"]), date.fromisoformat(q["end_date"])

        text, missing = format_answer(q, rows, people, today, public=holidays_sg.public_holidays(start, end))
        texts.append(text)
        for person, dates in missing.items():
            missing_all.setdefault(person, set()).update(dates)

    options = []   # remembered so the button press knows who and which dates
    buttons = []
    cannot_message = []
    for person, dates in sorted(missing_all.items()):
        if person.lower() == sender.lower():
            continue  # they can just tell me their own schedule
        if find_user_id(person) is None:
            cannot_message.append(person)
            continue
        options.append({"person": person, "dates": sorted(dates), "requester": sender})
        buttons.append([InlineKeyboardButton(f"Ask {person} to update", callback_data=f"nudge:{len(options) - 1}")])
    context.user_data["nudges"] = options

    reply = "\n\n".join(texts)
    if cannot_message:
        reply += f"\n\nI can't message {', '.join(cannot_message)} because they aren't listed in users.json."
    await send_reply(
        update.message, update.effective_user.id, lead_in + reply,
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        speak=speak, spoken=reply,   # the spoken version is just the answer, not the lead-in
    )


async def nudge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The user pressed "Ask Sam to update": message Sam on Telegram."""
    query = update.callback_query
    await query.answer()

    who = _callback_user_id(query)  # whoever pressed the button; replies below go to them

    # Remove just the button that was pressed, keep any others.
    keyboard = query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
    remaining = [[b for b in row if b.callback_data != query.data] for row in keyboard]
    remaining = [row for row in remaining if row]
    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(remaining) if remaining else None)
    except TelegramError:
        pass  # the buttons were already gone; carry on

    try:
        option = context.user_data["nudges"][int(query.data.split(":")[1])]
    except (KeyError, IndexError, ValueError):
        await send_reply(query.message, who, "That request has expired. Please ask again.")
        return

    person = option["person"]
    target_id = find_user_id(person)
    if target_id is None:
        await send_reply(query.message, who, f"I can't message {person} because they aren't listed in users.json.")
        return

    nudged = context.bot_data.setdefault("nudged", {})
    last = nudged.get(person.lower())
    if last is not None and time.monotonic() - last < NUDGE_COOLDOWN:
        await send_reply(query.message, who, f"I already asked {person} a little while ago. Give it a bit before I ask again.")
        return

    message = (
        f"Hi {person}! {option['requester']} was checking where you are on "
        f"{describe_dates(option['dates'])}, but nothing has been entered yet. "
        "Please reply with your schedule, e.g. 'WFH Thu' or 'office Mon to Wed'."
    )
    try:
        await context.bot.send_message(chat_id=target_id, text=message)
    except TelegramError as e:
        logger.warning("Could not message %s (ID %s): %s", person, target_id, e)
        await send_reply(query.message, who, f"I couldn't reach {person} on Telegram. {person} may need to send /start to me first.")
        return
    log_reply(target_id, message)  # the message that went to the person being asked

    nudged[person.lower()] = time.monotonic()
    logger.info("Asked %s (ID %s) to update their schedule", person, target_id)
    await send_reply(query.message, who, f"Done, I've asked {person} to update.")


# python-telegram-bot waits only 5 seconds for Telegram by default, which a slow or busy
# connection can miss. These are more patient.
NETWORK_TIMEOUT = 20    # seconds to wait for Telegram to connect and reply
POLL_TIMEOUT = 30       # seconds to wait during the long poll for new messages
STARTUP_RETRIES = 5     # times to retry if Telegram can't be reached at startup


def build_application():
    """Create the Telegram application and register what each kind of message does."""
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .connect_timeout(NETWORK_TIMEOUT)
        .read_timeout(NETWORK_TIMEOUT)
        .write_timeout(NETWORK_TIMEOUT)
        .get_updates_connect_timeout(NETWORK_TIMEOUT)
        .get_updates_read_timeout(POLL_TIMEOUT)
        .build()
    )

    # group=-1 runs before the normal handlers (group 0), so it acts as a gatekeeper.
    app.add_handler(TypeHandler(Update, check_allowed), group=-1)

    app.add_handler(CommandHandler("start", start))
    # Plain text and voice notes go to the model; the Yes/No buttons under a summary come back as callbacks.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern="^confirm:"))
    app.add_handler(CallbackQueryHandler(nudge_callback, pattern="^nudge:"))
    # Unknown /commands get the same reply as anything the bot can't understand.
    app.add_handler(MessageHandler(filters.COMMAND, fallback))
    return app


def main():
    parser = argparse.ArgumentParser(description="Availability assistant Telegram bot")
    parser.add_argument("--env", default=".env", help="env file to use (default: .env)")
    args = parser.parse_args()

    config.load(args.env)
    db.DB_PATH = config.DB_PATH
    db.init_db()

    source = f"env file: {config.ENV_FILE}" if config.ENV_FILE else "environment variables (no .env file)"
    print(f"Using {source} | database: {config.DB_PATH}")
    print(f"Understanding messages with Gemini model {config.GEMINI_MODEL}")
    if voice.can_speak():
        print(f"Voice: ON, voice notes in and spoken replies out (speech to text {config.ELEVENLABS_STT_MODEL}, text to speech {config.ELEVENLABS_TTS_MODEL})")
    elif voice.can_transcribe():
        print("Voice: voice notes in, text replies out (set ELEVENLABS_VOICE_ID for spoken replies)")
    else:
        print("Voice: OFF (set ELEVENLABS_API_KEY to turn it on)")
    print("Users: " + ", ".join(sorted(config.USER_NAMES.values())))

    app = build_application()
    logger.info("Bot is running. Press Ctrl+C to stop.")
    # bootstrap_retries: if Telegram can't be reached at startup, try again a few times
    # instead of giving up on the first slow moment.
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=STARTUP_RETRIES)


if __name__ == "__main__":
    main()
