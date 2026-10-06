"""All the SQLite database code lives here, so bot.py never writes SQL."""

import sqlite3
from datetime import datetime
from pathlib import Path

# The database the bot uses when no db_path is given. bot.py overwrites this
# from config.DB_PATH; the tests pass their own db_path instead.
DB_PATH = Path(__file__).parent / "schedule.db"

VALID_STATUSES = ("office", "wfh", "half_day", "leave")


def tidy_name(name):
    """Tidy a person's name the way the bot does ("  sam " -> "Sam").

    Kept in step with llm.normalize_person (a test checks that they agree).
    """
    return " ".join(str(name).split()).title()


def _connect(db_path):
    # None means "use the current DB_PATH" (looked up now, so bot.py can change it).
    return sqlite3.connect(db_path or DB_PATH)


def init_db(db_path=None):
    """Create the schedule table if it doesn't exist yet.

    person is COLLATE NOCASE, so UNIQUE (person, date) treats "alex" and "Alex" as the
    same person, and nobody can end up with two rows for one day.
    """
    allowed = ", ".join(f"'{s}'" for s in VALID_STATUSES)
    conn = _connect(db_path)
    try:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY,
                person TEXT NOT NULL COLLATE NOCASE,
                date TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ({allowed})),
                source TEXT,
                updated_at TEXT,
                UNIQUE (person, date)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def set_status(person, date, status, source, db_path=None):
    """Insert a row, or update it if this person already has one for that date.

    Names are matched ignoring upper/lower case and stored tidy ("alex" -> "Alex").
    """
    person = tidy_name(person)
    now = datetime.now().isoformat(timespec="seconds")
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO schedule (person, date, status, source, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (person, date) DO UPDATE SET
                status = excluded.status,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (person, date, status, source, now),
        )
        conn.commit()
    finally:
        conn.close()


def delete_status(person, date, db_path=None):
    """Remove someone's status for a date, so it goes back to "not entered".

    Returns how many rows were removed (0 if there was nothing to remove).
    """
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM schedule WHERE person = ? COLLATE NOCASE AND date = ?",
            (person, date),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def get_statuses_between(start, end, person=None, db_path=None):
    """Return (person, date, status) rows from start to end (inclusive), sorted.

    Pass person to only get one person's rows (upper/lower case doesn't matter).
    Dates are YYYY-MM-DD text, which sorts and compares correctly as text.
    """
    sql = "SELECT person, date, status FROM schedule WHERE date BETWEEN ? AND ?"
    args = [start, end]
    if person:
        sql += " AND person = ? COLLATE NOCASE"
        args.append(person)
    sql += " ORDER BY person, date"

    conn = _connect(db_path)
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return rows


def get_statuses_for_date(date, db_path=None):
    """Return a list of (person, status) pairs for one date, sorted by name."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT person, status FROM schedule WHERE date = ? ORDER BY person",
            (date,),
        ).fetchall()
    finally:
        conn.close()
    return rows
