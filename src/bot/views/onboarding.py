"""The screens of the way in (TZ 5.1, plan task 22).

Pure functions of what `AccessService` answered, in the sense of `src/bot/views/__init__.py`:
they render, they send nothing, and they never ask the service anything themselves. That is
what lets the empty state of this block be a snapshot in a test rather than a branch nobody
reads (plan task 41).

Two decisions of TZ 5.1 are visible in the shapes below.

**A refusal names the reason and offers nothing.** Four things can be wrong with a code and
TZ 5.1 gives three of them their own wording; :data:`REJECTION_TEXTS` is the whole mapping,
in one place, so that a fifth outcome added to
:class:`~src.services.access.InviteRejection` fails at the lookup instead of falling into
some other screen's default. None of the four carries a button: the way to a working code is
the manager, and a button here could only lead back to the screen the person is already on
(TZ 8.1).

**The empty state of this block is a venue that does not exist yet** (TZ 8.1, decision A3).
Every other section is empty because the venue has not filled it in; onboarding is empty
because there is no venue at all, and the person looking at it is the one who can create
one. :func:`venue_missing` is that screen, and it carries the single button that leads
somewhere real.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from src.bot import texts
from src.bot.keyboards import onboarding as keyboards
from src.bot.views import Screen, quoted
from src.services.access import InviteRejection

#: Blank line between the greeting and the question under it. A message of TZ 5.1 is two
#: sentences at most, so the separator is a constant rather than a formatting policy.
PARAGRAPH: Final = "\n\n"


@dataclass(frozen=True, slots=True)
class VenueOption:
    """One venue on the choice screen: the id a button carries, the name it shows.

    Not an `AccessContext`: that object knows the role and the membership and not the name,
    which is the only thing this screen shows. Keeping the view's input to the two values it
    prints is what makes it renderable from a test without a database.
    """

    venue_id: int
    name: str


#: Why a code did not work, in the wording TZ 5.1 asks for. `REVOKED` shares the text of an
#: unknown code deliberately: a withdrawn code is one the manager took back, so "there is no
#: such code, check it with your manager" is both true and the action the person needs —
#: while "it has already been used" would be a plain lie about a code nobody used.
REJECTION_TEXTS: Final[Mapping[InviteRejection, str]] = {
    InviteRejection.MALFORMED: texts.ONBOARDING_CODE_UNKNOWN,
    InviteRejection.UNKNOWN: texts.ONBOARDING_CODE_UNKNOWN,
    InviteRejection.REVOKED: texts.ONBOARDING_CODE_UNKNOWN,
    InviteRejection.EXPIRED: texts.ONBOARDING_CODE_EXPIRED,
    InviteRejection.ALREADY_USED: texts.ONBOARDING_CODE_USED,
    InviteRejection.ALREADY_MEMBER: texts.ONBOARDING_CODE_ALREADY_MEMBER,
}


def _joined(*parts: str) -> str:
    return PARAGRAPH.join(part for part in parts if part)


def invite_rejected(rejection: InviteRejection) -> Screen:
    """Why the code did not work, and nothing else (TZ 5.1)."""
    return Screen(text=REJECTION_TEXTS[rejection])


def invite_confirmation(*, venue: str, full_name: str, position: str | None) -> Screen:
    """The greeting, the name the code carries and the question (TZ 5.1, decision B8).

    The position is part of the sentence when the manager filled it in and disappears when
    they did not — TZ 5.1 makes it optional on the code, and a single template would print
    the trailing comma of an empty placeholder into the employee's first screen.
    """
    confirmation = (
        texts.ONBOARDING_NAME_CONFIRM_TEMPLATE.format(
            full_name=quoted(full_name), position=quoted(position)
        )
        if position
        else texts.ONBOARDING_NAME_CONFIRM_SHORT_TEMPLATE.format(full_name=quoted(full_name))
    )
    return Screen(
        text=_joined(texts.ONBOARDING_WELCOME_TEMPLATE.format(venue=quoted(venue)), confirmation),
        markup=keyboards.invite_confirmation(),
    )


def name_prompt(*, venue: str | None = None) -> Screen:
    """Ask for the name, with the greeting when this is the first screen of the road.

    Two callers, one screen. A code whose `full_name` the manager left empty has nothing to
    confirm, so the greeting and the question arrive together; the correction button on
    the confirmation asks the same question a message later, where repeating the greeting
    would read as if the bot had forgotten the conversation.

    No keyboard: the answer is typed, and a button on a screen waiting for text is a button
    that says the typing is optional.
    """
    greeting = (
        "" if venue is None else texts.ONBOARDING_WELCOME_TEMPLATE.format(venue=quoted(venue))
    )
    return Screen(text=_joined(greeting, texts.ONBOARDING_NAME_PROMPT))


def name_too_short() -> Screen:
    """The correction was not a name. The screen stays where it was (TZ 8.2)."""
    return Screen(text=texts.ONBOARDING_NAME_TOO_SHORT)


def activated(*, full_name: str, venue: str) -> Screen:
    """The line that says the person is in the team of the venue (TZ 5.1).

    No inline keyboard, and not by omission: what follows this screen is the permanent reply
    keyboard of TZ 5.2, which is not an :class:`~aiogram.types.InlineKeyboardMarkup` and so
    cannot travel in a :class:`~src.bot.views.Screen`. The handler attaches it when it sends
    the text.
    """
    return Screen(
        text=texts.ONBOARDING_DONE_TEMPLATE.format(full_name=quoted(full_name), venue=quoted(venue))
    )


def welcomed_back(*, full_name: str, venue: str) -> Screen:
    """The same moment for somebody whose row was here all along (TZ 5.1).

    Their checklists, shifts and write-offs never went anywhere — only their access did —
    so the greeting says «again». The keyboard is the reply one, attached by the handler,
    exactly as in :func:`activated`.
    """
    return Screen(
        text=texts.ONBOARDING_WELCOME_BACK_TEMPLATE.format(
            full_name=quoted(full_name), venue=quoted(venue)
        )
    )


def venue_choice(options: Sequence[VenueOption]) -> Screen:
    """The venues somebody works in, to pick one from (TZ 5.1).

    An empty list is answered with the refusal of TZ 5.1 rather than with an empty keyboard.
    It is not a state the gate can produce — a person with no membership never reaches a
    screen at all — but a view is a total function, and "pick a venue" over nothing
    is the one rendering that would be worse than saying the truth.
    """
    if not options:
        return Screen(text=texts.ONBOARDING_NO_ACCESS)
    return Screen(
        text=texts.ONBOARDING_PICK_VENUE,
        markup=keyboards.venue_choice((option.venue_id, option.name) for option in options),
    )


def venue_missing() -> Screen:
    """The empty state of stage 0: no venue exists yet (TZ 8.1, decision A3)."""
    return Screen(
        text=texts.ONBOARDING_CREATE_VENUE_OFFER,
        markup=keyboards.venue_missing(),
    )


__all__ = [
    "PARAGRAPH",
    "REJECTION_TEXTS",
    "VenueOption",
    "activated",
    "invite_confirmation",
    "invite_rejected",
    "name_prompt",
    "name_too_short",
    "venue_choice",
    "venue_missing",
    "welcomed_back",
]
