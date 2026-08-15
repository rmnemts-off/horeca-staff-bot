"""Structured logging and the global error middleware (plan, task 9; TZ 9, 8.2).

TZ 9 states four things about logs, and three of them are checkable in a test rather than
in a review:

* logs are structured (JSON), INFO for what a person did, ERROR with a traceback for a
  failure;
* **"logs contain no personal data beyond `telegram_id`"** — the requirement the brief
  singles out. It is checked twice over: once on the mechanism (a field outside the
  allowlist never reaches the output, and its value appears nowhere in the line) and once
  end to end (an update carrying a name, a username and a phone produces lines that hold
  the `telegram_id` and nothing else about the person);
* one `request_id` runs through every line an update produces;
* the user never sees a traceback (TZ 8.2): the handler blows up, the log gets the
  traceback, the user gets the sentence from `src/bot/texts/`.

What is deliberately *not* asserted here is the wording of the apology: it belongs to
`src/bot/texts/` and to plan task 21. The middleware names a key; these tests put a value
behind that key and check the plumbing, so the two tasks do not have to land together.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from aiogram.dispatcher.event.bases import CancelHandler, SkipHandler
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineQuery,
    Message,
    TelegramObject,
    Update,
)
from aiogram.types import User as TelegramUser
from src.bot import texts
from src.bot.middlewares.errors import (
    ERROR_MESSAGE_KEY,
    ErrorsMiddleware,
    apology_text,
    event_fields,
    inner_event,
    setup,
)
from src.logging import (
    REDACTED,
    REQUEST_ID_FIELD,
    ROOT_LOGGER_NAME,
    JsonFormatter,
    configure_logging,
    current_request_id,
    get_logger,
    is_allowed_field,
    new_request_id,
    request_context,
    scrub,
)

# --------------------------------------------------------------------------------------
# Fixture data. Answer A1 of decisions-stage0.md: these are the two test accounts.
# --------------------------------------------------------------------------------------

MANAGER_TELEGRAM_ID = 1672818749
STAFF_TELEGRAM_ID = 917323199
CHAT_ID = -1002233445566

#: Personal data a bot naturally has at hand on every update. None of it may be logged.
PERSON_NAME = "Ivan Petrov"
PERSON_USERNAME = "rmnemts_off"
PERSON_PHONE = "+7 (985) 127 - 54 - 73"
PERSON_EMAIL = "ivan.petrov@example.com"

#: Wording stands in for the real one; TZ 8.2 puts the real sentence in src/bot/texts/.
APOLOGY = "Something went wrong, try again"


class Captured:
    """The lines one test produced, already parsed.

    `lines()` keeps only the project's own tree: the handler sits on the root logger, so
    a stray `asyncio` line would otherwise count as one of ours.
    """

    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    @property
    def raw(self) -> str:
        return self._stream.getvalue()

    def lines(self) -> list[dict[str, Any]]:
        parsed = [json.loads(line) for line in self.raw.splitlines() if line.strip()]
        return [line for line in parsed if str(line["logger"]).startswith(ROOT_LOGGER_NAME)]

    def one(self) -> dict[str, Any]:
        lines = self.lines()
        assert len(lines) == 1, f"expected exactly one log line, got {len(lines)}: {self.raw}"
        return lines[0]

    def at(self, level: str) -> list[dict[str, Any]]:
        return [line for line in self.lines() if line["level"] == level]


@pytest.fixture
def logs() -> Iterator[Captured]:
    """Install the JSON handler on a private stream and put the root logger back after."""
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    try:
        yield Captured(stream)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in previous_handlers:
            root.addHandler(handler)
        root.setLevel(previous_level)


@pytest.fixture
def log() -> logging.Logger:
    return get_logger("tests.logging")


# --------------------------------------------------------------------------------------
# The shape TZ 9 asks for: one JSON object per line
# --------------------------------------------------------------------------------------


def test_a_line_is_one_json_object(logs: Captured, log: logging.Logger) -> None:
    log.info("update handled", extra={"telegram_id": STAFF_TELEGRAM_ID})
    line = logs.one()
    assert line["level"] == "INFO"
    assert line["logger"] == "barpoint.tests.logging"
    assert line["message"] == "update handled"
    assert line["telegram_id"] == STAFF_TELEGRAM_ID


def test_timestamp_is_utc_with_milliseconds(logs: Captured, log: logging.Logger) -> None:
    log.info("tick")
    stamp = logs.one()["ts"]
    assert stamp.endswith("Z"), stamp
    moment = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert moment.tzinfo is not None
    assert moment.utcoffset() == dt.timedelta(0)


def test_several_records_are_several_lines(logs: Captured, log: logging.Logger) -> None:
    log.info("first")
    log.info("second")
    assert [line["message"] for line in logs.lines()] == ["first", "second"]


def test_cyrillic_survives_readable(logs: Captured, log: logging.Logger) -> None:
    """`ensure_ascii=False`: a checklist item inside an exception must stay readable."""
    log.info("Проверить лёд")
    assert "\\u04" not in logs.raw
    assert logs.one()["message"] == "Проверить лёд"


def test_info_for_actions_error_with_traceback_for_failures(
    logs: Captured, log: logging.Logger
) -> None:
    log.info("checklist item toggled", extra={"run_id": 7, "item_id": 12})
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("update failed")

    info, error = logs.at("INFO"), logs.at("ERROR")
    assert [line["message"] for line in info] == ["checklist item toggled"]
    assert info[0]["run_id"] == 7
    assert len(error) == 1
    assert error[0]["error"]["error_type"] == "ValueError"
    assert error[0]["error"]["message"] == "boom"
    assert "Traceback (most recent call last)" in error[0]["error"]["traceback"]


def test_a_record_without_an_exception_carries_no_error_key(
    logs: Captured, log: logging.Logger
) -> None:
    log.warning("template is empty")
    assert "error" not in logs.one()


# --------------------------------------------------------------------------------------
# TZ 9: no personal data beyond telegram_id
# --------------------------------------------------------------------------------------

#: Every field name a bot has to hand on an ordinary update and must never write down.
#: `name` is absent on purpose — `logging` refuses it in `extra=` before this module ever
#: sees it, which the test below states outright.
PERSONAL_FIELDS: dict[str, object] = {
    "full_name": PERSON_NAME,
    "employee_name": PERSON_NAME,
    "first_name": "Ivan",
    "last_name": "Petrov",
    "username": PERSON_USERNAME,
    "phone": PERSON_PHONE,
    "email": PERSON_EMAIL,
    "text": "Мохито",
    "comment": "opened late, sorry",
    "caption": "photo of the bar",
    "query": "mojito",
    "position": "bartender",
}


@pytest.mark.parametrize("field", sorted(PERSONAL_FIELDS))
def test_a_personal_field_is_dropped_and_its_value_never_printed(
    logs: Captured, log: logging.Logger, field: str
) -> None:
    value = PERSONAL_FIELDS[field]
    log.info("update handled", extra={field: value, "telegram_id": STAFF_TELEGRAM_ID})
    line = logs.one()
    assert field not in line, f"{field} reached the log"
    assert field in line["dropped_fields"], "a dropped field must stay visible by name"
    assert str(value) not in logs.raw, f"the value of {field} reached the log"
    assert line["telegram_id"] == STAFF_TELEGRAM_ID, "the one allowed identifier is gone"


@pytest.mark.parametrize(
    "field",
    [
        "telegram_id",
        "request_id",
        "venue_id",
        "run_id",
        "item_id",
        "shift_id",
        "template_id",
        "notification_id",
        "chat_id",
        "update_id",
        "duration_ms",
        "item_count",
        "notification_type",
        "outcome_status",
        "event_type",
        "handler_name",
        "scheduled_at",
        "sent_at",
        "attempts",
        "group_index",
    ],
)
def test_machinery_fields_are_allowed(field: str) -> None:
    assert is_allowed_field(field), f"{field} is machinery and must be loggable"


@pytest.mark.parametrize("field", sorted(PERSONAL_FIELDS))
def test_personal_field_names_are_refused_by_the_allowlist(field: str) -> None:
    assert not is_allowed_field(field)


def test_logging_itself_refuses_the_bare_field_name(logs: Captured, log: logging.Logger) -> None:
    """`extra={"name": ...}` never reaches this module: `logging` owns that attribute."""
    assert not is_allowed_field("name")
    with pytest.raises(KeyError):
        log.info("update handled", extra={"name": PERSON_NAME})


def test_no_personal_data_beyond_telegram_id_end_to_end(logs: Captured) -> None:
    """The headline requirement of TZ 9, checked on a real update through the middleware.

    The update carries a full name, a username and a phone number, the way every Telegram
    update does. What the log is allowed to know about the person afterwards is the
    `telegram_id` and nothing else.
    """
    update = _message_update(
        telegram_id=STAFF_TELEGRAM_ID,
        full_name=PERSON_NAME,
        username=PERSON_USERNAME,
        text=f"{PERSON_PHONE} {PERSON_EMAIL}",
    )
    asyncio.run(ErrorsMiddleware()(_ok_handler, update, {}))

    body = logs.raw
    assert str(STAFF_TELEGRAM_ID) in body, "the one permitted identifier is missing"
    for personal in (PERSON_NAME, "Ivan", "Petrov", PERSON_USERNAME, PERSON_EMAIL):
        assert personal not in body, f"{personal!r} was written to the log"
    assert "127" not in body, "part of a phone number reached the log"


@pytest.mark.parametrize(
    ("raw", "expected_gone"),
    [
        (f"delivery failed for {PERSON_PHONE}", "127 - 54 - 73"),
        ("write to ivan.petrov@example.com", "ivan.petrov@example.com"),
        ("blocked by @rmnemts_off", "@rmnemts_off"),
        ("phone 8-999-1234567 is unreachable", "8-999-1234567"),
        ("+7 ( 912 ) 874 - 12 - 24 did not answer", "874 - 12 - 24"),
    ],
)
def test_personal_data_inside_a_string_is_scrubbed(raw: str, expected_gone: str) -> None:
    cleaned = scrub(raw)
    assert expected_gone not in cleaned
    assert REDACTED in cleaned


@pytest.mark.parametrize("telegram_id", [MANAGER_TELEGRAM_ID, STAFF_TELEGRAM_ID])
def test_a_telegram_id_is_not_mistaken_for_a_phone(telegram_id: int) -> None:
    """The one identifier TZ 9 permits must survive the phone pattern intact."""
    assert scrub(f"user {telegram_id} started") == f"user {telegram_id} started"


def test_the_scrubber_leaves_ordinary_text_alone() -> None:
    assert scrub("checklist 3 of 8 done at 08:50") == "checklist 3 of 8 done at 08:50"
    assert scrub("@id") == "@id", "a short handle-looking token is not a handle"


def test_a_traceback_carrying_a_phone_number_is_scrubbed(
    logs: Captured, log: logging.Logger
) -> None:
    try:
        raise RuntimeError(f"could not reach {PERSON_PHONE}")
    except RuntimeError:
        log.exception("delivery failed")
    error = logs.one()["error"]
    assert "127 - 54 - 73" not in json.dumps(error, ensure_ascii=False)
    assert REDACTED in error["message"]


def test_a_dropped_value_is_not_smuggled_through_an_object(
    logs: Captured, log: logging.Logger
) -> None:
    """`extra={"user": some_object}` must not print the object's repr either."""

    class Person:
        def __repr__(self) -> str:
            return f"Person({PERSON_NAME!r})"

    log.info("update handled", extra={"user": Person()})
    assert PERSON_NAME not in logs.raw
    assert logs.one()["dropped_fields"] == ["user"]


def test_an_allowed_field_holding_a_string_is_still_scrubbed(
    logs: Captured, log: logging.Logger
) -> None:
    """Rule 1 is about names, rule 2 about shapes; the second one catches what the first
    cannot — an allowed field whose value happens to be a phone number."""
    log.info("notification failed", extra={"notification_type": PERSON_PHONE})
    assert "127 - 54 - 73" not in logs.raw
    assert logs.one()["notification_type"] == REDACTED


def test_reserved_logging_attributes_do_not_leak_as_fields(
    logs: Captured, log: logging.Logger
) -> None:
    """`taskName`, `pathname` and friends are the logging module's, not a caller's."""
    log.info("tick")
    line = logs.one()
    assert set(line) <= {"ts", "level", "logger", "message", REQUEST_ID_FIELD}


# --------------------------------------------------------------------------------------
# request_id
# --------------------------------------------------------------------------------------


def test_request_id_is_stamped_on_every_line_of_one_update(
    logs: Captured, log: logging.Logger
) -> None:
    with request_context() as request_id:
        log.info("first")
        log.info("second")
    ids = {line[REQUEST_ID_FIELD] for line in logs.lines()}
    assert ids == {request_id}


def test_outside_a_context_there_is_no_request_id(logs: Captured, log: logging.Logger) -> None:
    log.info("startup")
    assert REQUEST_ID_FIELD not in logs.one()
    assert current_request_id() is None


def test_two_updates_get_two_request_ids(logs: Captured, log: logging.Logger) -> None:
    with request_context():
        log.info("first")
    with request_context():
        log.info("second")
    first, second = (line[REQUEST_ID_FIELD] for line in logs.lines())
    assert first != second


def test_the_context_is_reset_even_when_the_handler_raises() -> None:
    with pytest.raises(ValueError), request_context():
        raise ValueError("boom")
    assert current_request_id() is None, "a finished update must not leak its id"


def test_an_explicit_request_id_wins(logs: Captured, log: logging.Logger) -> None:
    with request_context("aaaabbbbccccdddd"):
        log.info("carried", extra={REQUEST_ID_FIELD: "eeeeffff00001111"})
    assert logs.one()[REQUEST_ID_FIELD] == "eeeeffff00001111"


def test_new_request_id_is_short_and_random() -> None:
    ids = {new_request_id() for _ in range(64)}
    assert len(ids) == 64
    assert all(re.fullmatch(r"[0-9a-f]{16}", value) for value in ids)


def test_configure_logging_is_idempotent(log: logging.Logger) -> None:
    """Called from an entry point and again from a test, it must not double every line."""
    root = logging.getLogger()
    previous_handlers, previous_level = list(root.handlers), root.level
    stream = io.StringIO()
    try:
        configure_logging("INFO", stream=stream)
        configure_logging("INFO", stream=stream)
        log.info("once")
        assert len(stream.getvalue().strip().splitlines()) == 1
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in previous_handlers:
            root.addHandler(handler)
        root.setLevel(previous_level)


def test_the_formatter_can_be_used_on_its_own() -> None:
    record = logging.LogRecord(
        name="barpoint.probe",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "hello world"


# --------------------------------------------------------------------------------------
# Global error middleware (TZ 8.2: the user never sees a traceback)
# --------------------------------------------------------------------------------------


def _message_update(
    *,
    telegram_id: int = STAFF_TELEGRAM_ID,
    full_name: str = PERSON_NAME,
    username: str | None = PERSON_USERNAME,
    text: str = "hi",
    update_id: int = 4242,
) -> Update:
    first, _, last = full_name.partition(" ")
    message = Message(
        message_id=11,
        date=dt.datetime(2026, 1, 1, 8, 50, tzinfo=dt.UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=TelegramUser(
            id=telegram_id,
            is_bot=False,
            first_name=first,
            last_name=last or None,
            username=username,
        ),
        text=text,
    )
    return Update(update_id=update_id, message=message)


def _inline_update(*, query: str = "mojito", update_id: int = 4444) -> Update:
    """A name being typed into the input field (part IV of the stage 1 spec)."""
    return Update(
        update_id=update_id,
        inline_query=InlineQuery(
            id="iq1",
            from_user=TelegramUser(id=STAFF_TELEGRAM_ID, is_bot=False, first_name="Ivan"),
            query=query,
            offset="",
            chat_type="sender",
        ),
    )


def _callback_update(*, data: str = "cl:tg:1:2", update_id: int = 4343) -> Update:
    message = Message(
        message_id=12,
        date=dt.datetime(2026, 1, 1, 8, 51, tzinfo=dt.UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
    )
    query = CallbackQuery(
        id="q1",
        from_user=TelegramUser(
            id=STAFF_TELEGRAM_ID,
            is_bot=False,
            first_name="Ivan",
            username=PERSON_USERNAME,
        ),
        chat_instance="chat-instance",
        message=message,
        data=data,
    )
    return Update(update_id=update_id, callback_query=query)


async def _ok_handler(event: TelegramObject, data: dict[str, Any]) -> str:
    return "handled"


def _boom_handler(
    message: str = "template_id 7 is missing",
) -> Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]:
    async def handler(event: TelegramObject, data: dict[str, Any]) -> Any:
        raise RuntimeError(message)

    return handler


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record what the bot would have sent, without a Bot instance or a network."""
    sent: list[dict[str, Any]] = []

    async def message_answer(self: Message, text: str, **kwargs: Any) -> None:
        sent.append({"kind": "message", "text": text})

    async def callback_answer(
        self: CallbackQuery,
        text: str | None = None,
        show_alert: bool = False,
        **kwargs: Any,
    ) -> None:
        sent.append({"kind": "callback", "text": text, "show_alert": show_alert})

    async def inline_answer(self: InlineQuery, results: list[Any], **kwargs: Any) -> None:
        sent.append({"kind": "inline", "results": results, **kwargs})

    monkeypatch.setattr(Message, "answer", message_answer, raising=True)
    monkeypatch.setattr(CallbackQuery, "answer", callback_answer, raising=True)
    monkeypatch.setattr(InlineQuery, "answer", inline_answer, raising=True)
    return sent


@pytest.fixture
def apology(monkeypatch: pytest.MonkeyPatch) -> str:
    """Stand in for plan task 21: put a value behind the key the middleware reads."""
    monkeypatch.setattr(texts, ERROR_MESSAGE_KEY, APOLOGY, raising=False)
    return APOLOGY


def test_an_exception_in_a_handler_does_not_reach_the_user(
    logs: Captured, answers: list[dict[str, Any]], apology: str
) -> None:
    """The single acceptance line of plan task 9."""
    update = _message_update()
    result = asyncio.run(ErrorsMiddleware()(_boom_handler(), update, {}))

    assert result is None, "the update dies in the middleware; the bot stays up"
    assert answers == [{"kind": "message", "text": apology}]
    assert "Traceback" not in apology
    assert "RuntimeError" not in apology


def test_a_failed_inline_query_is_answered_with_an_empty_personal_list(
    logs: Captured, answers: list[dict[str, Any]], apology: str
) -> None:
    """The fourth road of «a refusal is an answer» (`src/bot/inline.py`).

    An inline query has nowhere to put an apology, and one that is never answered leaves the
    dropdown loading until the client gives up — the bartender's read on that is a bot that
    hangs. Personal and uncached like every other answer, because Telegram would otherwise
    hand this empty result to the next person who types the same word.
    """
    result = asyncio.run(ErrorsMiddleware()(_boom_handler(), _inline_update(), {}))

    assert result is None
    assert answers == [
        {
            "kind": "inline",
            "results": [],
            "cache_time": 0,
            "is_personal": True,
            "next_offset": "",
            "button": None,
        }
    ]
    assert logs.at("ERROR"), "the traceback still reaches the log"


def test_the_traceback_goes_to_the_log_with_the_request_id(
    logs: Captured, answers: list[dict[str, Any]], apology: str
) -> None:
    data: dict[str, Any] = {}
    asyncio.run(ErrorsMiddleware()(_boom_handler(), _message_update(), data))

    failures = logs.at("ERROR")
    assert len(failures) == 1
    failure = failures[0]
    assert failure["error"]["error_type"] == "RuntimeError"
    assert "Traceback (most recent call last)" in failure["error"]["traceback"]
    assert failure["outcome_status"] == "failed"
    assert failure[REQUEST_ID_FIELD] == data[REQUEST_ID_FIELD]


def test_the_request_id_reaches_the_handler_context(logs: Captured) -> None:
    seen: dict[str, Any] = {}

    async def handler(event: TelegramObject, data: dict[str, Any]) -> None:
        seen["from_data"] = data[REQUEST_ID_FIELD]
        seen["from_context"] = current_request_id()

    asyncio.run(ErrorsMiddleware()(handler, _message_update(), {}))
    assert seen["from_data"] == seen["from_context"]
    assert {line[REQUEST_ID_FIELD] for line in logs.lines()} == {seen["from_data"]}


def test_a_successful_update_is_logged_at_info_with_a_duration(logs: Captured) -> None:
    result = asyncio.run(ErrorsMiddleware()(_ok_handler, _message_update(), {}))
    assert result == "handled"

    lines = logs.at("INFO")
    assert [line["message"] for line in lines] == ["update received", "update handled"]
    assert lines[-1]["outcome_status"] == "ok"
    assert lines[-1]["duration_ms"] >= 0
    assert lines[-1]["telegram_id"] == STAFF_TELEGRAM_ID
    assert lines[-1]["event_type"] == "message"
    assert lines[-1]["update_id"] == 4242
    assert not logs.at("ERROR")


def test_a_callback_query_is_always_answered(
    logs: Captured, answers: list[dict[str, Any]], apology: str
) -> None:
    """An unanswered callback spins on the client for a minute (plan, task 20)."""
    asyncio.run(ErrorsMiddleware()(_boom_handler(), _callback_update(), {}))
    assert answers == [{"kind": "callback", "text": apology, "show_alert": True}]


def test_without_a_texts_entry_the_user_still_gets_no_traceback(
    logs: Captured, answers: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until plan task 21 lands the key resolves to nothing — a closed spinner, not a dump."""
    monkeypatch.delattr(texts, ERROR_MESSAGE_KEY, raising=False)
    assert apology_text() is None

    asyncio.run(ErrorsMiddleware()(_boom_handler(), _callback_update(), {}))
    assert answers == [{"kind": "callback", "text": None, "show_alert": False}]
    assert logs.at("WARNING"), "a missing apology must be visible in the log"


def test_a_blank_texts_entry_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(texts, ERROR_MESSAGE_KEY, "   ", raising=False)
    assert apology_text() is None


def test_a_failing_apology_does_not_replace_the_original_failure(
    logs: Captured, apology: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked bot must not turn one logged failure into an unhandled exception."""

    async def blocked(self: Message, text: str, **kwargs: Any) -> None:
        raise RuntimeError("bot was blocked by the user")

    monkeypatch.setattr(Message, "answer", blocked, raising=True)
    result = asyncio.run(ErrorsMiddleware()(_boom_handler(), _message_update(), {}))

    assert result is None
    messages = [line["message"] for line in logs.at("ERROR")]
    assert messages == ["update failed", "apology could not be delivered"]


@pytest.mark.parametrize("control", [SkipHandler, CancelHandler])
def test_aiogram_routing_control_flow_is_not_swallowed(
    logs: Captured, control: type[Exception]
) -> None:
    """`SkipHandler` means "try the next router", not "the update failed"."""

    async def handler(event: TelegramObject, data: dict[str, Any]) -> None:
        raise control()

    with pytest.raises(control):
        asyncio.run(ErrorsMiddleware()(handler, _message_update(), {}))
    assert not logs.at("ERROR")


def test_event_fields_collect_ids_and_nothing_else() -> None:
    fields = event_fields(_message_update())
    assert fields == {
        "update_id": 4242,
        "event_type": "message",
        "telegram_id": STAFF_TELEGRAM_ID,
        "chat_id": CHAT_ID,
    }
    assert all(is_allowed_field(name) for name in fields)


def test_event_fields_of_a_callback_query_find_the_chat() -> None:
    fields = event_fields(_callback_update())
    assert fields["event_type"] == "callback_query"
    assert fields["telegram_id"] == STAFF_TELEGRAM_ID
    assert fields["chat_id"] == CHAT_ID


def test_an_update_of_an_unknown_kind_does_not_break_logging(logs: Captured) -> None:
    """A field this aiogram version does not know must not take the bot down."""
    empty = Update(update_id=99)
    fields = event_fields(empty)
    assert fields == {"update_id": 99, "event_type": "unknown"}
    assert inner_event(empty) is empty

    result = asyncio.run(ErrorsMiddleware()(_ok_handler, empty, {}))
    assert result == "handled"


def test_setup_registers_the_middleware_as_an_outer_one() -> None:
    from aiogram import Dispatcher

    dispatcher = Dispatcher()
    middleware = setup(dispatcher)
    assert middleware in dispatcher.update.outer_middleware
