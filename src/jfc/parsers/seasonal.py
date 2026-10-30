"""Movable-holiday date ranges for Kometa-style seasonal collections (`dynamic:` key)."""

from datetime import date, timedelta
from typing import Optional

# Chinese New Year (Gregorian date of the new moon festival)
_CHINESE_NEW_YEAR = {
    2025: date(2025, 1, 29),
    2026: date(2026, 2, 17),
    2027: date(2027, 2, 6),
    2028: date(2028, 1, 26),
    2029: date(2029, 2, 13),
    2030: date(2030, 2, 3),
    2031: date(2031, 1, 23),
    2032: date(2032, 2, 11),
    2033: date(2033, 1, 31),
    2034: date(2034, 2, 19),
    2035: date(2035, 2, 8),
}


def easter_sunday(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _fmt(value: date) -> str:
    return f"{value.month:02d}/{value.day:02d}"


def dynamic_range(name: str, year: int) -> Optional[tuple[str, str]]:
    """
    Return the (start, end) as "MM/DD" strings for a `dynamic:` seasonal key, or None
    if the name is unknown (the template's own start/end dates are then kept).
    """
    key = (name or "").strip().lower()

    if key == "easter_dates":
        easter = easter_sunday(year)
        return _fmt(easter - timedelta(days=2)), _fmt(easter + timedelta(days=1))

    if key == "good_friday_dates":
        good_friday = easter_sunday(year) - timedelta(days=2)
        return _fmt(good_friday), _fmt(good_friday + timedelta(days=1))

    if key == "chinese_new_year_dates":
        new_year = _CHINESE_NEW_YEAR.get(year)
        if new_year is None:
            return None
        return _fmt(new_year), _fmt(new_year + timedelta(days=3))

    return None
