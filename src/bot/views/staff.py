"""Screens of the employees section: the roster, a card, the invite wizard (TZ 5.1, 5.8).

Pure functions, in the sense `src/bot/views/__init__.py` gives the word: a view of the
service layer goes in, a :class:`~src.bot.views.Screen` comes out, and nothing is sent. The
roster is therefore assertable without a bot at all, which is what the empty-state snapshot
of plan task 41 needs.

**The empty state is a screen here, not a branch in the handler** (TZ 8.1). Every venue
starts with nobody in it — the owner creates the venue and is its only member — so
`STAFF_EMPTY` is the *first* thing a manager sees in this section, and it carries the same
`STAFF_ADD_BUTTON` the full list does.

**Marks hang off the line, not off a second one.** TZ 5.8 wants `STAFF_BLOCKED_MARK` in the
list, and TZ 5.1 keeps a dismissed employee's row forever, so `STAFF_DEACTIVATED_MARK` has
to be readable at a glance too. Both are appended to the person's line with the separator
the line template already uses, so a roster stays one line per person on a phone.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from src.bot import texts
from src.bot.callbacks import MemberField
from src.bot.keyboards.staff import (
    code_keyboard,
    member_edit_keyboard,
    member_keyboard,
    name_keyboard,
    position_keyboard,
    revoked_keyboard,
    role_keyboard,
    roster_keyboard,
)
from src.bot.views import Screen, quoted
from src.bot.views.shifts import format_date
from src.db.models import InviteCode, MemberRole
from src.services.access import AccessContext
from src.services.members import RosterEntry
from src.services.timezones import ensure_utc, venue_timezone

#: What `STAFF_LINE_TEMPLATE` writes between the fields of a line. The marks are appended
#: with the same one, so that a line with a mark on it still reads as one list and not as a
#: line with a suffix bolted on.
MARK_SEPARATOR: Final = " · "

#: `t.me/<bot>?start=<payload>` of TZ 5.1. The payload is built by
#: `AccessService.invite_deeplink_payload`; the username can only come from the running bot
#: (`Bot.me()`), never from configuration, so it is an argument of :func:`deeplink`.
DEEPLINK_URL_TEMPLATE: Final = "https://t.me/{username}?start={payload}"


def deeplink(username: str, payload: str) -> str:
    """The link a manager forwards instead of dictating the code (TZ 5.1)."""
    return DEEPLINK_URL_TEMPLATE.format(username=username, payload=payload)


def roster_line(entry: RosterEntry) -> str:
    """Name, position and role on one line, plus the marks that apply (TZ 5.8, 5.1, 6)."""
    template = (
        texts.STAFF_LINE_WITH_POSITION_TEMPLATE if entry.position else texts.STAFF_LINE_TEMPLATE
    )
    parts = [
        template.format(
            full_name=quoted(entry.full_name),
            position=quoted(entry.position or ""),
            role=texts.role_label(entry.role),
        )
    ]
    if entry.is_bot_blocked:
        # TZ 6: the worker wrote this flag when a notification bounced. The manager is the
        # only person who can act on it, so this list is where it belongs.
        parts.append(texts.STAFF_BLOCKED_MARK)
    if not entry.is_active:
        parts.append(texts.STAFF_DEACTIVATED_MARK)
    return MARK_SEPARATOR.join(parts)


def pending_line(code: InviteCode, *, timezone: str) -> str:
    """One code nobody has used yet: who it is for, what it grants, when it dies.

    The expiry is printed as a venue-local date (TZ 3.4 — the database keeps UTC and every
    screen reads the venue's clock). A code lives seven days, so a day and a month are
    the whole of what a manager needs; the name comes from the venue and is escaped.
    """
    until = ensure_utc(code.expires_at).astimezone(venue_timezone(timezone)).date()
    return texts.STAFF_INVITE_LINE_TEMPLATE.format(
        full_name=quoted(code.full_name or texts.STAFF_INVITE_NO_NAME),
        role=texts.role_label(code.role),
        until=format_date(until),
    )


def roster_screen(
    entries: Sequence[RosterEntry],
    pending: Sequence[InviteCode] = (),
    *,
    timezone: str,
) -> Screen:
    """The employees list, or the empty state that starts one (TZ 5.8, 8.1).

    Two blocks, and the second one only when it has something in it: a heading over an
    empty list is noise on the screen every venue sees on its first day.
    """
    body = [roster_line(entry) for entry in entries] if entries else [texts.STAFF_EMPTY]
    if pending:
        body.extend(
            [
                "",
                texts.STAFF_PENDING_TITLE,
                *(pending_line(code, timezone=timezone) for code in pending),
            ]
        )
    return Screen(
        text="\n".join([texts.STAFF_TITLE, "", *body]),
        markup=roster_keyboard(entries, pending),
    )


def member_screen(entry: RosterEntry, *, actor: AccessContext) -> Screen:
    """One employee's card: the same line, and what can be done about it (TZ 5.1).

    The actor is needed to decide *what* can be done: nobody switches themselves off, and
    a manager does not switch off the owner (TZ 2).
    """
    return Screen(text=roster_line(entry), markup=member_keyboard(entry, actor=actor))


def member_edit_screen(field: MemberField) -> Screen:
    """The step that asks for a new name or position on an existing card (TZ 5.8)."""
    prompt = (
        texts.STAFF_RENAME_PROMPT if field is MemberField.FULL_NAME else texts.STAFF_POSITION_PROMPT
    )
    return Screen(text=prompt, markup=member_edit_keyboard())


def invite_name_screen() -> Screen:
    """Decision B8: the manager writes the name, the employee confirms it on activation."""
    return Screen(text=texts.INVITE_NAME_PROMPT, markup=name_keyboard())


def invite_role_screen(roles: Sequence[MemberRole]) -> Screen:
    """TZ 5.1: the code is bound to a role, and TZ 2 says which ones may be handed out."""
    return Screen(text=texts.INVITE_ROLE_PROMPT, markup=role_keyboard(roles))


def invite_position_screen(role: MemberRole) -> Screen:
    """The position is optional, so the step offers a skip rather than demanding a word."""
    return Screen(text=texts.INVITE_POSITION_PROMPT, markup=position_keyboard(role))


def invite_code_screen(*, full_name: str, code: str, link: str, code_id: int) -> Screen:
    """The code, the deep link and how long they live (TZ 5.1).

    Both roads in are on the screen at once on purpose: the manager forwards the link to a
    person who has Telegram open, and reads the code out to a person who does not.
    """
    return Screen(
        text="\n".join(
            [
                texts.INVITE_READY_TEMPLATE.format(
                    full_name=quoted(full_name), code=quoted(code), link=quoted(link)
                ),
                texts.INVITE_HINT,
            ]
        ),
        markup=code_keyboard(code_id),
    )


def invite_revoked_screen() -> Screen:
    """A withdrawn code: nothing to press on it any more but the way back (TZ 8.1)."""
    return Screen(text=texts.INVITE_REVOKED, markup=revoked_keyboard())


__all__ = [
    "DEEPLINK_URL_TEMPLATE",
    "MARK_SEPARATOR",
    "deeplink",
    "invite_code_screen",
    "invite_name_screen",
    "invite_position_screen",
    "invite_revoked_screen",
    "invite_role_screen",
    "member_edit_screen",
    "member_screen",
    "roster_line",
    "roster_screen",
]
