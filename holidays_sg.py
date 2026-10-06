"""Singapore public holidays.

They are built in (from the `holidays` package), so nobody has to enter them.
"""

import holidays


def public_holidays(start, end):
    """{"2026-12-25": "Christmas Day", ...} for Singapore public holidays from start to end (dates)."""
    sg = holidays.Singapore(years=range(start.year, end.year + 1))
    return {d.isoformat(): name for d, name in sorted(sg.items()) if start <= d <= end}
