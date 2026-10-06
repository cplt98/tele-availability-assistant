"""Tests for bot.py. Telegram is replaced by simple fakes, so nothing connects to the internet."""

import asyncio
import json
from datetime import date, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import bot
import config
import db
import holidays_sg
import llm


def test_format_summary_groups_by_person():
    entries = [
        {"person": "Sam", "date": "2026-10-01", "status": "wfh"},
        {"person": "Sam", "date": "2026-10-02", "status": "half_day"},
        {"person": "Taylor", "date": "2026-10-05", "status": "office"},
    ]
    assert bot.format_summary(entries) == (
        "Sam:\nThu 1 Oct: WFH\nFri 2 Oct: Half day\nTaylor:\nMon 5 Oct: Office"
    )


def test_log_eval_appends_one_json_line(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env.test")

    bot.log_eval(123, "wfh tomorrow", {"entries": [], "question": None}, True)
    bot.log_eval(123, "hi", None, None, error="boom")

    lines = (tmp_path / "eval_log.jsonl").read_text(encoding="utf-8").splitlines()
    first, second = json.loads(lines[0]), json.loads(lines[1])
    assert first["message"] == "wfh tomorrow" and first["confirmed"] is True
    assert first["env"] == ".env.test"
    assert second["error"] == "boom" and second["result"] is None


# ---------- Answering questions ----------

TODAY = date(2026, 9, 30)  # a Wednesday


def one_day(person, day):
    return {"person": person, "start_date": day, "end_date": day}


def test_answer_when_sam_has_entered_it():
    rows = [("Sam", "2026-09-30", "wfh")]
    text, missing = bot.format_answer(one_day("Sam", "2026-09-30"), rows, ["Sam"], TODAY)
    assert text == "Sam on Wed 30 Sep: WFH"
    assert missing == {}


def test_answer_when_sam_has_not_entered_it():
    text, missing = bot.format_answer(one_day("Sam", "2026-10-01"), [], ["Sam"], TODAY)
    assert text == "Nothing from Sam for Thu 1 Oct yet."
    assert missing == {"Sam": ["2026-10-01"]}


def test_past_gaps_are_not_worth_asking_about():
    _, missing = bot.format_answer(one_day("Sam", "2026-09-29"), [], ["Sam"], TODAY)
    assert missing == {}


def test_weekend_gaps_are_not_worth_asking_about():
    _, missing = bot.format_answer(one_day("Sam", "2026-10-03"), [], ["Sam"], TODAY)  # a Saturday
    assert missing == {}


def test_week_answer_lists_each_day_and_flags_gaps():
    rows = [("Sam", "2026-09-28", "office"), ("Sam", "2026-10-01", "wfh")]
    query = {"person": "Sam", "start_date": "2026-09-28", "end_date": "2026-10-02"}
    text, missing = bot.format_answer(query, rows, ["Sam"], TODAY)
    assert text.splitlines() == [
        "Sam, Mon 28 Sep to Fri 2 Oct:",
        "Mon 28 Sep: Office",
        # Tue 29 Sep is already past and empty, so it is left out
        "Wed 30 Sep: not entered yet",
        "Thu 1 Oct: WFH",
        "Fri 2 Oct: not entered yet",
    ]
    assert missing == {"Sam": ["2026-09-30", "2026-10-02"]}


def test_everyone_answer_for_one_day():
    rows = [("Alex", "2026-10-01", "office")]
    text, missing = bot.format_answer(one_day(None, "2026-10-01"), rows, ["Alex", "Sam"], TODAY)
    assert text.splitlines() == ["Thu 1 Oct:", "Alex: Office", "Sam: not entered yet"]
    assert missing == {"Sam": ["2026-10-01"]}


def test_nobody_to_report_on():
    text, missing = bot.format_answer(one_day(None, "2026-10-01"), [], [], TODAY)
    assert text == "Nobody has entered a schedule for Thu 1 Oct yet." and missing == {}


def test_answer_matches_names_ignoring_case():
    rows = [("Sam", "2026-09-30", "office")]
    text, _ = bot.format_answer(one_day("sam", "2026-09-30"), rows, ["sam"], TODAY)
    assert "Office" in text


def test_find_user_id_needs_a_name_and_an_allowed_user(monkeypatch):
    monkeypatch.setattr(config, "USER_NAMES", {111: "Alex", 222: "Sam", 333: "Guest"})
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {111, 222})
    assert bot.find_user_id("sam") == 222
    assert bot.find_user_id("Guest") is None   # not on the allowlist
    assert bot.find_user_id("Taylor") is None     # no name mapping


def test_sender_name_uses_configured_name_or_first_name(monkeypatch):
    monkeypatch.setattr(config, "USER_NAMES", {222: "Sam"})
    assert bot.sender_name(NS(id=222, first_name="Riley")) == "Sam"
    assert bot.sender_name(NS(id=999, first_name="jordan")) == "Jordan"


# A pretend Telegram chat, used to run the whole ask -> button -> message-Sam flow.

def make_message(text=""):
    replies = []

    async def reply_text(t, reply_markup=None):
        replies.append((t, reply_markup))

    return NS(text=text, reply_text=reply_text, replies=replies, reply_markup=None)


def test_question_flow_answers_then_asks_sam_to_update(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env.test")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake")
    monkeypatch.setattr(config, "USER_NAMES", {111: "Alex", 222: "Sam"})
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {111, 222, 333})
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()

    day = llm.today_sg().isoformat()
    reply = {"intent": "query", "entries": [], "question": None,
             "queries": [{"person": "sam", "start_date": day, "end_date": day}]}
    monkeypatch.setattr(llm, "_call_model", lambda text, today, sender: json.dumps(reply))

    jordan = NS(id=333, first_name="Jordan")
    context = NS(user_data={}, bot_data={}, bot=NS(send_message=AsyncMock()))

    async def run():
        # Jordan asks, without a question mark.
        message = make_message("is sam wfh today")
        await bot.handle_text(NS(effective_user=jordan, message=message), context)
        text, markup = message.replies[0]
        assert text.startswith("Nothing from Sam for")
        button = markup.inline_keyboard[0][0]
        assert button.text == "Ask Sam to update"

        # She presses the button: Sam gets a Telegram message.
        pressed = NS(data=button.callback_data, message=make_message(), edit_message_reply_markup=AsyncMock(),
                     answer=AsyncMock())
        pressed.message.reply_markup = markup
        await bot.nudge_callback(NS(callback_query=pressed), context)
        context.bot.send_message.assert_awaited_once()
        kwargs = context.bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == 222
        assert "Jordan was checking" in kwargs["text"]
        assert pressed.message.replies[-1][0] == "Done, I've asked Sam to update."

        # Pressing it again straight away doesn't spam Sam.
        again = NS(data=button.callback_data, message=make_message(), edit_message_reply_markup=AsyncMock(),
                   answer=AsyncMock())
        again.message.reply_markup = None
        await bot.nudge_callback(NS(callback_query=again), context)
        assert context.bot.send_message.await_count == 1
        assert "already asked" in again.message.replies[-1][0]

    asyncio.run(run())
    assert db.get_statuses_for_date(day) == []  # asking a question never saves anything


def test_question_about_someone_who_has_entered_gets_the_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env.test")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake")
    monkeypatch.setattr(config, "USER_NAMES", {})
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()

    day = llm.today_sg().isoformat()
    db.set_status("Sam", day, "office", "test")
    reply = {"intent": "query", "entries": [], "question": None,
             "queries": [{"person": "Sam", "start_date": day, "end_date": day}]}
    monkeypatch.setattr(llm, "_call_model", lambda text, today, sender: json.dumps(reply))

    message = make_message("where is sam")
    context = NS(user_data={}, bot_data={})
    asyncio.run(bot.handle_text(NS(effective_user=NS(id=333, first_name="Jordan"), message=message), context))

    text, markup = message.replies[0]
    assert text.endswith(": Office") and markup is None  # answered, and no button since nothing is missing


# ---------- Holidays and leave in answers ----------

def test_leave_shows_as_on_leave():
    rows = [("Sam", "2026-10-01", "leave")]
    text, missing = bot.format_answer(one_day("Sam", "2026-10-01"), rows, ["Sam"], TODAY)
    assert text == "Sam on Thu 1 Oct: On leave"
    assert missing == {}   # on leave counts as answered, so nobody is asked to update


def test_public_holiday_with_no_entry_is_not_a_gap():
    public = {"2026-12-25": "Christmas Day"}
    text, missing = bot.format_answer(one_day("Sam", "2026-12-25"), [], ["Sam"], TODAY, public=public)
    assert text == "Fri 25 Dec is a public holiday (Christmas Day), so nothing is needed from Sam."
    assert missing == {}


def test_public_holiday_with_an_entry_shows_both():
    public = {"2026-12-25": "Christmas Day"}
    rows = [("Sam", "2026-12-25", "office")]
    text, _ = bot.format_answer(one_day("Sam", "2026-12-25"), rows, ["Sam"], TODAY, public=public)
    assert text == "Sam on Fri 25 Dec: Office (public holiday: Christmas Day)"


def test_week_answer_with_a_public_holiday():
    public = {"2026-12-25": "Christmas Day"}
    query = {"person": "Sam", "start_date": "2026-12-21", "end_date": "2026-12-25"}
    text, missing = bot.format_answer(query, [], ["Sam"], date(2026, 12, 21), public=public)
    assert "Fri 25 Dec: Public holiday (Christmas Day)" in text.splitlines()
    assert "2026-12-25" not in missing["Sam"]
    assert len(missing["Sam"]) == 4   # Mon to Thu still need entering


def test_everyone_on_a_public_holiday():
    public = {"2026-12-25": "Christmas Day"}
    text, missing = bot.format_answer(one_day(None, "2026-12-25"), [], ["Alex", "Sam"], TODAY, public=public)
    assert text.splitlines() == ["Fri 25 Dec:", "Alex: public holiday", "Sam: public holiday"]
    assert missing == {}


# ---------- Clearing a day ----------


def test_clearing_a_day_deletes_it_only_after_yes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env.test")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.set_status("Alex", "2026-12-08", "leave", "test")

    reply = {"intent": "update", "queries": [], "question": None,
             "entries": [{"person": None, "date": "2026-12-08", "status": "clear"}]}
    monkeypatch.setattr(llm, "_call_model", lambda text, today, sender: json.dumps(reply))
    context = NS(user_data={}, bot_data={})
    user = NS(id=111, first_name="Alex")

    async def run():
        message = make_message("actually not sure about 8 dec")
        await bot.handle_text(NS(effective_user=user, message=message), context)
        assert message.replies[0][0] == "Alex:\nTue 8 Dec: cleared (back to not entered)\nCorrect?"
        assert db.get_statuses_for_date("2026-12-08") == [("Alex", "leave")]   # still there before Yes

        pressed = NS(data="confirm:yes", from_user=user, message=make_message(),
                     edit_message_reply_markup=AsyncMock(), answer=AsyncMock())
        await bot.confirm_callback(NS(callback_query=pressed), context)

    asyncio.run(run())
    assert db.get_statuses_for_date("2026-12-08") == []


# ---------- The conversation log: reply and ignored records ----------

import pytest
from telegram.ext import ApplicationHandlerStop


def read_log(tmp_path):
    path = tmp_path / "eval_log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """Point the log at a temporary folder and set the usual test config."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env.test")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake")
    monkeypatch.setattr(config, "USER_NAMES", {})
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return tmp_path


def test_send_reply_sends_and_writes_a_reply_record(log_dir):
    message = make_message()
    asyncio.run(bot.send_reply(message, 111, "Saved!"))

    assert message.replies == [("Saved!", None)]   # it really was sent
    [record] = read_log(log_dir)
    assert record["kind"] == "reply"
    assert record["user_id"] == 111
    assert record["text"] == "Saved!"
    assert record["env"] == ".env.test" and record["time"]
    assert set(record) == {"time", "env", "kind", "user_id", "text"}


def test_nothing_is_logged_if_the_reply_could_not_be_sent(log_dir):
    async def broken(text, reply_markup=None):
        raise RuntimeError("telegram is down")

    with pytest.raises(RuntimeError):
        asyncio.run(bot.send_reply(NS(reply_text=broken), 111, "Saved!"))
    assert read_log(log_dir) == []


def test_ignored_record_has_the_id_and_never_the_message(log_dir, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {111})
    stranger = NS(effective_user=NS(id=999, first_name="Stranger"), message=NS(text="my secret words"))

    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(bot.check_allowed(stranger, NS()))

    [record] = read_log(log_dir)
    assert record["kind"] == "ignored" and record["user_id"] == 999
    assert set(record) == {"time", "env", "kind", "user_id"}
    raw = (log_dir / "eval_log.jsonl").read_text(encoding="utf-8")
    assert "my secret words" not in raw and "Stranger" not in raw


def test_an_update_with_no_user_is_logged_as_ignored_too(log_dir, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {111})
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(bot.check_allowed(NS(effective_user=None), NS()))
    assert read_log(log_dir)[0]["kind"] == "ignored" and read_log(log_dir)[0]["user_id"] is None


def test_an_allowed_user_is_not_logged_as_ignored(log_dir, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {111})
    asyncio.run(bot.check_allowed(NS(effective_user=NS(id=111)), NS()))   # no exception
    assert read_log(log_dir) == []


def test_parse_records_keep_their_old_fields_and_gain_a_kind(log_dir):
    bot.log_eval(123, "wfh tomorrow", {"entries": []}, True)
    [record] = read_log(log_dir)
    assert record["kind"] == "parse"
    for old_field in ("time", "env", "user_id", "message", "result", "confirmed", "error"):
        assert old_field in record
    assert record["message"] == "wfh tomorrow" and record["confirmed"] is True


def test_secrets_are_scrubbed_from_the_log(log_dir, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:SECRET-token")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "AIza-secret-key")
    bot.log_eval(1, "hi", None, None, error="401 for key AIza-secret-key on bot123456:SECRET-token")
    bot.log_reply(1, "token is 123456:SECRET-token")
    raw = (log_dir / "eval_log.jsonl").read_text(encoding="utf-8")
    assert "SECRET-token" not in raw and "AIza-secret-key" not in raw
    assert raw.count("[redacted]") == 3


def test_a_problem_with_the_log_file_does_not_stop_the_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "does" / "not" / "exist")
    monkeypatch.setattr(config, "ENV_FILE", None)
    message = make_message()
    asyncio.run(bot.send_reply(message, 1, "still works"))   # no exception
    assert message.replies == [("still works", None)]


def test_a_whole_question_flow_logs_every_reply(log_dir, monkeypatch):
    monkeypatch.setattr(config, "USER_NAMES", {111: "Alex"})
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {111, 333})
    day = llm.today_sg().isoformat()
    reply = {"intent": "query", "entries": [], "question": None,
             "queries": [{"person": "alex", "start_date": day, "end_date": day}]}
    monkeypatch.setattr(llm, "_call_model", lambda text, today, sender: json.dumps(reply))

    jordan = NS(id=333, first_name="Jordan")
    context = NS(user_data={}, bot_data={}, bot=NS(send_message=AsyncMock()))

    async def run():
        message = make_message("is alex wfh today")
        await bot.handle_text(NS(effective_user=jordan, message=message), context)
        text, markup = message.replies[0]
        button = markup.inline_keyboard[0][0]

        pressed = NS(data=button.callback_data, from_user=jordan, message=make_message(),
                     edit_message_reply_markup=AsyncMock(), answer=AsyncMock())
        pressed.message.reply_markup = markup
        await bot.nudge_callback(NS(callback_query=pressed), context)
        return text, pressed.message.replies[-1][0]

    answer, done = asyncio.run(run())
    records = read_log(log_dir)
    kinds = [(r["kind"], r["user_id"]) for r in records]

    assert ("parse", 333) in kinds                                   # what the AI understood
    replies = [(r["user_id"], r["text"]) for r in records if r["kind"] == "reply"]
    assert (333, answer) in replies                                  # the answer jordan got
    assert (333, done) in replies                                    # "Done, I've asked Alex to update."
    [ask] = [text for uid, text in replies if uid == 111]            # the message that went to Alex
    assert "Jordan was checking" in ask


def test_confirmation_saved_and_no_problem_replies_are_logged(log_dir, monkeypatch):
    reply = {"intent": "update", "queries": [], "question": None,
             "entries": [{"person": None, "date": "2026-12-08", "status": "wfh"}]}
    monkeypatch.setattr(llm, "_call_model", lambda text, today, sender: json.dumps(reply))
    context = NS(user_data={}, bot_data={})
    user = NS(id=111, first_name="Alex")

    async def press(data):
        pressed = NS(data=data, from_user=user, message=make_message(),
                     edit_message_reply_markup=AsyncMock(), answer=AsyncMock())
        await bot.confirm_callback(NS(callback_query=pressed), context)

    async def run():
        await bot.handle_text(NS(effective_user=user, message=make_message("wfh 8 dec")), context)
        await press("confirm:no")
        await bot.handle_text(NS(effective_user=user, message=make_message("actually wfh")), context)
        await press("confirm:yes")

    asyncio.run(run())
    texts = [r["text"] for r in read_log(log_dir) if r["kind"] == "reply"]
    assert texts == [
        "Alex:\nTue 8 Dec: WFH\nCorrect?",
        "No problem. What should I change?",
        "Alex:\nTue 8 Dec: WFH\nCorrect?",
        "Saved!",
    ]


def test_sorry_try_again_is_logged(log_dir, monkeypatch):
    def down(text, today, sender):
        raise llm.LLMError("429 rate limit")

    monkeypatch.setattr(llm, "_call_model", down)
    asyncio.run(bot.handle_text(NS(effective_user=NS(id=111, first_name="Alex"), message=make_message("hi")), NS(user_data={}, bot_data={})))
    records = read_log(log_dir)
    assert [(r["kind"], r.get("text")) for r in records if r["kind"] == "reply"] == [("reply", "Sorry, try again.")]
    assert any(r["kind"] == "parse" and r["error"] for r in records)


# ---------- Settings from the host's environment variables (no .env file) ----------

import os


def test_log_lines_say_environment_when_there_is_no_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ENV_FILE", None)
    bot.log_reply(1, "hi")
    assert json.loads((tmp_path / "eval_log.jsonl").read_text(encoding="utf-8"))["env"] == "environment"
