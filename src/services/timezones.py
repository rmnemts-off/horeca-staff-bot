"""Time conversion and the clock — the only module in the project that does either (D12).

Two kinds of time meet here and must never be mixed up (TZ 3.4):

* every ``timestamptz`` column is a **moment**, stored in UTC;
* every ``TIME`` column — ``shifts.start_time``, ``venue_settings.default_shift_start``,
  ``venue_settings.order_reminder_time`` — is a **wall clock reading in the venue's own
  timezone**. It is not a moment at all until a date and a timezone are attached to it.

Everything below exists so that the arithmetic between the two lives in one file. A handler,
a service or a worker that needs "now" calls :func:`utc_now`; one that needs "the moment the
shift starts" calls :func:`combine_to_utc` or :func:`shift_window`. Nothing else in ``src/``
calls ``datetime.now()`` — the rule is checked by a static test in
``tests/services/test_timezones.py``, because a second clock is exactly how a bot ends up
sending yesterday's checklist.

Daylight saving time produces two wall-clock readings that are not moments, and both are
resolved here, once, the way decision D12 fixes it:

* **the reading never happened.** Europe/Berlin, 2026-03-29: the clock jumps 02:00 -> 03:00,
  so 02:30 does not exist. Such a reading is moved *forward* by the length of the
  transition — 02:30 becomes 03:30.
* **the reading happened twice.** Europe/Berlin, 2026-10-25: the clock jumps 03:00 -> 02:00,
  so 02:30 occurs once in CEST and once in CET. The first occurrence is taken (``fold=0``).

Both rules are deliberate and neither is Python's default: ``datetime.replace(tzinfo=...)``
alone silently picks an offset for a reading that never existed.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.UTC


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class NaiveDatetimeError(ValueError):
    """A moment arrived without a timezone, so its meaning is a guess."""

    def __init__(self, moment: dt.datetime) -> None:
        super().__init__(
            f"{moment!r} carries no timezone; a moment must be aware (TZ 3.4). "
            f"A venue-local wall clock reading goes through resolve_local() instead."
        )
        self.moment = moment


class AwareDatetimeError(ValueError):
    """A wall clock reading arrived with a timezone already attached."""

    def __init__(self, moment: dt.datetime) -> None:
        super().__init__(
            f"{moment!r} already carries a timezone; this function takes a venue-local "
            f"wall clock reading, which by definition has none."
        )
        self.moment = moment


class UnknownTimezoneError(LookupError):
    """`venues.timezone` does not name a zone this machine knows about."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"unknown IANA timezone {name!r}: either the venue holds a typo or the "
            f"runtime has no time zone database (install the `tzdata` package)"
        )
        self.name = name


# --------------------------------------------------------------------------------------
# The clock
# --------------------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    """A source of "now", always in UTC. Injected so that tests can pin it."""

    def now(self) -> dt.datetime: ...


class SystemClock:
    """The real clock. The single call to ``datetime.now()`` in the delivery."""

    __slots__ = ()

    def now(self) -> dt.datetime:
        return dt.datetime.now(UTC)


class FixedClock:
    """A clock pinned to one moment.

    It lives in ``src/`` rather than in the test harness on purpose: several waves of the
    plan need to pin time (scheduling, the checklist run, the worker), and the alternative
    is a copy of this class in every test module — or one in ``tests/conftest.py``, which
    task 5a owns alone.
    """

    __slots__ = ("_moment",)

    def __init__(self, moment: dt.datetime) -> None:
        self._moment = ensure_utc(moment)

    def now(self) -> dt.datetime:
        return self._moment

    def set_to(self, moment: dt.datetime) -> None:
        self._moment = ensure_utc(moment)

    def advance(self, delta: dt.timedelta) -> dt.datetime:
        """Move the clock forward (or back, with a negative delta) and return the new now."""
        self._moment += delta
        return self._moment


_installed_clock: Clock = SystemClock()


def utc_now() -> dt.datetime:
    """The current moment in UTC. Everything in ``src/`` asks here and nowhere else."""
    return _installed_clock.now()


def install_clock(clock: Clock) -> Clock:
    """Replace the process-wide clock; returns the one that was installed before."""
    # One shared clock is the point of the module: a service that was handed no clock of
    # its own must still see the pinned one (D12).
    global _installed_clock
    previous, _installed_clock = _installed_clock, clock
    return previous


@contextmanager
def use_clock(clock: Clock) -> Iterator[Clock]:
    """Install `clock` for the duration of the block, then restore the previous one."""
    previous = install_clock(clock)
    try:
        yield clock
    finally:
        install_clock(previous)


# --------------------------------------------------------------------------------------
# Zones
# --------------------------------------------------------------------------------------


@lru_cache(maxsize=256)
def venue_timezone(name: str) -> ZoneInfo:
    """`venues.timezone` (an IANA name) as a zone object, with a readable failure."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise UnknownTimezoneError(name) from error


# --------------------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------------------


def _offset(moment: dt.datetime) -> dt.timedelta:
    """UTC offset of an aware moment. ``utcoffset()`` is ``None`` only for naive ones."""
    offset = moment.utcoffset()
    return dt.timedelta() if offset is None else offset


def _require_naive(local: dt.datetime) -> None:
    if local.tzinfo is not None:
        raise AwareDatetimeError(local)


def ensure_utc(moment: dt.datetime) -> dt.datetime:
    """Any aware moment as UTC; a naive one is a bug and says so."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise NaiveDatetimeError(moment)
    return moment.astimezone(UTC)


def is_nonexistent(local: dt.datetime, tz: ZoneInfo) -> bool:
    """True when the venue's clock never showed this reading (spring transition)."""
    _require_naive(local)
    return _offset(local.replace(tzinfo=tz, fold=1)) > _offset(local.replace(tzinfo=tz, fold=0))


def is_ambiguous(local: dt.datetime, tz: ZoneInfo) -> bool:
    """True when the venue's clock showed this reading twice (autumn transition)."""
    _require_naive(local)
    return _offset(local.replace(tzinfo=tz, fold=1)) < _offset(local.replace(tzinfo=tz, fold=0))


def resolve_local(local: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """A venue-local wall clock reading as an aware moment, DST anomalies resolved (D12).

    A reading that never existed moves forward by the length of the transition; a reading
    that happened twice is taken at its first occurrence.
    """
    _require_naive(local)
    early = _offset(local.replace(tzinfo=tz, fold=0))
    late = _offset(local.replace(tzinfo=tz, fold=1))
    if late > early:
        # The gap of a spring transition: nothing on the venue's clock ever read this.
        return (local + (late - early)).replace(tzinfo=tz, fold=0)
    # Either an ordinary reading, or an ambiguous one — `fold=0` is the first occurrence.
    return local.replace(tzinfo=tz, fold=0)


def to_utc(local: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """A venue-local wall clock reading as a moment in UTC."""
    return resolve_local(local, tz).astimezone(UTC)


def to_local(moment: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """A moment as the venue's own wall clock, still aware."""
    return ensure_utc(moment).astimezone(tz)


def combine_to_utc(day: dt.date, time_of_day: dt.time, tz: ZoneInfo) -> dt.datetime:
    """A venue-local DATE plus a venue-local TIME as one moment in UTC.

    This is the conversion every ``TIME`` column of the schema needs, and the reason those
    columns may be stored without an offset at all (D12).
    """
    if time_of_day.tzinfo is not None:
        raise AwareDatetimeError(dt.datetime.combine(day, time_of_day))
    return to_utc(dt.datetime.combine(day, time_of_day), tz)


def local_now(tz: ZoneInfo) -> dt.datetime:
    """Now, on the venue's wall clock."""
    return to_local(utc_now(), tz)


def local_date(moment: dt.datetime, tz: ZoneInfo) -> dt.date:
    """The venue's calendar date at `moment` — which is not the UTC date after midnight."""
    return to_local(moment, tz).date()


def local_time(moment: dt.datetime, tz: ZoneInfo) -> dt.time:
    """The venue's wall clock reading at `moment`."""
    return to_local(moment, tz).time()


def shift_window(
    day: dt.date,
    start_time: dt.time,
    end_time: dt.time,
    tz: ZoneInfo,
) -> tuple[dt.datetime, dt.datetime]:
    """The two moments a shift spans, in UTC.

    A shift belongs to the date it *starts* on (TZ 5.3), so an end that is not later than
    the start falls on the next local day: 18:00-02:00 ends at 02:00 tomorrow, and
    14:00-00:00 ends at midnight tonight, which is tomorrow's 00:00. Both readings go
    through :func:`resolve_local`, so a shift spanning a DST transition covers the hours
    the clock actually ran, not the hours the arithmetic suggests.
    """
    start = combine_to_utc(day, start_time, tz)
    end_day = day if end_time > start_time else day + dt.timedelta(days=1)
    return start, combine_to_utc(end_day, end_time, tz)


__all__ = [
    "UTC",
    "AwareDatetimeError",
    "Clock",
    "FixedClock",
    "NaiveDatetimeError",
    "SystemClock",
    "UnknownTimezoneError",
    "combine_to_utc",
    "ensure_utc",
    "install_clock",
    "is_ambiguous",
    "is_nonexistent",
    "local_date",
    "local_now",
    "local_time",
    "resolve_local",
    "shift_window",
    "to_local",
    "to_utc",
    "use_clock",
    "utc_now",
    "venue_timezone",
]
