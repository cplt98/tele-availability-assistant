"""Tests for llm.py. The model is replaced by a fake, so no test calls the real API."""

import json
from datetime import date

import pytest

import llm

# A Wednesday. Its work week is Mon 2026-09-28 to Fri 2026-10-02.
TODAY = date(2026, 9, 30)
SENDER = "Alex"


def fake_model(monkeypatch, reply):
    """Make llm._call_model return `reply` (a dict, turned into JSON text)."""
    text = reply if isinstance(reply, str) else json.dumps(reply)
    monkeypatch.setattr(llm, "_call_model", lambda message, today, sender: text)


def parse(text="whatever"):
    return llm.parse_schedule(text, TODAY, SENDER)


def test_valid_reply_is_returned_sorted(monkeypatch):
    fake_model(monkeypatch, {
        "entries": [
            {"date": "2026-10-02", "status": "wfh"},
            {"date": "2026-10-01", "status": "office"},
        ],
        "question": None,
    })
    assert parse() == {
        "intent": "update",
        "entries": [
            {"person": "Alex", "date": "2026-10-01", "status": "office"},
            {"person": "Alex", "date": "2026-10-02", "status": "wfh"},
        ],
        "queries": [],
        "question": None,
    }


def test_no_person_means_the_sender(monkeypatch):
    fake_model(monkeypatch, {"entries": [{"person": None, "date": "2026-10-01", "status": "wfh"}]})
    assert parse()["entries"][0]["person"] == "Alex"


@pytest.mark.parametrize("word", ["I", "me", "My", "", "   "])
def test_self_words_mean_the_sender(monkeypatch, word):
    fake_model(monkeypatch, {"entries": [{"person": word, "date": "2026-10-01", "status": "wfh"}]})
    assert parse()["entries"][0]["person"] == "Alex"


def test_named_person_is_used_instead_of_the_sender(monkeypatch):
    # Jordan (the sender) types "sam is WFH friday": it belongs to Sam, not Jordan.
    fake_model(monkeypatch, {"entries": [{"person": "sam", "date": "2026-10-02", "status": "wfh"}]})
    result = llm.parse_schedule("sam is WFH friday", TODAY, "Jordan")
    assert result["entries"] == [{"person": "Sam", "date": "2026-10-02", "status": "wfh"}]


def test_names_are_tidied_so_they_match(monkeypatch):
    fake_model(monkeypatch, {"entries": [
        {"person": "  SAM ", "date": "2026-10-01", "status": "wfh"},
        {"person": "sam", "date": "2026-10-02", "status": "office"},
        {"person": "jamie lee", "date": "2026-10-01", "status": "office"},
    ]})
    people = {(e["person"], e["date"]) for e in parse()["entries"]}
    assert people == {("Sam", "2026-10-01"), ("Sam", "2026-10-02"), ("Jamie Lee", "2026-10-01")}


def test_several_people_in_one_message(monkeypatch):
    fake_model(monkeypatch, {"entries": [
        {"person": "Taylor", "date": "2026-10-01", "status": "office"},
        {"person": "Sam", "date": "2026-10-01", "status": "wfh"},
        {"person": None, "date": "2026-10-01", "status": "half_day"},
    ]})
    entries = parse()["entries"]
    assert [(e["person"], e["status"]) for e in entries] == [
        ("Alex", "half_day"), ("Sam", "wfh"), ("Taylor", "office"),
    ]


def test_odd_person_name_is_rejected(monkeypatch):
    fake_model(monkeypatch, {"entries": [{"person": "Sam; DROP TABLE", "date": "2026-10-01", "status": "wfh"}]})
    result = parse()
    assert result["entries"] == [] and result["question"]


def test_mixed_statuses_in_one_message(monkeypatch):
    fake_model(monkeypatch, {
        "entries": [
            {"date": "2026-09-28", "status": "office"},
            {"date": "2026-09-29", "status": "office"},
            {"date": "2026-09-30", "status": "half_day"},
            {"date": "2026-10-01", "status": "wfh"},
            {"date": "2026-10-02", "status": "wfh"},
        ],
    })
    result = parse("office Mon Tue, half day Wed, rest WFH")
    assert [e["status"] for e in result["entries"]] == ["office", "office", "half_day", "wfh", "wfh"]
    assert result["question"] is None


def test_question_is_passed_through(monkeypatch):
    fake_model(monkeypatch, {"entries": [], "question": "Which Thursday do you mean?"})
    result = parse("Thu wfh")
    assert result["question"] == "Which Thursday do you mean?"
    assert result["entries"] == []


def test_empty_reply_gets_a_default_question(monkeypatch):
    fake_model(monkeypatch, {"entries": []})
    result = parse("hello there")
    assert result["entries"] == []
    assert "couldn't find a schedule" in result["question"]


def test_invalid_status_is_rejected(monkeypatch):
    fake_model(monkeypatch, {"entries": [{"date": "2026-10-01", "status": "holiday"}]})
    result = parse("holiday Thu")
    assert result["entries"] == []
    assert result["question"]


def test_impossible_date_is_rejected(monkeypatch):
    fake_model(monkeypatch, {"entries": [{"date": "2026-02-30", "status": "wfh"}]})
    result = parse("wfh 30 Feb")
    assert result["entries"] == []
    assert result["question"]


def test_badly_formatted_date_is_rejected(monkeypatch):
    fake_model(monkeypatch, {"entries": [{"date": "1 Oct", "status": "wfh"}]})
    assert parse("wfh 1 Oct")["entries"] == []


def test_date_far_from_today_is_rejected(monkeypatch):
    fake_model(monkeypatch, {"entries": [{"date": "2019-10-01", "status": "wfh"}]})
    assert parse("wfh 1 Oct")["entries"] == []


def test_one_bad_entry_rejects_the_whole_reply(monkeypatch):
    fake_model(monkeypatch, {"entries": [
        {"date": "2026-10-01", "status": "wfh"},
        {"date": "not a date", "status": "office"},
    ]})
    result = parse("wfh Thu, office Fri")
    assert result["entries"] == []
    assert result["question"]


def test_repeated_date_keeps_the_last_one(monkeypatch):
    fake_model(monkeypatch, {"entries": [
        {"date": "2026-10-01", "status": "wfh"},
        {"date": "2026-10-01", "status": "office"},
    ]})
    result = parse("wfh Thu, actually office")
    assert result["entries"] == [{"person": "Alex", "date": "2026-10-01", "status": "office"}]


def test_reply_that_is_not_json_raises(monkeypatch):
    fake_model(monkeypatch, "Sure! Here is your schedule")
    with pytest.raises(llm.LLMError):
        parse("wfh Thu")


def test_reply_with_wrong_shape_raises(monkeypatch):
    fake_model(monkeypatch, "[1, 2, 3]")
    with pytest.raises(llm.LLMError):
        parse("wfh Thu")


def test_model_failure_is_passed_on(monkeypatch):
    def broken(message, today, sender):
        raise llm.LLMError("429 rate limit")

    monkeypatch.setattr(llm, "_call_model", broken)
    with pytest.raises(llm.LLMError):
        parse("wfh Thu")


def test_prompt_contains_todays_date_and_weekday():
    prompt = llm.build_system_prompt(TODAY, "Jordan")
    assert "Wednesday 2026-09-30" in prompt
    assert "Asia/Singapore" in prompt
    # The current work week Mon-Fri is spelled out for "rest of the week".
    assert "Mon 2026-09-28" in prompt and "Fri 2026-10-02" in prompt
    # The model is told who is sending, so it can tell "I" from "sam".
    assert 'sent by "Jordan"' in prompt


# ---------- Questions ("is sam wfh today?") ----------

def query_reply(*queries, question=None):
    return {"intent": "query", "entries": [], "queries": list(queries), "question": question}


def test_single_day_query(monkeypatch):
    fake_model(monkeypatch, query_reply({"person": "sam", "start_date": "2026-09-30", "end_date": "2026-09-30"}))
    result = llm.parse_schedule("is sam wfh today", TODAY, "Jordan")
    assert result == {
        "intent": "query",
        "entries": [],
        "queries": [{"person": "Sam", "start_date": "2026-09-30", "end_date": "2026-09-30"}],
        "question": None,
    }


def test_week_query_keeps_the_range(monkeypatch):
    fake_model(monkeypatch, query_reply({"person": "Sam", "start_date": "2026-09-28", "end_date": "2026-10-02"}))
    q = parse("sam's schedule this week")["queries"][0]
    assert (q["start_date"], q["end_date"]) == ("2026-09-28", "2026-10-02")


@pytest.mark.parametrize("word", [None, "", "me", "I"])
def test_query_with_no_person_is_about_the_sender(monkeypatch, word):
    fake_model(monkeypatch, query_reply({"person": word, "start_date": "2026-09-30", "end_date": "2026-09-30"}))
    assert parse("am I in office today")["queries"][0]["person"] == "Alex"


@pytest.mark.parametrize("word", ["everyone", "Everybody", "the team"])
def test_query_about_everyone_has_no_person(monkeypatch, word):
    fake_model(monkeypatch, query_reply({"person": word, "start_date": "2026-10-02", "end_date": "2026-10-02"}))
    assert parse("who is in the office friday")["queries"][0]["person"] is None


def test_query_can_ask_about_the_past(monkeypatch):
    fake_model(monkeypatch, query_reply({"person": "Sam", "start_date": "2026-09-25", "end_date": "2026-09-25"}))
    assert parse("was sam in office last friday")["queries"]


@pytest.mark.parametrize("bad", [
    {"person": "Sam", "start_date": "2026-10-02", "end_date": "2026-09-28"},   # ends before it starts
    {"person": "Sam", "start_date": "2026-09-01", "end_date": "2026-12-31"},   # far too long
    {"person": "Sam", "start_date": "yesterday", "end_date": "today"},         # not dates
    {"person": "Sam", "start_date": "2019-01-01", "end_date": "2019-01-01"},   # long ago
    {"person": "Sam; DROP", "start_date": "2026-09-30", "end_date": "2026-09-30"},
])
def test_invalid_query_is_rejected(monkeypatch, bad):
    fake_model(monkeypatch, query_reply(bad))
    result = parse()
    assert result["intent"] == "query" and result["queries"] == [] and result["question"]


def test_query_with_no_queries_gets_a_default_question(monkeypatch):
    fake_model(monkeypatch, query_reply())
    assert parse("hmm")["question"]


def test_query_reply_ignores_stray_entries(monkeypatch):
    # A question must never turn into something that gets saved.
    reply = query_reply({"person": "Sam", "start_date": "2026-09-30", "end_date": "2026-09-30"})
    reply["entries"] = [{"person": "Sam", "date": "2026-09-30", "status": "wfh"}]
    fake_model(monkeypatch, reply)
    assert parse("is sam wfh today")["entries"] == []


def test_unclear_telling_or_asking_returns_a_question(monkeypatch):
    fake_model(monkeypatch, {"intent": "update", "entries": [], "queries": [],
                             "question": "Do you want to check Sam's schedule, or tell me it?"})
    result = parse("sam wfh today")
    assert result["question"].startswith("Do you want")
    assert result["entries"] == [] and result["queries"] == []


def test_prompt_explains_questions_without_question_marks():
    prompt = llm.build_system_prompt(TODAY, "Jordan")
    assert "NO question mark" in prompt
    assert "Next work week" in prompt and "Mon 2026-10-05" in prompt


# ---------- Leave and public holidays ----------

def test_leave_is_a_valid_status(monkeypatch):
    fake_model(monkeypatch, {"entries": [{"person": "Sam", "date": "2026-10-01", "status": "leave"}]})
    assert parse("sam on leave thu")["entries"] == [{"person": "Sam", "date": "2026-10-01", "status": "leave"}]


def test_prompt_lists_public_holidays_and_leave():
    prompt = llm.build_system_prompt(date(2026, 12, 20), "Jordan")
    assert "Fri 2026-12-25: Christmas Day" in prompt
    assert '"leave"' in prompt


def test_query_with_a_usable_query_ignores_the_models_chatter(monkeypatch):
    # The model can answer-by-query AND add a comment in "question". We answer; we don't ask.
    fake_model(monkeypatch, {
        "intent": "query",
        "queries": [{"person": "Sam", "start_date": "2026-12-25", "end_date": "2026-12-25"}],
        "question": "25 Dec 2026 is Christmas Day, which is a public holiday.",
    })
    result = parse("is sam in the office 25 dec")
    assert result["question"] is None and len(result["queries"]) == 1


# ---------- Clearing a status ("not sure any more") ----------

def test_clear_is_a_valid_entry_status(monkeypatch):
    fake_model(monkeypatch, {"entries": [{"person": None, "date": "2026-12-08", "status": "clear"}]})
    assert parse("clear my status for 8 dec")["entries"] == [
        {"person": "Alex", "date": "2026-12-08", "status": "clear"}
    ]


def test_prompt_explains_clear_and_changing():
    prompt = llm.build_system_prompt(TODAY, "Jordan")
    assert '"clear"' in prompt and "CHANGE a day" in prompt
