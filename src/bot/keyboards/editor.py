"""Buttons of the checklist template editor (TZ 5.8; decisions B3, B6, D2, D11).

Captions come from `src/bot/texts/admin.py`, payloads from `src/bot/callbacks.py`. Two
things are decided here and both are worth reading before editing this file.

**The screen is the run screen of decision D11, turned around.** A real checklist is eight
groups and about forty lines (TZ 5.4), and forty buttons captioned with the venue's own
wording fit neither the twenty characters of TZ 8.2 nor a phone. So the editor draws the
whole checklist as *text* and puts markers under one group at a time: `1`, `2`, `3` are the
lines of the current group, numbered in the message by `src/bot/views/editor.py`, and the
group switcher is what changes which group that is. A manager who taps `2` gets the card of
the line written `2.` — the same contract `src/bot/keyboards/checklist.py` gives the
bartender, which is why the two constants and :func:`~src.bot.keyboards.checklist.fit` are
imported from there rather than repeated.

**Every press that is not a line and not a group rides in the negative half of
`EditorGroup.group_index`** (:class:`EditorCommand`). This is the same stopgap
:class:`~src.bot.keyboards.admin.VenueCommand` documents for the settings screen, and it is
here for the same reason: the editor needs "add a line to group 3", "rename group 3",
"reword line 41", "move line 41 up" and "open the whole checklist", `src/bot/callbacks.py`
declares no factory for any of them, and a factory declared outside that file registers
itself into the scheme without a rule in the resolver — which fails the build
(`tests/bot/test_middlewares.py::test_every_callback_factory_has_a_rule`).

The encoding is arithmetic rather than a table of magic numbers, because three of those five
presses carry a target and a table cannot: a group index is small, a line's id is a `BIGINT`.
One number holds both — ``-1 - command - COMMAND_COUNT * target`` — and :func:`decoded`
takes it apart again. It is total in both directions: every negative index decodes to a
command and a target, so a stale or hand-made payload lands on a screen rather than on an
exception, and a non-negative one is a group index and is never mistaken for a command. The
day `callbacks.py` gains an `EditorAction(template_id, action, target)`, this class and
:func:`decoded` go away and only the registrations in
`src/bot/handlers/admin_checklists.py` change.

**Back leads to the board of the management section, and it is spelled as `OpenSection`.**
Not `Nav(BACK, section=MANAGEMENT)`, which `src/bot/keyboards/admin.py::block` draws:
`src/bot/middlewares/menu.py` clears a half-open scenario on an `OpenSection` press and
knows nothing about `Nav`, and this section deliberately keeps the FSM in
`TemplateEditor.bulk` while its screen is open (decision B6 — a message typed here is a list
of lines). Leaving through a payload the menu middleware does not watch would carry that
state into the next section, where the manager's next line would be read as checklist items.
"""

from __future__ import annotations

import enum
from collections.abc import Iterator, Sequence
from typing import Final

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot import texts
from src.bot.callbacks import (
    EditorGroup,
    EditorLine,
    EditorLineCritical,
    EditorLineDelete,
    EditorLinePhoto,
    MenuAction,
    OpenSection,
)
from src.bot.keyboards.checklist import GROUP_COLUMNS, MARKER_COLUMNS, fit
from src.bot.keyboards.menu import submenu, wizard
from src.services.templates import TemplateGroupView, TemplateItemView, TemplateView


class EditorCommand(enum.IntEnum):
    """A press of this block that names no group of the checklist (see the module docstring).

    The value is the command's slot in the encoding and nothing else; it never reaches a
    screen and never reaches the database. Members may be appended, and reordering them
    changes what a button drawn a minute ago means — the same append-only rule
    :func:`~src.services.venues.wizard_timezones` carries, and for the same reason.
    """

    #: The whole checklist, with no group singled out — the way back from a line's card.
    TEMPLATE = 0
    #: `EDITOR_ADD_BUTTON`: one more line at the end of the group in `target`.
    ADD = 1
    #: `EDITOR_ADD_TO_NEW_GROUP_BUTTON`: a new group, created with its first line (D2).
    NEW_GROUP = 2
    #: `EDITOR_RENAME_GROUP_BUTTON` on the checklist: rename the group in `target`.
    RENAME = 3
    #: `EDITOR_RENAME_GROUP_BUTTON` on a card: reword the line in `target`.
    REWORD = 4
    #: `EDITOR_MOVE_BUTTON`: the line in `target` swaps with the one above it.
    MOVE_UP = 5


#: The stride of the encoding. Read off the enum so that appending a command cannot leave
#: it behind — a stride shorter than the number of commands would make two of them one.
COMMAND_COUNT: Final = len(EditorCommand)


def encoded(action: EditorCommand, target: int = 0) -> int:
    """The `group_index` a command travels in. Always negative, so never a group (D2)."""
    return -1 - int(action) - COMMAND_COUNT * target


def decoded(group_index: int) -> tuple[EditorCommand, int] | None:
    """The command and its target, or `None` when this index is an ordinary group.

    Total on purpose: every negative number decodes, so a payload from a screen drawn by an
    older version of this file answers with a screen and not with a traceback (TZ 9).
    """
    if group_index >= 0:
        return None
    position = -group_index - 1
    return EditorCommand(position % COMMAND_COUNT), position // COMMAND_COUNT


def command(template_id: int, action: EditorCommand, target: int = 0) -> str:
    """The payload of one command button of this block."""
    return EditorGroup(template_id=template_id, group_index=encoded(action, target)).pack()


# --------------------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------------------


def line_button(number: int, item: TemplateItemView) -> InlineKeyboardButton:
    """One line's marker: the caption is its number, the payload is its id (D11, D14)."""
    return InlineKeyboardButton(
        text=texts.CHECKLIST_MARK_BUTTON_TEMPLATE.format(number=number),
        callback_data=EditorLine(item_id=item.item_id).pack(),
    )


def group_button(template_id: int, number: int, group: TemplateGroupView) -> InlineKeyboardButton:
    """A group of the switcher: `2. <name>`, cut to the twenty characters of TZ 8.2.

    `number` is the group's position in the message — the same number its heading carries in
    the text — while the payload carries `group.index`, the venue's own `group_index`
    column, which has holes in it after a group was emptied (decision D2).
    """
    caption = (
        texts.CHECKLIST_GROUP_BUTTON_TEMPLATE.format(index=number, group_name=group.name)
        if group.name
        else texts.CHECKLIST_GROUP_UNNAMED_TEMPLATE.format(index=number)
    )
    return InlineKeyboardButton(
        text=fit(caption),
        callback_data=EditorGroup(template_id=template_id, group_index=group.index).pack(),
    )


def command_button(
    caption: str,
    template_id: int,
    action: EditorCommand,
    target: int = 0,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=caption, callback_data=command(template_id, action, target))


def critical_button(item: TemplateItemView) -> InlineKeyboardButton:
    """TZ 5.4: a critical line escalates the moment it is skipped.

    The payload carries the state the press asks for and not the state the line is in, so
    two managers on the same card do not undo each other (the rule
    `src/bot/keyboards/staff.py::active_button` states).
    """
    return InlineKeyboardButton(
        text=texts.EDITOR_CRITICAL_BUTTON,
        callback_data=EditorLineCritical(item_id=item.item_id, is_set=not item.is_critical).pack(),
    )


def photo_button(item: TemplateItemView) -> InlineKeyboardButton:
    """TZ 5.4: a line with `requires_photo` is not ticked until a photo arrives."""
    return InlineKeyboardButton(
        text=texts.EDITOR_PHOTO_BUTTON,
        callback_data=EditorLinePhoto(item_id=item.item_id, is_set=not item.requires_photo).pack(),
    )


def delete_button(item: TemplateItemView) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=texts.EDITOR_DELETE_BUTTON,
        callback_data=EditorLineDelete(item_id=item.item_id).pack(),
    )


def back_to_checklist(template_id: int) -> InlineKeyboardButton:
    """The way back from a card or from a step: the checklist itself, not the board."""
    return InlineKeyboardButton(
        text=texts.BACK_BUTTON,
        callback_data=command(template_id, EditorCommand.TEMPLATE),
    )


def back_to_management() -> InlineKeyboardButton:
    """Back to the board of the management section (see the module docstring)."""
    return InlineKeyboardButton(
        text=texts.BACK_BUTTON,
        callback_data=OpenSection(section=MenuAction.MANAGEMENT).pack(),
    )


# --------------------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------------------


def template_keyboard(
    view: TemplateView,
    current: TemplateGroupView | None,
) -> InlineKeyboardMarkup:
    """The checklist: markers of `current`, the group switcher, and what can be added.

    The empty state keeps every button the full screen has except the ones that need a line
    or a group to act on (TZ 8.1: an empty screen carries the button that ends it, and not
    one that leads nowhere). A venue whose template row does not exist yet has no id to
    address at all, so it gets the navigation alone — and the road decision B6 opens: the
    screen leaves the FSM in `TemplateEditor.bulk`, so the list can simply be typed.
    """
    template_id = view.template_id
    if template_id is None:
        return submenu([back_to_management()], back=False)

    rows: list[list[InlineKeyboardButton]] = []
    if current is not None:
        rows.extend(_marker_rows(current))
    if len(view.groups) > 1:
        # One group is its own switcher: a button that redraws the screen it was pressed on.
        rows.extend(_group_rows(view, template_id))
    additions = [
        command_button(
            texts.EDITOR_ADD_BUTTON,
            template_id,
            EditorCommand.ADD,
            0 if current is None else current.index,
        ),
        command_button(texts.EDITOR_ADD_TO_NEW_GROUP_BUTTON, template_id, EditorCommand.NEW_GROUP),
    ]
    rows.append(additions)
    if current is not None:
        rows.append(
            [
                command_button(
                    texts.EDITOR_RENAME_GROUP_BUTTON,
                    template_id,
                    EditorCommand.RENAME,
                    current.index,
                )
            ]
        )
    rows.append([back_to_management()])
    return submenu(*rows, back=False)


def item_keyboard(
    item: TemplateItemView,
    *,
    template_id: int,
    movable: bool,
) -> InlineKeyboardMarkup:
    """One line's card: the two flags of TZ 5.4, its wording, its place, and removing it.

    `movable` is false for the first line of a group, and then `EDITOR_MOVE_BUTTON` is not
    drawn at all: a line never leaves its group (`TemplateService.move_item`), so the button
    on the first line could only redraw the same screen — the button that does nothing TZ 8.1
    forbids. Moving is upwards only because there is one caption for it; any order can still
    be reached, by lifting the line that should be higher.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [critical_button(item), photo_button(item)],
        [
            command_button(
                texts.EDITOR_RENAME_GROUP_BUTTON,
                template_id,
                EditorCommand.REWORD,
                item.item_id,
            )
        ],
    ]
    if movable:
        rows[-1].append(
            command_button(
                texts.EDITOR_MOVE_BUTTON, template_id, EditorCommand.MOVE_UP, item.item_id
            )
        )
    rows.append([delete_button(item)])
    rows.append([back_to_checklist(template_id)])
    return submenu(*rows, back=False)


def step_keyboard(template_id: int | None) -> InlineKeyboardMarkup:
    """One step of a step-by-step scenario: back to the checklist, and cancel (TZ 8.2)."""
    rows = [] if template_id is None else [[back_to_checklist(template_id)]]
    return wizard(*rows, back=False)


# --------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------


def _marker_rows(group: TemplateGroupView) -> Iterator[list[InlineKeyboardButton]]:
    buttons = [line_button(number, item) for number, item in enumerate(group.items, start=1)]
    yield from _chunks(buttons, MARKER_COLUMNS)


def _group_rows(view: TemplateView, template_id: int) -> Iterator[list[InlineKeyboardButton]]:
    buttons = [
        group_button(template_id, number, group)
        for number, group in enumerate(view.groups, start=1)
    ]
    yield from _chunks(buttons, GROUP_COLUMNS)


def _chunks[T](items: Sequence[T], size: int) -> Iterator[list[T]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


__all__ = [
    "COMMAND_COUNT",
    "EditorCommand",
    "back_to_checklist",
    "back_to_management",
    "command",
    "command_button",
    "critical_button",
    "decoded",
    "delete_button",
    "encoded",
    "group_button",
    "item_keyboard",
    "line_button",
    "photo_button",
    "step_keyboard",
    "template_keyboard",
]
