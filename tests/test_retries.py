from datetime import datetime, time, timedelta

import pytest

from calls.retries import CALLING_TZ, next_call_time

OPENS, CLOSES = time(9), time(21)

def madrid(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=CALLING_TZ)

@pytest.mark.parametrize(
    ('moment', 'expected'),
    [
        (madrid(2026, 11, 2, 10, 30), madrid(2026, 11, 2, 10, 30)),  # within hours
        (madrid(2026, 11, 2, 20, 59), madrid(2026, 11, 2, 20, 59)),  # just before closing
        (madrid(2026, 11, 2, 21, 0), madrid(2026, 11, 3, 9, 0)),  # closing is excluded
        (madrid(2026, 11, 2, 21, 15), madrid(2026, 11, 3, 9, 0)),  # after closing
        (madrid(2026, 11, 2, 3, 30), madrid(2026, 11, 2, 9, 0)),  # before opening
        (madrid(2026, 11, 3, 0, 20), madrid(2026, 11, 3, 9, 0)),  # past midnight
    ]
)
def test_next_call_time(moment: datetime, expected: datetime) -> None:
    assert next_call_time(moment, OPENS, CLOSES) == expected


def test_next_opening_across_a_clock_change() -> None:
    # Clocks go forward on 29 March 2026: 09:00 is UTC+2 that day, UTC+1 the day before.
    result = next_call_time(madrid(2026, 3, 28, 21, 15), OPENS, CLOSES)

    assert result == madrid(2026, 3, 29, 9, 0)
    assert result.utcoffset() == timedelta(hours=2)


def test_without_calling_hours_any_moment_is_fine() -> None:
    moment = madrid(2026, 11, 2, 3, 30)

    assert next_call_time(moment, None, None) == moment
