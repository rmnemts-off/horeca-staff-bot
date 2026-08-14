"""Screens as pure functions: a service view in, a text and a keyboard out.

**Why this package exists at all.** Two different processes have to draw the same screen.
The bartender opens the opening checklist from the shift screen — that is a handler in the bot
process; the same checklist arrives on its own before the shift (TZ 1.4#2, 5.4) — that is
the worker, a separate process with no update, no callback query and no router. If the
screen were built inside the handler, the worker would have a second copy of it, and the
copy that fell behind would be the one the employee sees at six in the morning.

So a view is a function of data, and only of data:

* it takes the view object a service returned (`RunView`, `ShiftView`, `RecipeCard`, …) —
  never a repository, never a session, never an aiogram `Message`;
* it returns the text and the markup, and nothing else. It sends nothing, edits nothing and
  answers no callback query: who delivers the screen is the caller's business, and that is
  precisely the difference between the two callers above;
* it is therefore testable without a bot at all, which is what the empty-state snapshots of
  plan task 41 need.

Wording comes from `src/bot/texts/`, payloads from `src/bot/callbacks.py`. A view holds no
Russian string of its own — that rule is checked over the whole of `src/`.

**Empty states are views, not branches in a handler** (TZ 8.1). Every venue starts with
nothing, so "there are no shifts yet" and "no recipes have been entered" are ordinary
outputs of the same function, with a keyboard that leads somewhere real rather than a bare
line of text.

**Every screen is HTML** (`src/bot/dispatcher.py::build_bot` sets the parse mode for the
whole process), because some of the wording needs it: an invite code is `<code>` so that it
can be tapped to copy, a drink's name is `<b>`. That is a property of the package and not of
those two texts — a screen cannot opt out, since the parse mode is set on the bot and not
per message. So **anything that came from a person or from a venue's own data goes through
:func:`quoted` before it is formatted into a text.** A bar called «Rum & Cola» or an
employee who typed `<` is not an edge case to be handled later: with the parse mode on and
the quoting off, Telegram rejects the whole message and the employee sees nothing at all.
`tests/bot/test_views_html.py` renders every screen with hostile data and parses the result,
so a new view that forgets this is red rather than merely wrong on some venue's name.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.text_decorations import html_decoration


def quoted(value: str) -> str:
    """Text from a person or a venue, made safe for the HTML every screen is written in.

    One function for the whole package rather than one per module: the rule is a property of
    the parse mode, which is set once for the process, so a module deciding it for itself is
    a module that can decide it differently.
    """
    return html_decoration.quote(value)


@dataclass(frozen=True, slots=True)
class Screen:
    """One rendered screen: what it says and what can be pressed on it.

    A dataclass rather than a tuple because both callers pass it on — the handler into
    `safe_edit`, the worker into `OutgoingMessage` — and an unpacked pair of positional
    values swaps itself sooner or later.
    """

    text: str
    markup: InlineKeyboardMarkup | None = None


__all__ = ["Screen", "quoted"]
