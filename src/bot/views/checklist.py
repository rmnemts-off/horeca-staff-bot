"""The checklist as one message: what it says and what can be pressed (TZ 5.4, D11).

**Pure, and that is the requirement rather than the style.** Two processes draw this screen:
the handler of `src/bot/handlers/checklist.py` when the employee taps something, and the
notification worker when the checklist arrives on its own ten minutes before the shift
(TZ 1.4#2, 5.4). Neither may hold the only copy. So every function here takes the
:class:`~src.services.checklists.RunView` a service returned and answers with a
:class:`~src.bot.views.Screen`; nothing here calls a service, a repository or a clock.

The shape the worker is written against:

    render_run(view: RunView, *, group_index: int | None = None) -> Screen
    render_skip_question(run_id: int, pending: Sequence[ItemView]) -> Screen
    render_skip_prompt() -> Screen
    render_finished(checklist_type: ChecklistType, *, skipped: bool) -> Screen
    group_index_of(view: RunView, item_id: int) -> int | None

**How the text and the buttons are tied together** (decision D11). The message is the list:
a heading per group, a `[ ]` / `[✓]` line per item, and the counter of TZ 5.4 at the bottom.
The buttons are the markers of *one* group. Which one is not left to the reader — the lines
of the current group are numbered in the text, and the markers carry those same numbers, so
`2` and the line written `2.` are the same line. That is the one thing this screen has to
get right: the bartender is holding a bar spoon in the other hand.

`group_index` is the venue's own `group_index` column (decision D2), not a position: it is
what :class:`~src.bot.callbacks.ChecklistGroup` carries. An index no group answers to falls
back to the group with work left rather than raising — a button from a screen drawn before
a manager reopened the run is stale, not hostile, and TZ 9 answers stale screens with a
screen and not with an error.

**The empty state is here and not in the handler** (TZ 8.1). A venue that has entered no
lines yet has a template and no items, so `create_run` sends the employee nothing at all
(decision B1) — and if he goes looking through the shift screen anyway, this is what he
finds: one sentence, and no button that leads nowhere.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from src.bot import texts
from src.bot.keyboards.checklist import (
    run_keyboard,
    skip_prompt_keyboard,
    skip_question_keyboard,
)
from src.bot.views import Screen, quoted
from src.db.models import ChecklistType
from src.services.checklists import GroupView, ItemView, RunView

#: Groups are separated by a blank line, lines inside a group by a single break.
BLOCK_SEPARATOR: Final = "\n\n"
LINE_SEPARATOR: Final = "\n"

#: Between a line's number and the venue's wording. Punctuation, not wording: it exists so
#: that «2.» in the text and the button «2» are visibly the same line (decision D11).
NUMBER_SEPARATOR: Final = ". "

#: Between the critical mark and the wording it applies to.
MARK_SEPARATOR: Final = " "

#: What the employee hears when everything is ticked (TZ 5.4). Spelled per type rather than
#: branched on, the way `texts.checklist_title` is: a type added later is a line here.
#: `stock` has no wording of its own because TZ 5.4 ends it differently — with the list of
#: what is missing, which is stage 1 — so it gets the neutral `SAVED` and says nothing
#: untrue in the meantime.
_COMPLETED_BY_TYPE: Final[Mapping[ChecklistType, str]] = {
    ChecklistType.OPENING: texts.CHECKLIST_COMPLETED,
    ChecklistType.CLOSING: texts.CHECKLIST_CLOSING_COMPLETED,
    ChecklistType.STOCK: texts.SAVED,
}


def render_run(view: RunView, *, group_index: int | None = None) -> Screen:
    """The checklist itself: the list as text, the markers of one group as buttons.

    `group_index` is the group to put the markers on. `None` means "wherever the work is" —
    the first group that is not fully ticked — which is what a re-entry from the shift
    section wants: the employee swiped the message away in the middle of group four and does
    not want to start reading at group one.
    """
    if view.is_empty:
        # TZ 8.1: the template has no lines, so there is nothing to tick and nothing to
        # press. The manager has already been told (decision B1).
        return Screen(text=texts.CHECKLIST_NOT_FILLED_IN)

    current = _current_group(view, group_index)
    blocks = [texts.checklist_title(view.checklist_type)]
    blocks.extend(
        _group_block(number, group, numbered=group is current)
        for number, group in enumerate(view.groups, start=1)
    )
    blocks.append(
        texts.CHECKLIST_PROGRESS_TEMPLATE.format(done=view.done_items, total=view.total_items)
    )
    return Screen(
        text=BLOCK_SEPARATOR.join(blocks),
        markup=run_keyboard(view, current=current),
    )


def render_skip_question(run_id: int, pending: Sequence[ItemView]) -> Screen:
    """TZ 5.4: what was left unticked, and the question whether to send it as it is.

    The order is the service's — critical lines first (`RunView.pending`) — and so is the
    decision about what counts as critical. This function marks and prints; it does not
    sort, because a screen that sorts is a screen that can sort differently from the
    notification the manager gets about the same run.
    """
    lines = LINE_SEPARATOR.join(
        texts.BULLET_LINE_TEMPLATE.format(text=_wording(item)) for item in pending
    )
    return Screen(
        text=texts.CHECKLIST_PENDING_QUESTION_TEMPLATE.format(lines=lines),
        markup=skip_question_keyboard(run_id),
    )


def render_skip_prompt() -> Screen:
    """TZ 5.4: the bot asks for a short comment saying why."""
    return Screen(text=texts.CHECKLIST_SKIP_COMMENT_PROMPT, markup=skip_prompt_keyboard())


def render_finished(checklist_type: ChecklistType, *, skipped: bool) -> Screen:
    """The last screen of a run: the checklist closes and nothing on it can be pressed.

    Two endings, and TZ 5.4 makes the difference: everything ticked and the employee hears
    a good word while the manager hears nothing at all; something skipped and the reason is
    already on its way to the manager — through `notification_service`, never from a
    handler.
    """
    if skipped:
        return Screen(text=texts.CHECKLIST_SKIP_SENT)
    return Screen(text=_COMPLETED_BY_TYPE[checklist_type])


def group_index_of(view: RunView, item_id: int) -> int | None:
    """The group a line belongs to, so a tap redraws the screen where the tap happened.

    :class:`~src.bot.callbacks.ChecklistToggle` carries the run and the line and no group —
    two ids are already what decision D14 sized the budget for. The group is not missing
    from the payload, it is derivable from it, and deriving it is this function.
    """
    for group in view.groups:
        if any(item.item_id == item_id for item in group.items):
            return group.index
    return None


# --------------------------------------------------------------------------------------
# The text
# --------------------------------------------------------------------------------------


def _current_group(view: RunView, group_index: int | None) -> GroupView:
    """The group the markers belong to. Never raises: see the module docstring."""
    if group_index is not None:
        for group in view.groups:
            if group.index == group_index:
                return group
    return next((group for group in view.groups if not group.is_complete), view.groups[0])


def _group_block(number: int, group: GroupView, *, numbered: bool) -> str:
    lines = [_heading(number, group)]
    lines.extend(
        _line(item, number=position if numbered else None)
        for position, item in enumerate(group.items, start=1)
    )
    return LINE_SEPARATOR.join(lines)


def _heading(number: int, group: GroupView) -> str:
    """`2. <name>`, or the unnamed form when the venue entered lines without naming a group.

    The number is the group's position in this message and not `group.index`: the stored
    index is the venue's own (decision D2) and may start at zero, which would print a
    heading no employee counts from.
    """
    if group.name:
        return texts.CHECKLIST_GROUP_TEMPLATE.format(index=number, group_name=quoted(group.name))
    return texts.CHECKLIST_GROUP_UNNAMED_TEMPLATE.format(index=number)


def _line(item: ItemView, *, number: int | None) -> str:
    template = (
        texts.CHECKLIST_LINE_DONE_TEMPLATE
        if item.is_done
        else texts.CHECKLIST_LINE_PENDING_TEMPLATE
    )
    return template.format(text=_wording(item, number=number))


def _wording(item: ItemView, *, number: int | None = None) -> str:
    """The venue's own line, marked critical where it is, numbered where it is pressable.

    TZ 5.4 marks `is_critical` in place — the escalation on a skipped run is decided on
    that flag, so it has to be visible *before* the finish button and not only in what the
    reads afterwards.
    """
    text = quoted(item.text)
    if item.is_critical:
        text = f"{texts.CHECKLIST_CRITICAL_MARK}{MARK_SEPARATOR}{text}"
    if number is None:
        return text
    return f"{number}{NUMBER_SEPARATOR}{text}"


__all__ = [
    "BLOCK_SEPARATOR",
    "LINE_SEPARATOR",
    "group_index_of",
    "render_finished",
    "render_run",
    "render_skip_prompt",
    "render_skip_question",
]
