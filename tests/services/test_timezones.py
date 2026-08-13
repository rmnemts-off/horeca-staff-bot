"""Time conversion and the single clock (decision D12, TZ 3.4, acceptance 11.2).

Two things are checked here, and the second one is the reason the module exists at all.

**The arithmetic.** A ``TIME`` column is a wall clock reading in the venue's zone and a
``timestamptz`` is an instant in UTC; every test below fixes one rule of the translation
between them, including the two readings a daylight saving transition makes impossible or
double.

**The rule that nothing else keeps its own clock.** :func:`test_no_module_calls_the_system_clock`
walks every shipped Python file and fails on ``datetime.now()``, ``utcnow()``,
``date.today()`` and ``time.time()`` outside ``src/services/timezones.py``. Without it D12
is a sentence in a document: a handler that calls ``datetime.now()`` works perfectly on a
laptop in Moscow and sends yesterday's checklist to a venue in Berlin.

Europe/Berlin is used for every DST case on purpose. Europe/Moscow — the timezone of the
test venue (answer A2) — has had no transition since 2014, so a DST test written on it
would pass forever without testing anything; :func:`test_moscow_has_no_transitions` states
that in code so the choice is not mistaken for an accident later.
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from src.services.timezones import (
    UTC,
    AwareDatetimeError,
    FixedClock,
    NaiveDatetimeError,
    SystemClock,
    UnknownTimezoneError,
    combine_to_utc,
    ensure_utc,
    install_clock,
    is_ambiguous,
    is_nonexistent,
    local_date,
    local_now,
    local_time,
    resolve_local,
    shift_window,
    to_local,
    to_utc,
    use_clock,
    utc_now,
    venue_timezone,
)

from tests.repo_scan import SRC_DIR, python_files, read, relative

BERLIN = ZoneInfo("Europe/Berlin")
MOSCOW = ZoneInfo("Europe/Moscow")

#: Europe/Berlin, 2026: the clock jumps 02:00 -> 03:00 on 29 March (so 02:30 never happens)
#: and 03:00 -> 02:00 on 25 October (so 02:30 happens twice).
SPRING_FORWARD = dt.date(2026, 3, 29)
FALL_BACK = dt.date(2026, 10, 25)


# --------------------------------------------------------------------------------------
# The clock
# --------------------------------------------------------------------------------------


def test_system_clock_is_aware_and_utc() -> None:
    moment = SystemClock().now()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == dt.timedelta(0)


def test_utc_now_reads_the_installed_clock() -> None:
    pinned = dt.datetime(2026, 8, 13, 5, 50, tzinfo=UTC)
    with use_clock(FixedClock(pinned)):
        assert utc_now() == pinned


def test_use_clock_restores_the_previous_one() -> None:
    before = utc_now()
    with use_clock(FixedClock(dt.datetime(2000, 1, 1, tzinfo=UTC))):
        assert utc_now().year == 2000
    assert utc_now() >= before


def test_install_clock_returns_what_was_installed() -> None:
    pinned = FixedClock(dt.datetime(2026, 8, 13, tzinfo=UTC))
    previous = install_clock(pinned)
    try:
        assert utc_now() == pinned.now()
    finally:
        restored = install_clock(previous)
    assert restored is pinned


def test_fixed_clock_normalises_and_moves() -> None:
    clock = FixedClock(dt.datetime(2026, 8, 13, 9, 0, tzinfo=MOSCOW))
    assert clock.now() == dt.datetime(2026, 8, 13, 6, 0, tzinfo=UTC)
    assert clock.advance(dt.timedelta(minutes=10)) == dt.datetime(2026, 8, 13, 6, 10, tzinfo=UTC)
    clock.set_to(dt.datetime(2026, 8, 14, tzinfo=UTC))
    assert clock.now() == dt.datetime(2026, 8, 14, tzinfo=UTC)


def test_fixed_clock_refuses_a_naive_moment() -> None:
    with pytest.raises(NaiveDatetimeError):
        FixedClock(dt.datetime(2026, 8, 13, 9, 0))


def test_local_now_is_the_venue_wall_clock() -> None:
    with use_clock(FixedClock(dt.datetime(2026, 8, 13, 21, 30, tzinfo=UTC))):
        assert local_now(MOSCOW).replace(tzinfo=None) == dt.datetime(2026, 8, 14, 0, 30)


# --------------------------------------------------------------------------------------
# Zones and guards
# --------------------------------------------------------------------------------------


def test_venue_timezone_is_cached() -> None:
    assert venue_timezone("Europe/Moscow") is venue_timezone("Europe/Moscow")


def test_venue_timezone_refuses_an_unknown_name() -> None:
    with pytest.raises(UnknownTimezoneError):
        venue_timezone("Europe/Moscov")


def test_ensure_utc_refuses_a_naive_moment() -> None:
    with pytest.raises(NaiveDatetimeError):
        ensure_utc(dt.datetime(2026, 8, 13, 9, 0))


def test_ensure_utc_converts_an_offset() -> None:
    assert ensure_utc(dt.datetime(2026, 8, 13, 9, 0, tzinfo=MOSCOW)) == dt.datetime(
        2026, 8, 13, 6, 0, tzinfo=UTC
    )


def test_resolve_local_refuses_an_aware_reading() -> None:
    with pytest.raises(AwareDatetimeError):
        resolve_local(dt.datetime(2026, 8, 13, 9, 0, tzinfo=MOSCOW), MOSCOW)


def test_combine_to_utc_refuses_a_time_with_an_offset() -> None:
    with pytest.raises(AwareDatetimeError):
        combine_to_utc(dt.date(2026, 8, 13), dt.time(9, 0, tzinfo=UTC), MOSCOW)


# --------------------------------------------------------------------------------------
# Ordinary conversion
# --------------------------------------------------------------------------------------


def test_combine_to_utc_moscow() -> None:
    assert combine_to_utc(dt.date(2026, 8, 13), dt.time(9, 0), MOSCOW) == dt.datetime(
        2026, 8, 13, 6, 0, tzinfo=UTC
    )


def test_to_local_and_back() -> None:
    moment = dt.datetime(2026, 8, 13, 6, 0, tzinfo=UTC)
    local = to_local(moment, MOSCOW)
    assert local.hour == 9
    assert to_utc(local.replace(tzinfo=None), MOSCOW) == moment


def test_local_date_after_midnight_is_not_the_utc_date() -> None:
    """A venue in Moscow is already in tomorrow while UTC is still in today."""
    moment = dt.datetime(2026, 8, 13, 21, 30, tzinfo=UTC)
    assert moment.date() == dt.date(2026, 8, 13)
    assert local_date(moment, MOSCOW) == dt.date(2026, 8, 14)
    assert local_time(moment, MOSCOW) == dt.time(0, 30)


# --------------------------------------------------------------------------------------
# Daylight saving: the two impossible readings (decision D12)
# --------------------------------------------------------------------------------------


def test_moscow_has_no_transitions() -> None:
    """Why every DST case below is written on Europe/Berlin (answer A2 picks Moscow)."""
    readings = [dt.datetime(2026, month, 29, 2, 30) for month in (3, 10)]
    assert not any(is_nonexistent(reading, MOSCOW) for reading in readings)
    assert not any(is_ambiguous(reading, MOSCOW) for reading in readings)


def test_spring_reading_that_never_happened_is_detected() -> None:
    reading = dt.datetime.combine(SPRING_FORWARD, dt.time(2, 30))
    assert is_nonexistent(reading, BERLIN)
    assert not is_ambiguous(reading, BERLIN)


def test_spring_reading_moves_forward_by_the_length_of_the_transition() -> None:
    """02:30 does not exist, so it becomes 03:30 — one hour, the size of the jump."""
    resolved = resolve_local(dt.datetime.combine(SPRING_FORWARD, dt.time(2, 30)), BERLIN)
    assert resolved.replace(tzinfo=None) == dt.datetime.combine(SPRING_FORWARD, dt.time(3, 30))
    assert resolved.utcoffset() == dt.timedelta(hours=2)
    assert resolved.astimezone(UTC) == dt.datetime(2026, 3, 29, 1, 30, tzinfo=UTC)


def test_autumn_reading_that_happened_twice_is_detected() -> None:
    reading = dt.datetime.combine(FALL_BACK, dt.time(2, 30))
    assert is_ambiguous(reading, BERLIN)
    assert not is_nonexistent(reading, BERLIN)


def test_autumn_reading_is_taken_at_its_first_occurrence() -> None:
    """`fold=0`: summer time, the earlier of the two instants that read 02:30."""
    resolved = resolve_local(dt.datetime.combine(FALL_BACK, dt.time(2, 30)), BERLIN)
    assert resolved.utcoffset() == dt.timedelta(hours=2)
    assert resolved.astimezone(UTC) == dt.datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_the_same_wall_clock_is_two_different_instants_across_a_transition() -> None:
    """The point of storing UTC: 09:00 in the bar is not the same moment on both days."""
    before = combine_to_utc(dt.date(2026, 3, 28), dt.time(9, 0), BERLIN)
    after = combine_to_utc(SPRING_FORWARD, dt.time(9, 0), BERLIN)
    assert before == dt.datetime(2026, 3, 28, 8, 0, tzinfo=UTC)
    assert after == dt.datetime(2026, 3, 29, 7, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# Shift windows (TZ 5.3: a shift belongs to the date it starts on)
# --------------------------------------------------------------------------------------


def test_shift_window_ordinary_day() -> None:
    start, end = shift_window(dt.date(2026, 8, 13), dt.time(9, 0), dt.time(21, 0), MOSCOW)
    assert start == dt.datetime(2026, 8, 13, 6, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 8, 13, 18, 0, tzinfo=UTC)


def test_shift_window_ends_exactly_at_midnight() -> None:
    """14:00-00:00 ends at midnight *tonight*, which is tomorrow's 00:00 — not today's."""
    start, end = shift_window(dt.date(2026, 8, 13), dt.time(14, 0), dt.time(0, 0), MOSCOW)
    assert start == dt.datetime(2026, 8, 13, 11, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 8, 13, 21, 0, tzinfo=UTC)
    assert local_date(end, MOSCOW) == dt.date(2026, 8, 14)
    assert end - start == dt.timedelta(hours=10)


def test_shift_window_night_shift() -> None:
    start, end = shift_window(dt.date(2026, 8, 13), dt.time(18, 0), dt.time(2, 0), MOSCOW)
    assert start == dt.datetime(2026, 8, 13, 15, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 8, 13, 23, 0, tzinfo=UTC)
    assert local_date(start, MOSCOW) == dt.date(2026, 8, 13)
    assert local_date(end, MOSCOW) == dt.date(2026, 8, 14)


def test_shift_window_over_the_spring_transition_resolves_the_end_forward() -> None:
    """18:00-02:00 on the night the clock jumps: 02:00 itself never happens.

    The reading moves forward by the length of the transition (D12), so the shift ends at
    03:00 local and really does last the eight hours the schedule promises. Resolving it
    backwards instead would have ended the shift at 01:00 local — an hour before it was
    written to end, and before the transition it is supposed to span.
    """
    start, end = shift_window(dt.date(2026, 3, 28), dt.time(18, 0), dt.time(2, 0), BERLIN)
    assert start == dt.datetime(2026, 3, 28, 17, 0, tzinfo=UTC)
    assert is_nonexistent(dt.datetime(2026, 3, 29, 2, 0), BERLIN)
    assert end == dt.datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    assert to_local(end, BERLIN).replace(tzinfo=None) == dt.datetime(2026, 3, 29, 3, 0)
    assert end - start == dt.timedelta(hours=8)


def test_shift_window_over_the_autumn_transition_takes_the_first_occurrence() -> None:
    """The same night in October: 02:00 happens twice and `fold=0` picks the earlier one.

    Eight hours of real time; the second occurrence would have made it nine. The rule is
    the one D12 fixes, and this is where the choice becomes an hour of somebody's evening.
    """
    start, end = shift_window(dt.date(2026, 10, 24), dt.time(18, 0), dt.time(2, 0), BERLIN)
    assert start == dt.datetime(2026, 10, 24, 16, 0, tzinfo=UTC)
    assert is_ambiguous(dt.datetime(2026, 10, 25, 2, 0), BERLIN)
    assert end == dt.datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
    assert end - start == dt.timedelta(hours=8)

    later = dt.datetime(2026, 10, 25, 2, 0).replace(tzinfo=BERLIN, fold=1).astimezone(UTC)
    assert later - start == dt.timedelta(hours=9)


# --------------------------------------------------------------------------------------
# D12 as a rule, not a promise: one clock in the whole delivery
# --------------------------------------------------------------------------------------

#: Calls that read the wall clock. `time.monotonic()` and `perf_counter()` are absent on
#: purpose: they measure a duration, not a calendar, and a rate limiter may need one.
SYSTEM_CLOCK_CALLS = frozenset(
    {
        "datetime.now",
        "datetime.utcnow",
        "datetime.today",
        "date.today",
        "date.fromtimestamp",
        "time.time",
        "time.localtime",
        "time.gmtime",
    }
)

#: The single module allowed to read it (decision D12).
CLOCK_OWNER = SRC_DIR / "services" / "timezones.py"


def system_clock_calls(source: str, filename: str = "<probe>") -> list[str]:
    """Every call in `source` that asks the operating system what time it is.

    Matched on the dotted spelling of the callee, so `sa.func.now()` — SQL, evaluated by
    PostgreSQL — is not mistaken for `dt.datetime.now()`.
    """
    tree = ast.parse(source, filename=filename)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = ast.unparse(node.func)
        if any(dotted == name or dotted.endswith(f".{name}") for name in SYSTEM_CLOCK_CALLS):
            offenders.append(f"line {node.lineno}: {dotted}()")
    return sorted(set(offenders))


@pytest.mark.parametrize("path", python_files(SRC_DIR), ids=relative)
def test_no_module_calls_the_system_clock(path: Path) -> None:
    if path == CLOCK_OWNER:
        pytest.skip("the clock lives here")
    offenders = system_clock_calls(read(path), relative(path))
    assert not offenders, (
        f"{relative(path)}: decision D12 — the clock is `utc_now()` from "
        "src/services/timezones.py, so that a venue in another timezone and a test with a "
        "pinned clock both work: " + "; ".join(offenders)
    )


def test_the_clock_owner_does_call_it() -> None:
    """The guard above is only meaningful if it would fire — here is the one place it does."""
    assert system_clock_calls(read(CLOCK_OWNER), relative(CLOCK_OWNER))


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("import datetime as dt", "import datetime as dt\nx = dt.datetime.now(dt.UTC)\n"),
        ("from datetime import datetime", "from datetime import datetime\nx = datetime.now()\n"),
        ("utcnow", "import datetime\nx = datetime.datetime.utcnow()\n"),
        ("date.today", "import datetime as dt\nx = dt.date.today()\n"),
        ("time.time", "import time\nx = time.time()\n"),
    ],
)
def test_the_guard_catches_every_spelling(name: str, source: str) -> None:
    assert system_clock_calls(source), name


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("sql now()", "from sqlalchemy import func\nc = func.now()\n"),
        ("sa.func.now()", "import sqlalchemy as sa\nc = sa.func.now()\n"),
        ("monotonic", "import time\nx = time.monotonic()\n"),
        ("the sanctioned clock", "from src.services.timezones import utc_now\nx = utc_now()\n"),
    ],
)
def test_the_guard_leaves_honest_code_alone(name: str, source: str) -> None:
    assert not system_clock_calls(source), name
