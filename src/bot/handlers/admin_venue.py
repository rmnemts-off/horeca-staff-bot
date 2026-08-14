"""Creating a venue and its settings (TZ 5.8; plan tasks 26 and 30).

Owner: plan tasks 26 and 30. Two flows live here because they are two ends of one object:
the wizard that raises a venue from nothing, and the screen that edits what the wizard set.

**The wizard (task 26, decisions A3 and B1).** Name, city, zone *by buttons* (TZ 3.4 wants an
IANA identifier and principle 1.4#5 wants a choice to be pressed, not typed), then the
default shift window. The last step calls
:meth:`~src.services.venues.VenueService.create` **once**: the venue, its settings row, the
owner's `venue_members` row and the two checklist templates are one transaction, which the
container commits when this handler returns. Nothing is written before that call, so
cancelling at any step leaves the database exactly as it was — and criterion 11.6 holds in
both directions: the venue comes up through the interface, and it comes up with zero
checklist items, because decision B1 has the templates created empty and this module has no
way to add an item to one.

The person running it is the bootstrap owner of decision A3 — no membership anywhere, so
`data["actor"]` is `None` throughout and the right to be here is
:attr:`~src.services.access.Identity.may_create_venue`, checked on the server at the entry.
The `users` row was created by `/start` (TZ 5.1), and it is that row the venue is hung off.

**The settings (task 30).** The zone, `opening_checklist_lead_minutes` and the default shift
window, each with the button that changes it. The numbers are typed —
:class:`~src.bot.states.SettingsEdit` is what remembers which of the two is being typed —
and the zone is pressed, from the same list the wizard offers. Changing the lead time moves
the moment the opening checklist is pushed, and this module does not move it: the value is
written to `venue_settings` and `src/services/notifications.py` computes the moment from it
(TZ 6, plan tasks 33 and 37).

**Cancel is not handled here.** `Nav(CANCEL)` and a main-menu press both belong to the menu
module and to `src/bot/middlewares/menu.py`; a second answer to them in this file would be
the same screen in two places (see `src/bot/handlers/__init__.py`).

**Who checks the role, and where.** The three settings-edit presses carry
:class:`~src.bot.callbacks.AdminCommand`, whose rule in `src/bot/middlewares/resolver.py`
is `minimum_role=MANAGER`, so their handlers are reached already vetted and check nothing
themselves. The two zone presses carry :class:`~src.bot.callbacks.TimezoneChoice`, whose
rule is `needs_actor=False` — it has to be, because the wizard offers the same list before
any membership exists — and the message steps have no resolver on them at all: aiogram runs
it on callback queries only. Both of those check for themselves, and that is the whole of
what is left to check by hand here.

:func:`window_in` and :func:`minutes_in` read what a manager typed; they are reported with
plan task 26.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Final

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot.callbacks import (
    AdminAction,
    AdminCommand,
    AdminSection,
    OpenAdmin,
    TimezoneChoice,
    VenueCreate,
)
from src.bot.handlers.admin import BOT_KEY, deliver, manager_of, refuse
from src.bot.keyboards.menu import main_menu
from src.bot.middlewares.auth import IDENTITY_KEY
from src.bot.middlewares.menu import STATE_KEY
from src.bot.middlewares.resolver import PAYLOAD_KEY
from src.bot.middlewares.services import VENUE_WIZARD_KEY
from src.bot.states import SettingsEdit, VenueWizard
from src.bot.views import admin as screens
from src.services.access import AccessContext, Identity
from src.services.venues import VenueConfiguration, VenueService, timezone_at, wizard_timezones

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "admin_venue"

#: What the wizard remembers between steps. Free text never travels in a button (the 64-byte
#: budget of `src/bot/callbacks.py`), so the three answers live here until the one call that
#: writes them.
NAME_KEY: Final = "venue_name"
CITY_KEY: Final = "venue_city"
TIMEZONE_KEY: Final = "venue_timezone"

#: The zones the wizard actually drew, kept beside the answers so that
#: `TimezoneChoice.index` is resolved against *the page that was shown* and not against
#: whatever the code offers now. A deploy that appends a zone must not turn a button drawn a
#: minute ago into a different city.
ZONES_KEY: Final = "venue_zones"

#: A wall clock reading anywhere inside what somebody typed: `08:00`, `8.00`, `23:00`.
_CLOCK: Final = re.compile(r"(\d{1,2})[:.](\d{2})")

#: A plain count of minutes, and nothing else on the line.
_COUNT: Final = re.compile(r"\d{1,4}")


# --------------------------------------------------------------------------------------
# Reading what a manager typed
# --------------------------------------------------------------------------------------


def window_in(typed: str | None) -> tuple[dt.time, dt.time] | None:
    """The two wall clock readings of a shift window, or `None` when there are not two.

    Deliberately indifferent to what separates them: `VENUE_WINDOW_PROMPT` spells the
    example with a word between the times, and a manager on a phone types a hyphen, an en
    dash or a space. Two readings and a valid shape for each is the whole of what this
    asserts — what the pair *means* (an end at or before the start is the next local day) is
    :func:`~src.services.timezones.shift_window`'s and stays there.
    """
    found = _CLOCK.findall(typed or "")
    if len(found) != 2:
        return None
    readings: list[dt.time] = []
    for hour, minute in found:
        try:
            readings.append(dt.time(hour=int(hour), minute=int(minute)))
        except ValueError:
            # `25:00`, `08:74` — a shape the regexp accepts and a clock does not.
            return None
    return readings[0], readings[1]


def minutes_in(typed: str | None) -> int | None:
    """A count of minutes, or `None`. Negative is not spelled: the prompt asks "how long
    before", so the sign is in the question and a minus in the answer is a typo."""
    found = _COUNT.fullmatch((typed or "").strip())
    return None if found is None else int(found.group())


# --------------------------------------------------------------------------------------
# The wizard (task 26)
# --------------------------------------------------------------------------------------


async def open_wizard(event: CallbackQuery, **data: Any) -> None:
    """The entry: the bootstrap owner of decision A3 presses "create a venue".

    The right is checked here and nowhere else. `VenueCreate` is declared `needs_actor=False`
    in the resolver — it has to be, since the presser belongs to no venue — so the resolver
    lets the press through without asking anything about rights, and a member of any venue
    could otherwise replay this payload (TZ 9).
    """
    identity = data.get(IDENTITY_KEY)
    if not isinstance(identity, Identity) or identity.user is None or not identity.may_create_venue:
        await refuse(event)
        return
    state: FSMContext = data[STATE_KEY]
    await state.set_state(VenueWizard.name)
    await state.set_data({})
    await deliver(event, screens.name_step(), bot=data[BOT_KEY])


async def take_name(event: Message, **data: Any) -> None:
    """Step 1. Blank is not an answer: the service refuses an empty name anyway (TZ 4.1)."""
    typed = (event.text or "").strip()
    state: FSMContext = data[STATE_KEY]
    if not typed:
        await deliver(event, screens.name_step(), bot=data[BOT_KEY])
        return
    await state.update_data({NAME_KEY: typed})
    await state.set_state(VenueWizard.city)
    await deliver(event, screens.city_step(), bot=data[BOT_KEY])


async def take_city(event: Message, **data: Any) -> None:
    """Step 2, and the point where the zone page is fixed for this run of the wizard."""
    typed = (event.text or "").strip()
    state: FSMContext = data[STATE_KEY]
    if not typed:
        await deliver(event, screens.city_step(), bot=data[BOT_KEY])
        return
    zones = wizard_timezones()
    await state.update_data({CITY_KEY: typed, ZONES_KEY: list(zones)})
    await state.set_state(VenueWizard.timezone)
    await deliver(event, screens.timezone_step(zones), bot=data[BOT_KEY])


async def take_timezone(event: CallbackQuery, **data: Any) -> None:
    """Step 3. An index that names nothing re-draws the step instead of failing.

    The index is resolved against the page in the FSM state, for the reason
    :data:`ZONES_KEY` gives; a state that has lost it (a restart past the TTL) falls back to
    the service's own list, which is append-only precisely so that the fallback is safe.
    """
    payload = data[PAYLOAD_KEY]
    state: FSMContext = data[STATE_KEY]
    zones = await _offered_zones(state)
    chosen = zones[payload.index] if 0 <= payload.index < len(zones) else None
    if chosen is None:
        await deliver(event, screens.timezone_step(zones), bot=data[BOT_KEY])
        return
    await state.update_data({TIMEZONE_KEY: chosen})
    await state.set_state(VenueWizard.window)
    await deliver(event, screens.window_step(), bot=data[BOT_KEY])


async def take_window(event: Message, **data: Any) -> None:
    """Step 4, and the whole of the writing: one call, one transaction (task 26).

    Anything the wizard cannot make sense of sends the person back a step rather than
    forward into a half-built venue: an unreadable window re-asks, and answers lost from the
    state (a deploy past the TTL, a state cleared underneath) restart the wizard at its first
    step. Neither case reaches `create()`, so there is no partial venue to roll back.
    """
    state: FSMContext = data[STATE_KEY]
    window = window_in(event.text)
    if window is None:
        await deliver(event, screens.window_step(), bot=data[BOT_KEY])
        return

    identity = data.get(IDENTITY_KEY)
    if not isinstance(identity, Identity) or identity.user is None or not identity.may_create_venue:
        await state.clear()
        await refuse(event)
        return

    remembered = await state.get_data()
    name = _remembered(remembered, NAME_KEY)
    city = _remembered(remembered, CITY_KEY)
    timezone = _remembered(remembered, TIMEZONE_KEY)
    if not (name and city and timezone):
        await state.set_state(VenueWizard.name)
        await deliver(event, screens.name_step(), bot=data[BOT_KEY])
        return

    service: VenueService = data[VENUE_WIZARD_KEY]
    start, end = window
    # The identity and not the row: the service checks `may_create_venue` itself, so the
    # guard above is the screen being honest early rather than the only thing standing
    # between `staff` and a new venue (see `VenueCreationNotAllowedError`).
    creation = await service.create(
        identity,
        name=name,
        city=city,
        timezone=timezone,
        default_shift_start=start,
        default_shift_end=end,
    )
    await state.clear()
    # Not `deliver()`: what follows the wizard is the reply keyboard of TZ 5.2, and a reply
    # keyboard cannot be attached to an edited message. Decision D3 in one line — the role
    # comes from the membership that was just written, not from `OWNER_TELEGRAM_IDS`.
    await event.answer(
        screens.created(creation.venue.name).text,
        reply_markup=main_menu(creation.owner.role),
    )


# --------------------------------------------------------------------------------------
# The settings (task 30)
# --------------------------------------------------------------------------------------


async def open_settings(event: CallbackQuery, **data: Any) -> None:
    """The settings screen of TZ 5.8: three values, three buttons."""
    actor = manager_of(data)
    if actor is None:
        await refuse(event)
        return
    configuration = await _configuration(data, actor)
    await deliver(event, screens.settings(configuration), bot=data[BOT_KEY])


async def edit_timezone(event: CallbackQuery, **data: Any) -> None:
    """The zone, from the same buttons the wizard offers (TZ 3.4).

    No role check here: the press carries `AdminCommand`, whose rule in the resolver is
    `minimum_role=MANAGER`, so a bartender never reaches this line (TZ 9).
    """
    await deliver(event, screens.settings_timezone(wizard_timezones()), bot=data[BOT_KEY])


async def take_settings_timezone(event: CallbackQuery, **data: Any) -> None:
    """A zone pressed outside the wizard: this is the settings screen, so it is saved now.

    There is no page in the state to resolve against — the settings screen is not a scenario
    and holds none — so the index means what
    :func:`~src.services.venues.timezone_at` says it means. That is safe for exactly the
    reason `WizardTimezone` is documented as append-only.

    The role is checked here because `TimezoneChoice` is `needs_actor=False` in the resolver
    — the wizard draws the same buttons before any membership exists — so this press is the
    one of the settings screen that arrives unvetted (TZ 9).
    """
    actor = manager_of(data)
    if actor is None:
        await refuse(event)
        return
    payload = data[PAYLOAD_KEY]
    chosen = timezone_at(payload.index)
    if chosen is None:
        await deliver(event, screens.settings_timezone(wizard_timezones()), bot=data[BOT_KEY])
        return
    service: VenueService = data[VENUE_WIZARD_KEY]
    configuration = await service.update_settings(actor, timezone=chosen)
    await deliver(event, screens.settings(configuration), bot=data[BOT_KEY])


async def edit_lead_minutes(event: CallbackQuery, **data: Any) -> None:
    """ "How long before the shift does the opening checklist arrive?" (TZ 5.4, TZ 6).

    The role was checked by the resolver, which is what `AdminCommand`'s rule is for; what
    is left here is the step that asks for the number.
    """
    state: FSMContext = data[STATE_KEY]
    await state.set_state(SettingsEdit.lead_minutes)
    await deliver(event, screens.settings_lead_minutes(), bot=data[BOT_KEY])


async def take_lead_minutes(event: Message, **data: Any) -> None:
    """The number that moves the push. The move is the notification service's (task 37).

    The role is checked here and cannot be anywhere else: no resolver runs on a message
    (TZ 9), so a person who stopped being a manager while the step was open is stopped by
    this line.
    """
    actor = manager_of(data)
    state: FSMContext = data[STATE_KEY]
    if actor is None:
        await state.clear()
        await refuse(event)
        return
    minutes = minutes_in(event.text)
    if minutes is None:
        await deliver(event, screens.settings_lead_minutes(), bot=data[BOT_KEY])
        return
    service: VenueService = data[VENUE_WIZARD_KEY]
    configuration = await service.update_settings(actor, opening_checklist_lead_minutes=minutes)
    await state.clear()
    await deliver(event, screens.settings(configuration), bot=data[BOT_KEY])


async def edit_window(event: CallbackQuery, **data: Any) -> None:
    """The default shift window — what a shift with no time of its own inherits (TZ 4.2).

    Vetted by the resolver through `AdminCommand`, like the two edit buttons above it.
    """
    state: FSMContext = data[STATE_KEY]
    await state.set_state(SettingsEdit.window)
    await deliver(event, screens.settings_window(), bot=data[BOT_KEY])


async def take_window_setting(event: Message, **data: Any) -> None:
    """Both times move together: half a window is not a window.

    A message step, so the role is checked here for the reason
    :func:`take_lead_minutes` gives.
    """
    actor = manager_of(data)
    state: FSMContext = data[STATE_KEY]
    if actor is None:
        await state.clear()
        await refuse(event)
        return
    window = window_in(event.text)
    if window is None:
        await deliver(event, screens.settings_window(), bot=data[BOT_KEY])
        return
    start, end = window
    service: VenueService = data[VENUE_WIZARD_KEY]
    configuration = await service.update_settings(
        actor,
        default_shift_start=start,
        default_shift_end=end,
    )
    await state.clear()
    await deliver(event, screens.settings(configuration), bot=data[BOT_KEY])


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------


async def _offered_zones(state: FSMContext) -> tuple[str, ...]:
    remembered = await state.get_data()
    stored = remembered.get(ZONES_KEY)
    if isinstance(stored, list) and stored:
        return tuple(str(zone) for zone in stored)
    return wizard_timezones()


def _remembered(remembered: dict[str, Any], key: str) -> str:
    value = remembered.get(key)
    return str(value).strip() if isinstance(value, str) else ""


async def _configuration(data: dict[str, Any], actor: AccessContext) -> VenueConfiguration:
    """The venue and its settings row, as the screen reads them.

    `update_settings()` with nothing to update is the read: it checks the rights, fetches
    both rows and returns them, and writes neither a column nor an `audit_log` record
    because the trail is built from a diff and there is none. That it *is* the read is a
    property of the service and not a promise of its name — see the report of task 30:
    `VenueService` wants a `configuration(actor)` of its own.
    """
    service: VenueService = data[VENUE_WIZARD_KEY]
    return await service.update_settings(actor)


def router() -> Router:
    """The screens of this block (see the module docstring).

    Order matters in one place: the wizard's zone handler is state-filtered and is
    registered before the settings one, which takes every other `TimezoneChoice`. Each
    factory means exactly one thing now — an `AdminCommand` is a command and a
    `TimezoneChoice` is a zone — so nothing else here can overlap.

    `VenueCreate` reaches this handler from both screens that offer it — the board of this
    section, and the "there is no venue yet" screen `src/bot/handlers/onboarding.py` shows
    the bootstrap owner of decision A3, who has no board to reach.
    """
    instance = Router(name=ROUTER_NAME)

    instance.callback_query.register(open_wizard, VenueCreate.filter())
    instance.callback_query.register(
        edit_timezone,
        AdminCommand.filter(F.action == AdminAction.VENUE_TIMEZONE),
    )
    instance.callback_query.register(
        edit_lead_minutes,
        AdminCommand.filter(F.action == AdminAction.VENUE_LEAD),
    )
    instance.callback_query.register(
        edit_window,
        AdminCommand.filter(F.action == AdminAction.VENUE_WINDOW),
    )
    # A zone: inside the wizard it answers the step, anywhere else it is the settings screen.
    instance.callback_query.register(
        take_timezone,
        TimezoneChoice.filter(),
        StateFilter(VenueWizard.timezone),
    )
    instance.callback_query.register(
        take_settings_timezone,
        TimezoneChoice.filter(),
    )
    instance.callback_query.register(
        open_settings,
        OpenAdmin.filter(F.section == AdminSection.SETTINGS),
    )

    instance.message.register(take_name, StateFilter(VenueWizard.name))
    instance.message.register(take_city, StateFilter(VenueWizard.city))
    instance.message.register(take_window, StateFilter(VenueWizard.window))
    instance.message.register(take_lead_minutes, StateFilter(SettingsEdit.lead_minutes))
    instance.message.register(take_window_setting, StateFilter(SettingsEdit.window))
    return instance


__all__ = [
    "CITY_KEY",
    "NAME_KEY",
    "ROUTER_NAME",
    "TIMEZONE_KEY",
    "ZONES_KEY",
    "edit_lead_minutes",
    "edit_timezone",
    "edit_window",
    "minutes_in",
    "open_settings",
    "open_wizard",
    "router",
    "take_city",
    "take_lead_minutes",
    "take_name",
    "take_settings_timezone",
    "take_timezone",
    "take_window",
    "take_window_setting",
    "window_in",
]
