"""Every step-by-step scenario of stage 0, as FSM state groups (TZ 3.2, 8.2).

State lives in Redis with a day's TTL (`src/bot/dispatcher.py`), so a scenario survives a
deploy in the middle of a shift and an abandoned one does not survive until tomorrow.

**All the groups are declared here, in one file, rather than next to their handlers.** Two
reasons, and both were paid for elsewhere in this project:

* a state group is a *name in the storage*. Two scenarios that happen to name a step
  `name` are two different states only because their groups differ, and a group declared
  next to its handler is a name nobody compares against the others. Here they are read in
  one screenful;
* wave 2 is written by several people at once, and this is the one file they share. A
  handler module owns its screens; it does not own the vocabulary of steps.

**Wizard steps do not count towards the two-level navigation limit** (decision D10): TZ 5.2
limits how deep the *menu* goes before a scenario starts, and TZ's own scenarios (the
write-off is six steps) would otherwise break the rule they are written in.

**Every group needs a cancel** (TZ 8.2), and gets one without asking: `keyboards.menu.wizard`
draws it on every step, and a main-menu press drops the whole scenario through
`src/bot/middlewares/menu.py`. So no group declares an "aborted" state — there is no such
step, there is only the absence of state.

What is kept in `state.get_data()` and what travels in a button is decided by
`src/bot/callbacks.py`: text goes into the state (a search query, a half-typed name),
ids go into the button. The 64-byte budget is why, and it is enforced at import.
"""

from __future__ import annotations

from typing import Final

from aiogram.fsm.state import State, StatesGroup


class Onboarding(StatesGroup):
    """TZ 5.1: turning an invite code into a membership.

    The deep link (`?start=inv_XXXX`) skips :attr:`code` — the code arrived with the
    command — and lands straight on the confirmation. Typing the code into the chat is the
    other road in, for a person who was sent the code and not the link.
    """

    #: Waiting for a code typed by hand.
    code = State()
    #: "You are recorded as Ivan Ivanov, bartender. Correct?" (decision B8: the manager set
    #: the name when issuing the code; the employee may fix it here).
    confirm = State()
    #: Waiting for the corrected name.
    name = State()


class RecipeSearch(StatesGroup):
    """TZ 5.5: the search, whose *query* has to outlive the message that carried it.

    The query is in the state and never in a button: pagination
    (`callbacks.RecipePage`) and "report that the recipe is missing"
    (`callbacks.RecipeMissing`) both need to know what was searched for, and free text in
    `callback_data` is refused by the scheme (rule 2 of `src/bot/callbacks.py`).
    """

    #: Waiting for the words to search by.
    query = State()
    #: A list of hits is on the screen; the next line typed is a new search.
    results = State()


class ChecklistSkip(StatesGroup):
    """TZ 5.4: finishing with lines unticked asks for a short reason.

    Only the comment is a step. Ticking lines is not a scenario — it is a screen that edits
    itself, and putting it in an FSM would make a tap on yesterday's message a state
    transition rather than a refusal.
    """

    #: Waiting for "why weren't they ticked?"; the run id is in the state data.
    comment = State()


class VenueWizard(StatesGroup):
    """TZ 5.8 and decision A3: the first screen a new owner sees.

    Ends by creating the venue, its settings, the owner's membership and two empty
    templates in one transaction (decision B1, plan task 26).
    """

    name = State()
    city = State()
    #: Chosen from buttons (TZ 3.4); the page of zones is in the state, the button carries
    #: an index into it (`callbacks.TimezoneChoice`).
    timezone = State()
    #: The default shift window, typed as `08:00-23:00` — two times, one line.
    window = State()


class InviteWizard(StatesGroup):
    """TZ 5.1 and 5.8: a manager issues a code for a named person (decision B8)."""

    #: The name the employee will be known by in the schedule.
    full_name = State()
    #: Chosen from buttons (`callbacks.InviteRole`).
    role = State()
    #: Optional; the wizard offers a skip.
    position = State()


class TemplateEditor(StatesGroup):
    """TZ 5.8: the checklist template editor (plan task 28, decisions B3 and B6)."""

    #: Waiting for the wording of one line.
    item_text = State()
    #: Waiting for the name of a new group, which is created together with its first line
    #: (decision D2: a group does not exist without lines).
    group_name = State()
    #: Waiting for the new name of an existing group.
    rename_group = State()
    #: Decision B6: the whole checklist in one message, one line per item, `# Name` opening
    #: a group. Forty lines in eight groups is minutes instead of an hour.
    bulk = State()


class ShiftWizard(StatesGroup):
    """TZ 5.3 and 5.8: creating a shift by hand (plan task 29)."""

    #: Chosen from buttons — active members of this venue only (`callbacks.ShiftMember`).
    person = State()
    #: A date, typed or picked; the format the prompt asks for is in `texts/`.
    date = State()
    #: `08:00-23:00`, the same shape as the venue wizard asks for.
    window = State()


class SettingsEdit(StatesGroup):
    """TZ 5.8: venue settings (plan task 30).

    The timezone is chosen from buttons and needs no step of its own; the two numbers do.
    """

    #: `opening_checklist_lead_minutes` — how long before the shift the checklist arrives.
    lead_minutes = State()
    #: The default shift window of new shifts.
    window = State()


class RecipeWizard(StatesGroup):
    """TZ 5.5 and decision B5: the minimal way to enter a recipe by hand (plan task 30a).

    Optional steps are offered with a skip rather than removed: TZ 5.5 renders a card
    without them, and an empty field is not the same as a field nobody was asked about.
    """

    name = State()
    #: Existing categories are offered as buttons; a new one is typed.
    category = State()
    glassware = State()
    method = State()
    ice = State()
    #: One ingredient per line, `name — amount`; amounts are kept as written (decision D4).
    composition = State()
    garnish = State()
    instruction = State()


#: Groups whose half-filled input is worth a question before it is dropped (TZ 5.2:
#: "asks only when there is unsaved data").
#:
#: :class:`RecipeSearch` is deliberately absent, and that absence is the whole point of the
#: set. What it keeps in the state is not input — it is where the screen is: the words
#: searched for and the page of hits, remembered because pagination and "report the recipe
#: is missing" both need them and free text may not travel in a button. Counting that as
#: unsaved data meant that after any search at all, every press of the main menu asked
#: whether to interrupt and warned that the input would be lost — about a search whose
#: answer the person had already read. A question asked when there is nothing to lose is a
#: question people learn to dismiss unread, which costs exactly the wizard this mechanism
#: exists to protect.
WIZARDS: Final[frozenset[str]] = frozenset(
    {
        ChecklistSkip.__name__,
        InviteWizard.__name__,
        Onboarding.__name__,
        RecipeWizard.__name__,
        SettingsEdit.__name__,
        ShiftWizard.__name__,
        TemplateEditor.__name__,
        VenueWizard.__name__,
    }
)


#: The one scenario a person walks through *before* they are anybody in the system (TZ 5.1).
#:
#: The gate in `src/bot/middlewares/auth.py` recognises an invite code by reading the text
#: of the update, and only the *first* message of this scenario carries one — the deep link
#: or the typed code. Every step after it holds the code in the state instead, because a
#: code may not travel in a `callback_data` and retyping it is not something to ask of
#: anybody.
#:
#: Without this set the gate let a stranger reach the confirmation screen and then refused
#: the button on it: the invited owner was shown his own name, asked to confirm it, and
#: told the action was unavailable — on that button and on every other one. The whole road
#: in of TZ 5.1 was closed to the only person it is for, and no test saw it, because
#: everybody who activated a code in testing was already a member or the bootstrap owner,
#: and for both of those the gate opens on an earlier line.
JOINING: Final[frozenset[str]] = frozenset({Onboarding.__name__})


def is_joining(raw_state: str | None) -> bool:
    """Is this update a step of somebody's way in (TZ 5.1)?

    Being here grants nothing at all. The state is aiogram's own, keyed by bot, chat and
    user, so it cannot be aimed at anybody else; and the handler behind it still hands the
    code to `AccessService.activate_invite_code`, which decides for itself whether that
    code exists, is live, and is not already spent. This only tells the gate to stop
    treating the person as a stranger with nothing to say.
    """
    if raw_state is None:
        return False
    return raw_state.split(":", 1)[0] in JOINING


def keeps_unsaved_input(raw_state: str | None) -> bool:
    """Whether dropping this scenario loses something the person typed (TZ 5.2).

    `raw_state` is aiogram's own `"Group:step"` string, which is what the interception
    middleware holds; a state of a group nobody declared above is treated as a screen and
    not as a wizard, so a new group added at stage 1 has to opt *in* to being asked about.
    That is the safe default here: an unnecessary question is noise on every press, while a
    missing one costs one form, once, and only if somebody forgot this line.
    """
    if raw_state is None:
        return False
    return raw_state.split(":", 1)[0] in WIZARDS


__all__ = [
    "JOINING",
    "WIZARDS",
    "ChecklistSkip",
    "InviteWizard",
    "Onboarding",
    "RecipeSearch",
    "RecipeWizard",
    "SettingsEdit",
    "ShiftWizard",
    "TemplateEditor",
    "VenueWizard",
    "is_joining",
    "keeps_unsaved_input",
]
