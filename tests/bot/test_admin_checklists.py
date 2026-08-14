"""«✅ Чек-листы»: the checklist template editor (TZ 5.8; plan task 28; decisions B3, B6, D2).

Three kinds of test, and the split is the one `tests/bot/test_admin_staff.py` uses.

* **The screens are checked as values.** `src/bot/views/editor.py` is a pure function of the
  view the service returned, so the empty state of TZ 8.1 is an ordinary assert — including
  the property that matters most about it, that every button on it goes somewhere real
  (`test_no_button_of_the_empty_checklist_leads_nowhere`).
* **The flows are exercised through a real `Dispatcher` with the real callback resolver on
  it**, because every payload of this block travels through the resolver in production: a
  button whose factory has no rule, or whose id belongs to the bar next door, is refused
  there and nowhere else (TZ 9, acceptance 11.3).
* **The service is the real `TemplateService`**, over the fake repositories of
  `tests/services/test_templates.py`. This is deliberate and is the opposite choice from the
  staff block, where a fake service is enough: what these screens *report on* is
  copy-on-write (decision B3) and the bulk parser (decision B6), and a fake service would
  make «Сохранил как версию 2» a sentence this file asserts about itself. The protocols the
  handler module declares are still checked, by mypy, in
  :func:`_the_real_service_satisfies_the_protocols`.

The three properties this file exists to hold on to:

1. an empty checklist — the state every venue is delivered in (decision B1) — renders, and
   **both** of the ways out of it work: one line at a time, and forty at once;
2. forty lines in eight groups arrive in one message and cost **one** template version, not
   forty (decision B6 read together with B3);
3. an edit made while a run is open says, in words, that a new version was created, and the
   version the run reads is left alone (decision B3, TZ 4.3).
"""

from __future__ import annotations

from typing import Any, Final

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import InlineKeyboardMarkup, Update
from src.bot import texts
from src.bot.callbacks import (
    AdminSection,
    EditorAction,
    EditorCommand,
    EditorGroup,
    EditorLine,
    EditorLineCritical,
    EditorLineDelete,
    EditorLinePhoto,
    OpenAdmin,
    parse,
)
from src.bot.handlers import admin_checklists
from src.bot.handlers.admin_checklists import EDITED_TYPE, EditorServices, Templates
from src.bot.keyboards.editor import command
from src.bot.middlewares.auth import ACTOR_KEY
from src.bot.middlewares.resolver import RULES, CallbackResolverMiddleware, Refusal, resolve
from src.bot.middlewares.services import SERVICES_KEY, VenueServices
from src.bot.states import TemplateEditor
from src.bot.views import Screen
from src.bot.views import editor as views
from src.db.models import ChecklistItem, ChecklistTemplate
from src.services.templates import TemplateService, TemplateView

from tests.bot.test_middlewares import (
    CHAT_ID,
    MANAGER,
    STAFF,
    STAFF_TELEGRAM_ID,
    VENUE_ID,
    make_bot,
    make_callback,
    make_message,
    session_of,
)
from tests.services.test_templates import (
    OTHER_VENUE_ID,
    FakeAudit,
    FakeItems,
    FakeTemplates,
    build,
    make_item,
    make_template,
)

#: The template every venue is delivered with (decision B1): it exists and holds no lines.
TEMPLATE_ID: Final = 1

#: Eight groups and about forty lines is the checklist TZ 5.4 describes and decision B6 is
#: about. The wording is latin on purpose: what a bar checks at opening is its own data and
#: never travels in this repository (CLAUDE.md).
BULK_GROUPS: Final = 8
BULK_LINES_PER_GROUP: Final = 5
BULK_MESSAGE: Final = "\n".join(
    line
    for group in range(1, BULK_GROUPS + 1)
    for line in (
        f"# station {group}",
        *(f"line {group}-{item}" for item in range(1, BULK_LINES_PER_GROUP + 1)),
    )
)


# --------------------------------------------------------------------------------------
# The stand: the real resolver, the real service, fake repositories
# --------------------------------------------------------------------------------------


class Nothing:
    """A `RowLookup` over a table this block never addresses."""

    async def get(self, row_id: int, /) -> Any:
        return None


class Subjects:
    """`SubjectRepositories` over the very tables the service writes into.

    Live and not a snapshot: a fork (decision B3) inserts a template and copies every line
    into it *during* the update, and the resolver has to see them on the next press. A copy
    of the two tables taken when the stand was built would answer for the version before the
    fork, and the test that matters most here would pass for the wrong reason.
    """

    def __init__(self, repos: FakeTemplates) -> None:
        self.templates = repos
        self.items = repos.items
        self.runs = Nothing()
        self.recipes = Nothing()
        self.members = Nothing()
        self.shifts = Nothing()


class FakeServices:
    """`data["services"]` as this block and the resolver read it."""

    def __init__(self, repos: FakeTemplates) -> None:
        self.audit = FakeAudit()
        self.repositories = Subjects(repos)
        self.templates: TemplateService = build(repos, self.audit)


def _the_real_service_satisfies_the_protocols(
    services: VenueServices,
) -> tuple[EditorServices, Templates]:
    """mypy is the assertion here, and it is the point of the protocols in the handler.

    Nothing calls this function: it fails at type-check time or not at all. The stand above
    builds a real `TemplateService`, so the two halves together say that what the handlers
    ask for is what a venue's services actually offer.
    """
    return services, services.templates


class Stand:
    """A dispatcher holding this block's router, with the resolver in front of it."""

    def __init__(self, repos: FakeTemplates, *, actor: Any = MANAGER) -> None:
        self.bot: Bot = make_bot()
        storage = MemoryStorage()
        self.dispatcher = Dispatcher(storage=storage)
        self.dispatcher.callback_query.outer_middleware(CallbackResolverMiddleware())
        self.dispatcher.include_router(admin_checklists.router())
        self.repos = repos
        self.services = FakeServices(repos)
        self.context: dict[str, Any] = {ACTOR_KEY: actor, SERVICES_KEY: self.services}
        self.state = FSMContext(
            storage=storage,
            key=StorageKey(bot_id=self.bot.id, chat_id=CHAT_ID, user_id=STAFF_TELEGRAM_ID),
        )

    async def types(self, text: str) -> None:
        await self.dispatcher.feed_update(
            self.bot,
            Update(update_id=1, message=make_message(text)),
            **self.context,
        )

    async def presses(self, payload: str) -> None:
        await self.dispatcher.feed_update(
            self.bot,
            Update(update_id=1, callback_query=make_callback(payload)),
            **self.context,
        )

    async def opens(self) -> None:
        await self.presses(OpenAdmin(section=AdminSection.CHECKLISTS).pack())

    # -- reading the bot back -----------------------------------------------------------

    def alerts(self) -> list[str]:
        return [
            str(call.text)
            for call in session_of(self.bot).calls
            if isinstance(call, AnswerCallbackQuery) and call.text
        ]

    def _last_screen(self) -> SendMessage | EditMessageText:
        shown = [
            call
            for call in session_of(self.bot).calls
            if isinstance(call, SendMessage | EditMessageText)
        ]
        assert shown, "nothing was shown at all"
        return shown[-1]

    def screen(self) -> str:
        return str(self._last_screen().text)

    def keyboard(self) -> InlineKeyboardMarkup:
        markup = self._last_screen().reply_markup
        assert isinstance(markup, InlineKeyboardMarkup)
        return markup

    # -- reading the tables back --------------------------------------------------------

    def lines(self, template_id: int = TEMPLATE_ID) -> list[ChecklistItem]:
        return sorted(
            (item for item in self.repos.items.items if item.template_id == template_id),
            key=lambda item: (item.group_index, item.order_index, item.id),
        )

    def versions(self) -> list[ChecklistTemplate]:
        return sorted(self.repos.templates.values(), key=lambda template: template.version)


# --------------------------------------------------------------------------------------
# Fixtures and small helpers
# --------------------------------------------------------------------------------------


def stand_with(
    *items: ChecklistItem,
    templates: tuple[ChecklistTemplate, ...] = (),
    runs: list[tuple[int, int]] | None = None,
) -> Stand:
    """A venue with this template and these lines, and optionally a run pointing at it."""
    rows = templates if templates else (make_template(TEMPLATE_ID),)
    repos = FakeTemplates(FakeItems(items), rows, runs=runs)
    return Stand(repos)


def captions(markup: InlineKeyboardMarkup | None) -> list[str]:
    assert markup is not None
    return [button.text for row in markup.inline_keyboard for button in row]


def payloads(markup: InlineKeyboardMarkup | None) -> list[str]:
    assert markup is not None
    return [button.callback_data or "" for row in markup.inline_keyboard for button in row]


async def screen_of(*items: ChecklistItem, group_index: int | None = None) -> Screen:
    """The screen a view of these lines draws, without a bot anywhere near it."""
    return views.template_screen(await view_of(*items), group_index=group_index)


async def view_of(*items: ChecklistItem) -> TemplateView:
    repos = FakeTemplates(FakeItems(items), (make_template(TEMPLATE_ID),))
    service = build(repos)
    return await service.view(MANAGER, EDITED_TYPE)


# --------------------------------------------------------------------------------------
# The empty state (TZ 8.1) — the first thing every venue sees in this section
# --------------------------------------------------------------------------------------


async def test_the_empty_checklist_says_so_and_offers_both_ways_to_fill_it() -> None:
    """TZ 8.1 and decision B1: the template exists, holds nothing, and says what to do.

    Both roads are on the screen at once: the buttons that add one line at a time, and the
    syntax of decision B6 for the manager who has the whole checklist in front of them.
    """
    screen = await screen_of()
    assert texts.EDITOR_EMPTY in screen.text
    assert texts.EDITOR_BULK_PROMPT in screen.text
    assert (
        texts.EDITOR_TITLE_TEMPLATE.format(checklist=texts.checklist_title(EDITED_TYPE), total=0)
        in screen.text
    )
    assert texts.EDITOR_ADD_BUTTON in captions(screen.markup)
    assert texts.EDITOR_ADD_TO_NEW_GROUP_BUTTON in captions(screen.markup)


async def test_no_button_of_the_empty_checklist_leads_nowhere() -> None:
    """TZ 8.1: «ни одной кнопки в никуда» — every payload parses and has a resolver rule."""
    for payload in payloads((await screen_of()).markup):
        assert type(parse(payload)) in RULES, payload


async def test_the_empty_checklist_offers_nothing_that_needs_a_line() -> None:
    """A group can only be renamed while it exists (decision D2), so the button is absent
    rather than drawn onto a checklist that has no groups."""
    drawn = captions((await screen_of()).markup)
    assert texts.EDITOR_RENAME_GROUP_BUTTON not in drawn
    assert texts.EDITOR_MOVE_BUTTON not in drawn


async def test_the_add_button_of_the_empty_checklist_actually_writes_a_line() -> None:
    """The empty state is only done if its buttons work — pressed, and not read."""
    stand = stand_with()
    await stand.opens()
    pressed = _button(stand.keyboard(), texts.EDITOR_ADD_BUTTON)

    await stand.presses(pressed)
    assert stand.screen() == texts.EDITOR_TEXT_PROMPT
    assert await stand.state.get_state() == TemplateEditor.item_text.state

    await stand.types("wipe the bar")
    assert [item.text for item in stand.lines()] == ["wipe the bar"]
    assert "wipe the bar" in stand.screen()


async def test_the_new_group_button_of_the_empty_checklist_opens_a_group() -> None:
    """Decision D2: a group is created together with its first line, so the wizard is two
    steps and neither of them writes on its own."""
    stand = stand_with()
    await stand.opens()

    await stand.presses(_button(stand.keyboard(), texts.EDITOR_ADD_TO_NEW_GROUP_BUTTON))
    assert stand.screen() == texts.EDITOR_GROUP_PROMPT
    await stand.types("bar station")
    assert stand.screen() == texts.EDITOR_TEXT_PROMPT
    assert stand.lines() == [], "a group with no lines is not a group and is not written"

    await stand.types("check the ice")
    written = stand.lines()
    assert [(item.group_name, item.text) for item in written] == [("bar station", "check the ice")]
    assert "bar station" in stand.screen()


async def test_the_empty_checklist_leaves_a_typed_message_meaning_the_whole_list() -> None:
    """Decision B6 without a button: `EDITOR_EMPTY` promises the list can just be sent.

    The state is set with **empty** data on purpose — `src/bot/middlewares/menu.py` asks
    "interrupt?" only about a scenario holding something unsaved, and looking at a checklist
    is not one (TZ 5.2).
    """
    stand = stand_with()
    await stand.opens()
    assert await stand.state.get_state() == TemplateEditor.bulk.state
    assert await stand.state.get_data() == {}


# --------------------------------------------------------------------------------------
# Decision B6: eight groups and forty lines in one message
# --------------------------------------------------------------------------------------


async def test_forty_lines_in_eight_groups_arrive_in_one_message() -> None:
    """TZ 5.4's volume, TZ 7's complaint about entering it by hand, decision B6's answer."""
    stand = stand_with()
    await stand.opens()
    await stand.types(BULK_MESSAGE)

    written = stand.lines()
    assert len(written) == BULK_GROUPS * BULK_LINES_PER_GROUP == 40
    assert len({item.group_index for item in written}) == BULK_GROUPS
    assert [item.group_name for item in written[:BULK_LINES_PER_GROUP]] == ["station 1"] * 5
    assert texts.EDITOR_BULK_RESULT_TEMPLATE.format(added=40, groups=8) in stand.screen()


async def test_one_pasted_checklist_costs_one_version_and_not_forty() -> None:
    """Decision B3 read together with B6: `bulk_add` opens the template once.

    Forty calls to «Добавить» against a template a run points at would leave forty versions
    behind and a version list nobody can read — which is the whole reason the service has a
    bulk entry point at all.
    """
    stand = stand_with(runs=[(VENUE_ID, TEMPLATE_ID)])
    await stand.opens()
    await stand.types(BULK_MESSAGE)
    assert [template.version for template in stand.versions()] == [1, 2]


async def test_a_message_the_parser_makes_nothing_of_writes_nothing() -> None:
    """An empty message is not an empty checklist: no line, no template version, no fork."""
    stand = stand_with()
    await stand.opens()
    await stand.types("   \n\n  ")
    assert stand.lines() == []
    assert stand.screen() == texts.EDITOR_BULK_PROMPT


# --------------------------------------------------------------------------------------
# Decision B3: an edit while a run is open
# --------------------------------------------------------------------------------------


async def test_an_edit_while_a_run_is_open_says_a_new_version_was_made() -> None:
    """TZ 5.8 promises the manager is told, and TZ 4.3 that last month's report does not move.

    Both halves are asserted: the sentence on the screen, and the fact that the line the run
    reads is still hanging off the version it was created from.
    """
    stand = stand_with(
        make_item(1, TEMPLATE_ID, text="check the taps"),
        runs=[(VENUE_ID, TEMPLATE_ID)],
    )
    await stand.opens()
    await stand.presses(command(TEMPLATE_ID, EditorAction.ADD, 0))
    await stand.types("check the glasses")

    assert texts.EDITOR_NEW_VERSION_TEMPLATE.format(version=2) in stand.screen()
    forked = stand.versions()
    assert [(template.version, template.is_active) for template in forked] == [
        (1, False),
        (2, True),
    ]
    assert [item.text for item in stand.lines(TEMPLATE_ID)] == ["check the taps"], (
        "the version the run reads keeps exactly the lines it was created with (TZ 4.3)"
    )
    assert [item.text for item in stand.lines(forked[-1].id)] == [
        "check the taps",
        "check the glasses",
    ]


async def test_an_edit_with_no_run_behind_it_says_nothing_about_versions() -> None:
    """The other half of decision B3: until a run points at the template, edits are in place
    and there is no version to announce."""
    stand = stand_with(make_item(1, TEMPLATE_ID, text="check the taps"))
    await stand.opens()
    await stand.presses(command(TEMPLATE_ID, EditorAction.ADD, 0))
    await stand.types("check the glasses")

    assert texts.EDITOR_NEW_VERSION_TEMPLATE.format(version=2) not in stand.screen()
    assert [template.version for template in stand.versions()] == [1]


# --------------------------------------------------------------------------------------
# Groups (decision D2: a group exists exactly while it has lines)
# --------------------------------------------------------------------------------------


async def test_deleting_the_last_line_of_a_group_takes_the_group_off_the_screen() -> None:
    """Decision D2 is a screen property before it is a data one: no empty heading is left."""
    stand = stand_with(
        make_item(1, TEMPLATE_ID, text="wipe the bar", group_index=0, group_name="bar"),
        make_item(2, TEMPLATE_ID, text="water the plants", group_index=1, group_name="hall"),
    )
    await stand.opens()
    assert "hall" in stand.screen()

    await stand.presses(EditorLineDelete(item_id=2).pack())
    assert "hall" not in stand.screen()
    assert "bar" in stand.screen()
    assert [item.text for item in stand.lines()] == ["wipe the bar"]


async def test_the_add_button_writes_into_the_group_the_screen_is_open_on() -> None:
    """«Добавить» means "one more line here", so the group the markers are on is the target.

    Pressed off the screen and not built by the test: what the button carries is the whole
    of how the handler learns which group was meant.
    """
    stand = stand_with(
        make_item(1, TEMPLATE_ID, text="wipe the bar", group_index=0, group_name="bar"),
        make_item(2, TEMPLATE_ID, text="water the plants", group_index=1, group_name="hall"),
    )
    await stand.opens()
    await stand.presses(EditorGroup(template_id=TEMPLATE_ID, group_index=0).pack())
    await stand.presses(_button(stand.keyboard(), texts.EDITOR_ADD_BUTTON))
    await stand.types("check the ice")

    assert [(item.group_name, item.text) for item in stand.lines()] == [
        ("bar", "wipe the bar"),
        ("bar", "check the ice"),
        ("hall", "water the plants"),
    ]


async def test_renaming_a_group_renames_every_line_in_it() -> None:
    """A group is not a row (decision D2), so its name is a column of each of its lines."""
    stand = stand_with(
        make_item(1, TEMPLATE_ID, text="wipe the bar", group_name="bar", order_index=0),
        make_item(2, TEMPLATE_ID, text="check the ice", group_name="bar", order_index=1),
    )
    await stand.opens()
    await stand.presses(_button(stand.keyboard(), texts.EDITOR_RENAME_GROUP_BUTTON))
    assert stand.screen() == texts.EDITOR_GROUP_PROMPT

    await stand.types("bar station")
    assert {item.group_name for item in stand.lines()} == {"bar station"}
    assert "bar station" in stand.screen()


async def test_a_group_button_moves_the_markers_without_leaving_the_message() -> None:
    """Decision D11: the text is the whole checklist, the buttons are one group of it."""
    stand = stand_with(
        make_item(1, TEMPLATE_ID, text="wipe the bar", group_index=0, group_name="bar"),
        make_item(2, TEMPLATE_ID, text="water the plants", group_index=1, group_name="hall"),
    )
    await stand.opens()
    # The last group is where a manager filling a checklist in is working.
    assert EditorLine(item_id=2).pack() in payloads(stand.keyboard())

    await stand.presses(EditorGroup(template_id=TEMPLATE_ID, group_index=0).pack())
    assert EditorLine(item_id=1).pack() in payloads(stand.keyboard())
    assert EditorLine(item_id=2).pack() not in payloads(stand.keyboard())
    assert "wipe the bar" in stand.screen(), "the text still holds the whole checklist"
    assert "water the plants" in stand.screen()


# --------------------------------------------------------------------------------------
# One line: the card (TZ 5.4)
# --------------------------------------------------------------------------------------


async def test_the_card_shows_the_line_and_what_can_be_done_to_it() -> None:
    stand = stand_with(make_item(1, TEMPLATE_ID, text="wipe the bar"))
    await stand.opens()
    await stand.presses(EditorLine(item_id=1).pack())

    assert "wipe the bar" in stand.screen()
    drawn = captions(stand.keyboard())
    assert texts.EDITOR_CRITICAL_BUTTON in drawn
    assert texts.EDITOR_PHOTO_BUTTON in drawn
    assert texts.EDITOR_DELETE_BUTTON in drawn


async def test_marking_a_line_critical_marks_it_on_the_screen() -> None:
    """TZ 5.4: `is_critical` decides the escalation, so it is visible where it is set."""
    stand = stand_with(make_item(1, TEMPLATE_ID, text="wipe the bar"))
    await stand.opens()
    await stand.presses(EditorLineCritical(item_id=1, is_set=True).pack())

    assert stand.lines()[0].is_critical is True
    assert texts.CHECKLIST_CRITICAL_MARK in stand.screen()
    # The button now asks for the opposite state, so two managers cannot undo each other.
    assert EditorLineCritical(item_id=1, is_set=False).pack() in payloads(stand.keyboard())


async def test_requiring_a_photo_is_readable_on_the_card() -> None:
    stand = stand_with(make_item(1, TEMPLATE_ID, text="wipe the bar"))
    await stand.opens()
    await stand.presses(EditorLinePhoto(item_id=1, is_set=True).pack())

    assert stand.lines()[0].requires_photo is True
    assert texts.EDITOR_PHOTO_BUTTON in stand.screen()


async def test_rewording_a_line_replaces_it_instead_of_adding_one() -> None:
    stand = stand_with(make_item(1, TEMPLATE_ID, text="wipe the bar"))
    await stand.opens()
    await stand.presses(command(TEMPLATE_ID, EditorAction.REWORD, 1))
    assert stand.screen() == texts.EDITOR_TEXT_PROMPT

    await stand.types("wipe the bar and the shelf")
    assert [item.text for item in stand.lines()] == ["wipe the bar and the shelf"]


async def test_the_order_button_lifts_a_line_and_is_not_drawn_on_the_first() -> None:
    """`EDITOR_MOVE_BUTTON` moves a line up inside its group and never out of it.

    On the first line of a group it could only redraw the same screen, so it is absent —
    TZ 8.1 forbids a button that does nothing as loudly as one that leads nowhere.
    """
    stand = stand_with(
        make_item(1, TEMPLATE_ID, text="first", order_index=0),
        make_item(2, TEMPLATE_ID, text="second", order_index=1),
    )
    await stand.opens()
    await stand.presses(EditorLine(item_id=1).pack())
    assert texts.EDITOR_MOVE_BUTTON not in captions(stand.keyboard())

    await stand.presses(EditorLine(item_id=2).pack())
    assert texts.EDITOR_MOVE_BUTTON in captions(stand.keyboard())
    await stand.presses(_button(stand.keyboard(), texts.EDITOR_MOVE_BUTTON))
    assert [item.text for item in stand.lines()] == ["second", "first"]


# --------------------------------------------------------------------------------------
# Rights and stale screens (TZ 2, TZ 9, acceptance 11.3)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        OpenAdmin(section=AdminSection.CHECKLISTS),
        EditorGroup(template_id=TEMPLATE_ID, group_index=0),
        EditorLine(item_id=1),
        EditorLineCritical(item_id=1, is_set=True),
        EditorLinePhoto(item_id=1, is_set=True),
        EditorLineDelete(item_id=1),
        EditorCommand(action=EditorAction.ADD, template_id=TEMPLATE_ID, target=0),
    ],
    ids=lambda payload: type(payload).__name__,
)
async def test_staff_is_refused_every_payload_of_this_section(payload: Any) -> None:
    """TZ 2: the editor is the manager's. The refusal happens in the resolver, before any
    handler of this module is reached, which is why it is asserted there."""
    repos = FakeTemplates(
        FakeItems((make_item(1, TEMPLATE_ID, text="wipe the bar"),)),
        (make_template(TEMPLATE_ID),),
    )
    resolution = await resolve(payload.pack(), actor=STAFF, repositories=Subjects(repos))
    assert resolution.refusal is Refusal.FORBIDDEN
    assert resolution.payload is None


async def test_staff_pressing_an_editor_button_never_reaches_a_handler() -> None:
    """The same rule as above, pressed rather than resolved: `staff` taps «Добавить» on a
    screen they kept from a promotion that has since been undone.

    This is the half no handler of this module checks any more, so it is asserted end to end
    through the dispatcher the resolver is registered on: the bartender is told no, no step
    is opened, and the message they type next is therefore not a line of anybody's checklist.
    """
    stand = Stand(FakeTemplates(FakeItems(()), (make_template(TEMPLATE_ID),)), actor=STAFF)

    await stand.presses(command(TEMPLATE_ID, EditorAction.ADD, 0))

    assert stand.alerts() == [texts.ERROR_NOT_ALLOWED]
    assert await stand.state.get_state() is None, "no step was opened by a refused press"
    await stand.types("wipe the bar")
    assert stand.lines() == []


async def test_a_line_of_another_venue_is_not_reachable_by_id() -> None:
    """Acceptance 11.3: `checklist_items` has no venue of its own (decision D9), so a forged
    id is refused through the join to the template and not by a check in this module."""
    repos = FakeTemplates(
        FakeItems((make_item(9, 2, text="theirs"),)),
        (make_template(TEMPLATE_ID), make_template(2, venue_id=OTHER_VENUE_ID)),
    )
    resolution = await resolve(
        EditorLine(item_id=9).pack(), actor=MANAGER, repositories=Subjects(repos)
    )
    assert resolution.refusal is Refusal.MISSING


async def test_a_command_naming_another_venues_template_is_refused() -> None:
    """Acceptance 11.3, the check the encoding used to carry and the factory now carries.

    `template_id` is the venue anchor of every command: the resolver fetches it through this
    venue's repository, where the bar next door's template is not "rejected" but invisible.
    Both halves are asserted — the resolution, and the press, which must open no step and
    leave both venues' lines where they were.
    """
    theirs = 2
    repos = FakeTemplates(
        FakeItems((make_item(9, theirs, text="theirs"),)),
        (make_template(TEMPLATE_ID), make_template(theirs, venue_id=OTHER_VENUE_ID)),
    )
    forged = command(theirs, EditorAction.ADD, 0)

    resolution = await resolve(forged, actor=MANAGER, repositories=Subjects(repos))
    assert resolution.refusal is Refusal.MISSING
    assert resolution.payload is None

    stand = Stand(repos)
    await stand.opens()
    await stand.presses(forged)
    assert stand.alerts()[-1] == texts.ERROR_NOT_ALLOWED
    assert await stand.state.get_state() == TemplateEditor.bulk.state, "no step was opened"
    assert [item.text for item in stand.lines(theirs)] == ["theirs"]
    assert stand.lines() == []


async def test_a_press_on_a_line_that_is_already_gone_is_answered() -> None:
    """TZ 9: an action answers in a second, even when the answer is «the screen is old»."""
    stand = stand_with(make_item(1, TEMPLATE_ID, text="wipe the bar"))
    await stand.opens()
    await stand.presses(EditorLineDelete(item_id=1).pack())
    await stand.presses(EditorLine(item_id=1).pack())
    assert stand.alerts()[-1] == texts.ERROR_NOT_ALLOWED


async def test_a_blank_line_is_asked_for_again_and_nothing_is_written() -> None:
    stand = stand_with()
    await stand.opens()
    await stand.presses(command(TEMPLATE_ID, EditorAction.ADD, 0))
    await stand.types("    ")
    assert stand.screen() == texts.EDITOR_TEXT_PROMPT
    assert stand.lines() == []
    assert await stand.state.get_state() == TemplateEditor.item_text.state


# --------------------------------------------------------------------------------------
# The commands of the editor (`EditorCommand`, `src/bot/keyboards/editor.py`)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", list(EditorAction), ids=lambda action: action.name)
async def test_every_editor_action_is_answered_by_a_handler(action: EditorAction) -> None:
    """One registration per action, asserted against the enum and not against a list.

    The router filters on `action`, so an action added to `src/bot/callbacks.py` without a
    registration here is a button that spins and does nothing (TZ 9) — and a member of an
    enum is exactly the kind of thing a per-handler test cannot notice missing. Whatever the
    press means, something has to come back: a screen, or an alert saying the screen is old.
    """
    stand = stand_with(make_item(1, TEMPLATE_ID, text="wipe the bar"))
    await stand.opens()
    before = len(session_of(stand.bot).calls)

    await stand.presses(command(TEMPLATE_ID, action, 1))

    assert len(session_of(stand.bot).calls) > before, f"nothing answers {action.name}"


def test_a_command_button_still_fits_the_callback_budget() -> None:
    """Decision D14: the widest thing this block can pack is a command against a full-width
    `BIGINT` line id, and Telegram truncates anything over 64 bytes into another payload."""
    packed = command(2**63 - 1, EditorAction.MOVE_UP, 2**63 - 1)
    assert len(packed.encode()) <= 64


def test_a_command_carries_the_action_and_never_a_group_index() -> None:
    """`EditorGroup` is what its name says again: the commands left that field for good.

    The regression this holds on to is the one the arithmetic made possible — a press that
    parsed as an ordinary «open group N» because its number happened to be read that way.
    """
    packed = parse(command(TEMPLATE_ID, EditorAction.REWORD, 41))
    assert isinstance(packed, EditorCommand)
    assert (packed.action, packed.template_id, packed.target) == (
        EditorAction.REWORD,
        TEMPLATE_ID,
        41,
    )
    group = parse(EditorGroup(template_id=TEMPLATE_ID, group_index=1).pack())
    assert isinstance(group, EditorGroup)


# --------------------------------------------------------------------------------------
# The router
# --------------------------------------------------------------------------------------


def test_the_router_is_named_and_registers_the_screens() -> None:
    instance = admin_checklists.router()
    assert instance.name == "admin_checklists"
    # `OpenAdmin`, `EditorGroup`, one per `EditorAction`, and the four `EditorLine*`.
    assert len(instance.callback_query.handlers) == 2 + len(EditorAction) + 4
    assert len(instance.message.handlers) == 4


def _button(markup: InlineKeyboardMarkup, caption: str) -> str:
    """The payload behind a caption, so a test presses what the screen actually drew."""
    found = next(
        (
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.text == caption and button.callback_data
        ),
        None,
    )
    assert found is not None, f"no button captioned {caption!r} on this screen"
    return found
