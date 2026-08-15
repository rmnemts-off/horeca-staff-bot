"""The one place a `callback_data` becomes an object (TZ 9, plan task 20).

TZ 9 requires protection from somebody else's `callback_data`: check that the presser has a
right to this object, and that the object belongs to their venue. Nine handler modules will
be written for stage 0 and stage 1. If each one checks for itself, eight will get it right.

So the check is not in the handlers at all. Before a callback handler is called, this
middleware:

1. **parses** the payload through `src/bot/callbacks.py` — the only parser in the project,
   which is why a handler never sees `callback.data` and never splits a string;
2. **finds the rule** the factory declares in :data:`RULES`. A factory with no rule is a
   refusal, not a default: `test_every_callback_factory_has_a_rule` fails the build the day
   somebody adds a button and forgets the rule, which is the whole point of a single
   resolver;
3. **checks the caller's rights** — the minimum role of the rule, and, for an object that
   belongs to a person, `require_self_or_manager` (TZ 2: staff sees its own, a manager sees
   everyone's);
4. **fetches the object through a venue-scoped repository.** The venue is not in the
   payload and cannot be: it comes from the actor's membership (`src/bot/middlewares/venue.py`),
   so a row of another venue is not "rejected", it is invisible — `runs.get(id)` of a
   foreign run returns `None` exactly as a deleted one does;
5. **re-checks the venue on the row it got back**, along the path the rule declares
   (:class:`OwnColumn` / :class:`ViaParent`) rather than by looking for an attribute and
   hoping. That is the second line of defence, and a declaration that the schema
   contradicts stops the import, not the check;
6. **answers a refusal itself**, with the same wording whichever of the above failed. A
   missing row and a row of the bar next door read the same to the presser, so a forged id
   cannot be used to find out what exists (TZ 9).

The handler is then called with `data["callback_payload"]` (the typed object) and
`data["subject"]` (the row), both already checked. `tests/bot/test_handler_boundary.py`
keeps it that way.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

from src.bot import texts
from src.bot.callbacks import (
    AdminCommand,
    Callback,
    CallbackError,
    ChecklistFinish,
    ChecklistGroup,
    ChecklistShow,
    ChecklistSkipAccept,
    ChecklistToggle,
    EditorCommand,
    EditorGroup,
    EditorLine,
    EditorLineCritical,
    EditorLineDelete,
    EditorLinePhoto,
    InviteConfirm,
    InviteRevoke,
    InviteRole,
    MalformedCallbackError,
    MemberActive,
    MemberEdit,
    MemberSetRole,
    MemberShow,
    MenuKeep,
    Nav,
    OpenAdmin,
    OpenSection,
    RecipeCategory,
    RecipeMissing,
    RecipePage,
    RecipeShow,
    ShiftCloser,
    ShiftDelete,
    ShiftMember,
    ShiftOpener,
    ShiftShow,
    TimezoneChoice,
    VenueCreate,
    VenueSelect,
    parse,
)
from src.bot.middlewares.auth import ACTOR_KEY
from src.bot.middlewares.services import SERVICES_KEY, VenueServices
from src.db.models import (
    ChecklistItem,
    ChecklistRun,
    ChecklistTemplate,
    MemberRole,
    Recipe,
    Shift,
    VenueMember,
)
from src.db.models.base import Base
from src.db.repositories.base import CHILD_PARENTS, LIBRARY_TABLES
from src.logging import get_logger
from src.services.access import (
    AccessContext,
    AccessError,
    require_role,
    require_self_or_manager,
    require_venue,
)

#: Handler-context key of the parsed, checked callback object.
PAYLOAD_KEY: Final = "callback_payload"

#: Handler-context key of the row the callback names, or `None` when it names none.
SUBJECT_KEY: Final = "subject"

#: Tells "the attribute is not there" from "the attribute is there and is `None`". The
#: difference is the whole of decision D9: a `None` venue is the shared BarPoint library,
#: a *missing* venue means the row is a child and has to be checked through its parent.
_UNSET: Final = object()

logger = get_logger("bot.resolver")

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]
Owner = Callable[[Any], int]


class RowLookup(Protocol):
    """One row of one table, by id — with whatever predicate the repository carries.

    Narrower than the repository protocols on purpose. Fetching the object a button names
    is the only thing this module does with the data layer, and asking for exactly that
    keeps `VenueRepositories` (which satisfies this structurally) from being the one shape
    a test can build: the venue predicate is what these tests exist to exercise, and it is
    the repository's, not the resolver's.

    The parameter is positional-only because the real repositories name it after their own
    table (`run_id`, `recipe_id`, `member_id`), which is right for them and irrelevant here.
    """

    async def get(self, row_id: int, /) -> Any: ...


class SubjectRepositories(Protocol):
    """The venue-scoped repositories a callback subject can be fetched through.

    Every attribute is the repository of the *actor's* venue: the venue middleware built
    them around `actor.venue_id`, so a row of another venue is not visible through any of
    them (TZ 3.3).
    """

    @property
    def runs(self) -> RowLookup: ...

    @property
    def recipes(self) -> RowLookup: ...

    @property
    def members(self) -> RowLookup: ...

    @property
    def shifts(self) -> RowLookup: ...

    @property
    def items(self) -> RowLookup: ...

    @property
    def templates(self) -> RowLookup: ...


Loader = Callable[[SubjectRepositories, int], Awaitable[Any]]


class Refusal(enum.StrEnum):
    """Why a callback did not reach its handler."""

    #: A prefix no factory owns: an old keyboard, or a string somebody made up.
    UNKNOWN = "unknown"
    #: The prefix is known, the rest is not the shape the factory declares.
    MALFORMED = "malformed"
    #: The factory exists but nobody wrote a rule for it (a bug, refused loudly).
    UNREGISTERED = "unregistered"
    #: No membership behind the press, so nothing can be scoped or checked.
    NO_CONTEXT = "no_context"
    #: The role is below what the rule needs, or the object is another venue's.
    FORBIDDEN = "forbidden"
    #: No such row for this venue — deleted, or never theirs to begin with.
    MISSING = "missing"


#: What the presser is told. Only two answers exist on purpose: "the screen is stale" for a
#: payload that cannot be read at all, and one flat "not available" for everything else, so
#: that trying ids cannot tell an existing row from a missing one (TZ 9).
REFUSAL_TEXTS: Final[Mapping[Refusal, str]] = {
    Refusal.UNKNOWN: texts.ERROR_OUTDATED_SCREEN,
    Refusal.MALFORMED: texts.ERROR_OUTDATED_SCREEN,
    Refusal.UNREGISTERED: texts.ERROR_SOMETHING_WENT_WRONG,
    Refusal.NO_CONTEXT: texts.ERROR_NOT_ALLOWED,
    Refusal.FORBIDDEN: texts.ERROR_NOT_ALLOWED,
    Refusal.MISSING: texts.ERROR_NOT_ALLOWED,
}


# --------------------------------------------------------------------------------------
# Loaders: every one of them reads through a repository scoped to the actor's venue
# --------------------------------------------------------------------------------------


async def load_run(repositories: SubjectRepositories, run_id: int) -> Any:
    return await repositories.runs.get(run_id)


async def load_recipe(repositories: SubjectRepositories, recipe_id: int) -> Any:
    return await repositories.recipes.get(recipe_id)


async def load_member(repositories: SubjectRepositories, member_id: int) -> Any:
    return await repositories.members.get(member_id)


async def load_shift(repositories: SubjectRepositories, shift_id: int) -> Any:
    return await repositories.shifts.get(shift_id)


async def load_item(repositories: SubjectRepositories, item_id: int) -> Any:
    return await repositories.items.get(item_id)


async def load_template(repositories: SubjectRepositories, template_id: int) -> Any:
    return await repositories.templates.get(template_id)


# --------------------------------------------------------------------------------------
# How one gets from a row to a venue. Declared, never guessed
# --------------------------------------------------------------------------------------


class SubjectContractError(RuntimeError):
    """A row is not shaped the way the rule that fetched it says it is.

    Raised while :data:`RULES` is being built — that is, at import, before a single update
    is served — when a venue path contradicts the schema, and at the moment of a check when
    a row turns out not to carry what its path names. Both used to be the same silent
    "attribute not found, skip the check" (TZ 3.3, acceptance 11.3).
    """


@dataclass(frozen=True, slots=True)
class OwnColumn:
    """The row carries the venue itself: `venue_id` is a column of its own table.

    `library` is the whitelist of TZ 3.3 (`recipes`, `knowledge_articles`), where
    `venue_id IS NULL` is the shared BarPoint library and every venue may read it. On any
    other table a `NULL` is a broken row and not a pass.
    """

    library: bool = False


@dataclass(frozen=True, slots=True)
class ViaParent:
    """The row has no `venue_id` at all and is reached through its parent (decision D9).

    `checklist_items`, `checklist_run_items`, `recipe_ingredients` and `order_items` are all
    of this shape: `getattr(row, "venue_id", None)` on any of them answers "no such
    attribute", which is not the same fact as "this row is in the shared library" — and
    reading the two as one is how the check below came to be skipped for every child table
    in the schema.

    `field` is the foreign key on the child row and `load` fetches the parent through the
    repository of the actor's venue, so both halves are real: the parent has to be visible
    at all (the repository's join), and the parent's own venue has to be the actor's.
    """

    field: str
    #: The parent's table. Declared rather than inferred from `load`, so that a loader for
    #: the wrong table is caught — at import against `CHILD_PARENTS`, and again on the row
    #: that comes back. Otherwise `ViaParent("template_id", ..., load_run)` would quietly
    #: check *a run with the template's id*, which for some ids is a row that exists.
    model: type[Base]
    load: Loader
    #: The parent's own way to a venue, spelled out because a parent may itself be a
    #: library row: `recipe_ingredients` hangs off `recipes`, where `venue_id IS NULL` is
    #: legal and readable from everywhere.
    venue: OwnColumn = OwnColumn()


#: Every shape a row's venue can take. A subject has to name one — there is no default, so
#: a new entity declared without a path is a `TypeError` while this module is imported.
VenuePath = OwnColumn | ViaParent


def _check_venue_path(model: type[Base], path: VenuePath) -> None:
    """Refuse a venue path the schema contradicts, at import time.

    `CHILD_PARENTS` (`src/db/repositories/base.py`) is the machine-readable form of decision
    D9, and the repositories are checked against it the same way. So "this row has no
    `venue_id`" is answered by the schema here, not by an attribute lookup at request time.
    """
    tablename: str = model.__tablename__
    child = CHILD_PARENTS.get(tablename)
    if isinstance(path, ViaParent):
        if child is None:
            raise SubjectContractError(
                f"{tablename!r} owns venue_id and is nobody's child: declare OwnColumn()"
            )
        parent_table, parent_fk = child
        if (path.model.__tablename__, path.field) != (parent_table, parent_fk):
            raise SubjectContractError(
                f"{tablename!r} is a child of {parent_table!r} through {parent_fk!r}, not of "
                f"{path.model.__tablename__!r} through {path.field!r} (D9, CHILD_PARENTS)"
            )
        # The parent has a venue of its own, and the same rules apply to it.
        _check_venue_path(path.model, path.venue)
        return
    if child is not None:
        raise SubjectContractError(
            f"{tablename!r} carries no venue_id (decision D9): declare "
            f"ViaParent({child[1]!r}, load_...) so the venue is read off {child[0]!r}"
        )
    if "venue_id" not in model.__table__.c:
        raise SubjectContractError(f"{tablename!r} has no venue_id column to check")
    if path.library and tablename not in LIBRARY_TABLES:
        raise SubjectContractError(
            f"{tablename!r} is not part of the shared library; allowed: {sorted(LIBRARY_TABLES)}"
        )


@dataclass(frozen=True, slots=True)
class Subject:
    """The row a callback names: its table, which field holds its id, how it is fetched —
    and how one gets from it to a venue.

    `venue` is required and is checked against `model` on construction: a table whose venue
    lives on a parent cannot be declared as carrying its own, and vice versa. The point is
    that "no `venue_id` here" becomes a statement made in the diff and verified against the
    schema, instead of an `AttributeError` swallowed at request time.
    """

    model: type[Base]
    field: str
    load: Loader
    venue: VenuePath
    #: Reads the user a row belongs to, when it belongs to one. TZ 2: `staff` may act on
    #: its own checklist and nobody else's; a manager may act on anyone's.
    owner: Owner | None = None

    def __post_init__(self) -> None:
        _check_venue_path(self.model, self.venue)


@dataclass(frozen=True, slots=True)
class Rule:
    """What has to be true before a handler of this factory runs."""

    minimum_role: MemberRole = MemberRole.STAFF
    subject: Subject | None = None
    #: `False` only where there cannot be a membership yet (TZ 5.1, decision A3).
    needs_actor: bool = True
    #: Why a factory that carries an id has no subject. Prose, and prose grants nothing —
    #: it is here so the omission is a decision in the diff rather than an oversight.
    note: str = ""


def _run_owner(run: Any) -> int:
    return int(run.user_id)


#: `checklist_runs`, `venue_members`, `shifts` and `checklist_templates` all carry venue_id.
OWN: Final = OwnColumn()
#: `recipes` is one of the two library tables of TZ 3.3: `venue_id IS NULL` is BarPoint's.
LIBRARY: Final = OwnColumn(library=True)
#: `checklist_items` has no venue of its own (decision D9) — its template has one.
VIA_TEMPLATE: Final = ViaParent("template_id", ChecklistTemplate, load_template)


#: Every factory of `src/bot/callbacks.py`, and what it takes to press it.
RULES: Final[Mapping[type[Callback], Rule]] = {
    # -- navigation, no object ---------------------------------------------------------
    Nav: Rule(needs_actor=False),
    MenuKeep: Rule(needs_actor=False),
    OpenSection: Rule(),
    OpenAdmin: Rule(minimum_role=MemberRole.MANAGER),
    # -- the checklist (TZ 5.4). The run is the venue-scoped anchor; the item of a toggle
    # is checked against the run by the service, which is where that rule belongs.
    ChecklistShow: Rule(subject=Subject(ChecklistRun, "run_id", load_run, OWN, owner=_run_owner)),
    ChecklistGroup: Rule(subject=Subject(ChecklistRun, "run_id", load_run, OWN, owner=_run_owner)),
    ChecklistToggle: Rule(subject=Subject(ChecklistRun, "run_id", load_run, OWN, owner=_run_owner)),
    ChecklistFinish: Rule(subject=Subject(ChecklistRun, "run_id", load_run, OWN, owner=_run_owner)),
    ChecklistSkipAccept: Rule(
        subject=Subject(ChecklistRun, "run_id", load_run, OWN, owner=_run_owner)
    ),
    # -- recipes (TZ 5.5) --------------------------------------------------------------
    RecipeShow: Rule(subject=Subject(Recipe, "recipe_id", load_recipe, LIBRARY)),
    RecipePage: Rule(),
    RecipeMissing: Rule(),
    # -- the roster (TZ 5.8) -----------------------------------------------------------
    MemberShow: Rule(
        minimum_role=MemberRole.MANAGER,
        subject=Subject(VenueMember, "member_id", load_member, OWN),
    ),
    MemberActive: Rule(
        minimum_role=MemberRole.MANAGER,
        subject=Subject(VenueMember, "member_id", load_member, OWN),
    ),
    # TZ 2 reserves handing out `manager` and `owner` to the owner, and the service refuses
    # anything else this rule lets through — a manager reaching for a manager, or anybody
    # reaching for themselves. The rule is the cheap half of the check, not the whole of it.
    MemberSetRole: Rule(
        minimum_role=MemberRole.MANAGER,
        subject=Subject(VenueMember, "member_id", load_member, OWN),
    ),
    MemberEdit: Rule(
        minimum_role=MemberRole.MANAGER,
        subject=Subject(VenueMember, "member_id", load_member, OWN),
    ),
    ShiftMember: Rule(
        minimum_role=MemberRole.MANAGER,
        subject=Subject(VenueMember, "member_id", load_member, OWN),
    ),
    InviteRole: Rule(minimum_role=MemberRole.MANAGER),
    InviteRevoke: Rule(
        minimum_role=MemberRole.MANAGER,
        note=(
            "the invite repository has no lookup by id (it is addressed by the code a "
            "person types), so the row is fetched by the access service, which is built "
            "for the actor's venue and refuses a code of any other"
        ),
    ),
    # -- the schedule (TZ 5.3, 5.8) ----------------------------------------------------
    ShiftShow: Rule(
        minimum_role=MemberRole.MANAGER, subject=Subject(Shift, "shift_id", load_shift, OWN)
    ),
    ShiftOpener: Rule(
        minimum_role=MemberRole.MANAGER, subject=Subject(Shift, "shift_id", load_shift, OWN)
    ),
    ShiftCloser: Rule(
        minimum_role=MemberRole.MANAGER, subject=Subject(Shift, "shift_id", load_shift, OWN)
    ),
    ShiftDelete: Rule(
        minimum_role=MemberRole.MANAGER, subject=Subject(Shift, "shift_id", load_shift, OWN)
    ),
    # -- the checklist editor (TZ 5.8). A line has no venue_id: it is its template's (D9).
    EditorLine: Rule(
        minimum_role=MemberRole.MANAGER,
        subject=Subject(ChecklistItem, "item_id", load_item, VIA_TEMPLATE),
    ),
    EditorLineCritical: Rule(
        minimum_role=MemberRole.MANAGER,
        subject=Subject(ChecklistItem, "item_id", load_item, VIA_TEMPLATE),
    ),
    EditorLinePhoto: Rule(
        minimum_role=MemberRole.MANAGER,
        subject=Subject(ChecklistItem, "item_id", load_item, VIA_TEMPLATE),
    ),
    EditorLineDelete: Rule(
        minimum_role=MemberRole.MANAGER,
        subject=Subject(ChecklistItem, "item_id", load_item, VIA_TEMPLATE),
    ),
    EditorGroup: Rule(
        minimum_role=MemberRole.MANAGER,
        subject=Subject(ChecklistTemplate, "template_id", load_template, OWN),
    ),
    # Everything the editor does that is not "open this group": the template is the venue
    # anchor, so the row is fetched through this venue's repository like any other subject.
    EditorCommand: Rule(
        minimum_role=MemberRole.MANAGER,
        subject=Subject(ChecklistTemplate, "template_id", load_template, OWN),
    ),
    # -- management presses that name no row (TZ 5.8) -----------------------------------
    AdminCommand: Rule(
        minimum_role=MemberRole.MANAGER,
        note=(
            "starts a wizard or opens a settings field; there is no row until the wizard "
            "finishes, and the venue is the actor's by construction. The role is the whole "
            "check, and it is here rather than in each of the four handlers that draw these "
            "buttons -- see the class docstring for what the previous arrangement cost"
        ),
    ),
    RecipeCategory: Rule(
        minimum_role=MemberRole.MANAGER,
        note="an index into the page of categories held in the FSM state, not a row",
    ),
    # -- before there is a venue to scope anything to (TZ 5.1, decision A3) ------------
    InviteConfirm: Rule(
        needs_actor=False,
        note=(
            "the answer to «is this your name?», pressed by somebody the bot has never "
            "seen: TZ 5.1 lets an invite code make a membership, so requiring one here "
            "would refuse every first-time employee. Nothing is granted by the press — "
            "the code itself is in the FSM state and AccessService.activate_invite_code "
            "re-reads it, so an expired, revoked or spent code dies there as usual"
        ),
    ),
    VenueCreate: Rule(
        needs_actor=False,
        note=(
            "decision A3: the wizard runs before the first venue exists, so there is no "
            "membership to check and nothing venue-scoped to fetch. The gate in "
            "src/bot/middlewares/auth.py is what lets such an update through at all, and "
            "it does so only for a telegram_id that OWNER_TELEGRAM_IDS names and that "
            "belongs to no venue (Identity.may_create_venue)"
        ),
    ),
    VenueSelect: Rule(
        needs_actor=False,
        note=(
            "choosing between venues happens before one is chosen, so it cannot be scoped "
            "to one; AccessService.select_venue refuses a venue the person is not an "
            "active member of"
        ),
    ),
    TimezoneChoice: Rule(
        needs_actor=False,
        note="an index into the page held in the FSM state, not a row of any table",
    ),
}


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome: either a checked payload (and its row), or why not."""

    payload: Callback | None = None
    subject: Any | None = None
    refusal: Refusal | None = None

    @property
    def is_allowed(self) -> bool:
        return self.refusal is None

    @property
    def text(self) -> str | None:
        return None if self.refusal is None else REFUSAL_TEXTS[self.refusal]


async def resolve(
    raw: str,
    *,
    actor: AccessContext | None,
    repositories: SubjectRepositories | None,
) -> Resolution:
    """Turn a raw payload into a checked object, or into the reason it is refused.

    Separate from the middleware so it can be exercised against real repositories with two
    venues in the same table — which is the only way to prove the venue predicate is doing
    anything (CLAUDE.md, acceptance 11.3).
    """
    try:
        payload = parse(raw)
    except MalformedCallbackError:
        return Resolution(refusal=Refusal.MALFORMED)
    except CallbackError:
        return Resolution(refusal=Refusal.UNKNOWN)

    rule = RULES.get(type(payload))
    if rule is None:
        logger.error(
            "callback factory has no rule in the resolver",
            extra={"event_type": type(payload).__name__},
        )
        return Resolution(refusal=Refusal.UNREGISTERED)

    if not rule.needs_actor and rule.subject is None:
        return Resolution(payload=payload)

    if actor is None:
        return Resolution(refusal=Refusal.NO_CONTEXT)

    try:
        require_role(actor, rule.minimum_role)
    except AccessError:
        return Resolution(refusal=Refusal.FORBIDDEN)

    if rule.subject is None:
        return Resolution(payload=payload)

    if repositories is None:
        return Resolution(refusal=Refusal.NO_CONTEXT)

    subject_id = getattr(payload, rule.subject.field)
    row = await rule.subject.load(repositories, subject_id)
    if row is None:
        # Missing, or another venue's — indistinguishable on purpose (TZ 9).
        return Resolution(refusal=Refusal.MISSING)

    refusal = await _check_ownership(actor, row, rule.subject, repositories)
    if refusal is not None:
        return Resolution(refusal=refusal)

    return Resolution(payload=payload, subject=row)


async def _check_ownership(
    actor: AccessContext,
    row: Any,
    subject: Subject,
    repositories: SubjectRepositories,
) -> Refusal | None:
    """Second line of defence, and the personal-data rule of TZ 2.

    The venue check is redundant while every repository filters — and that is exactly why
    it is here: the day one does not, this is what fails instead of a bartender reading the
    bar next door's schedule (acceptance 11.3). It is worth only as much as the path it
    follows, which is why the path is declared per subject and verified against the schema
    at import: reading `venue_id` off a child row answers "no such attribute" for every
    child table in the database, and that answer used to be treated as "the shared library,
    let it through".

    Returns the refusal, or `None` when the row may be served. The presser cannot tell this
    from a missing row: both refusals carry the same text (TZ 9).
    """
    try:
        if not await _in_actors_venue(actor, row, subject.venue, repositories):
            return Refusal.FORBIDDEN
        if subject.owner is not None:
            require_self_or_manager(actor, subject.owner(row))
    except AccessError:
        return Refusal.FORBIDDEN
    return None


async def _in_actors_venue(
    actor: AccessContext,
    row: Any,
    path: VenuePath,
    repositories: SubjectRepositories,
) -> bool:
    """Walk the declared path from a row to a venue and compare it with the actor's.

    `False` means the row cannot be reached from the actor's venue at all — its parent is
    another venue's, or a `NULL` venue on a table that has no library. A venue that *is*
    known and is not the actor's raises through `require_venue`, so that the one place a
    venue comparison is decided stays `src/services/access.py`. Both answers end up as the
    same refusal one frame up.
    """
    if isinstance(path, ViaParent):
        parent_id: Any = getattr(row, path.field, _UNSET)
        if parent_id is _UNSET or parent_id is None:
            raise SubjectContractError(
                f"{type(row).__name__} reaches its venue through {path.field!r} and has no "
                "such value: a child row with no parent cannot be checked against a venue"
            )
        parent = await path.load(repositories, int(parent_id))
        if parent is None:
            # The parent is not visible here, so neither is the child (decision D9).
            return False
        if not isinstance(parent, path.model):
            raise SubjectContractError(
                f"the venue of a {type(row).__name__} is on {path.model.__name__}, and the "
                f"loader answered with {type(parent).__name__}"
            )
        return await _in_actors_venue(actor, parent, path.venue, repositories)

    venue_id: Any = getattr(row, "venue_id", _UNSET)
    if venue_id is _UNSET:
        raise SubjectContractError(
            f"{type(row).__name__} was declared as carrying venue_id and does not; a child "
            "row is reached with ViaParent (decision D9)"
        )
    if venue_id is None:
        return path.library
    require_venue(actor, int(venue_id))
    return True


class CallbackResolverMiddleware(BaseMiddleware):
    """Outer middleware of `Dispatcher.callback_query`; see the module docstring."""

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)

        # The one read of a raw payload in the whole project.
        raw = event.data
        if raw is None:
            # A game callback, or a button with no data at all. Nothing to route on.
            await event.answer(texts.ERROR_OUTDATED_SCREEN)
            return None

        services: VenueServices | None = data.get(SERVICES_KEY)
        resolution = await resolve(
            raw,
            actor=data.get(ACTOR_KEY),
            repositories=None if services is None else services.repositories,
        )
        if not resolution.is_allowed:
            logger.info(
                "callback refused",
                extra={"outcome_status": str(resolution.refusal)},
            )
            await event.answer(resolution.text, show_alert=True)
            return None

        data[PAYLOAD_KEY] = resolution.payload
        data[SUBJECT_KEY] = resolution.subject
        return await handler(event, data)


def setup(dispatcher: Any) -> CallbackResolverMiddleware:
    """Register the resolver on callback queries, after the update-level middlewares."""
    middleware = CallbackResolverMiddleware()
    dispatcher.callback_query.outer_middleware(middleware)
    return middleware


__all__ = [
    "LIBRARY",
    "OWN",
    "PAYLOAD_KEY",
    "REFUSAL_TEXTS",
    "RULES",
    "SUBJECT_KEY",
    "VIA_TEMPLATE",
    "CallbackResolverMiddleware",
    "OwnColumn",
    "Refusal",
    "Resolution",
    "RowLookup",
    "Rule",
    "Subject",
    "SubjectContractError",
    "SubjectRepositories",
    "VenuePath",
    "ViaParent",
    "load_item",
    "load_member",
    "load_recipe",
    "load_run",
    "load_shift",
    "load_template",
    "resolve",
    "setup",
]
