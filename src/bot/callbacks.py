"""The one scheme of `callback_data` prefixes, with a budget that cannot be overrun.

Decision D14 in one sentence: **64 bytes, and exceeding them must be impossible rather than
unlikely.** Telegram truncates anything longer, and a truncated payload does not fail — it
arrives as a different, shorter string that either matches no handler or, worse, matches the
wrong one. The checklist button alone carries a `run_id` and an `item_id` (TZ 5.4), which is
why the primary keys of this project are `BIGINT` and not UUIDs: two UUIDs plus a prefix are
72 bytes and the keyboard simply cannot be built.

Three things enforce the budget, in order of when they fire:

1. **At class creation.** Every subclass of :class:`Callback` is measured the moment it is
   declared: the worst case is computed from the *types* of its fields — 20 bytes for an
   `int` (`-9223372036854775808`), the longest member of an enum, one byte for a `bool` —
   and a factory whose worst case does not fit raises :class:`CallbackBudgetError` on
   import. The bot does not start; nobody sees a broken button. The separator is counted
   off **the class being measured**, not off :data:`SEPARATOR`: aiogram lets a subclass
   pick its own with the class keyword `sep=`, and a factory declared with a ten-byte one
   would otherwise be measured against a payload it never produces. A separator other than
   the scheme's is then refused outright (:class:`ForeignSeparatorError`) — see the
   constant.
2. **At field declaration.** `str` is refused outright (:class:`CallbackFieldTypeError`).
   A free-text field has no worst case, so accepting one would reduce rule 1 to a guess —
   and a search query or an item's wording in a callback is content in a place content does
   not belong. Text that a scenario has to remember lives in the FSM state; what travels in
   a button is an id.
3. **At pack time**, as a backstop: Python integers are unbounded, so a caller could pass
   `10**40` and beat the arithmetic of rule 1. :meth:`Callback.pack` turns that into
   :class:`CallbackBudgetError` instead of aiogram's bare `ValueError`.

`tests/bot/test_callbacks.py` walks every declared factory with a full-width `BIGINT` in
every field and asserts the packed result fits, then asserts that rules 1 and 2 actually
fire on a factory built to break them.

**Parsing happens here and nowhere else.** :func:`parse` maps a raw string to the factory
that owns its prefix; handlers receive the typed object (plan task 20 wires the resolver
that also checks the object belongs to the venue — TZ 9, criterion 11.3) and never split a
string themselves. That is a static test in plan task 35, and the reason this module exports
`parse` rather than a set of regular expressions.

Prefixes are short on purpose and unique by construction: :data:`REGISTRY` refuses a second
factory for the same prefix, because two factories sharing one would each unpack the
other's payload and the failure would be a wrong screen rather than an error.
"""

from __future__ import annotations

import enum
import types
import typing
from collections.abc import Mapping
from typing import Any, Final

from aiogram.filters.callback_data import MAX_CALLBACK_LENGTH, CallbackData

from src.db.models import MemberRole

# --------------------------------------------------------------------------------------
# The budget
# --------------------------------------------------------------------------------------

#: Telegram's own limit, taken from aiogram so the two cannot drift (decision D14).
CALLBACK_BUDGET: Final[int] = MAX_CALLBACK_LENGTH

#: The separator of the scheme, and of every factory in it. aiogram treats it as a
#: per-class setting (`class X(CallbackData, prefix="x", sep="~")`), which this module does
#: not: :func:`factory_for` finds the owner of a raw payload by splitting on this constant,
#: so a factory that packed with its own separator would build buttons that reach no
#: handler at all. The arithmetic still reads the separator off each class rather than
#: trusting this constant — a number that only holds while a rule holds is a number that
#: silently stops holding the day the rule is edited.
SEPARATOR: Final = ":"

#: A prefix is a routing key, not a name. Four bytes leave room for two full-width ids.
MAX_PREFIX_LENGTH: Final = 4

#: `-9223372036854775808` — the widest a `BIGINT` can be written. Every id in this schema
#: is one (decision D18), so this is the honest worst case rather than "ids are small now".
_INT_WIDTH: Final = max(len(str(-(2**63))), len(str(2**63 - 1)))
_BOOL_WIDTH: Final = 1


class CallbackError(RuntimeError):
    """Base class for everything this module refuses to do."""


class CallbackFieldTypeError(CallbackError):
    """A field whose worst-case width cannot be known — free text, most of the time."""

    def __init__(self, factory: str, field: str, annotation: object) -> None:
        super().__init__(
            f"{factory}.{field}: {annotation!r} cannot travel in callback_data. Only int, "
            f"bool and enums of short values have a worst case; free text has none, so the "
            f"64-byte budget could not be guaranteed (decision D14). Keep the text in the "
            f"FSM state and put an id in the button."
        )
        self.factory = factory
        self.field = field


class CallbackBudgetError(CallbackError):
    """The 64 bytes are not enough — at declaration time, or for these very values."""


class ForeignSeparatorError(CallbackError):
    """A factory declared with aiogram's `sep=`, which the one scheme does not allow."""

    def __init__(self, factory: str, separator: str) -> None:
        super().__init__(
            f"{factory} packs with {separator!r}; the separator of this scheme is "
            f"{SEPARATOR!r}. `parse` finds the factory of a payload by splitting on it, so "
            f"a button packed with anything else would reach no handler at all — and the "
            f"64-byte arithmetic would be counting a payload nobody produces."
        )
        self.factory = factory
        self.separator = separator


class DuplicatePrefixError(CallbackError):
    """Two factories claim one prefix; each would unpack the other's payload."""

    def __init__(self, prefix: str, existing: str, incoming: str) -> None:
        super().__init__(
            f"prefix {prefix!r} is already taken by {existing}, {incoming} cannot have it too"
        )
        self.prefix = prefix


class UnknownCallbackError(CallbackError):
    """A payload whose prefix no factory declares (an old keyboard, or a forged string)."""

    def __init__(self, data: str) -> None:
        super().__init__(f"no callback factory owns the prefix of {data!r}")
        self.data = data


class MalformedCallbackError(CallbackError):
    """The prefix is known but the rest does not fit the factory's shape."""

    def __init__(self, data: str) -> None:
        super().__init__(f"callback data {data!r} does not match the factory of its prefix")
        self.data = data


# --------------------------------------------------------------------------------------
# Worst-case arithmetic
# --------------------------------------------------------------------------------------


def _annotation_width(annotation: Any) -> int | None:
    """Widest byte length a value of this annotation can encode to, or None if unbounded."""
    if annotation is None or annotation is type(None):
        return 0
    origin = typing.get_origin(annotation)
    if origin in {typing.Union, types.UnionType}:
        widths = [_annotation_width(argument) for argument in typing.get_args(annotation)]
        return None if any(width is None for width in widths) else max(w or 0 for w in widths)
    if annotation is bool:
        return _BOOL_WIDTH
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return max((len(str(member.value).encode()) for member in annotation), default=0)
    # `bool` is a subclass of `int`, so it is answered above.
    if annotation is int:
        return _INT_WIDTH
    return None


def worst_case_length(factory: type[Callback]) -> int:
    """Longest payload this factory can ever produce, in bytes, prefix included.

    The separator is read off `factory` and not from :data:`SEPARATOR`, because that is
    what the factory will actually pack with: aiogram writes the class keyword `sep=` into
    `__separator__`, so a subclass can hand itself a ten-byte separator, and a budget
    computed from the module's one byte would then pass a factory whose real payloads are
    ninety. :class:`ForeignSeparatorError` forbids the trick a second time; this function
    is what makes the *number* honest for any class it is handed.
    """
    separator = len(factory.__separator__.encode())
    total = len(factory.__prefix__.encode())
    for name, field in factory.model_fields.items():
        width = _annotation_width(field.annotation)
        if width is None:
            raise CallbackFieldTypeError(factory.__name__, name, field.annotation)
        total += separator + width
    return total


# --------------------------------------------------------------------------------------
# The base class
# --------------------------------------------------------------------------------------

_REGISTRY: dict[str, type[Callback]] = {}

#: Prefix -> factory. Read-only view; membership is claimed by declaring a subclass.
REGISTRY: Mapping[str, type[Callback]] = types.MappingProxyType(_REGISTRY)


class Callback(CallbackData, prefix="bp"):
    """Base of every callback factory of the bot.

    Subclassing is the whole registration mechanism: a factory declared anywhere in the bot
    layer is measured against the budget and entered into :data:`REGISTRY` by the act of
    being declared, so a screen added at stage 1 cannot quietly opt out of either.

    The prefix of this class itself is never packed — it has no fields and nothing builds
    it; only its subclasses appear in a keyboard.
    """

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        prefix = cls.__prefix__
        if not prefix or len(prefix) > MAX_PREFIX_LENGTH:
            raise CallbackBudgetError(
                f"{cls.__name__}: prefix {prefix!r} must be 1..{MAX_PREFIX_LENGTH} characters; "
                f"a prefix is a routing key and every byte of it is a byte an id cannot use"
            )
        for name, field in cls.model_fields.items():
            width = _annotation_width(field.annotation)
            if width is None:
                raise CallbackFieldTypeError(cls.__name__, name, field.annotation)
        # Measured before the separator is vetted, so that a class that widened it is told
        # the truth about its own bytes rather than a number computed from a separator it
        # does not use.
        longest = worst_case_length(cls)
        if longest > CALLBACK_BUDGET:
            raise CallbackBudgetError(
                f"{cls.__name__} can produce {longest} bytes of callback_data, the budget is "
                f"{CALLBACK_BUDGET} (decision D14). Telegram truncates the rest, and a "
                f"truncated payload routes somewhere else instead of failing. Carry fewer "
                f"ids in the button, or shorten the prefix {prefix!r}."
            )
        if cls.__separator__ != SEPARATOR:
            raise ForeignSeparatorError(cls.__name__, cls.__separator__)
        taken = _REGISTRY.get(prefix)
        if taken is not None:
            raise DuplicatePrefixError(prefix, taken.__name__, cls.__name__)
        _REGISTRY[prefix] = cls

    def pack(self) -> str:
        """The payload, or :class:`CallbackBudgetError`.

        Rule 1 above proves the declared *shape* fits; this catches the value that beats the
        arithmetic anyway — a Python `int` is unbounded, and an id from somewhere other than
        this schema could be wider than a `BIGINT`.
        """
        try:
            return super().pack()
        except ValueError as error:
            raise CallbackBudgetError(f"{type(self).__name__}: {error}") from error


def factory_for(data: str) -> type[Callback]:
    """The factory owning the prefix of a raw payload."""
    prefix = data.split(SEPARATOR, 1)[0]
    factory = _REGISTRY.get(prefix)
    if factory is None:
        raise UnknownCallbackError(data)
    return factory


def parse(data: str) -> Callback:
    """Turn a raw `callback_data` string into the object it stands for.

    The only place in the project that takes a callback payload apart. A handler that needs
    to know which button was pressed either filters on a factory (`Factory.filter()`) or
    receives the parsed object from the resolver of plan task 20; splitting the string by
    hand is checked for by the static test of plan task 35.

    Note what this function does *not* do: it does not check that the object named by the
    payload belongs to the caller's venue. That is a server-side check on live data (TZ 9,
    criterion 11.3) and it lives in the resolver, not in a parser.
    """
    factory = factory_for(data)
    try:
        return factory.unpack(data)
    except (TypeError, ValueError) as error:
        raise MalformedCallbackError(data) from error


def registered() -> tuple[type[Callback], ...]:
    """Every declared factory, in declaration order."""
    return tuple(_REGISTRY.values())


# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------


class MemberField(enum.StrEnum):
    """Which typed field of an employee's card is being corrected (TZ 5.8)."""

    #: Decision B8: the manager owns the full name, the employee only confirms it.
    FULL_NAME = "n"
    POSITION = "p"


class MenuAction(enum.StrEnum):
    """The sections of TZ 5.2, as one vocabulary for both keyboards.

    The main menu is a reply keyboard matched by caption (TZ 5.2 wants it permanent), while
    inline buttons name a section through :class:`OpenSection`; both resolve to a member of
    this enum, so the router of plan task 20 has one thing to switch on.

    The values are two-letter codes rather than words because they travel in
    `callback_data`, where every byte is budgeted. They are never shown to anybody: the
    captions live in `src/bot/texts/menu.py`.
    """

    MY_SHIFT = "sh"
    SCHEDULE = "sc"
    CATALOGUE = "tt"
    KNOWLEDGE = "kb"
    WRITEOFF = "wo"
    ORDER = "od"
    MANAGEMENT = "mg"


class AdminSection(enum.StrEnum):
    """The blocks of the management section that stage 0 implements (TZ 5.8)."""

    STAFF = "st"
    SCHEDULE = "sc"
    CHECKLISTS = "cl"
    CATALOGUE = "tt"
    SETTINGS = "se"


class NavTarget(enum.StrEnum):
    """The three buttons TZ 5.2 and 8.2 require of every submenu and every wizard."""

    BACK = "b"
    HOME = "h"
    CANCEL = "c"


# --------------------------------------------------------------------------------------
# Factories
# --------------------------------------------------------------------------------------


class Nav(Callback, prefix="nv"):
    """Back, home and cancel — the three buttons TZ 5.2 and 8.2 require.

    **Back carries its destination, the way :class:`OpenSection` does.** A callback query
    arrives with the press and nothing else: aiogram tells a handler which button was
    tapped, never which screen drew it, and there is no screen stack in this framework —
    the FSM state holds the scenario a wizard is in the middle of (`src/bot/states/`), not
    a trail of visited screens, and under TZ 8.2 the screen is *edited in place*, so a
    button tapped an hour later is still live and a trail would long have gone stale. The
    destination is therefore either in the payload or invented at handling time, and
    inventing it puts the bartender on a screen they did not come from — the wrong-screen
    failure the whole of decision D14 exists to prevent.

    One field is enough, and that is decision D10: navigation is at most two levels deep
    before a scenario starts. From a second-level screen back leads to the section it
    belongs to — a :class:`MenuAction`, the same vocabulary :class:`OpenSection` speaks —
    and from a first-level screen it leads to the main menu, which is what `section=None`
    says. Those two cases are the whole of it; `None` is a statement ("this screen sits one
    step from the menu", the `back=False` case of `src/bot/keyboards/menu.py`), not a
    default waiting to be filled in.

    A parent that needs ids (the checklist of a run, a page of a list) is *not* spelled
    here: it is reached through its own factory under the back caption, which is why this
    field is an enum of sections and not a second, parallel routing vocabulary.

    `HOME` and `CANCEL` take no destination and never will: home is the main menu by
    definition, and cancel drops a half-filled scenario (TZ 8.2's own wording).
    """

    target: NavTarget
    #: Where `BACK` returns to; `None` is the main menu. Carries nothing for `HOME` and
    #: `CANCEL`, which have one destination each.
    section: MenuAction | None = None


class OpenSection(Callback, prefix="sn"):
    """Open a section of the main menu from an inline button."""

    section: MenuAction


class MenuKeep(Callback, prefix="mk"):
    """The negative answer to the interrupt question (TZ 5.2, plan task 20).

    A main-menu press on top of a half-filled wizard asks before dropping it; the positive
    answer is an ordinary :class:`OpenSection`, which the interrupting router of
    `src/bot/routers.py` treats as the confirmation and which the section handler then
    serves as usual. This is the other button, and it carries nothing: the scenario it
    returns to is the one already in the FSM state.
    """


class OpenAdmin(Callback, prefix="ad"):
    """Open a block of the management section (TZ 5.8)."""

    section: AdminSection


class ChecklistShow(Callback, prefix="co"):
    """Re-open the current checklist from the shift screen (TZ 5.4: the message was swiped)."""

    run_id: int


class ChecklistGroup(Callback, prefix="cg"):
    """Switch the markers to another group without leaving the message (decision D11)."""

    run_id: int
    group_index: int


class ChecklistToggle(Callback, prefix="ct"):
    """`[ ]` -> `[✓]` on one line — the button decision D14 exists for."""

    run_id: int
    item_id: int


class ChecklistFinish(Callback, prefix="cf"):
    """The finish button of a checklist (TZ 5.4)."""

    run_id: int


class ChecklistSkipAccept(Callback, prefix="cs"):
    """Send it as it is, after the list of what was left unticked (TZ 5.4)."""

    run_id: int


class RecipeShow(Callback, prefix="rs"):
    """A card, from a list of hits or from a composition line (TZ 5.5)."""

    recipe_id: int


class RecipePage(Callback, prefix="rp"):
    """Pagination of the hit list. The query itself stays in the FSM state, never here."""

    offset: int


class RecipeMissing(Callback, prefix="rm"):
    """Report that a recipe is missing (TZ 5.5). What was searched for is in the state."""


class MemberShow(Callback, prefix="ms"):
    """One employee's card in the staff list (TZ 5.8)."""

    member_id: int


class MemberActive(Callback, prefix="ma"):
    """Deactivate or bring back; the history stays either way (TZ 5.1)."""

    member_id: int
    is_active: bool


class MemberSetRole(Callback, prefix="mr"):
    """A new role for an employee (TZ 5.8 asks for role and position to be changeable).

    The role travels in the button rather than in the state, because this is a choice from
    a closed set and not something typed — rule 1 of this module. Worst case is a two-byte
    prefix, a member id and the longest value of :class:`MemberRole`, well inside budget.
    """

    member_id: int
    role: MemberRole


class MemberEdit(Callback, prefix="me"):
    """Start typing a new name or position for an employee (decision B8, TZ 5.8).

    One factory for both because what follows is the same shape — a step that waits for a
    line of text — and the field is what the step differs by. A separate factory each would
    be two prefixes and two rules for one screen.
    """

    member_id: int
    field: MemberField


class MemberRebind(Callback, prefix="mb"):
    """Issue a code that points this card at another Telegram account (TZ 5.1)."""

    member_id: int


class InviteRole(Callback, prefix="ir"):
    """The role an invite code carries (TZ 5.1: code, venue, role and position)."""

    role: MemberRole


class InviteRevoke(Callback, prefix="iv"):
    """Withdraw a code that has not been used yet."""

    code_id: int


class InviteConfirm(Callback, prefix="ic"):
    """Yes / "correct the name" under the name a code carries (TZ 5.1, decision B8).

    One factory with a flag rather than two: the two buttons are the two answers to one
    question, and answers declared apart are answers one of which is forgotten when the
    question is reworded.

    It names no row, and cannot. The code the answer belongs to is a *string* typed by
    somebody the bot has never seen, so it lives in the FSM state where rule 2 of this
    module's docstring puts every string a scenario has to remember; the venue it points at
    is not settled until `AccessService.activate_invite_code` has read it, which is also
    the check this button ultimately passes through.
    """

    #: `True` is «the manager wrote it down correctly»; `False` opens `Onboarding.name`.
    is_correct: bool


class ShiftShow(Callback, prefix="sw"):
    """One shift in the manager's schedule (TZ 5.3, 5.8)."""

    shift_id: int


class ShiftOpener(Callback, prefix="so"):
    """Assign or clear the opener of a shift (TZ 4.2: one per date)."""

    shift_id: int
    is_set: bool


class ShiftCloser(Callback, prefix="sl"):
    """Assign or clear the closer of a shift (TZ 4.2: one per date)."""

    shift_id: int
    is_set: bool


class ShiftDelete(Callback, prefix="sd"):
    """Delete a shift; the scheduler cancels what hung off it (TZ 6, plan task 33)."""

    shift_id: int


class ShiftMember(Callback, prefix="sm"):
    """Pick who works the shift, from the active members of this venue only (TZ 5.8)."""

    member_id: int


class EditorLine(Callback, prefix="el"):
    """One line of a checklist template in the editor (TZ 5.8)."""

    item_id: int


class EditorLineCritical(Callback, prefix="ec"):
    """Mark a line critical, or stop marking it (TZ 5.4: `is_critical` escalates)."""

    item_id: int
    is_set: bool


class EditorLinePhoto(Callback, prefix="ep"):
    """Require a photo on a line, or stop requiring it (TZ 5.4)."""

    item_id: int
    is_set: bool


class EditorLineDelete(Callback, prefix="ed"):
    """Remove a line from the template (decision B3 decides whether that forks a version)."""

    item_id: int


class EditorGroup(Callback, prefix="eg"):
    """A group of the template: open it, or rename it (TZ 5.4: groups have names)."""

    template_id: int
    group_index: int


class VenueSelect(Callback, prefix="vs"):
    """Choose the active venue when somebody works in two (TZ 5.1)."""

    venue_id: int


class VenueCreate(Callback, prefix="vc"):
    """Open the "create a venue" wizard from the onboarding screen (decision A3).

    The one button of this bot that is pressed from *outside* every venue, which is why it
    is neither an :class:`OpenSection` nor an :class:`OpenAdmin`: both of those are read as
    "a member of this venue asks for one of its screens", and the bootstrap owner of
    decision A3 is a member of nowhere at the moment he presses this. There is therefore
    nothing to scope, nothing to check against a membership and no id worth carrying — the
    wizard's first question is the venue's name, and the venue does not exist yet.
    """


class TimezoneChoice(Callback, prefix="tz"):
    """A zone from the list the wizard is showing (TZ 3.4, plan task 26).

    An index into the page held in the FSM state, not the IANA name: «Europe/Moscow» is a
    string, and rule 2 of the module docstring refuses those for a reason that this field
    illustrates — the widest zone name in the database is more than half the budget.
    """

    index: int


class AdminAction(enum.StrEnum):
    """A press of the management section that names no row of any table.

    These share one factory because they share one rule: every action here is a manager's
    (TZ 2), so the resolver checks the role once, and a screen added at stage 1 inherits
    the check by declaring its action instead of by remembering to ask.

    Two-letter codes because they travel in `callback_data`; nobody ever sees them.
    """

    #: Venue settings (plan task 30), one per editable field.
    VENUE_TIMEZONE = "vt"
    VENUE_LEAD = "vl"
    VENUE_WINDOW = "vw"
    #: Start the shift wizard (plan task 29).
    SHIFT_NEW = "sn"
    #: Start the recipe form (plan task 30a, decision B5).
    RECIPE_NEW = "rn"
    #: Leave an optional step of a wizard empty — TZ 5.5 draws a card without any of them.
    STEP_SKIP = "sk"


class AdminCommand(Callback, prefix="am"):
    """One press of TZ 5.8 that carries an intent and no id.

    **This factory exists because the alternative was tried and measured.** Three blocks of
    the management section needed a payload for "start this wizard", "edit this setting",
    "skip this step"; the scheme declared none, and each block took a slice of the negative
    half of :class:`TimezoneChoice`'s index. That cost two things.

    The resolver's rule for `TimezoneChoice` is `needs_actor=False` — the venue wizard runs
    before any membership exists — so every one of those presses arrived **unchecked**, and
    each handler had to verify the role by hand. That is precisely the arrangement
    `src/bot/middlewares/resolver.py` was written to abolish: nine handler modules will be
    written, and if each checks for itself, eight will get it right.

    And the slices were a namespace agreed in comments between modules written in parallel,
    where two blocks picking one number route one block's button into the other's handler —
    the wrong-screen failure decision D14 exists to prevent.
    """

    action: AdminAction


class RecipeCategory(Callback, prefix="rk"):
    """A category from the page the recipe form is showing (decision B5).

    An index into the page held in the FSM state, not the category itself: a category is a
    word the venue typed, and rule 2 of this module's docstring keeps words out of buttons.
    Same shape and same reason as :class:`TimezoneChoice`.
    """

    index: int


class EditorAction(enum.StrEnum):
    """What a press of the checklist editor does (TZ 5.8, plan task 28)."""

    #: Back to the whole checklist, with no group singled out.
    TEMPLATE = "t"
    #: One more line at the end of the group in `target`.
    ADD = "a"
    #: A new group, created together with its first line (decision D2).
    NEW_GROUP = "n"
    #: Rename the group in `target`.
    RENAME_GROUP = "g"
    #: Reword the line in `target`.
    REWORD = "w"
    #: Swap the line in `target` with the one above it.
    MOVE_UP = "u"
    #: The whole checklist in one message (decision B6).
    BULK = "b"


class EditorCommand(Callback, prefix="ek"):
    """A press of the template editor: what to do, and to what.

    `template_id` is not decoration — it is the venue anchor, so the resolver fetches the
    template through *this* venue's repository and refuses another venue's before any
    handler runs (TZ 9). `target` is a group index or a `checklist_items` id depending on
    `action`, and `0` where the action names neither; which of the two it is belongs to the
    action and to the service, not to this scheme.

    This replaces an arithmetic encoding that packed the same three facts into the negative
    half of `EditorGroup.group_index`. That encoding was correct, and even kept the venue
    check, and was still the wrong shape: a field named `group_index` that sometimes means
    "command five applied to item 91" is a field every future reader has to be warned about,
    and `EditorGroup` is drawn by more than one screen.
    """

    action: EditorAction
    template_id: int
    #: A group index, a `checklist_items` id, or `0`. See the class docstring.
    target: int = 0


__all__ = [
    "CALLBACK_BUDGET",
    "MAX_PREFIX_LENGTH",
    "REGISTRY",
    "SEPARATOR",
    "AdminAction",
    "AdminCommand",
    "AdminSection",
    "Callback",
    "CallbackBudgetError",
    "CallbackError",
    "CallbackFieldTypeError",
    "ChecklistFinish",
    "ChecklistGroup",
    "ChecklistShow",
    "ChecklistSkipAccept",
    "ChecklistToggle",
    "DuplicatePrefixError",
    "EditorAction",
    "EditorCommand",
    "EditorGroup",
    "EditorLine",
    "EditorLineCritical",
    "EditorLineDelete",
    "EditorLinePhoto",
    "ForeignSeparatorError",
    "InviteConfirm",
    "InviteRevoke",
    "InviteRole",
    "MalformedCallbackError",
    "MemberActive",
    "MemberEdit",
    "MemberField",
    "MemberRebind",
    "MemberSetRole",
    "MemberShow",
    "MenuAction",
    "MenuKeep",
    "Nav",
    "NavTarget",
    "OpenAdmin",
    "OpenSection",
    "RecipeCategory",
    "RecipeMissing",
    "RecipePage",
    "RecipeShow",
    "ShiftCloser",
    "ShiftDelete",
    "ShiftMember",
    "ShiftOpener",
    "ShiftShow",
    "TimezoneChoice",
    "UnknownCallbackError",
    "VenueCreate",
    "VenueSelect",
    "factory_for",
    "parse",
    "registered",
    "worst_case_length",
]
