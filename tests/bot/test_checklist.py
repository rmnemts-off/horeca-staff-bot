"""The checklist screen (TZ 5.4, decisions D11 and B1; plan task 24).

What is worth asserting about this block is not that a button exists — it is the handful of
behaviours a bar breaks on, and every test below is named after one of them:

* **one run is one message.** Re-entering from «Моя смена» is a press on a *different*
  screen, and it must rewrite the checklist's own message. A handler that edited the message
  it was pressed on would paint a checklist over the shift screen and leave the real one
  live somewhere above (TZ 5.4, 8.2);
* **the message that was swiped away is replaced and the new id is written down**, or the
  next tap edits a ghost forever;
* **a repeated tap does not move the counter.** The counter is `view.done_items`, recomputed
  by the service from `checklist_run_items`; the screen never adds one to a number it drew;
* **a tap inside a finished run changes nothing and rewrites nothing** — the message it came
  from may be an hour old (TZ 5.4);
* **finishing with lines left asks, then asks for a reason, and the manager is told by the
  service.** Not one `SendMessage` leaves these handlers except the one that replaces the
  employee's own missing screen (TZ 6, plan task 24);
* **eight groups and forty lines stay one readable message** — the volume TZ 5.4 names, and
  the reason decision D11 puts the list in the text and only the markers in the buttons;
* **the empty template is a screen** (TZ 8.1, decision B1), with nothing on it to press.

The views are exercised directly, without a bot: they are pure functions of a `RunView`, and
that is precisely why the notification worker can call them too. The service is a fake with
the same signatures — the real one has its own tests (plan task 17) and a database here
would only make these slower and less specific.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from types import SimpleNamespace
from typing import Any, Final

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage
from aiogram.types import InlineKeyboardMarkup
from src.bot import texts
from src.bot.callbacks import (
    ChecklistFinish,
    ChecklistGroup,
    ChecklistShow,
    ChecklistSkipAccept,
    ChecklistToggle,
    parse,
)
from src.bot.handlers import checklist as handlers
from src.bot.keyboards.checklist import CAPTION_LIMIT, fit
from src.bot.middlewares.auth import ACTOR_KEY
from src.bot.middlewares.resolver import PAYLOAD_KEY, SUBJECT_KEY
from src.bot.middlewares.services import SERVICES_KEY
from src.bot.states import ChecklistSkip
from src.bot.views.checklist import (
    group_index_of,
    render_finished,
    render_run,
    render_skip_question,
)
from src.db.models import ChecklistRun, ChecklistType, RunStatus
from src.services.checklists import (
    CompletionOutcome,
    CompletionResult,
    GroupView,
    ItemView,
    RunView,
    ToggleOutcome,
    ToggleResult,
)

from tests.bot.test_middlewares import (
    CHAT_ID,
    STAFF,
    STAFF_TELEGRAM_ID,
    STAFF_USER_ID,
    VENUE_ID,
    make_bot,
    make_callback,
    make_message,
    session_of,
)

RUN_ID: Final = 77
#: The message the run lives in. Deliberately not the id `make_callback` presses on (1), so
#: that "edits the run's message" and "edits the message it was pressed on" cannot pass for
#: each other.
RUN_MESSAGE_ID: Final = 900

NOW: Final = dt.datetime(2026, 8, 14, 6, 50, tzinfo=dt.UTC)

#: Telegram's own limit on the text of one message. The volume TZ 5.4 names has to fit it.
MESSAGE_LIMIT: Final = 4096


# --------------------------------------------------------------------------------------
# Building a render model
# --------------------------------------------------------------------------------------


def item(
    item_id: int,
    text: str,
    *,
    group_index: int,
    group_name: str | None = None,
    order_index: int = 0,
    is_done: bool = False,
    is_critical: bool = False,
) -> ItemView:
    return ItemView(
        item_id=item_id,
        text=text,
        group_index=group_index,
        group_name=group_name,
        order_index=order_index,
        is_done=is_done,
        is_critical=is_critical,
        requires_photo=False,
        requires_comment=False,
        done_at=None,
        photo_file_id=None,
        comment=None,
    )


def group(index: int, name: str | None, *items: ItemView) -> GroupView:
    return GroupView(index=index, name=name, items=items)


def run_view(
    *groups: GroupView,
    run_id: int = RUN_ID,
    chat_id: int | None = CHAT_ID,
    message_id: int | None = RUN_MESSAGE_ID,
    completed_at: dt.datetime | None = None,
    checklist_type: ChecklistType = ChecklistType.OPENING,
) -> RunView:
    return RunView(
        run_id=run_id,
        template_id=1,
        checklist_type=checklist_type,
        status=RunStatus.SENT,
        shift_id=None,
        user_id=STAFF_USER_ID,
        chat_id=chat_id,
        message_id=message_id,
        sent_at=NOW,
        started_at=None,
        completed_at=completed_at,
        skip_comment=None,
        groups=groups,
    )


def small_view(**kwargs: Any) -> RunView:
    """Two groups, three lines, one of them critical — the shape most tests need."""
    return run_view(
        group(
            1,
            "Visual",
            item(101, "Light", group_index=1, group_name="Visual", order_index=1),
            item(
                102,
                "Music",
                group_index=1,
                group_name="Visual",
                order_index=2,
                is_critical=True,
            ),
        ),
        group(
            2,
            "Station",
            item(201, "Ice", group_index=2, group_name="Station", order_index=1),
        ),
        **kwargs,
    )


def wide_view() -> RunView:
    """The volume TZ 5.4 names as the benchmark: eight groups, forty lines."""
    return run_view(
        *(
            group(
                index,
                f"Group {index}",
                *(
                    item(
                        index * 100 + position,
                        f"Line {position} of group {index}",
                        group_index=index,
                        group_name=f"Group {index}",
                        order_index=position,
                        is_critical=position == 1,
                    )
                    for position in range(1, 6)
                ),
            )
            for index in range(1, 9)
        )
    )


def flip(view: RunView, item_id: int) -> RunView:
    """The same view with one line ticked the other way — what the service would answer."""
    groups = tuple(
        dataclasses.replace(
            bucket,
            items=tuple(
                dataclasses.replace(line, is_done=not line.is_done)
                if line.item_id == item_id
                else line
                for line in bucket.items
            ),
        )
        for bucket in view.groups
    )
    return dataclasses.replace(view, groups=groups)


# --------------------------------------------------------------------------------------
# The service, faked at its own signatures
# --------------------------------------------------------------------------------------


class FakeChecklists:
    """`ChecklistService` as this screen uses it, and no wider.

    The four methods are copied from `src/services/checklists.py` down to the keyword-only
    arguments, so a handler that called them differently would fail here rather than in
    production. What it deliberately does *not* copy is any decision: the outcome each call
    returns is set by the test, because the point of every assertion below is what the
    screen does with an outcome, not how the service arrived at one.
    """

    def __init__(self, view: RunView | None) -> None:
        self.stored = view
        self.toggle_outcome = ToggleOutcome.TOGGLED
        self.completion = CompletionOutcome.COMPLETED
        self.toggled: list[int] = []
        self.completions: list[dict[str, Any]] = []
        self.remembered: list[tuple[int, int, int]] = []

    async def view(self, run_id: int) -> RunView | None:
        return self.stored if self.stored is not None and self.stored.run_id == run_id else None

    async def toggle(
        self,
        *,
        run_id: int,
        item_id: int,
        moment: dt.datetime,
        photo_file_id: str | None = None,
        comment: str | None = None,
    ) -> ToggleResult:
        self.toggled.append(item_id)
        if self.stored is None or self.stored.run_id != run_id:
            return ToggleResult(
                outcome=ToggleOutcome.NOT_FOUND, item_id=item_id, is_done=False, view=None
            )
        if self.toggle_outcome is ToggleOutcome.TOGGLED:
            self.stored = flip(self.stored, item_id)
        return ToggleResult(
            outcome=self.toggle_outcome,
            item_id=item_id,
            is_done=True,
            view=self.stored,
        )

    async def complete(
        self,
        *,
        run_id: int,
        completed_by: int,
        moment: dt.datetime,
        skip_comment: str | None = None,
    ) -> CompletionResult:
        self.completions.append(
            {"run_id": run_id, "completed_by": completed_by, "skip_comment": skip_comment}
        )
        pending = () if self.stored is None else self.stored.pending
        run = make_run(chat_id=CHAT_ID, message_id=RUN_MESSAGE_ID)
        return CompletionResult(outcome=self.completion, run=run, pending=pending)

    async def remember_message(self, *, run_id: int, chat_id: int, message_id: int) -> None:
        self.remembered.append((run_id, chat_id, message_id))


def make_run(
    *,
    chat_id: int | None = CHAT_ID,
    message_id: int | None = RUN_MESSAGE_ID,
    completed_at: dt.datetime | None = None,
) -> ChecklistRun:
    """The row the resolver hands the handler as `data["subject"]`."""
    run = ChecklistRun(
        id=RUN_ID,
        venue_id=VENUE_ID,
        shift_id=None,
        user_id=STAFF_USER_ID,
        template_id=1,
        type=ChecklistType.OPENING,
        status=RunStatus.SENT,
        done_items=0,
        total_items=3,
    )
    run.chat_id = chat_id
    run.message_id = message_id
    run.completed_at = completed_at
    return run


def context(
    checklists: FakeChecklists,
    *,
    payload: Any,
    subject: Any = None,
) -> dict[str, Any]:
    """The handler context as it looks once the resolver has done its half (TZ 9)."""
    return {
        PAYLOAD_KEY: payload,
        SUBJECT_KEY: subject,
        SERVICES_KEY: SimpleNamespace(checklists=checklists),
        ACTOR_KEY: STAFF,
    }


def make_state(bot: Bot) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot.id, chat_id=CHAT_ID, user_id=STAFF_TELEGRAM_ID),
    )


def bad_request(text: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SendMessage(chat_id=CHAT_ID, text="x"), message=text)


def deletes(bot: Bot) -> list[DeleteMessage]:
    """The messages this bot asked Telegram to remove."""
    return [call for call in session_of(bot).calls if isinstance(call, DeleteMessage)]


def edits(bot: Bot) -> list[EditMessageText]:
    return [call for call in session_of(bot).calls if isinstance(call, EditMessageText)]


def sends(bot: Bot) -> list[SendMessage]:
    return [call for call in session_of(bot).calls if isinstance(call, SendMessage)]


def markup_of(call: EditMessageText | SendMessage) -> InlineKeyboardMarkup:
    assert isinstance(call.reply_markup, InlineKeyboardMarkup)
    return call.reply_markup


def captions(markup: InlineKeyboardMarkup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def payloads(markup: InlineKeyboardMarkup) -> list[Any]:
    return [
        parse(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


# --------------------------------------------------------------------------------------
# The empty state (TZ 8.1, decision B1)
# --------------------------------------------------------------------------------------


def test_a_template_with_no_lines_is_a_screen_and_not_a_blank_checklist() -> None:
    """TZ 8.1: the venue has entered nothing yet, and the employee is told so in one line."""
    screen = render_run(run_view())
    assert screen.text == texts.CHECKLIST_NOT_FILLED_IN
    assert screen.markup is None, "an empty checklist must not offer a button to press"


# --------------------------------------------------------------------------------------
# The text and the buttons (decision D11)
# --------------------------------------------------------------------------------------


def markup_of_screen(screen: Any) -> InlineKeyboardMarkup:
    assert isinstance(screen.markup, InlineKeyboardMarkup)
    return screen.markup


def test_the_list_is_the_text_and_the_markers_are_the_buttons() -> None:
    screen = render_run(small_view())
    lines = screen.text.splitlines()

    assert lines[0] == texts.CHECKLIST_OPENING_TITLE
    assert texts.CHECKLIST_PROGRESS_TEMPLATE.format(done=0, total=3) in lines
    # Every line of the venue's own wording travels in the text, not in a caption.
    assert any("Light" in line for line in lines)
    for row in captions(markup_of_screen(screen)):
        assert all("Light" not in caption for caption in row)


def test_a_critical_line_is_marked_in_place() -> None:
    """TZ 5.4: `is_critical` has to be visible before «Готово», not only afterwards."""
    text = render_run(small_view()).text
    marked = [line for line in text.splitlines() if texts.CHECKLIST_CRITICAL_MARK in line]
    assert len(marked) == 1
    assert "Music" in marked[0]


def test_only_the_current_group_is_numbered_so_a_marker_names_a_line() -> None:
    """Decision D11: the buttons are bare numbers, so the text has to say which lines."""
    screen = render_run(small_view(), group_index=2)
    numbered = [line for line in screen.text.splitlines() if line.startswith("[ ] 1.")]
    assert numbered == ["[ ] 1. Ice"], "exactly the lines of the group on the buttons"

    markers = captions(markup_of_screen(screen))[0]
    assert markers == [texts.CHECKLIST_MARK_BUTTON_TEMPLATE.format(number=1)]


def test_a_ticked_line_carries_a_ticked_marker() -> None:
    view = flip(small_view(), 101)
    screen = render_run(view, group_index=1)
    assert "[✓] 1. Light" in screen.text
    assert captions(markup_of_screen(screen))[0] == [
        texts.CHECKLIST_MARK_DONE_BUTTON_TEMPLATE.format(number=1),
        texts.CHECKLIST_MARK_BUTTON_TEMPLATE.format(number=2),
    ]


def test_an_unnamed_group_still_gets_a_heading() -> None:
    """Decision D2 lets a venue enter lines without naming the group; forty of them without
    a heading is the flat list TZ 5.4 refuses."""
    screen = render_run(
        run_view(group(1, None, item(1, "Line", group_index=1)), message_id=None, chat_id=None)
    )
    assert texts.CHECKLIST_GROUP_UNNAMED_TEMPLATE.format(index=1) in screen.text


def test_without_an_index_the_markers_land_where_the_work_is() -> None:
    """Re-entry from «Моя смена»: the employee stopped in group two, not in group one."""
    view = flip(flip(small_view(), 101), 102)
    screen = render_run(view)
    assert "[ ] 1. Ice" in screen.text, "group one is done, so the markers moved on"


def test_a_group_index_no_group_answers_to_does_not_break_the_screen() -> None:
    """A press from a screen drawn before a manager reopened the run is stale, not hostile."""
    screen = render_run(small_view(), group_index=404)
    assert texts.CHECKLIST_PROGRESS_TEMPLATE.format(done=0, total=3) in screen.text


def test_a_single_group_gets_no_switcher() -> None:
    screen = render_run(run_view(group(1, "Visual", item(1, "Light", group_index=1))))
    rows = captions(markup_of_screen(screen))
    assert rows == [
        [texts.CHECKLIST_MARK_BUTTON_TEMPLATE.format(number=1)],
        [texts.CHECKLIST_FINISH_BUTTON],
    ]


def test_the_group_switcher_carries_the_venues_own_group_index() -> None:
    """The heading counts from one; the payload carries `group_index` as stored (D2)."""
    view = run_view(
        group(0, "Visual", item(1, "Light", group_index=0)),
        group(7, "Station", item(2, "Ice", group_index=7)),
    )
    screen = render_run(view, group_index=0)
    switches = [
        row for row in payloads(markup_of_screen(screen)) if isinstance(row, ChecklistGroup)
    ]
    assert [row.group_index for row in switches] == [0, 7]
    assert "1. Visual" in screen.text and "2. Station" in screen.text


def test_a_finished_group_is_ticked_on_the_switcher() -> None:
    view = flip(flip(small_view(), 101), 102)
    screen = render_run(view, group_index=2)
    switch_row = captions(markup_of_screen(screen))[1]
    assert switch_row[0].startswith(texts.CHECKLIST_MARK_DONE_BUTTON_TEMPLATE.format(number=1))


def test_a_group_named_at_length_is_cut_to_the_caption_limit() -> None:
    """TZ 8.2: nothing bounds a venue's own wording, so the caption is cut here."""
    assert fit("x" * 40) == "x" * (CAPTION_LIMIT - 3) + "..."
    assert len(fit("x" * 40)) == CAPTION_LIMIT
    assert fit("short") == "short"


def test_eight_groups_and_forty_lines_stay_one_readable_message() -> None:
    """The benchmark of TZ 5.4, and the reason decision D11 exists at all."""
    screen = render_run(wide_view())
    lines = screen.text.splitlines()

    assert sum(1 for line in lines if line.startswith("[")) == 40
    headings = [line for line in lines if not line.startswith("[") and ". Group " in line]
    assert len(headings) == 8
    assert headings[0] == "1. Group 1" and headings[-1] == "8. Group 8"
    assert lines[-1] == texts.CHECKLIST_PROGRESS_TEMPLATE.format(done=0, total=40)
    assert len(screen.text) < MESSAGE_LIMIT, "one checklist has to be one message"

    rows = captions(markup_of_screen(screen))
    # Five markers of the current group, eight groups two by two, and «Готово».
    assert [len(row) for row in rows] == [5, 2, 2, 2, 2, 1]
    assert rows[-1] == [texts.CHECKLIST_FINISH_BUTTON]
    for row in rows:
        for caption in row:
            assert len(caption) <= CAPTION_LIMIT, caption


def test_every_marker_of_the_wide_screen_names_its_own_line() -> None:
    view = wide_view()
    screen = render_run(view, group_index=3)
    toggles = [
        row for row in payloads(markup_of_screen(screen)) if isinstance(row, ChecklistToggle)
    ]
    assert [row.item_id for row in toggles] == [301, 302, 303, 304, 305]
    assert all(row.run_id == RUN_ID for row in toggles)


def test_the_group_of_a_line_is_derivable_from_the_run() -> None:
    """`ChecklistToggle` carries no group; the screen after a tap must not move anyway."""
    view = wide_view()
    assert group_index_of(view, 402) == 4
    assert group_index_of(view, 999) is None


# --------------------------------------------------------------------------------------
# Finishing, as text (TZ 5.4)
# --------------------------------------------------------------------------------------


def test_the_question_lists_what_is_left_and_offers_exactly_two_answers() -> None:
    view = small_view()
    screen = render_skip_question(RUN_ID, view.pending)

    assert captions(markup_of_screen(screen)) == [
        [texts.CHECKLIST_SEND_AS_IS_BUTTON, texts.CHECKLIST_KEEP_GOING_BUTTON]
    ]
    # The critical line comes first, and carries its mark: the order is the service's.
    first, *_ = [line for line in screen.text.splitlines() if line.startswith("•")]
    assert texts.CHECKLIST_CRITICAL_MARK in first and "Music" in first


def test_the_two_endings_are_the_two_sentences_of_the_tz() -> None:
    assert render_finished(ChecklistType.OPENING, skipped=False).text == texts.CHECKLIST_COMPLETED
    assert (
        render_finished(ChecklistType.CLOSING, skipped=False).text
        == texts.CHECKLIST_CLOSING_COMPLETED
    )
    assert render_finished(ChecklistType.OPENING, skipped=True).text == texts.CHECKLIST_SKIP_SENT
    assert render_finished(ChecklistType.OPENING, skipped=False).markup is None


# --------------------------------------------------------------------------------------
# One run, one message (TZ 5.4, 8.2)
# --------------------------------------------------------------------------------------


async def test_reopening_brings_the_checklist_down_and_takes_the_old_copy_away() -> None:
    """TZ 5.4: the shift screen re-opens the checklist «in case he swiped it away».

    The press is answered by putting the checklist *in front of* the employee, because that
    is what they asked for — the button is on the shift screen precisely when the checklist
    is not in sight. Editing a message forty messages up answers it with nothing visible.

    One run still has one live message: the run's id moves to the new one and the copy left
    above is deleted. That is the property «two messages of one run» is about, and it is
    asserted here rather than the mechanism that used to provide it.
    """
    bot = make_bot()
    checklists = FakeChecklists(small_view())
    payload = ChecklistShow(run_id=RUN_ID)
    event = make_callback(payload.pack(), bot=bot)

    await handlers.show(event, bot=bot, **context(checklists, payload=payload))

    (sent,) = sends(bot)
    assert sent.chat_id == CHAT_ID
    assert not edits(bot), "the checklist is brought down, not rewritten where it was"
    assert checklists.remembered == [(RUN_ID, CHAT_ID, 1000)], (
        "the run points at the message the employee is now looking at"
    )
    (deleted,) = deletes(bot)
    assert (deleted.chat_id, deleted.message_id) == (CHAT_ID, RUN_MESSAGE_ID)


async def test_a_swiped_message_is_replaced_and_the_new_id_is_written_down() -> None:
    """TZ 5.4 names the case: the employee smahnul the message, so there is nothing to edit."""
    bot = make_bot()
    session_of(bot).fail_with["EditMessageText"] = bad_request(
        "Bad Request: message to edit not found"
    )
    checklists = FakeChecklists(small_view())
    payload = ChecklistShow(run_id=RUN_ID)

    await handlers.show(
        make_callback(payload.pack(), bot=bot),
        bot=bot,
        **context(checklists, payload=payload),
    )

    (sent,) = sends(bot)
    assert sent.chat_id == CHAT_ID
    assert checklists.remembered == [(RUN_ID, CHAT_ID, 1000)], (
        "without the new message_id the next tap edits the message that is gone"
    )


async def test_a_run_with_no_message_yet_gets_one_and_it_is_remembered() -> None:
    """The employee looked before the scheduled delivery went out (TZ 6, plan task 19)."""
    bot = make_bot()
    checklists = FakeChecklists(small_view(chat_id=None, message_id=None))
    payload = ChecklistShow(run_id=RUN_ID)

    await handlers.show(
        make_callback(payload.pack(), bot=bot),
        bot=bot,
        **context(checklists, payload=payload),
    )

    assert not edits(bot)
    assert [call.chat_id for call in sends(bot)] == [CHAT_ID]
    assert checklists.remembered == [(RUN_ID, CHAT_ID, 1000)]


async def test_a_run_of_another_shift_is_answered_and_not_drawn() -> None:
    bot = make_bot()
    checklists = FakeChecklists(None)
    payload = ChecklistShow(run_id=RUN_ID)

    await handlers.show(
        make_callback(payload.pack(), bot=bot),
        bot=bot,
        **context(checklists, payload=payload),
    )

    assert not edits(bot) and not sends(bot)
    assert [answer.text for answer in session_of(bot).answers()] == [texts.ERROR_OUTDATED_SCREEN]


# --------------------------------------------------------------------------------------
# Ticking (TZ 5.4)
# --------------------------------------------------------------------------------------


async def test_a_tap_flips_one_line_and_redraws_its_own_group() -> None:
    bot = make_bot()
    checklists = FakeChecklists(wide_view())
    payload = ChecklistToggle(run_id=RUN_ID, item_id=403)

    await handlers.toggle(
        make_callback(payload.pack(), bot=bot),
        bot=bot,
        **context(checklists, payload=payload),
    )

    (edit,) = edits(bot)
    assert checklists.toggled == [403]
    assert "[✓] 3. Line 3 of group 4" in str(edit.text), "the screen stayed on group four"
    toggles = [row for row in payloads(markup_of(edit)) if isinstance(row, ChecklistToggle)]
    assert [row.item_id for row in toggles] == [401, 402, 403, 404, 405]


async def test_the_same_tap_arriving_twice_does_not_move_the_counter() -> None:
    """The service answers `UNCHANGED` for the tap that lost the race; the screen must draw
    the same number and must not turn the redraw into a second message."""
    bot = make_bot()
    checklists = FakeChecklists(small_view())
    payload = ChecklistToggle(run_id=RUN_ID, item_id=101)

    await handlers.toggle(
        make_callback(payload.pack(), bot=bot),
        bot=bot,
        **context(checklists, payload=payload),
    )
    checklists.toggle_outcome = ToggleOutcome.UNCHANGED
    await handlers.toggle(
        make_callback(payload.pack(), bot=bot),
        bot=bot,
        **context(checklists, payload=payload),
    )

    first, second = edits(bot)
    progress = texts.CHECKLIST_PROGRESS_TEMPLATE.format(done=1, total=3)
    assert progress in str(first.text)
    assert progress in str(second.text), "the counter is the service's, never incremented here"
    assert not sends(bot)


async def test_a_tap_inside_a_finished_run_changes_nothing_and_rewrites_no_screen() -> None:
    """TZ 5.4: «нажатие в устаревшем сообщении данные не меняет»."""
    bot = make_bot()
    checklists = FakeChecklists(small_view(completed_at=NOW))
    checklists.toggle_outcome = ToggleOutcome.RUN_FINISHED
    payload = ChecklistToggle(run_id=RUN_ID, item_id=101)

    await handlers.toggle(
        make_callback(payload.pack(), bot=bot),
        bot=bot,
        **context(checklists, payload=payload),
    )

    assert not edits(bot) and not sends(bot), "the stale message is not ours to rewrite"
    assert [answer.text for answer in session_of(bot).answers()] == [
        texts.CHECKLIST_ALREADY_FINISHED
    ]


async def test_switching_a_group_moves_the_markers_inside_one_message() -> None:
    bot = make_bot()
    checklists = FakeChecklists(wide_view())
    payload = ChecklistGroup(run_id=RUN_ID, group_index=6)

    await handlers.switch_group(
        make_callback(payload.pack(), bot=bot),
        bot=bot,
        **context(checklists, payload=payload),
    )

    (edit,) = edits(bot)
    assert not sends(bot)
    toggles = [row for row in payloads(markup_of(edit)) if isinstance(row, ChecklistToggle)]
    assert [row.item_id for row in toggles] == [601, 602, 603, 604, 605]


# --------------------------------------------------------------------------------------
# «Готово» and the skipped lines (TZ 5.4, 6)
# --------------------------------------------------------------------------------------


async def test_everything_ticked_closes_the_checklist_and_tells_the_manager_nothing() -> None:
    bot = make_bot()
    checklists = FakeChecklists(small_view())
    payload = ChecklistFinish(run_id=RUN_ID)

    await handlers.finish(
        make_callback(payload.pack(), bot=bot),
        bot=bot,
        **context(checklists, payload=payload, subject=make_run()),
    )

    (edit,) = edits(bot)
    assert str(edit.text) == texts.CHECKLIST_COMPLETED
    assert edit.reply_markup is None, "a closed checklist has nothing left to press"
    assert not sends(bot), "the manager hears nothing, and a handler never sends anyway"


async def test_finishing_with_lines_left_asks_before_it_sends_anything() -> None:
    bot = make_bot()
    checklists = FakeChecklists(small_view())
    checklists.completion = CompletionOutcome.NEEDS_REASON
    payload = ChecklistFinish(run_id=RUN_ID)

    await handlers.finish(
        make_callback(payload.pack(), bot=bot),
        bot=bot,
        **context(checklists, payload=payload, subject=make_run()),
    )

    (edit,) = edits(bot)
    assert captions(markup_of(edit)) == [
        [texts.CHECKLIST_SEND_AS_IS_BUTTON, texts.CHECKLIST_KEEP_GOING_BUTTON]
    ]
    assert checklists.completions == [
        {"run_id": RUN_ID, "completed_by": STAFF_USER_ID, "skip_comment": None}
    ]


async def test_a_second_press_of_done_completes_the_run_once() -> None:
    bot = make_bot()
    checklists = FakeChecklists(small_view())
    checklists.completion = CompletionOutcome.ALREADY_FINISHED
    payload = ChecklistFinish(run_id=RUN_ID)

    await handlers.finish(
        make_callback(payload.pack(), bot=bot),
        bot=bot,
        **context(checklists, payload=payload, subject=make_run()),
    )

    assert not edits(bot) and not sends(bot)
    assert [answer.text for answer in session_of(bot).answers()] == [
        texts.CHECKLIST_ALREADY_FINISHED
    ]


async def test_send_as_is_asks_for_the_reason_and_opens_the_one_step_it_needs() -> None:
    bot = make_bot()
    checklists = FakeChecklists(small_view())
    state = make_state(bot)
    payload = ChecklistSkipAccept(run_id=RUN_ID)

    await handlers.accept_skip(
        make_callback(payload.pack(), bot=bot),
        bot=bot,
        state=state,
        **context(checklists, payload=payload, subject=make_run()),
    )

    (edit,) = edits(bot)
    assert str(edit.text) == texts.CHECKLIST_SKIP_COMMENT_PROMPT
    assert captions(markup_of(edit)) == [[texts.CANCEL_BUTTON]], "TZ 8.2: every step cancels"
    assert await state.get_state() == ChecklistSkip.comment.state
    stored = await state.get_data()
    assert stored[handlers.RUN_ID_KEY] == RUN_ID
    assert not checklists.completions, "nothing is written before the reason is given"


async def test_the_reason_finishes_the_run_and_the_service_is_what_tells_the_manager() -> None:
    """TZ 5.4 and plan task 24: the notification is a row in `notifications`, not a
    `bot.send_message` from a handler."""
    bot = make_bot()
    checklists = FakeChecklists(small_view())
    checklists.completion = CompletionOutcome.SKIPPED
    state = make_state(bot)
    await state.set_state(ChecklistSkip.comment)
    await state.update_data({handlers.RUN_ID_KEY: RUN_ID})

    await handlers.skip_comment(
        make_message("no ice left", bot=bot),
        bot=bot,
        state=state,
        **context(checklists, payload=None),
    )

    assert checklists.completions == [
        {"run_id": RUN_ID, "completed_by": STAFF_USER_ID, "skip_comment": "no ice left"}
    ]
    (edit,) = edits(bot)
    assert (edit.chat_id, edit.message_id) == (CHAT_ID, RUN_MESSAGE_ID)
    assert str(edit.text) == texts.CHECKLIST_SKIP_SENT
    assert not sends(bot), "the manager is told by the service, and by nobody else"
    assert await state.get_state() is None


async def test_a_reason_of_spaces_is_not_a_reason_and_the_step_stays_open() -> None:
    bot = make_bot()
    checklists = FakeChecklists(small_view())
    checklists.completion = CompletionOutcome.NEEDS_REASON
    state = make_state(bot)
    await state.set_state(ChecklistSkip.comment)
    await state.update_data({handlers.RUN_ID_KEY: RUN_ID})

    await handlers.skip_comment(
        make_message("   ", bot=bot),
        bot=bot,
        state=state,
        **context(checklists, payload=None),
    )

    assert [str(call.text) for call in sends(bot)] == [texts.CHECKLIST_SKIP_COMMENT_PROMPT]
    assert await state.get_state() == ChecklistSkip.comment.state


async def test_a_photo_where_a_reason_was_asked_for_is_asked_again() -> None:
    bot = make_bot()
    checklists = FakeChecklists(small_view())
    state = make_state(bot)
    await state.set_state(ChecklistSkip.comment)
    await state.update_data({handlers.RUN_ID_KEY: RUN_ID})
    message = make_message("x", bot=bot).model_copy(update={"text": None}).as_(bot)

    await handlers.skip_comment(
        message,
        bot=bot,
        state=state,
        **context(checklists, payload=None),
    )

    assert not checklists.completions
    assert [str(call.text) for call in sends(bot)] == [texts.CHECKLIST_SKIP_COMMENT_PROMPT]


# --------------------------------------------------------------------------------------
# The router (plan, task 24)
# --------------------------------------------------------------------------------------


def test_every_screen_of_this_block_is_registered() -> None:
    instance = handlers.router()
    assert instance.name == handlers.ROUTER_NAME
    assert {handler.callback for handler in instance.callback_query.handlers} == {
        handlers.show,
        handlers.switch_group,
        handlers.toggle,
        handlers.finish,
        handlers.accept_skip,
    }
    assert {handler.callback for handler in instance.message.handlers} == {handlers.skip_comment}


async def test_the_press_is_answered_even_though_the_screen_was_edited() -> None:
    """TZ 9: an unanswered callback leaves a spinner, and a spinner reads as a broken bot."""
    bot = make_bot()
    checklists = FakeChecklists(small_view())
    payload = ChecklistToggle(run_id=RUN_ID, item_id=101)

    await handlers.toggle(
        make_callback(payload.pack(), bot=bot),
        bot=bot,
        **context(checklists, payload=payload),
    )

    assert [answer.text for answer in session_of(bot).answers()] == [None]
