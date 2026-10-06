"""Tests for holidays_sg.py."""

from datetime import date

import db
import holidays_sg


def test_public_holidays_include_observed_days():
    found = holidays_sg.public_holidays(date(2026, 12, 1), date(2026, 12, 31))
    assert found == {"2026-12-25": "Christmas Day"}

    nov = holidays_sg.public_holidays(date(2026, 11, 1), date(2026, 11, 30))
    assert "2026-11-09" in nov   # Deepavali fell on a Sunday, so Monday is the holiday


def test_no_public_holidays_in_an_ordinary_week():
    assert holidays_sg.public_holidays(date(2026, 9, 28), date(2026, 10, 2)) == {}


