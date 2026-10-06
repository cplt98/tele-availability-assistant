"""Tests for db.py. Each test gets its own throwaway database in a temp folder."""

import sqlite3

import pytest

import db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


def test_set_and_get_status(db_path):
    db.set_status("Alex", "2026-10-01", "office", "command", db_path)
    assert db.get_statuses_for_date("2026-10-01", db_path) == [("Alex", "office")]


def test_set_status_twice_updates_instead_of_duplicating(db_path):
    db.set_status("Alex", "2026-10-01", "office", "command", db_path)
    db.set_status("Alex", "2026-10-01", "wfh", "command", db_path)
    assert db.get_statuses_for_date("2026-10-01", db_path) == [("Alex", "wfh")]


def test_get_returns_everyone_for_date_only(db_path):
    db.set_status("Taylor", "2026-10-01", "half_day", "command", db_path)
    db.set_status("Alex", "2026-10-01", "office", "command", db_path)
    db.set_status("Alex", "2026-10-02", "wfh", "command", db_path)
    assert db.get_statuses_for_date("2026-10-01", db_path) == [
        ("Alex", "office"),
        ("Taylor", "half_day"),
    ]


def test_empty_date_returns_empty_list(db_path):
    assert db.get_statuses_for_date("2026-10-01", db_path) == []


def test_invalid_status_is_rejected(db_path):
    with pytest.raises(sqlite3.IntegrityError):
        db.set_status("Alex", "2026-10-01", "holiday", "command", db_path)


def test_init_db_is_safe_to_run_twice(db_path):
    db.init_db(db_path)  # should not raise


def test_get_statuses_between_filters_by_range_and_person(db_path):
    db.set_status("Sam", "2026-09-28", "office", "command", db_path)
    db.set_status("Sam", "2026-09-30", "wfh", "command", db_path)
    db.set_status("Sam", "2026-10-05", "wfh", "command", db_path)
    db.set_status("Taylor", "2026-09-30", "half_day", "command", db_path)

    everyone = db.get_statuses_between("2026-09-28", "2026-10-02", db_path=db_path)
    assert everyone == [
        ("Sam", "2026-09-28", "office"),
        ("Sam", "2026-09-30", "wfh"),
        ("Taylor", "2026-09-30", "half_day"),
    ]
    assert db.get_statuses_between("2026-09-28", "2026-10-02", "Sam", db_path) == [
        ("Sam", "2026-09-28", "office"),
        ("Sam", "2026-09-30", "wfh"),
    ]


def test_get_statuses_between_ignores_name_case(db_path):
    db.set_status("Sam", "2026-09-30", "wfh", "command", db_path)
    assert db.get_statuses_between("2026-09-30", "2026-09-30", "sam", db_path) == [("Sam", "2026-09-30", "wfh")]


def test_get_statuses_between_with_no_rows(db_path):
    assert db.get_statuses_between("2026-09-28", "2026-10-02", "Sam", db_path) == []


def test_new_status_leave_is_accepted(db_path):
    db.set_status("Sam", "2026-10-01", "leave", "command", db_path)
    assert db.get_statuses_for_date("2026-10-01", db_path) == [("Sam", "leave")]


def test_delete_status_puts_a_day_back_to_not_entered(db_path):
    db.set_status("Alex", "2026-12-08", "leave", "command", db_path)
    db.set_status("Alex", "2026-12-09", "wfh", "command", db_path)

    assert db.delete_status("alex", "2026-12-08", db_path) == 1   # name case doesn't matter
    assert db.get_statuses_between("2026-12-08", "2026-12-09", "Alex", db_path) == [("Alex", "2026-12-09", "wfh")]
    assert db.delete_status("Alex", "2026-12-08", db_path) == 0   # nothing left to remove


def test_changing_your_mind_just_overwrites(db_path):
    db.set_status("Alex", "2026-12-08", "wfh", "command", db_path)
    db.set_status("Alex", "2026-12-08", "office", "command", db_path)
    assert db.get_statuses_for_date("2026-12-08", db_path) == [("Alex", "office")]


# ---------- Person names ignore upper/lower case ----------


def all_rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT id, person, date, status, updated_at FROM schedule ORDER BY id").fetchall()
    finally:
        conn.close()


def test_set_status_treats_names_that_differ_only_in_case_as_one_person(db_path):
    db.set_status("alex", "2026-10-01", "wfh", "test", db_path)
    db.set_status("ALEX", "2026-10-01", "office", "test", db_path)
    db.set_status("Alex", "2026-10-01", "leave", "test", db_path)

    assert all_rows(db_path) == [(1, "Alex", "2026-10-01", "leave", all_rows(db_path)[0][4])]
    assert db.get_statuses_for_date("2026-10-01", db_path) == [("Alex", "leave")]


def test_the_table_itself_refuses_two_rows_that_differ_only_in_case(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO schedule (person, date, status) VALUES ('alex', '2026-10-01', 'wfh')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO schedule (person, date, status) VALUES ('Alex', '2026-10-01', 'office')")
    conn.close()


def test_set_status_stores_the_tidy_name(db_path):
    db.set_status("  jamie   lee ", "2026-10-01", "wfh", "test", db_path)
    assert db.get_statuses_for_date("2026-10-01", db_path) == [("Jamie Lee", "wfh")]


def test_tidy_name_matches_the_name_tidying_the_bot_uses():
    import llm
    for name in ["sam", "  SAM ", "jamie   lee", "Alex", "o'brien", "anne-marie"]:
        assert db.tidy_name(name) == llm.normalize_person(name)


