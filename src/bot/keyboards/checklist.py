"""The buttons under the checklist message (TZ 5.4, decision D11).

**Why the buttons are numbers.** A venue's own wording — a whole phrase per line — is
what the *text* of the message carries; a button carries the line's number and nothing
else. Decision D11 spells out the arithmetic: eight groups and forty lines is the volume
TZ 5.4 asks for, the caption limit of TZ 8.2 is twenty characters, and forty inline buttons
captioned with the venue's phrases fit neither that limit nor a phone screen. A number
always fits, however long the bar writes its lines.

Three rows make the screen, in this order:

1. **the markers of the current group** — `1`, `2`, `✓3`. Only the current group's, because
   forty markers at once is the same wall of buttons in a shorter font. Which lines they
   point at is not left to guesswork: `src/bot/views/checklist.py` numbers exactly those
   lines in the message text, so `2` and the line marked `2.` are the same thing;
2. **the group switcher**, drawn only when there is more than one group — a group's own
   number and name, prefixed with `✓` once every line in it is ticked, so progress is
   readable without switching to look. It is a switcher and not a pager: a pager needs a
   previous and a next caption, and captions live in `src/bot/texts/`;
3. **the finish button**, which TZ 5.4 puts at the bottom of the checklist message.

**No navigation row, on purpose.** TZ 5.2 puts a back and a home button into every
*submenu*; this is not one. The checklist arrives on its own before the shift (TZ 1.4#2) —
there is no screen behind it to go back to — and the main menu is a *reply* keyboard,
permanent at the bottom of the chat, so the employee is never trapped here. `submenu()` is
used where a screen really is a submenu; the comment prompt of the skip scenario is a
wizard step and does take its cancel button from `keyboards.menu.wizard` below.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Final

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot import texts
from src.bot.callbacks import (
    ChecklistFinish,
    ChecklistGroup,
    ChecklistShow,
    ChecklistSkipAccept,
    ChecklistToggle,
)
from src.bot.keyboards.menu import wizard
from src.services.checklists import GroupView, ItemView, RunView

#: Markers are one or two characters wide, so a phone fits five of them across.
MARKER_COLUMNS: Final = 5

#: A group button carries the venue's own name for the group, so two per row and not five.
GROUP_COLUMNS: Final = 2

#: TZ 8.2 caps a caption at twenty characters. The group name comes from the venue and nothing
#: bounds it there, so the caption is cut here rather than left to wrap on the phone. The
#: number and the `✓` survive the cut because they are at the front — they are what the
#: button is *for*; the name is the reminder.
CAPTION_LIMIT: Final = 20

#: Marks a caption that was cut. Punctuation, not wording: there is nothing to translate.
ELLIPSIS: Final = "..."


def run_keyboard(view: RunView, *, current: GroupView) -> InlineKeyboardMarkup:
    """Markers of `current`, the group switcher, and the finish button (decision D11)."""
    rows: list[list[InlineKeyboardButton]] = list(_marker_rows(view.run_id, current))
    if len(view.groups) > 1:
        # One group is its own switcher: a single button that redraws the same screen.
        rows.extend(_group_rows(view))
    rows.append([_finish_button(view.run_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def skip_question_keyboard(run_id: int) -> InlineKeyboardMarkup:
    """ "Send it as it is?" — the two answers TZ 5.4 gives the employee.

    "Carry on" is a :class:`ChecklistShow` and not a navigation button: it puts the
    checklist back on the screen with the lines that are still open, which is the whole
    point of offering the choice. Saying no here must cost less than saying yes.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.CHECKLIST_SEND_AS_IS_BUTTON,
                    callback_data=ChecklistSkipAccept(run_id=run_id).pack(),
                ),
                InlineKeyboardButton(
                    text=texts.CHECKLIST_KEEP_GOING_BUTTON,
                    callback_data=ChecklistShow(run_id=run_id).pack(),
                ),
            ]
        ]
    )


def skip_prompt_keyboard() -> InlineKeyboardMarkup:
    """The one step of `states.ChecklistSkip`, so TZ 8.2 requires its cancel button.

    `back=False`: the step before it is the question above, whose "carry on" answer already
    leads back to the checklist. A second button to the same place is a second way to be
    wrong about which one does what.
    """
    return wizard(back=False)


# --------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------


def _marker_rows(run_id: int, group: GroupView) -> Iterator[list[InlineKeyboardButton]]:
    buttons = [
        _marker_button(run_id, number, item) for number, item in enumerate(group.items, start=1)
    ]
    yield from _chunks(buttons, MARKER_COLUMNS)


def _group_rows(view: RunView) -> Iterator[list[InlineKeyboardButton]]:
    buttons = [
        _group_button(view.run_id, number, group)
        for number, group in enumerate(view.groups, start=1)
    ]
    yield from _chunks(buttons, GROUP_COLUMNS)


def _chunks[T](items: Sequence[T], size: int) -> Iterator[list[T]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


# --------------------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------------------


def _marker_button(run_id: int, number: int, item: ItemView) -> InlineKeyboardButton:
    """One line's marker. `number` is its position inside the group, not its id.

    The id travels in the payload, where the 64-byte budget of decision D14 has already
    made room for it; the caption is what the employee reads against the numbered line in
    the text above.
    """
    template = (
        texts.CHECKLIST_MARK_DONE_BUTTON_TEMPLATE
        if item.is_done
        else texts.CHECKLIST_MARK_BUTTON_TEMPLATE
    )
    return InlineKeyboardButton(
        text=template.format(number=number),
        callback_data=ChecklistToggle(run_id=run_id, item_id=item.item_id).pack(),
    )


def _group_button(run_id: int, number: int, group: GroupView) -> InlineKeyboardButton:
    """A group of the switcher: `2. <name>`, or `✓2. <name>` once it is fully ticked.

    `number` is the group's position in the message — the same number its heading carries
    up in the text — while the payload carries `group.index`, which is the `group_index`
    column of TZ 5.4 and belongs to the venue's own numbering (decision D2). Printing the
    stored index instead would number the first heading `0.` in a template whose
    editor counts from zero.
    """
    index = (
        texts.CHECKLIST_MARK_DONE_BUTTON_TEMPLATE.format(number=number)
        if group.is_complete
        else texts.CHECKLIST_MARK_BUTTON_TEMPLATE.format(number=number)
    )
    caption = (
        texts.CHECKLIST_GROUP_BUTTON_TEMPLATE.format(index=index, group_name=group.name)
        if group.name
        else texts.CHECKLIST_GROUP_UNNAMED_TEMPLATE.format(index=index)
    )
    return InlineKeyboardButton(
        text=fit(caption),
        callback_data=ChecklistGroup(run_id=run_id, group_index=group.index).pack(),
    )


def _finish_button(run_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=texts.CHECKLIST_FINISH_BUTTON,
        callback_data=ChecklistFinish(run_id=run_id).pack(),
    )


def fit(caption: str) -> str:
    """Cut a caption to the 20 characters of TZ 8.2, keeping its front."""
    if len(caption) <= CAPTION_LIMIT:
        return caption
    return caption[: CAPTION_LIMIT - len(ELLIPSIS)] + ELLIPSIS


__all__ = [
    "CAPTION_LIMIT",
    "ELLIPSIS",
    "GROUP_COLUMNS",
    "MARKER_COLUMNS",
    "fit",
    "run_keyboard",
    "skip_prompt_keyboard",
    "skip_question_keyboard",
]
