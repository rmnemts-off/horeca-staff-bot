"""Screens of the checklist template editor (TZ 5.8; decisions B3, B6, D2, D11).

Pure functions, in the sense `src/bot/views/__init__.py` gives the word: a view the service
returned goes in, a :class:`~src.bot.views.Screen` comes out, nothing is sent and nothing is
asked of a repository. The manager's screen has only one caller today, but the rule is the
package's and not the caller's — and it is what lets the empty state of TZ 8.1 and the "no
button leads nowhere" rule be asserted in `tests/bot/test_admin_checklists.py` as values.

**The checklist is text, the buttons are numbers** (decision D11, the same arrangement
`views/checklist.py` uses for the bartender). The whole template is written out — a heading
per group, a line per item, the critical ones marked where TZ 5.4 marks them — and the lines
of the *current* group are numbered, because those are the ones the markers under the
message point at. Everything the venue typed goes through :func:`~src.bot.views.quoted`
first: a line with a `<` in it would otherwise take the whole message down with it, and a
bar that writes one is not an edge case (see the package docstring).

**The empty state is the first screen of every venue** (TZ 8.1, decision B1: the template
row is created empty together with the venue, so "empty" is a template with no lines and not
a missing template). It says `EDITOR_EMPTY` and it carries both roads out of it: the
`EDITOR_ADD_BUTTON` / `EDITOR_ADD_TO_NEW_GROUP_BUTTON` pair for one line at a time, and
`EDITOR_BULK_PROMPT` for the whole checklist in one message (decision B6) — which needs no
button, because the handler leaves the FSM in `TemplateEditor.bulk` while this screen is
open and the syntax is on the screen next to the input field.

**A fork is said in words** (decision B3). `TemplateService` answers `forked=True` when the
edit had to copy the template because a run already pointed at it, and TZ 5.8 promises the
manager is told; :func:`new_version_notice` is that sentence, and it is a line of the screen
rather than a toast, so it survives the manager looking away.
"""

from __future__ import annotations

from typing import Final

from src.bot import texts
from src.bot.keyboards import editor as keyboards
from src.bot.views import Screen, quoted
from src.services.templates import TemplateGroupView, TemplateItemView, TemplateView

#: Groups are separated by a blank line, the lines inside one by a single break.
BLOCK_SEPARATOR: Final = "\n\n"
LINE_SEPARATOR: Final = "\n"

#: Between a line's number and the venue's wording. Punctuation, not wording: it exists so
#: that «2.» in the text and the button «2» are visibly the same line (decision D11).
NUMBER_SEPARATOR: Final = ". "

#: Between a mark and what it applies to.
MARK_SEPARATOR: Final = " "


# --------------------------------------------------------------------------------------
# Reading a view: what the screen and the handler both need to find
# --------------------------------------------------------------------------------------


def group_of(view: TemplateView, group_index: int) -> TemplateGroupView | None:
    """The group with that stored index, or `None` — a group with no lines does not exist."""
    return next((group for group in view.groups if group.index == group_index), None)


def item_of(view: TemplateView, item_id: int) -> TemplateItemView | None:
    """One line of the active template, or `None` when it is gone or belongs to an old one.

    An id from a superseded version answers `None` here, which is right and not a loss:
    decision B3 makes old versions read-only, so the card of such a line has nothing it
    could offer.
    """
    return next((item for item in view.items if item.item_id == item_id), None)


def current_group(view: TemplateView, group_index: int | None = None) -> TemplateGroupView | None:
    """The group the markers belong to. `None` only when the checklist has no lines at all.

    An index no group answers to falls back to the **last** group rather than raising: a
    button drawn before somebody emptied that group is stale, not hostile (TZ 9), and the end
    of the checklist is where a manager who is filling one in wants to be.
    """
    if group_index is not None:
        found = group_of(view, group_index)
        if found is not None:
            return found
    return view.groups[-1] if view.groups else None


def position_of(group: TemplateGroupView | None, item_id: int) -> int:
    """A line's place in its group, counting from one; `0` when it is not in it."""
    if group is None:
        return 0
    return next(
        (number for number, item in enumerate(group.items, start=1) if item.item_id == item_id),
        0,
    )


# --------------------------------------------------------------------------------------
# Wording that belongs to an outcome rather than to a screen
# --------------------------------------------------------------------------------------


def new_version_notice(version: int | None) -> str | None:
    """Decision B3 in one sentence, or `None` when nothing was forked."""
    if version is None:
        return None
    return texts.EDITOR_NEW_VERSION_TEMPLATE.format(version=version)


def bulk_notice(*, added: int, groups: int, version: int | None = None) -> str:
    """What one bulk message did (decision B6), and what it cost (decision B3)."""
    lines = [texts.EDITOR_BULK_RESULT_TEMPLATE.format(added=added, groups=groups)]
    forked = new_version_notice(version)
    if forked is not None:
        lines.append(forked)
    return LINE_SEPARATOR.join(lines)


# --------------------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------------------


def template_screen(
    view: TemplateView,
    *,
    group_index: int | None = None,
    notice: str | None = None,
) -> Screen:
    """The whole checklist, empty state included (TZ 5.8, 8.1).

    `group_index` is the venue's own `group_index` (decision D2) and not a position, because
    that is what :class:`~src.bot.callbacks.EditorGroup` carries; `None` means "wherever the
    checklist currently ends".
    """
    current = current_group(view, group_index)
    blocks: list[str] = []
    if notice:
        blocks.append(notice)
    blocks.append(_title(view))
    if view.is_empty:
        # TZ 8.1: the sentence and both ways out of it, one of which is simply typing.
        blocks.append(texts.EDITOR_EMPTY)
        blocks.append(texts.EDITOR_BULK_PROMPT)
    else:
        blocks.extend(
            _group_block(number, group, numbered=group is current)
            for number, group in enumerate(view.groups, start=1)
        )
    return Screen(
        text=BLOCK_SEPARATOR.join(blocks),
        markup=keyboards.template_keyboard(view, current),
    )


def item_screen(view: TemplateView, item: TemplateItemView, *, notice: str | None = None) -> Screen:
    """One line's card: what it says, what is set on it, and what can be done about it."""
    template_id = view.template_id
    if template_id is None:
        # Unreachable while the line exists — a line hangs off a template — but the type
        # says otherwise, and a screen is a better answer than an assertion (TZ 9).
        return template_screen(view, notice=notice)

    group = group_of(view, item.group_index)
    lines = [_wording(item)]
    if item.requires_photo:
        # TZ 5.4: the flag decides whether the line can be ticked without a photo, so it is
        # readable on the card and not only in the button that toggles it.
        lines.append(texts.BULLET_LINE_TEMPLATE.format(text=texts.EDITOR_PHOTO_BUTTON))

    blocks = [notice] if notice else []
    blocks.append(LINE_SEPARATOR.join(lines))
    return Screen(
        text=BLOCK_SEPARATOR.join(blocks),
        markup=keyboards.item_keyboard(
            item,
            template_id=template_id,
            movable=position_of(group, item.item_id) > 1,
        ),
    )


def text_prompt(view: TemplateView) -> Screen:
    """`EDITOR_TEXT_PROMPT`: one line, typed. Used for a new line and for rewording one."""
    return Screen(text=texts.EDITOR_TEXT_PROMPT, markup=keyboards.step_keyboard(view.template_id))


def group_prompt(view: TemplateView) -> Screen:
    """`EDITOR_GROUP_PROMPT`: the name of a group, new or being renamed (decision D2)."""
    return Screen(text=texts.EDITOR_GROUP_PROMPT, markup=keyboards.step_keyboard(view.template_id))


def bulk_prompt(view: TemplateView) -> Screen:
    """`EDITOR_BULK_PROMPT`: eight groups and forty lines in one message (decision B6)."""
    return Screen(text=texts.EDITOR_BULK_PROMPT, markup=keyboards.step_keyboard(view.template_id))


# --------------------------------------------------------------------------------------
# The text
# --------------------------------------------------------------------------------------


def _title(view: TemplateView) -> str:
    """`EDITOR_TITLE_TEMPLATE`: which checklist this is, and how long it has got."""
    return texts.EDITOR_TITLE_TEMPLATE.format(
        checklist=texts.checklist_title(view.checklist_type),
        total=view.total_items,
    )


def _group_block(number: int, group: TemplateGroupView, *, numbered: bool) -> str:
    lines = [_heading(number, group)]
    lines.extend(
        _line(item, number=position if numbered else None)
        for position, item in enumerate(group.items, start=1)
    )
    return LINE_SEPARATOR.join(lines)


def _heading(number: int, group: TemplateGroupView) -> str:
    """`2. <name>`, or the unnamed form: a venue may enter lines without naming a group.

    The number is the group's position on this screen and not `group.index` — the stored
    index counts from zero and keeps the holes deleting a group leaves behind (decision D2).
    """
    if group.name:
        return texts.CHECKLIST_GROUP_TEMPLATE.format(index=number, group_name=quoted(group.name))
    return texts.CHECKLIST_GROUP_UNNAMED_TEMPLATE.format(index=number)


def _line(item: TemplateItemView, *, number: int | None) -> str:
    """A numbered line, or a bulleted one: the number *is* the bullet where there is one.

    Both marks say the same thing — "this is one line of the list" — and printing them
    together («• 2. …») reads as two lists, one inside the other.
    """
    if number is None:
        return texts.BULLET_LINE_TEMPLATE.format(text=_wording(item))
    return _wording(item, number=number)


def _wording(item: TemplateItemView, *, number: int | None = None) -> str:
    """The venue's own line, marked critical where it is, numbered where it is pressable."""
    text = quoted(item.text)
    if item.is_critical:
        text = f"{texts.CHECKLIST_CRITICAL_MARK}{MARK_SEPARATOR}{text}"
    if number is None:
        return text
    return f"{number}{NUMBER_SEPARATOR}{text}"


__all__ = [
    "BLOCK_SEPARATOR",
    "LINE_SEPARATOR",
    "MARK_SEPARATOR",
    "NUMBER_SEPARATOR",
    "bulk_notice",
    "bulk_prompt",
    "current_group",
    "group_of",
    "group_prompt",
    "item_of",
    "item_screen",
    "new_version_notice",
    "position_of",
    "template_screen",
    "text_prompt",
]
