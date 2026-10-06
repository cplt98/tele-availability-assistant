"""Checks that tests/cases.json is well formed. It does NOT run the cases against the AI:
that is what tests/run_eval.py does (see tests/README.md)."""

import json
from datetime import date
from pathlib import Path

import pytest

import db
import llm

CASES_FILE = Path(__file__).parent / "cases.json"
DATA = json.loads(CASES_FILE.read_text(encoding="utf-8"))
CASES = DATA["cases"]
KNOWN_NAMES = {"Alex", "Sam", "Jordan"}   # the people in users.example.json


def ids():
    return [case["id"] for case in CASES]


def test_there_are_enough_cases_and_the_ids_are_unique():
    assert len(CASES) >= 15
    assert len(set(ids())) == len(CASES)


def test_the_file_covers_the_tricky_situations_it_is_meant_to():
    tags = {tag for case in CASES for tag in case["tags"]}
    for wanted in ("update", "query", "typo", "relative-date", "half-day", "singlish", "range", "multi-status",
                   "leave", "clear", "ambiguous", "other-person", "everyone"):
        assert wanted in tags, f"no case is tagged {wanted!r}"


@pytest.mark.parametrize("case", CASES, ids=ids())
def test_each_case_has_the_required_fields(case):
    assert set(case) == {"id", "text", "sender", "today", "tags", "expected"}
    assert case["id"] == case["id"].lower() and " " not in case["id"]
    assert case["text"].strip()
    assert case["sender"] in KNOWN_NAMES
    assert case["tags"] and all(isinstance(tag, str) and tag for tag in case["tags"])
    date.fromisoformat(case["today"])           # a real date


@pytest.mark.parametrize("case", CASES, ids=ids())
def test_each_expected_result_has_exactly_one_valid_shape(case):
    expected = case["expected"]
    if expected == {"question": True}:
        return
    assert expected["intent"] in {"update", "query"}
    if expected["intent"] == "update":
        assert set(expected) == {"intent", "entries"} and expected["entries"]
        for entry in expected["entries"]:
            assert set(entry) == {"person", "date", "status"}
            assert entry["person"] in KNOWN_NAMES
            assert entry["status"] in {*db.VALID_STATUSES, llm.CLEAR}
            date.fromisoformat(entry["date"])
    else:
        assert set(expected) == {"intent", "queries"} and expected["queries"]
        for query in expected["queries"]:
            assert set(query) == {"person", "start_date", "end_date"}
            assert query["person"] is None or query["person"] in KNOWN_NAMES
            assert date.fromisoformat(query["start_date"]) <= date.fromisoformat(query["end_date"])


@pytest.mark.parametrize("case", CASES, ids=ids())
def test_each_expected_result_passes_the_codes_own_validation(case):
    """If the AI returned exactly the expected result, the validator must accept it unchanged.
    This catches impossible dates, dates outside the allowed window, and bad names."""
    expected = case["expected"]
    if expected == {"question": True}:
        return
    today = date.fromisoformat(case["today"])
    # In the validated result, "everyone" is person None. The model says it with the word "everyone"
    # (a missing person would mean "the sender"), so say it that way when feeding the result back in.
    queries = [{**q, "person": q["person"] or "everyone"} for q in expected.get("queries", [])]
    model_reply = {"intent": expected["intent"], "entries": expected.get("entries", []),
                   "queries": queries, "question": None}
    result = llm.validate(json.dumps(model_reply), today, case["sender"])
    assert result["question"] is None, "the validator rejected the expected result"
    if expected["intent"] == "update":
        assert sorted(result["entries"], key=str) == sorted(expected["entries"], key=str)
    else:
        assert result["queries"] == expected["queries"]


@pytest.mark.parametrize("case", [c for c in CASES if c["expected"].get("intent") == "update"], ids=lambda c: c["id"])
def test_no_update_expects_a_weekend_or_two_entries_for_one_person_and_day(case):
    seen = set()
    for entry in case["expected"]["entries"]:
        assert date.fromisoformat(entry["date"]).weekday() < 5, "work days only"
        key = (entry["person"], entry["date"])
        assert key not in seen
        seen.add(key)
