"""Who the user is, what they may do, and how they get in (plan, task 15).

Three things live here and nowhere else.

**Identity and rights.** TZ 2: the role is stored in the database and bound to the pair
``telegram_id + venue_id``. :meth:`AccessService.resolve` turns a Telegram id into an
:class:`Identity` — the person, every venue they are an active member of, and the venue
that is currently selected. Everything downstream takes an :class:`AccessContext`, which is
one membership: user, venue and role together. The guard functions at the bottom
(:func:`require_role`, :func:`require_venue`, …) are the only place a permission is
decided, so that a check cannot drift between handlers (TZ 9: rights are verified on the
server on every action, not by hiding buttons).

**The bootstrap owner (decision A3).** ``OWNER_TELEGRAM_IDS`` is a one-off trigger and
never a source of rights: it only unlocks the "create a venue" wizard, and only while the
person is a member of no venue at all. The wizard writes a real ``venue_members`` row, and
from that moment the environment variable is irrelevant — rights are read from the database
exactly as TZ 2 requires.

**Invite codes (TZ 5.1, decision B8).** Single use, seven days, bound to venue, role and
position; the manager types the full name when issuing the code and the employee confirms
or corrects it on activation. Issuing, previewing, activating and revoking are all here.

Separately and deliberately: :meth:`AccessService.manager_recipients` is the *single*
implementation of answer A4 — "a notification to the manager" means every active member
with role ``manager``, and the owners when the venue has no manager. It is one function on
purpose, so the rule cannot end up spelled differently in five callers.

Two notes on the shape of this module:

* it never calls ``datetime.now()`` (decision D12). Every method that needs the current
  moment takes it as an argument, timezone-aware and in UTC, which is also what makes the
  seven-day expiry testable without freezing a clock;
* venue-scoped repositories are created for one venue and carry it in the object, so the
  service receives *factories* for them (:class:`AccessRepositories`). It has to: at the
  moment an invite code is typed there is no venue context yet — resolving one is precisely
  what the code is for.

Where the provider ends and the contract begins
-----------------------------------------------

There are two shapes of service in this package and the line between them is deliberate,
not an accident of who wrote which file first.

**The normal shape — a venue-scoped repository, straight from**
``src/db/repositories/protocols.py``. ``checklists``, ``recipes``, ``shifts`` and
``notifications`` are constructed *after* the middleware has already put a ``venue_id`` in
the context, so the venue is settled before the service exists and the repository it holds
is scoped to it once and for all (TZ 3.3). That is the contract every new service should
follow.

**The exception — a provider of factories**, which is this module and
``src/services/members.py``, for two different reasons:

* :class:`AccessService` runs *before* a venue is known. ``resolve`` reads the global
  ``users`` table from a bare ``telegram_id`` and answers with every venue the person
  belongs to; ``preview_invite_code`` and ``activate_invite_code`` are handed a string typed
  by somebody the bot has never seen, and the venue only appears once the code is parsed;
  :meth:`AccessService.manager_recipients` is asked *about* a venue by callers standing
  outside it (the scheduler, the notification service). None of that can be given a scoped
  repository at construction, because working out the scope is the job.
* :class:`~src.services.members.MemberService` derives its scope from the actor on every
  call — ``self.repositories.members(actor.venue_id)`` — rather than from construction. For
  the roster that is a safety property, not a convenience: the repository scope and the
  actor's venue are the same value by construction, so a forged ``callback_data`` carrying
  a membership id from another venue cannot be served even by a service instance that was
  built for the wrong venue (TZ 9, acceptance 11.3).

A factory provider is therefore a justified exception with two members, not an alternative
style. A service that knows its venue up front takes the scoped repository.
"""

from __future__ import annotations

import datetime as dt
import enum
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from src.db.models import InviteCode, MemberRole, User, VenueMember
from src.db.repositories.protocols import (
    InviteCodeRepository,
    UserRepository,
    VenueMemberRepository,
    VenueRepository,
)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: TZ 5.1: "the code is single use, lives 7 days, bound to venue, role and position".
INVITE_CODE_TTL = dt.timedelta(days=7)

#: Length of the random part of a code. Eight characters out of the 32-symbol alphabet
#: below is 40 bits — far beyond guessing for a code that also expires in a week.
INVITE_CODE_SECRET_LENGTH = 8

#: How many times a fresh secret is drawn before giving up on a collision.
INVITE_CODE_ATTEMPTS = 5

#: Payload prefix of the deep link `t.me/<bot>?start=inv_<code>` (TZ 5.1).
DEEPLINK_PREFIX = "inv_"

#: Ambiguous glyphs (I/L/O/0/1) are left out: the code is read aloud and retyped by hand.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

#: Separator between the venue id and the secret inside a code, see `parse_invite_code`.
_CODE_SEPARATOR = "-"

#: TZ 2: `owner` includes everything `manager` may do, `manager` everything `staff` may do.
_ROLE_RANK = MappingProxyType(
    {
        MemberRole.STAFF: 0,
        MemberRole.MANAGER: 1,
        MemberRole.OWNER: 2,
    }
)


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class AccessError(RuntimeError):
    """Base class for everything this module refuses to do."""


class PermissionDeniedError(AccessError):
    """The actor's role is below what the action needs (TZ 2)."""

    def __init__(self, role: MemberRole, required: MemberRole) -> None:
        super().__init__(f"role {role.value!r} is below the required {required.value!r}")
        self.role = role
        self.required = required


class VenueMismatchError(AccessError):
    """The object belongs to another venue (TZ 3.3, acceptance 11.3).

    Raised where a forged `callback_data` would otherwise reach across venues: the actor is
    scoped to one venue, and an id from another one dies here rather than in the repository.
    """

    def __init__(self, actor_venue_id: int, venue_id: int) -> None:
        super().__init__(
            f"the actor works in venue {actor_venue_id}, the object belongs to {venue_id}"
        )
        self.actor_venue_id = actor_venue_id
        self.venue_id = venue_id


class NaiveMomentError(AccessError):
    """A moment arrived without a timezone (decision D12: every instant is UTC)."""

    def __init__(self, moment: dt.datetime) -> None:
        super().__init__(
            f"{moment!r} carries no timezone; instants are timezone-aware UTC, local wall "
            "clock time is converted in src/services/timezones.py"
        )
        self.moment = moment


class InviteCodeGenerationError(AccessError):
    """Several fresh secrets in a row collided with an existing code."""


class UnknownMembershipError(AccessError):
    """The user is not an active member of the venue they asked to work in."""

    def __init__(self, user_id: int, venue_id: int) -> None:
        super().__init__(f"user {user_id} is not an active member of venue {venue_id}")
        self.user_id = user_id
        self.venue_id = venue_id


# --------------------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccessContext:
    """One membership: the person, the venue and the role, resolved together.

    Every service method takes this instead of a bare user id, so that "which venue" and
    "with what rights" cannot be answered from two different places.
    """

    user_id: int
    telegram_id: int
    venue_id: int
    member_id: int
    role: MemberRole
    full_name: str
    position: str | None = None

    @property
    def is_manager(self) -> bool:
        """True for `manager` and `owner` (TZ 2: owner includes every manager right)."""
        return _ROLE_RANK[self.role] >= _ROLE_RANK[MemberRole.MANAGER]

    @property
    def is_owner(self) -> bool:
        return self.role == MemberRole.OWNER


@dataclass(frozen=True, slots=True)
class Identity:
    """Everything `/start` needs to decide what to show (TZ 5.1)."""

    user: User | None
    contexts: tuple[AccessContext, ...]
    active: AccessContext | None
    may_create_venue: bool

    @property
    def is_known(self) -> bool:
        """False for an unregistered `telegram_id`, which gets nothing but the code prompt."""
        return self.user is not None

    @property
    def needs_venue_choice(self) -> bool:
        """TZ 5.1: a person working in several venues picks one, and it is remembered."""
        return self.active is None and len(self.contexts) > 1


class InviteRejection(enum.StrEnum):
    """Why a code did not work. The wording for each lives in `src/bot/texts/`."""

    MALFORMED = "malformed"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    REVOKED = "revoked"
    ALREADY_USED = "already_used"


@dataclass(frozen=True, slots=True)
class InvitePreview:
    """What a code promises, without consuming it (TZ 5.1: confirm name and position)."""

    venue_id: int
    code_id: int
    role: MemberRole
    position: str | None
    suggested_full_name: str | None
    rejection: InviteRejection | None = None

    @property
    def is_valid(self) -> bool:
        return self.rejection is None


@dataclass(frozen=True, slots=True)
class InviteActivation:
    """Outcome of typing a code. `rejection` is `None` exactly when the person is in.

    `was_already_member` answers "were you working here a second ago?", which is a question
    about *access*, not about rows. TZ 5.1 keeps the `venue_members` row of a dismissed
    employee forever so their checklists and write-offs stay in the reports — that surviving
    row is history, not membership. So a returning employee whose row is reactivated is a
    return (`False`), and only somebody whose row was already active gets `True` and the
    "you are already in" wording.
    """

    rejection: InviteRejection | None = None
    user: User | None = None
    member: VenueMember | None = None
    venue_id: int | None = None
    was_already_member: bool = False

    @property
    def is_activated(self) -> bool:
        return self.rejection is None


# --------------------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------------------


class AccessRepositories(Protocol):
    """The data this service reaches for.

    `users` and `venues` are global — a person exists before any venue does, and listing
    the venues of a user is a cross-venue question by nature. `members` and `invites` are
    venue-scoped, so they arrive as factories: the venue is part of the repository object
    (TZ 3.3), and this service is the code that works out *which* venue in the first place.
    """

    @property
    def users(self) -> UserRepository: ...

    @property
    def venues(self) -> VenueRepository: ...

    def members(self, venue_id: int) -> VenueMemberRepository: ...

    def invites(self, venue_id: int) -> InviteCodeRepository: ...


# --------------------------------------------------------------------------------------
# Permission guards — the only place a right is decided
# --------------------------------------------------------------------------------------


def role_rank(role: MemberRole) -> int:
    """Position of a role in the `staff < manager < owner` order of TZ 2."""
    return _ROLE_RANK[role]


def require_role(actor: AccessContext, minimum: MemberRole) -> None:
    """Refuse an actor whose role is below `minimum` (TZ 2, TZ 9)."""
    if _ROLE_RANK[actor.role] < _ROLE_RANK[minimum]:
        raise PermissionDeniedError(actor.role, minimum)


def require_manager(actor: AccessContext) -> None:
    """Editing the schedule, the checklists, the nomenclature and the roster (TZ 2)."""
    require_role(actor, MemberRole.MANAGER)


def require_owner(actor: AccessContext) -> None:
    """Creating venues and managers is the owner's alone (TZ 2)."""
    require_role(actor, MemberRole.OWNER)


def require_venue(actor: AccessContext, venue_id: int) -> None:
    """Refuse an object from another venue (TZ 3.3, TZ 9, acceptance 11.3)."""
    if actor.venue_id != venue_id:
        raise VenueMismatchError(actor.venue_id, venue_id)


def require_self_or_manager(actor: AccessContext, user_id: int) -> None:
    """TZ 2: `staff` sees only its own data; a manager sees everyone in the venue."""
    if actor.user_id != user_id:
        require_manager(actor)


def as_utc(moment: dt.datetime) -> dt.datetime:
    """Every instant crossing this layer is timezone-aware and stored as UTC (D12)."""
    if moment.tzinfo is None:
        raise NaiveMomentError(moment)
    return moment.astimezone(dt.UTC)


# --------------------------------------------------------------------------------------
# Invite code format
# --------------------------------------------------------------------------------------


def new_invite_secret(length: int = INVITE_CODE_SECRET_LENGTH) -> str:
    """Random part of a code, drawn from an alphabet without ambiguous glyphs."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def format_invite_code(venue_id: int, secret: str) -> str:
    """`<venue id>-<secret>`.

    The venue travels inside the code on purpose. An invite is typed by a person the bot
    has never seen, so there is no venue context to build a repository from — and every
    repository is venue-scoped by construction (TZ 3.3). Carrying the id in the code is
    what lets the lookup stay scoped: a secret paired with the wrong venue simply finds
    nothing, because `get_by_code` still filters by the venue of its repository.
    """
    return f"{venue_id}{_CODE_SEPARATOR}{secret}"


def parse_invite_code(raw: str) -> tuple[int, str] | None:
    """`(venue id, canonical code)` for anything a person could plausibly type.

    Accepts the deep-link payload (`inv_12-A7K9QX4M`), the bare code, either case and
    surrounding whitespace. Returns `None` when the input cannot be a code at all.
    """
    candidate = raw.strip().upper()
    prefix = DEEPLINK_PREFIX.upper()
    if candidate.startswith(prefix):
        candidate = candidate[len(prefix) :]
    head, separator, secret = candidate.partition(_CODE_SEPARATOR)
    if not separator or not secret or not head.isdigit():
        return None
    venue_id = int(head)
    if venue_id <= 0 or any(symbol not in _CODE_ALPHABET for symbol in secret):
        return None
    return venue_id, format_invite_code(venue_id, secret)


def invite_deeplink_payload(code: str) -> str:
    """The `?start=` payload for a code; the full URL is built in the bot layer."""
    return f"{DEEPLINK_PREFIX}{code}"


# --------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------


class AccessService:
    """Identity, rights and invite codes (plan, task 15)."""

    def __init__(
        self,
        repositories: AccessRepositories,
        *,
        bootstrap_owner_ids: Iterable[int] = (),
    ) -> None:
        self.repositories = repositories
        #: Decision A3: a one-off trigger for the "create a venue" wizard, never a right.
        self.bootstrap_owner_ids = frozenset(bootstrap_owner_ids)

    # -- identity -----------------------------------------------------------------------

    async def resolve(self, telegram_id: int) -> Identity:
        """Turn a Telegram id into the person, their venues and the selected one.

        An unknown id gets an empty identity: TZ 5.1 says it is offered a code and nothing
        else, so there is nothing else to compute for it.
        """
        user = await self.repositories.users.get_by_telegram_id(telegram_id)
        if user is None or not user.is_active:
            return Identity(
                user=None,
                contexts=(),
                active=None,
                may_create_venue=telegram_id in self.bootstrap_owner_ids,
            )

        contexts = await self._contexts_for(user)
        return Identity(
            user=user,
            contexts=contexts,
            active=self._pick_active(user, contexts),
            # Decision A3: the trigger is spent as soon as the person belongs anywhere.
            may_create_venue=telegram_id in self.bootstrap_owner_ids and not contexts,
        )

    async def context_for(self, telegram_id: int, venue_id: int) -> AccessContext | None:
        """The membership of one person in one venue, or `None` if there is none."""
        identity = await self.resolve(telegram_id)
        return next(
            (context for context in identity.contexts if context.venue_id == venue_id),
            None,
        )

    async def select_venue(self, user_id: int, venue_id: int) -> AccessContext:
        """Remember the venue a multi-venue employee picked (TZ 5.1).

        Stored on `users.active_venue_id` rather than in the FSM: the choice has to outlive
        both a restart and the TTL of the Redis store.
        """
        member = await self.repositories.members(venue_id).get_for_user(user_id)
        if member is None or not member.is_active:
            raise UnknownMembershipError(user_id, venue_id)
        user = await self.repositories.users.get(user_id)
        if user is None:
            raise UnknownMembershipError(user_id, venue_id)
        await self.repositories.users.set_active_venue(user_id, venue_id)
        return _context(user, member)

    async def venue_names(self, venue_ids: Iterable[int]) -> Mapping[int, str]:
        """Venue id -> name, for the screens that run before a venue is chosen (TZ 5.1).

        Onboarding says the venue's name out loud three times — on the invite preview, in
        the list somebody working in two places picks from and in the line that confirms the
        membership — and every one of those screens is drawn while ``data["venue"]`` is still
        empty, because the
        venue context is built from a membership that does not exist yet
        (``src/bot/middlewares/venue.py``). A handler may not open a repository itself
        (TZ 3.2), so the lookup lives here, next to the other question that has to be
        answered before a venue is known.

        An id with no row is simply absent from the result rather than an error: the caller
        is a screen, and a venue that was deleted between the code being issued and the link
        being followed is a case it has to render either way. Activity is deliberately not
        judged here — whether a switched-off venue may still be entered is a rule of TZ 5.1
        and not of a name lookup.
        """
        names: dict[int, str] = {}
        for venue_id in dict.fromkeys(venue_ids):
            venue = await self.repositories.venues.get(venue_id)
            if venue is not None:
                names[venue_id] = venue.name
        return names

    async def _contexts_for(self, user: User) -> tuple[AccessContext, ...]:
        found: list[AccessContext] = []
        for venue in await self.repositories.venues.list_for_user(user.id):
            if not venue.is_active:
                continue
            member = await self.repositories.members(venue.id).get_for_user(user.id)
            if member is None or not member.is_active:
                continue
            found.append(_context(user, member))
        return tuple(sorted(found, key=lambda context: context.venue_id))

    @staticmethod
    def _pick_active(user: User, contexts: tuple[AccessContext, ...]) -> AccessContext | None:
        remembered = user.active_venue_id
        if remembered is not None:
            for context in contexts:
                if context.venue_id == remembered:
                    return context
        return contexts[0] if len(contexts) == 1 else None

    # -- notification recipients (answer A4) --------------------------------------------

    async def manager_recipients(self, venue_id: int) -> tuple[VenueMember, ...]:
        """Who "a notification to the manager" means — answer A4, in one place.

        Every active member with role `manager`; when the venue has none, the owners. This
        function is the whole rule: section 6 of the TZ addresses four notification types
        to "the manager", and the plan makes the single implementation an explicit
        requirement so the definition cannot drift between callers.
        """
        members = await self.repositories.members(venue_id).list_active()
        active = [member for member in members if member.is_active]
        managers = tuple(member for member in active if member.role == MemberRole.MANAGER)
        if managers:
            return managers
        return tuple(member for member in active if member.role == MemberRole.OWNER)

    async def manager_recipient_ids(self, venue_id: int) -> tuple[int, ...]:
        """User ids of :meth:`manager_recipients`, for the notification queue."""
        return tuple(member.user_id for member in await self.manager_recipients(venue_id))

    # -- invite codes -------------------------------------------------------------------

    async def issue_invite_code(
        self,
        actor: AccessContext,
        *,
        role: MemberRole,
        now: dt.datetime,
        position: str | None = None,
        full_name: str | None = None,
    ) -> InviteCode:
        """Hand out a single-use code that lives seven days (TZ 5.1, decision B8).

        The full name is set by the manager here; the employee sees it on activation and
        may correct it. TZ 2 reserves the creation of managers to the owner, so a code
        with a role above `staff` needs one.
        """
        require_manager(actor)
        if _ROLE_RANK[role] >= _ROLE_RANK[MemberRole.MANAGER]:
            require_owner(actor)

        moment = as_utc(now)
        invites = self.repositories.invites(actor.venue_id)
        for _ in range(INVITE_CODE_ATTEMPTS):
            code = format_invite_code(actor.venue_id, new_invite_secret())
            if await invites.get_by_code(code) is not None:
                continue
            return await invites.create(
                code=code,
                role=role,
                expires_at=moment + INVITE_CODE_TTL,
                position=position,
                full_name=full_name,
                created_by=actor.user_id,
            )
        raise InviteCodeGenerationError(
            f"could not draw a free invite code in {INVITE_CODE_ATTEMPTS} attempts"
        )

    async def list_pending_invite_codes(self, actor: AccessContext) -> Sequence[InviteCode]:
        """Codes of the venue that are still waiting to be used."""
        require_manager(actor)
        return await self.repositories.invites(actor.venue_id).list_pending()

    async def revoke_invite_code(
        self,
        actor: AccessContext,
        code_id: int,
        *,
        now: dt.datetime,
    ) -> InviteCode | None:
        """Withdraw a code that has not been used yet.

        Revocation is recorded in `revoked_at` rather than by moving `expires_at`: the two
        are different facts, and overwriting the expiry would lose the reason a code stopped
        working.
        """
        require_manager(actor)
        return await self.repositories.invites(actor.venue_id).revoke(code_id, as_utc(now))

    async def preview_invite_code(self, raw_code: str, *, now: dt.datetime) -> InvitePreview:
        """Validate a code without consuming it (TZ 5.1: confirm the name and position)."""
        moment = as_utc(now)
        parsed = parse_invite_code(raw_code)
        if parsed is None:
            return _rejected_preview(InviteRejection.MALFORMED)
        venue_id, code = parsed
        found = await self.repositories.invites(venue_id).get_by_code(code)
        if found is None:
            return _rejected_preview(InviteRejection.UNKNOWN)
        rejection = _invite_rejection(found, moment)
        return InvitePreview(
            venue_id=venue_id,
            code_id=found.id,
            role=found.role,
            position=found.position,
            suggested_full_name=found.full_name,
            rejection=rejection,
        )

    async def activate_invite_code(
        self,
        raw_code: str,
        *,
        telegram_id: int,
        full_name: str,
        now: dt.datetime,
        username: str | None = None,
    ) -> InviteActivation:
        """Consume a code and put the person into the venue (TZ 5.1).

        The name is passed in, not taken from the Telegram profile: profiles hold
        nicknames, and the schedule import matches employees by full name (TZ 5.3,
        decision B8). Re-entering a code that has already been used is refused — that is
        what "single use" means — and a person who is already a member keeps their row,
        because deactivation must never lose history (TZ 5.1).
        """
        moment = as_utc(now)
        parsed = parse_invite_code(raw_code)
        if parsed is None:
            return InviteActivation(rejection=InviteRejection.MALFORMED)
        venue_id, code = parsed

        invites = self.repositories.invites(venue_id)
        found = await invites.get_by_code(code)
        if found is None:
            return InviteActivation(rejection=InviteRejection.UNKNOWN)
        rejection = _invite_rejection(found, moment)
        if rejection is not None:
            return InviteActivation(rejection=rejection, venue_id=venue_id)

        user = await self._upsert_user(
            telegram_id=telegram_id,
            full_name=full_name or (found.full_name or ""),
            username=username,
        )
        members = self.repositories.members(venue_id)
        existing = await members.get_for_user(user.id)
        if existing is None:
            member = await members.add(
                user_id=user.id,
                role=found.role,
                position=found.position,
            )
            was_member = False
        else:
            # Read before writing: `update` mutates the very row `existing` points at (the
            # identity map hands back one object per id), so asking afterwards would always
            # answer "active" — the value this line just wrote.
            was_member = existing.is_active
            # TZ 5.1: a returning employee keeps the row that carries their history.
            updated = await members.update(
                existing.id,
                role=found.role,
                position=found.position,
                is_active=True,
            )
            member = updated if updated is not None else existing

        await invites.mark_used(found.id, used_by=user.id, used_at=moment)
        if user.active_venue_id is None:
            await self.repositories.users.set_active_venue(user.id, venue_id)

        return InviteActivation(
            user=user,
            member=member,
            venue_id=venue_id,
            was_already_member=was_member,
        )

    async def _upsert_user(
        self,
        *,
        telegram_id: int,
        full_name: str,
        username: str | None,
    ) -> User:
        users = self.repositories.users
        existing = await users.get_by_telegram_id(telegram_id)
        if existing is None:
            return await users.create(
                telegram_id=telegram_id,
                full_name=full_name,
                username=username,
            )
        fields: dict[str, object] = {}
        if full_name and full_name != existing.full_name:
            fields["full_name"] = full_name
        if username is not None and username != existing.username:
            fields["username"] = username
        if not existing.is_active:
            fields["is_active"] = True
        if not fields:
            return existing
        updated = await users.update(existing.id, **fields)
        return updated if updated is not None else existing


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _context(user: User, member: VenueMember) -> AccessContext:
    return AccessContext(
        user_id=user.id,
        telegram_id=user.telegram_id,
        venue_id=member.venue_id,
        member_id=member.id,
        role=member.role,
        full_name=user.full_name,
        position=member.position,
    )


def _invite_rejection(code: InviteCode, moment: dt.datetime) -> InviteRejection | None:
    if code.revoked_at is not None:
        return InviteRejection.REVOKED
    if code.used_at is not None or code.used_by is not None:
        return InviteRejection.ALREADY_USED
    if as_utc(code.expires_at) <= moment:
        return InviteRejection.EXPIRED
    return None


def _rejected_preview(rejection: InviteRejection) -> InvitePreview:
    return InvitePreview(
        venue_id=0,
        code_id=0,
        role=MemberRole.STAFF,
        position=None,
        suggested_full_name=None,
        rejection=rejection,
    )


__all__ = [
    "DEEPLINK_PREFIX",
    "INVITE_CODE_ATTEMPTS",
    "INVITE_CODE_SECRET_LENGTH",
    "INVITE_CODE_TTL",
    "AccessContext",
    "AccessError",
    "AccessRepositories",
    "AccessService",
    "Identity",
    "InviteActivation",
    "InviteCodeGenerationError",
    "InvitePreview",
    "InviteRejection",
    "NaiveMomentError",
    "PermissionDeniedError",
    "UnknownMembershipError",
    "VenueMismatchError",
    "as_utc",
    "format_invite_code",
    "invite_deeplink_payload",
    "new_invite_secret",
    "parse_invite_code",
    "require_manager",
    "require_owner",
    "require_role",
    "require_self_or_manager",
    "require_venue",
    "role_rank",
]
