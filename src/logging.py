"""Structured JSON logging with a request id that spans one update (TZ 9, plan task 9).

TZ 9 asks for three things, and this module is all three:

* structured logs in JSON, INFO for what a person did, ERROR with a traceback for a
  failure;
* **no personal data beyond `telegram_id`** in a log line;
* one `request_id` across every line an update produces, so that the traceback the user
  never sees (TZ 8.2) can be found from the apology they did see.

How the second one is actually held
-----------------------------------

Not by hoping nobody logs a name -- by an **allowlist of field names**. `extra=` may carry
anything, and only keys listed in `ALLOWED_FIELDS` or matching `ALLOWED_FIELD_SHAPE` (an
identifier, a timestamp, a duration, a counter, a code) reach the output. Everything else
is dropped and only its *name* is reported, under `dropped_fields`: a field that went
missing stays visible in the log without its value being in it. `full_name`, `username`,
`phone`, `first_name` and the text of a message match nothing and cannot be logged by
accident.

On top of that, every string that does get out -- the message, the exception text, the
traceback -- goes through `scrub()`, which replaces e-mail addresses, `@handles` and
phone-shaped numbers. That half is the net for personal data arriving *inside* a string
("delivery failed for +7 (985) 127 - 54 - 73"), where a rule about field names sees
nothing at all.

Threat model, the same one the static guards under `tests/` are written against: **a
mistake, not an adversary.** What happens in practice is that someone adds
`extra={"who": user.full_name}` while chasing a bug and it ships. Deliberately open:

* a name inside the log *message* -- `logger.info("start for %s", user.full_name)`.
  Catching that would mean banning `%s`, and a Russian first name in free text is
  indistinguishable from any other word;
* an 11-digit `telegram_id` starting with 8 would be scrubbed as a phone number. Telegram
  ids are 10 digits today, and losing an id is a smaller failure than printing a phone;
* a personal datum stored under an allowed name, e.g. `venue_id="Ivan Petrov"`. A value is
  only checked for the three shapes above, never for being a name.

Nothing here calls `datetime.now()` (decision D12): the timestamp of a line is
`record.created`, which the logging module has already taken.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import IO, Any, Final

from src.config import settings

#: Root of the project's logger tree; `get_logger("bot.errors")` lands under it.
ROOT_LOGGER_NAME: Final = "barpoint"

#: Key under which the request id travels in the aiogram handler context and in `extra=`.
REQUEST_ID_FIELD: Final = "request_id"

#: What a dropped value is replaced with, in the one place a value is rewritten in place.
REDACTED: Final = "[redacted]"

_request_id: ContextVar[str | None] = ContextVar("barpoint_request_id", default=None)


# --------------------------------------------------------------------------------------
# What may be written down (TZ 9: nothing personal beyond telegram_id)
# --------------------------------------------------------------------------------------

#: Field names emitted as they are. Every one of them is either machinery (a duration, the
#: name of a handler function) or the single identifier TZ 9 permits.
ALLOWED_FIELDS: Final = frozenset(
    {
        "request_id",
        "telegram_id",
        "event_type",
        "handler_name",
        "duration_ms",
        "error_type",
        "outcome_status",
        "app_env",
        # Delivery counter of the notification worker (plan, task 31). The schema spells it
        # without a prefix, so the shape rule below cannot reach it.
        "attempts",
    }
)

#: Shape of a field that is an identifier, a moment, a measurement or a code. This is what
#: keeps the allowlist from having to be extended by every task that logs something:
#: `venue_id`, `run_id`, `scheduled_at`, `sent_at`, `item_count`, `notification_type` all
#: pass, while `full_name`, `username`, `phone`, `text`, `comment` and `caption` do not.
#: `_name` is deliberately absent from the suffixes -- it is the shape of `full_name`.
ALLOWED_FIELD_SHAPE: Final = re.compile(
    r"^[a-z][a-z0-9_]*_(?:id|ids|at|ms|count|type|status|kind|index|version|attempt|attempts)$"
)

#: An e-mail address inside free text.
EMAIL_PATTERN: Final = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w")

#: A Telegram handle: `@rmnemts_off`. Telegram requires 5 characters at least, which keeps
#: this away from `@decorator` lines and from e-mail addresses (handled above and first).
HANDLE_PATTERN: Final = re.compile(r"(?<![\w@./])@[A-Za-z][A-Za-z0-9_]{4,31}")

#: A phone number in any spelling the reference workbook contains -- `+7 (985) 127 - 54 -
#: 73`, `+7 ( 912 ) 874 - 12 - 24`, `8-999-1234567` -- and nothing shorter than 11 digits,
#: so that a 10-digit `telegram_id` is not eaten by it.
PHONE_PATTERN: Final = re.compile(r"(?<![\d+])(?:\+\d|8)[\s\-()]*\d(?:[\s\-()]*\d){9,13}(?!\d)")


def scrub(value: str) -> str:
    """Replace anything shaped like personal data with `REDACTED` (TZ 9)."""
    scrubbed = EMAIL_PATTERN.sub(REDACTED, value)
    scrubbed = HANDLE_PATTERN.sub(REDACTED, scrubbed)
    return PHONE_PATTERN.sub(REDACTED, scrubbed)


def is_allowed_field(name: str) -> bool:
    """True when a structured field may be written to the log at all."""
    return name in ALLOWED_FIELDS or bool(ALLOWED_FIELD_SHAPE.match(name))


# --------------------------------------------------------------------------------------
# Request id
# --------------------------------------------------------------------------------------


def new_request_id() -> str:
    """Short random id; 16 hex characters are plenty to find one update in a day of logs."""
    return uuid.uuid4().hex[:16]


def current_request_id() -> str | None:
    return _request_id.get()


def bind_request_id(value: str) -> Token[str | None]:
    return _request_id.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)


@contextmanager
def request_context(request_id: str | None = None) -> Iterator[str]:
    """Bind a request id for the duration of the block; every line inside carries it.

    The contextvar is reset on the way out, including on an exception: a worker thread or
    an asyncio task that reuses the context must not inherit the id of a finished update.
    """
    value = request_id or new_request_id()
    token = bind_request_id(value)
    try:
        yield value
    finally:
        reset_request_id(token)


# --------------------------------------------------------------------------------------
# Formatter
# --------------------------------------------------------------------------------------


def _reserved_attributes() -> frozenset[str]:
    """Attribute names the logging module itself puts on every record.

    Taken from a throwaway record instead of a hand-written list, so that a new attribute
    in a future Python (`taskName` arrived in 3.12) does not start leaking into the output
    as if it were a caller's field.
    """
    probe = logging.LogRecord(
        name="probe",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    )
    return frozenset(vars(probe)) | {"message", "asctime"}


RESERVED_ATTRIBUTES: Final = _reserved_attributes()


def _timestamp(created: float) -> str:
    """UTC, milliseconds, `Z` suffix. `record.created` is already taken by `logging`."""
    moment = dt.datetime.fromtimestamp(created, tz=dt.UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _render(value: object) -> Any:
    """JSON-safe rendering of one field value, scrubbed if it is (or becomes) a string."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, bool | int | float) or value is None:
        return value
    return scrub(repr(value))


class JsonFormatter(logging.Formatter):
    """One JSON object per line: the shape TZ 9 asks for.

    `ensure_ascii=False` on purpose -- a Cyrillic checklist item inside an exception
    message stays readable instead of turning into `\\u0417...`, and Docker logs are UTF-8.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }

        request_id = getattr(record, REQUEST_ID_FIELD, None) or current_request_id()
        if request_id is not None:
            payload[REQUEST_ID_FIELD] = str(request_id)

        dropped: list[str] = []
        for name, value in vars(record).items():
            if name in RESERVED_ATTRIBUTES or name == REQUEST_ID_FIELD:
                continue
            if is_allowed_field(name):
                payload[name] = _render(value)
            else:
                dropped.append(name)
        if dropped:
            # The names are schema, not data: what was dropped stays visible, its value
            # does not (TZ 9).
            payload["dropped_fields"] = sorted(dropped)

        error = self._error(record)
        if error:
            payload["error"] = error
        if record.stack_info:
            payload["stack"] = scrub(record.stack_info)

        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=repr)

    def _error(self, record: logging.LogRecord) -> dict[str, str]:
        """Type, message and traceback of a failure -- the ERROR half of TZ 9.

        The traceback goes into the log and never to the user (TZ 8.2). Its text is
        scrubbed like any other string: an exception raised over a phone number would
        otherwise print one.
        """
        exc_info = record.exc_info
        if exc_info is None:
            return {}
        exc_type, exc_value, _traceback = exc_info
        error: dict[str, str] = {
            "error_type": exc_type.__name__ if exc_type is not None else "None",
            "message": scrub(str(exc_value)),
        }
        formatted = self.formatException(exc_info)
        if formatted:
            error["traceback"] = scrub(formatted)
        return error


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def configure_logging(
    level: str | None = None,
    *,
    stream: IO[str] | None = None,
) -> logging.Handler:
    """Install the JSON handler on the root logger and return it.

    Idempotent: handlers installed by an earlier call (or by `basicConfig`) are removed
    first, so calling this from an entry point and again from a test does not double every
    line. They are detached, not closed -- the stream may belong to the caller.
    The level comes from `LOG_LEVEL` unless the caller overrides it.
    """
    resolved = (level or settings.log_level or "INFO").strip().upper()
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved)
    return handler


def get_logger(name: str) -> logging.Logger:
    """Logger under the project's namespace: `get_logger("bot.errors")`."""
    if name == ROOT_LOGGER_NAME or name.startswith(f"{ROOT_LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


__all__ = [
    "ALLOWED_FIELDS",
    "ALLOWED_FIELD_SHAPE",
    "REDACTED",
    "REQUEST_ID_FIELD",
    "ROOT_LOGGER_NAME",
    "JsonFormatter",
    "bind_request_id",
    "configure_logging",
    "current_request_id",
    "get_logger",
    "is_allowed_field",
    "new_request_id",
    "request_context",
    "reset_request_id",
    "scrub",
]
