"""The checklist template editor (TZ 5.8; plan task 28; decisions B3, B6, D2, D11).

Owner: plan task 28. Four screens and four typed steps, all of them over one service —
`src/services/templates.py`, which owns the copy-on-write of decision B3, the bulk syntax of
decision B6 and the rule that a group exists exactly while it has lines (decision D2). This
module is the shape TZ 3.2 asks for and nothing more: take the update, call the service,
render the screen.

**The section is one checklist, and that is said by drawing one.** TZ 4.3 and decision D1
know three types; stage 0 sends exactly one of them to anybody — the opening checklist
(decision B9: the other five pushes of TZ 6 move to stage 1) — so :data:`EDITED_TYPE` is
what this section edits. The closing template exists in the database from minute one
(decision B1) and has no screen here on purpose: a button leading to a checklist that is
never delivered is the caption with nothing behind it TZ 8.1 forbids, and `src/bot/texts/`
has no wording that would explain the difference honestly. When stage 1 starts sending the
closing checklist, this becomes a list of two and the constant becomes the payload of the
button — see the report of this task, which asks `src/bot/callbacks.py` for a factory that
can carry a checklist type.

**The empty state is the state every venue is delivered in** (TZ 8.1, decision B1). The
screen is the same screen with zero groups, it says `EDITOR_EMPTY`, and both roads out of it
work: `EDITOR_ADD_BUTTON` and `EDITOR_ADD_TO_NEW_GROUP_BUTTON` add one line at a time, and a
message typed straight into the chat is read as the whole checklist at once.

**Why a typed message is a list of lines.** Decision B6 is the reason this module keeps the
FSM in :attr:`~src.bot.states.TemplateEditor.bulk` for as long as one of its screens is on
display: eight groups and forty lines through a step-by-step wizard is an hour on a phone,
and TZ 7 says so itself. The state is set **with empty data**, which is not an accident —
`src/bot/middlewares/menu.py` asks "interrupt?" only when the scenario holds something
unsaved, so a manager who presses a main-menu button while merely *looking* at the checklist
is not asked to confirm anything (TZ 5.2). The steps that really do hold something unsaved —
a group name waiting for its first line — fill that data and are asked about, correctly.

For the same reason the back button of this block is an `OpenSection`, not
`Nav(BACK, section=MANAGEMENT)`: the menu middleware clears the scenario on the first and
does not watch the second, and a `bulk` state carried out of this section would turn the
manager's next line, typed on somebody else's screen, into checklist items.

**Commands ride in `EditorGroup.group_index`** — see
:class:`~src.bot.keyboards.editor.EditorCommand` for the whole of that stopgap and why
declaring a factory here is not an option. This module only decodes what a press means; the
encoding is the keyboard's.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Final, Protocol

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message

from src.bot import texts
from src.bot.callbacks import (
    AdminSection,
    EditorGroup,
    EditorLine,
    EditorLineCritical,
    EditorLineDelete,
    EditorLinePhoto,
    OpenAdmin,
)
from src.bot.handlers.admin import BOT_KEY, deliver, manager_of
from src.bot.keyboards.editor import EditorCommand, decoded
from src.bot.middlewares.menu import STATE_KEY
from src.bot.middlewares.resolver import PAYLOAD_KEY
from src.bot.middlewares.services import SERVICES_KEY
from src.bot.states import TemplateEditor
from src.bot.views import Screen
from src.bot.views import editor as screens
from src.db.models import ChecklistType
from src.services.access import AccessContext
from src.services.templates import (
    BulkResult,
    EditResult,
    MoveDirection,
    TemplateError,
    TemplateView,
)

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "admin_checklists"

#: The one checklist stage 0 both edits and delivers (see the module docstring).
EDITED_TYPE: Final = ChecklistType.OPENING

#: What a typed step remembers between the question and the answer. Ids are here rather than
#: in the button only where the *answer* is a message: the press that opened the step
#: carried them (`src/bot/callbacks.py`: ids travel in buttons, text lives in the state),
#: and the message that answers it carries nothing but the text.
TEMPLATE_FIELD: Final = "template_id"
GROUP_FIELD: Final = "group_index"
ITEM_FIELD: Final = "item_id"
NAME_FIELD: Final = "group_name"

Event = Message | CallbackQuery


class Templates(Protocol):
    """What this block asks of `TemplateService` (TZ 5.8), and no more.

    Narrower than the service on purpose, the way `admin_staff.Roster` is: the screens are
    the thing under test, and a test that had to build the real service would have to build
    a session to build a repository to build it. `tests/bot/test_admin_checklists.py` checks
    with mypy that the real `TemplateService` still satisfies this — and then exercises the
    real one anyway, because copy-on-write is the behaviour the screens report on.
    """

    async def view(self, actor: AccessContext, checklist_type: ChecklistType) -> TemplateView: ...

    async def add_item(
        self,
        actor: AccessContext,
        *,
        checklist_type: ChecklistType,
        text: str,
        group_name: str | None,
        is_critical: bool = False,
        requires_photo: bool = False,
    ) -> EditResult: ...

    async def add_to_new_group(
        self,
        actor: AccessContext,
        *,
        checklist_type: ChecklistType,
        group_name: str,
        text: str,
    ) -> EditResult: ...

    async def edit_text(self, actor: AccessContext, item_id: int, text: str) -> EditResult: ...

    async def set_critical(
        self, actor: AccessContext, item_id: int, is_set: bool
    ) -> EditResult: ...

    async def set_photo(self, actor: AccessContext, item_id: int, is_set: bool) -> EditResult: ...

    async def delete_item(self, actor: AccessContext, item_id: int) -> EditResult: ...

    async def move_item(
        self, actor: AccessContext, item_id: int, direction: MoveDirection
    ) -> EditResult: ...

    async def rename_group(
        self,
        actor: AccessContext,
        *,
        template_id: int,
        group_index: int,
        name: str,
    ) -> EditResult: ...

    async def bulk_add(
        self, actor: AccessContext, *, checklist_type: ChecklistType, text: str
    ) -> BulkResult: ...


class EditorServices(Protocol):
    """The one service of `data["services"]` these screens use."""

    @property
    def templates(self) -> Templates: ...


# --------------------------------------------------------------------------------------
# The checklist (TZ 5.8)
# --------------------------------------------------------------------------------------


async def open_checklists(event: Event, **data: Any) -> None:
    """`OpenAdmin(CHECKLISTS)`: the checklist of this venue, empty state included."""
    actor = manager_of(data)
    if actor is None:
        await _refuse(event)
        return
    await _show(event, data, await _templates(data).view(actor, EDITED_TYPE))


async def open_group(event: CallbackQuery, **data: Any) -> None:
    """A group of the switcher: the same screen with the markers moved onto it (D11)."""
    actor = manager_of(data)
    if actor is None:
        await _refuse(event)
        return
    payload: EditorGroup = data[PAYLOAD_KEY]
    view = await _templates(data).view(actor, EDITED_TYPE)
    await _show(event, data, view, group_index=payload.group_index)


async def run_command(event: CallbackQuery, **data: Any) -> None:
    """Everything a press can mean that is neither a line nor a group.

    One handler and a `match` rather than six registrations, because the six share one
    field: `src/bot/keyboards/editor.py` packs the command *and* its target into a single
    number, and a magic filter cannot take that apart (it can only compare it). Routing is
    still routing — nothing below decides anything a service would.
    """
    actor = manager_of(data)
    if actor is None:
        await _refuse(event)
        return
    payload: EditorGroup = data[PAYLOAD_KEY]
    decoding = decoded(payload.group_index)
    if decoding is None:
        # Registered behind `F.group_index < 0`, so this cannot happen from the router; it
        # keeps the function total for a direct call and for whatever stage 1 registers.
        await open_group(event, **data)
        return

    action, target = decoding
    service = _templates(data)
    view = await service.view(actor, EDITED_TYPE)
    state: FSMContext = data[STATE_KEY]

    match action:
        case EditorCommand.TEMPLATE:
            await _show(event, data, view)
        case EditorCommand.ADD:
            await _ask(
                event,
                data,
                screens.text_prompt(view),
                step=TemplateEditor.item_text,
                remember={TEMPLATE_FIELD: payload.template_id, GROUP_FIELD: target},
            )
        case EditorCommand.NEW_GROUP:
            await _ask(
                event,
                data,
                screens.group_prompt(view),
                step=TemplateEditor.group_name,
                remember={TEMPLATE_FIELD: payload.template_id},
            )
        case EditorCommand.RENAME:
            await _ask(
                event,
                data,
                screens.group_prompt(view),
                step=TemplateEditor.rename_group,
                remember={TEMPLATE_FIELD: payload.template_id, GROUP_FIELD: target},
            )
        case EditorCommand.REWORD:
            await _ask(
                event,
                data,
                screens.text_prompt(view),
                step=TemplateEditor.item_text,
                remember={TEMPLATE_FIELD: payload.template_id, ITEM_FIELD: target},
            )
        case EditorCommand.MOVE_UP:
            result = await _edit(event, state, service.move_item(actor, target, MoveDirection.UP))
            if result is not None:
                await _show_line(event, data, result)


# --------------------------------------------------------------------------------------
# One line (TZ 5.4: the two flags a line can carry, its wording, its place)
# --------------------------------------------------------------------------------------


async def show_line(event: CallbackQuery, **data: Any) -> None:
    """`EditorLine`: the card of the line whose marker was pressed."""
    actor = manager_of(data)
    if actor is None:
        await _refuse(event)
        return
    payload: EditorLine = data[PAYLOAD_KEY]
    view = await _templates(data).view(actor, EDITED_TYPE)
    item = screens.item_of(view, payload.item_id)
    if item is None:
        # A line of a superseded version, or one somebody deleted from another screen.
        await _stale(event)
        return
    await _draw(event, data, screens.item_screen(view, item))


async def set_critical(event: CallbackQuery, **data: Any) -> None:
    """TZ 5.4: a critical line escalates to the manager the moment it is skipped."""
    actor = manager_of(data)
    if actor is None:
        await _refuse(event)
        return
    payload: EditorLineCritical = data[PAYLOAD_KEY]
    state: FSMContext = data[STATE_KEY]
    result = await _edit(
        event, state, _templates(data).set_critical(actor, payload.item_id, payload.is_set)
    )
    if result is not None:
        await _show_line(event, data, result, item_id=payload.item_id)


async def set_photo(event: CallbackQuery, **data: Any) -> None:
    """TZ 5.4: a line with `requires_photo` is not ticked until the photo arrives."""
    actor = manager_of(data)
    if actor is None:
        await _refuse(event)
        return
    payload: EditorLinePhoto = data[PAYLOAD_KEY]
    state: FSMContext = data[STATE_KEY]
    result = await _edit(
        event, state, _templates(data).set_photo(actor, payload.item_id, payload.is_set)
    )
    if result is not None:
        await _show_line(event, data, result, item_id=payload.item_id)


async def delete_line(event: CallbackQuery, **data: Any) -> None:
    """Remove a line; with its last line the group goes too (decision D2).

    The group is read *before* the deletion so that the screen can come back to it — and,
    when it was the last line of that group, fall back to the end of the checklist, which is
    what makes the group's disappearance visible instead of leaving an empty heading.
    """
    actor = manager_of(data)
    if actor is None:
        await _refuse(event)
        return
    payload: EditorLineDelete = data[PAYLOAD_KEY]
    service = _templates(data)
    state: FSMContext = data[STATE_KEY]
    before = screens.item_of(await service.view(actor, EDITED_TYPE), payload.item_id)
    result = await _edit(event, state, service.delete_item(actor, payload.item_id))
    if result is not None:
        await _show(
            event,
            data,
            result.view,
            group_index=None if before is None else before.group_index,
            notice=screens.new_version_notice(result.view.version if result.forked else None),
        )


# --------------------------------------------------------------------------------------
# The typed steps (TZ 8.2)
# --------------------------------------------------------------------------------------


async def take_item_text(event: Message, **data: Any) -> None:
    """One line, typed. Which of the three things it is, is what the step remembered.

    A blank message asks again instead of being saved: the service refuses it anyway
    (`BlankTextError`), and asking again is the answer TZ 8.2 wants — an error message for
    somebody who pressed send too early is a message about nothing.
    """
    actor = manager_of(data)
    if actor is None:
        await _refuse(event)
        return
    typed = (event.text or "").strip()
    service = _templates(data)
    state: FSMContext = data[STATE_KEY]
    view = await service.view(actor, EDITED_TYPE)
    if not typed:
        await _draw(event, data, screens.text_prompt(view))
        return

    remembered = await state.get_data()
    item_id = remembered.get(ITEM_FIELD)
    group_name = remembered.get(NAME_FIELD)
    if isinstance(item_id, int):
        edit = service.edit_text(actor, item_id, typed)
    elif isinstance(group_name, str):
        # Decision D2: a group is created together with its first line, which is this one.
        edit = service.add_to_new_group(
            actor, checklist_type=EDITED_TYPE, group_name=group_name, text=typed
        )
    else:
        edit = service.add_item(
            actor,
            checklist_type=EDITED_TYPE,
            text=typed,
            group_name=_target_group_name(view, remembered.get(GROUP_FIELD)),
        )

    result = await _edit(event, state, edit)
    if result is not None:
        await _show_result(event, data, result)


async def take_new_group(event: Message, **data: Any) -> None:
    """The name of a new group. Nothing is written yet — a group needs a line (D2)."""
    actor = manager_of(data)
    if actor is None:
        await _refuse(event)
        return
    typed = (event.text or "").strip()
    state: FSMContext = data[STATE_KEY]
    view = await _templates(data).view(actor, EDITED_TYPE)
    if not typed:
        await _draw(event, data, screens.group_prompt(view))
        return
    await state.update_data({NAME_FIELD: typed})
    await state.set_state(TemplateEditor.item_text)
    await _draw(event, data, screens.text_prompt(view))


async def take_group_name(event: Message, **data: Any) -> None:
    """The new name of a group: an update of every line in it (decision D2)."""
    actor = manager_of(data)
    if actor is None:
        await _refuse(event)
        return
    typed = (event.text or "").strip()
    service = _templates(data)
    state: FSMContext = data[STATE_KEY]
    view = await service.view(actor, EDITED_TYPE)
    if not typed:
        await _draw(event, data, screens.group_prompt(view))
        return

    remembered = await state.get_data()
    template_id = remembered.get(TEMPLATE_FIELD)
    group_index = remembered.get(GROUP_FIELD)
    if not isinstance(template_id, int) or not isinstance(group_index, int):
        # The step outlived what opened it (a storage TTL, a deploy). Nothing to rename.
        await state.clear()
        await _stale(event)
        return

    result = await _edit(
        event,
        state,
        service.rename_group(actor, template_id=template_id, group_index=group_index, name=typed),
    )
    if result is not None:
        await _show(
            event,
            data,
            result.view,
            group_index=group_index,
            notice=screens.new_version_notice(result.view.version if result.forked else None),
        )


async def take_bulk(event: Message, **data: Any) -> None:
    """Decision B6: the whole checklist in one message — eight groups, forty lines, one edit.

    One call and therefore one version (decision B3): `bulk_add` opens the template once, so
    a pasted checklist costs a single fork and not forty. A message the parser makes nothing
    of writes nothing at all — no line, no template, no version — and the prompt is shown
    again with the syntax on it.
    """
    actor = manager_of(data)
    if actor is None:
        await _refuse(event)
        return
    service = _templates(data)
    try:
        result = await service.bulk_add(actor, checklist_type=EDITED_TYPE, text=event.text or "")
    except TemplateError:
        await _stale(event)
        return

    if not result.changed:
        await _draw(event, data, screens.bulk_prompt(result.view))
        return
    await _show(
        event,
        data,
        result.view,
        notice=screens.bulk_notice(
            added=result.added_items,
            groups=result.added_groups,
            version=result.view.version if result.forked else None,
        ),
    )


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------


def _templates(data: dict[str, Any]) -> Templates:
    services: EditorServices = data[SERVICES_KEY]
    return services.templates


def _target_group_name(view: TemplateView, group_index: object) -> str | None:
    """The group `EDITOR_ADD_BUTTON` was pressed on, as `add_item` addresses groups.

    By name, because that is what the service takes and what the manager sees — which is
    also this screen's one blunt edge: two groups may legitimately share a name (decision B6
    says so in as many words), and then a line added to the second lands in the first. The
    index is what the button carries and what the service would need; see the report.
    """
    if not isinstance(group_index, int):
        return None
    group = screens.current_group(view, group_index)
    return None if group is None else group.name


async def _edit(
    event: Event,
    state: FSMContext,
    edit: Awaitable[EditResult],
) -> EditResult | None:
    """Await one service call, answering a refusal with a screen instead of a traceback.

    Every refusal of `src/services/templates.py` is a :class:`TemplateError`: an id from
    another venue, a line that has just been deleted, an edit addressed to a version that
    has been superseded (decision B3 makes old versions read-only). All three read the same
    to the manager — the screen is out of date, open the section again (TZ 9) — and the
    half-filled step, whose ids no longer name anything, goes with it.
    """
    try:
        result = await edit
    except TemplateError:
        await state.clear()
        await _stale(event)
        return None
    return result


async def _show(
    event: Event,
    data: dict[str, Any],
    view: TemplateView,
    *,
    group_index: int | None = None,
    notice: str | None = None,
) -> None:
    """Draw the checklist and leave the FSM where decision B6 wants it.

    `set_data({})` and not `update_data`: whatever a step remembered is answered by now, and
    an empty scenario is what keeps a main-menu press from asking "interrupt?" about a screen
    nobody was typing into (`src/bot/middlewares/menu.py`, TZ 5.2).
    """
    state: FSMContext = data[STATE_KEY]
    await state.set_state(TemplateEditor.bulk)
    await state.set_data({})
    await _draw(event, data, screens.template_screen(view, group_index=group_index, notice=notice))


async def _show_result(event: Event, data: dict[str, Any], result: EditResult) -> None:
    """The checklist after an edit, opened on the group the edited line ended up in."""
    item = None if result.item_id is None else screens.item_of(result.view, result.item_id)
    await _show(
        event,
        data,
        result.view,
        group_index=None if item is None else item.group_index,
        notice=screens.new_version_notice(result.view.version if result.forked else None),
    )


async def _show_line(
    event: Event,
    data: dict[str, Any],
    result: EditResult,
    *,
    item_id: int | None = None,
) -> None:
    """The card again after acting on it — with the line's new id if the version forked."""
    target = result.item_id if result.item_id is not None else item_id
    item = None if target is None else screens.item_of(result.view, target)
    if item is None:
        await _show(event, data, result.view)
        return
    state: FSMContext = data[STATE_KEY]
    await state.set_state(TemplateEditor.bulk)
    await state.set_data({})
    await _draw(
        event,
        data,
        screens.item_screen(
            result.view,
            item,
            notice=screens.new_version_notice(result.view.version if result.forked else None),
        ),
    )


async def _ask(
    event: Event,
    data: dict[str, Any],
    screen: Screen,
    *,
    step: State,
    remember: dict[str, Any],
) -> None:
    """Open one typed step: the question, the state it is answered in, and what it needs."""
    state: FSMContext = data[STATE_KEY]
    await state.set_state(step)
    await state.set_data(remember)
    await _draw(event, data, screen)


async def _draw(event: Event, data: dict[str, Any], screen: Screen) -> None:
    await deliver(event, screen, bot=data[BOT_KEY])


async def _refuse(event: Event) -> None:
    """TZ 9: one wording, whichever half of the check failed."""
    if isinstance(event, CallbackQuery):
        await event.answer(texts.ERROR_NOT_ALLOWED, show_alert=True)
        return
    await event.answer(texts.ERROR_NOT_ALLOWED)


async def _stale(event: Event) -> None:
    """TZ 9: an action answers within a second, even when the answer is "this is old"."""
    if isinstance(event, CallbackQuery):
        await event.answer(texts.ERROR_OUTDATED_SCREEN, show_alert=True)
        return
    await event.answer(texts.ERROR_OUTDATED_SCREEN)


def router() -> Router:
    """The screens of this block (see the module docstring).

    The two `EditorGroup` registrations cannot overlap: a group index is what the venue's own
    `group_index` column holds and is never negative (decision D2), and every command is
    encoded into the negative half (`src/bot/keyboards/editor.py`). The order is here so that
    reading the file is reading the routing.

    The message steps are filtered by state, so nothing typed outside a step is read as an
    answer — and a main-menu press never reaches them at all, because
    `src/bot/middlewares/menu.py` intercepts it before any router is consulted.
    """
    instance = Router(name=ROUTER_NAME)
    instance.callback_query.register(
        open_checklists, OpenAdmin.filter(F.section == AdminSection.CHECKLISTS)
    )
    instance.callback_query.register(open_group, EditorGroup.filter(F.group_index >= 0))
    instance.callback_query.register(run_command, EditorGroup.filter(F.group_index < 0))
    instance.callback_query.register(show_line, EditorLine.filter())
    instance.callback_query.register(set_critical, EditorLineCritical.filter())
    instance.callback_query.register(set_photo, EditorLinePhoto.filter())
    instance.callback_query.register(delete_line, EditorLineDelete.filter())

    instance.message.register(take_item_text, StateFilter(TemplateEditor.item_text))
    instance.message.register(take_new_group, StateFilter(TemplateEditor.group_name))
    instance.message.register(take_group_name, StateFilter(TemplateEditor.rename_group))
    instance.message.register(take_bulk, StateFilter(TemplateEditor.bulk))
    return instance


__all__ = [
    "EDITED_TYPE",
    "GROUP_FIELD",
    "ITEM_FIELD",
    "NAME_FIELD",
    "ROUTER_NAME",
    "TEMPLATE_FIELD",
    "EditorServices",
    "Templates",
    "delete_line",
    "open_checklists",
    "open_group",
    "router",
    "run_command",
    "set_critical",
    "set_photo",
    "show_line",
    "take_bulk",
    "take_group_name",
    "take_item_text",
    "take_new_group",
]
