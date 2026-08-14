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

**Every press that is not a line and not a group is an
:class:`~src.bot.callbacks.EditorCommand`** — "add a line to group 3", "rename group 3",
"reword line 41", "move line 41 up", "open the whole checklist". The factory says what it
does (:class:`~src.bot.callbacks.EditorAction`) and what it does it to (`target`: a group
index for a group action, a `checklist_items` id for a line action, `0` where the action
names neither), and it carries `template_id` as its venue anchor, so the resolver fetches
the template through *this* venue's repository and refuses another venue's before a handler
runs (TZ 9). Nothing here decides which of the two `target` is: the action does, and the
handler that filters on the action reads it accordingly.

:class:`~src.bot.callbacks.EditorGroup` is left meaning exactly what it is named: open the
group with this **non-negative** `group_index`. It used to carry the commands too, packed
into the negative half of that field by arithmetic this module owned; the encoding worked
and even kept the venue check, and was still the wrong shape — a field named `group_index`
that sometimes meant "command five applied to line 91" is a field every reader has to be
warned about, and `EditorGroup` is drawn by more than one screen.

**Back leads to the board of the management section, and it is spelled as `OpenSection`.**
Not `Nav(BACK, section=MANAGEMENT)`, which `src/bot/keyboards/admin.py::block` draws:
`src/bot/middlewares/menu.py` clears a half-open scenario on an `OpenSection` press and
knows nothing about `Nav`, and this section deliberately keeps the FSM in
`TemplateEditor.bulk` while its screen is open (decision B6 — a message typed here is a list
of lines). Leaving through a payload the menu middleware does not watch would carry that
state into the next section, where the manager's next line would be read as checklist items.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot import texts
from src.bot.callbacks import (
    EditorAction,
    EditorCommand,
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


def command(template_id: int, action: EditorAction, target: int = 0) -> str:
    """The payload of one command button of this block (see the module docstring)."""
    return EditorCommand(action=action, template_id=template_id, target=target).pack()


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
    action: EditorAction,
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
        callback_data=command(template_id, EditorAction.TEMPLATE),
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
            EditorAction.ADD,
            0 if current is None else current.index,
        ),
        command_button(texts.EDITOR_ADD_TO_NEW_GROUP_BUTTON, template_id, EditorAction.NEW_GROUP),
    ]
    rows.append(additions)
    if current is not None:
        rows.append(
            [
                command_button(
                    texts.EDITOR_RENAME_GROUP_BUTTON,
                    template_id,
                    EditorAction.RENAME_GROUP,
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
                EditorAction.REWORD,
                item.item_id,
            )
        ],
    ]
    if movable:
        rows[-1].append(
            command_button(
                texts.EDITOR_MOVE_BUTTON, template_id, EditorAction.MOVE_UP, item.item_id
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
    "back_to_checklist",
    "back_to_management",
    "command",
    "command_button",
    "critical_button",
    "delete_button",
    "group_button",
    "item_keyboard",
    "line_button",
    "photo_button",
    "step_keyboard",
    "template_keyboard",
]
