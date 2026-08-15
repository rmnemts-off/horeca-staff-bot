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

from src.db.models import InviteCode, InvitePurpose, MemberRole, User, VenueMember
from src.db.repositories.protocols import (
    InviteCodeRepository,
    UserRepository,
    VenueMemberRepository,
    VenueRepository,
)
from src.services.audit import AuditAction, AuditEntity, AuditSink, AuditTrail, snapshot

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


class BootstrapNotAllowedError(AccessError):
    """Somebody who is not in `OWNER_TELEGRAM_IDS` asked to be made the first owner."""

    def __init__(self, telegram_id: int) -> None:
        super().__init__(
            f"telegram id {telegram_id} is not a bootstrap owner (decision A3), so no "
            f"users row is created for it"
        )
        self.telegram_id = telegram_id


class InviteCodeGenerationError(AccessError):
    """Several fresh secrets in a row collided with an existing code."""


class SelfTargetError(AccessError):
    """The actor is the target of their own rights operation (TZ 2).

    Its own class rather than a `PermissionDeniedError`, because the refusal is not about
    rank: the owner has every right there is and is refused precisely because of that.
    """

    def __init__(self, member_id: int) -> None:
        super().__init__(f"member {member_id} is the actor themselves")
        self.member_id = member_id


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
    #: The person typing it already works here. Unlike every other member of this enum the
    #: code survives: it was never meant for them, so consuming it would destroy an invite
    #: somebody else is waiting for. This is not hypothetical — an owner opened his own
    #: link to check that it worked, the single use was spent on him, and the colleague he
    #: had already forwarded it to was told the code had been used.
    ALREADY_MEMBER = "already_member"
    #: A `rebind` code typed by an account that already belongs to somebody else. The code
    #: survives: the manager has to decide whose card this is, and issuing a fresh one would
    #: not answer that question.
    ACCOUNT_TAKEN = "account_taken"
    #: The card a `rebind` code points at is gone or switched off.
    CARD_UNAVAILABLE = "card_unavailable"


@dataclass(frozen=True, slots=True)
class InvitePreview:
    """What a code promises, without consuming it (TZ 5.1: confirm name and position)."""

    venue_id: int
    code_id: int
    role: MemberRole
    position: str | None
    suggested_full_name: str | None
    rejection: InviteRejection | None = None
    #: `join` brings somebody in; `rebind` points an existing card at this account. The two
    #: ask different questions on the next screen, so the difference has to survive here.
    purpose: InvitePurpose = InvitePurpose.JOIN
    #: The card a `rebind` code names, for the confirmation that follows.
    member_id: int | None = None

    @property
    def is_valid(self) -> bool:
        return self.rejection is None


@dataclass(frozen=True, slots=True)
class RebindOutcome:
    """What happened when a `rebind` code was typed.

    `rejection` is `None` exactly when the card now opens with the account that typed it.
    """

    rejection: InviteRejection | None = None
    member: VenueMember | None = None
    venue_id: int | None = None
    #: How many venues this person works in. The manager is told, because `telegram_id` is
    #: global and the change reaches every one of them (TZ 2).
    memberships: int = 1

    @property
    def is_rebound(self) -> bool:
        return self.rejection is None and self.member is not None


@dataclass(frozen=True, slots=True)
class InviteActivation:
    """Outcome of typing a code. `rejection` is `None` exactly when the person is in.

    `was_reinstated` says the row was already there and has been switched back on: the
    dismissed employee who came back. TZ 5.1 keeps that row forever so their checklists and
    write-offs stay in the reports, and the person on the other side of the screen should
    hear that they are *back* rather than be greeted as a newcomer — they know perfectly
    well they used to work here, and the wrong greeting reads as a system that forgot.

    Somebody who is working here *right now* never reaches this class at all: they are
    turned away with `ALREADY_MEMBER` and their code is left alone.
    """

    rejection: InviteRejection | None = None
    user: User | None = None
    member: VenueMember | None = None
    venue_id: int | None = None
    was_reinstated: bool = False

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

    def audit(self, venue_id: int) -> AuditSink | None:
        """The venue's audit log (TZ 2).

        A default implementation, not an abstract member, and deliberately: every provider
        in the product supplies one (`RepositoryBundle` has had `audit` since the trail was
        written), while the narrow fakes in the tests of unrelated behaviour have no reason
        to grow one. Silence is the safe default for them and impossible for the real
        bundle, which satisfies the protocol structurally.
        """
        return None


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


def require_not_self(actor: AccessContext, member: VenueMember) -> None:
    """Own role and own access are nobody's to change, least of all their own (TZ 2).

    One of these operations is a trap with no way out, and it has been walked into twice on
    the live stand. An owner who switches themselves off loses every screen; the code that
    would bring them back is issued by an owner (:meth:`AccessService.issue_invite_code`
    requires one), and there is no longer an active one. The only repair is a hand-written
    `UPDATE` in the database, which is not a product.

    Both fields are compared on purpose: `member_id` is the fast path, `user_id` the one
    that survives a card rebuilt from a different query.
    """
    if member.id == actor.member_id or member.user_id == actor.user_id:
        raise SelfTargetError(member.id)


def require_outranks_target(actor: AccessContext, member: VenueMember) -> None:
    """A manager does not dispose of a manager or of the owner (TZ 2).

    The mirror of the rule already enforced when a code is issued: `manager` may hand out
    `staff` and nothing above it, so it may not switch off what it could not create. Left
    open, it is the second road to a venue with no owner — the first is
    :func:`require_not_self`.
    """
    if role_rank(member.role) >= role_rank(MemberRole.MANAGER):
        require_owner(actor)


def issuable_roles(actor: AccessContext) -> tuple[MemberRole, ...]:
    """The roles this actor may put on a code or on a card (TZ 2).

    The single formulation of the rule `issue_invite_code` and `MemberService.set_role`
    both enforce. It used to be written twice — here as a check and again in
    `src/bot/keyboards/staff.py` as a list of buttons — and the docstring of the second copy
    said so out loud. Two copies of a permission rule are one copy and one guess about it.
    """
    if actor.is_owner:
        return tuple(MemberRole)
    return tuple(role for role in MemberRole if role_rank(role) < role_rank(MemberRole.MANAGER))


def may_act_on(actor: AccessContext, member: VenueMember) -> bool:
    """The two guards above asked as a question, for the screen that draws the buttons.

    A button the server will refuse is the broken button TZ 8.1 forbids, so the card hides
    what cannot be pressed. It asks *this* function rather than repeating the condition:
    a rule written twice is a rule that stops matching itself, which `issuable_roles` in
    `src/bot/keyboards/staff.py` already demonstrated. Hiding is not the protection —
    the guards are, and they run again on the server (TZ 9).
    """
    try:
        require_not_self(actor, member)
        require_outranks_target(actor, member)
    except AccessError:
        return False
    return True


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

    async def ensure_bootstrap_user(
        self,
        *,
        telegram_id: int,
        full_name: str,
        username: str | None = None,
    ) -> User:
        """Decision A3, word for word: `/start` from a bootstrap id makes the `users` row.

        **Without this a new installation cannot start at all**, and that is not a figure of
        speech: rights are read from `venue_members` (TZ 2), the first such row is written by
        the create-venue wizard, and the wizard needs a `users` row to point at. Nothing else
        creates one — :meth:`activate_invite_code` does, but a code can only be issued from
        inside a venue that does not exist yet. So the very first person pressed the one button
        the screen had and was refused, which is criterion 11.6 failing on an empty database.

        It was invisible to every test because the tests supplied the row: the fixture named
        it "`/start` made the `users` row" and made it itself, which is a belief about the
        code rather than an observation of it. A live stack found it in a minute.

        **Only a bootstrap id.** The check is here and not in the caller for the usual
        reason: the service holds the trigger (`OWNER_TELEGRAM_IDS`), and a method that
        created a user for anybody who asked would be a way into the one table the gate of
        TZ 5.1 protects. Idempotent — a second `/start` returns the row rather than a second
        one — and it grants nothing on its own: a `users` row without a `venue_members` row
        opens no screen but this wizard.

        The name comes from the Telegram profile, which is the only source there is: the
        owner arrives without an invite code, and decision B8's objection to profile names
        is about matching an imported schedule to employees, not about the person creating
        the venue in the first place.
        """
        if telegram_id not in self.bootstrap_owner_ids:
            raise BootstrapNotAllowedError(telegram_id)
        return await self._upsert_user(
            telegram_id=telegram_id,
            full_name=full_name.strip() or str(telegram_id),
            username=username,
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

    async def membership_count(self, user_id: int) -> int:
        """In how many venues this person works — the warning of TZ 2 on the rebind screen."""
        return len(await self.repositories.venues.list_for_user(user_id))

    async def issue_rebind_code(
        self,
        actor: AccessContext,
        *,
        member_id: int,
        now: dt.datetime,
    ) -> InviteCode:
        """A code that points at a card and moves the account that opens it (TZ 5.1).

        The problem it solves: somebody changes their Telegram account. Until now the only
        way back was an ordinary invite, which creates a *second* `users` row and a second
        membership — the roster shows the same name twice, and every shift, checklist and
        write-off stays on the first one, because they hang off `users.id`.

        This moves `users.telegram_id` instead, and nothing else. The history does not move
        because it was never attached to the account in the first place.

        Only one may be open per card at a time (`uq_invite_codes_open_rebind`): two
        accounts racing for one card would both be told to confirm, and the loser would
        quietly lose their access.
        """
        require_manager(actor)
        members = self.repositories.members(actor.venue_id)
        member = await members.get(member_id)
        if member is None:
            raise UnknownMembershipError(member_id, actor.venue_id)
        require_venue(actor, member.venue_id)
        require_outranks_target(actor, member)

        # The name travels on the code so that the *new* account can be shown whose card
        # it is about to take over. It is a copy for one screen and never written back.
        owner_of_card = await self.repositories.users.get(member.user_id)
        moment = as_utc(now)
        invites = self.repositories.invites(actor.venue_id)
        for _ in range(INVITE_CODE_ATTEMPTS):
            code = format_invite_code(actor.venue_id, new_invite_secret())
            if await invites.get_by_code(code) is not None:
                continue
            return await invites.create(
                code=code,
                role=member.role,
                expires_at=moment + INVITE_CODE_TTL,
                position=member.position,
                full_name=None if owner_of_card is None else owner_of_card.full_name,
                created_by=actor.user_id,
                purpose=InvitePurpose.REBIND,
                issued_to_member_id=member.id,
            )
        raise InviteCodeGenerationError(
            f"could not draw a free invite code in {INVITE_CODE_ATTEMPTS} attempts"
        )

    async def activate_rebind_code(
        self,
        raw_code: str,
        *,
        telegram_id: int,
        now: dt.datetime,
        username: str | None = None,
    ) -> RebindOutcome:
        """Point an existing card at the account that just typed this code (TZ 5.1).

        Writes exactly one column — `users.telegram_id` — and refuses in three ways that
        each leave the code alone, because each of them needs the manager to decide
        something rather than to issue a replacement:

        * the account already belongs to *somebody else*: whose card is this?
        * the card is gone or switched off: there is nothing to point at;
        * the code is spent, revoked or expired: the ordinary reasons, shared with joining.

        The one case that consumes the code without changing anything is the account that
        is *already* on this card — the person opened their own link twice.
        """
        moment = as_utc(now)
        parsed = parse_invite_code(raw_code)
        if parsed is None:
            return RebindOutcome(rejection=InviteRejection.MALFORMED)
        venue_id, code = parsed

        invites = self.repositories.invites(venue_id)
        found = await invites.get_by_code(code)
        if found is None or found.purpose is not InvitePurpose.REBIND:
            return RebindOutcome(rejection=InviteRejection.UNKNOWN)
        rejection = _invite_rejection(found, moment)
        if rejection is not None:
            return RebindOutcome(rejection=rejection, venue_id=venue_id)

        members = self.repositories.members(venue_id)
        member = (
            None
            if found.issued_to_member_id is None
            else await members.get(found.issued_to_member_id)
        )
        if member is None or not member.is_active:
            return RebindOutcome(rejection=InviteRejection.CARD_UNAVAILABLE, venue_id=venue_id)

        users = self.repositories.users
        occupant = await users.get_by_telegram_id(telegram_id)
        if occupant is not None and occupant.id != member.user_id:
            return RebindOutcome(rejection=InviteRejection.ACCOUNT_TAKEN, venue_id=venue_id)

        # Read the *value* before writing, not the row: `update` mutates the very object
        # `get` handed back (one instance per id, in the identity map and in the fakes), so
        # asking afterwards would answer with what was just written and the diff would come
        # out empty. The audit entry would then silently not exist.
        was = await users.get(member.user_id)
        before = None if was is None else was.telegram_id
        if occupant is None:
            await users.update(member.user_id, telegram_id=telegram_id, username=username)
        await invites.mark_used(found.id, used_by=member.user_id, used_at=moment)
        await self._record_rebind(
            venue_id,
            member=member,
            before=before,
            after=telegram_id,
        )
        return RebindOutcome(
            member=member,
            venue_id=venue_id,
            memberships=len(await self.repositories.venues.list_for_user(member.user_id)),
        )

    async def _record_rebind(
        self,
        venue_id: int,
        *,
        member: VenueMember,
        before: int | None,
        after: int,
    ) -> None:
        """TZ 2 and TZ 9: `telegram_id` is the key to somebody's access, so a move is logged.

        The actor is the person whose card moved. Nobody else's account changed, and the
        manager who issued the code is on the `invite_codes.created_by` of that same row.
        """
        trail = AuditTrail(self.repositories.audit(venue_id))
        if trail.is_silent:
            return
        await trail.updated(
            None,
            AuditEntity.MEMBER,
            member.id,
            before={"telegram_id": before},
            after={"telegram_id": after},
        )

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

    async def preview_invite_code(
        self,
        raw_code: str,
        *,
        now: dt.datetime,
        telegram_id: int | None = None,
    ) -> InvitePreview:
        """Validate a code without consuming it (TZ 5.1: confirm the name and position).

        `telegram_id` is optional only because a caller may genuinely not have one; when it
        is there, an active member is turned away here rather than three screens later, and
        without the code being touched. That is the whole point of refusing at the preview:
        the person is one confirmation away from spending somebody else's invitation.
        """
        moment = as_utc(now)
        parsed = parse_invite_code(raw_code)
        if parsed is None:
            return _rejected_preview(InviteRejection.MALFORMED)
        venue_id, code = parsed
        found = await self.repositories.invites(venue_id).get_by_code(code)
        if found is None:
            return _rejected_preview(InviteRejection.UNKNOWN)
        rejection = _invite_rejection(found, moment)
        if (
            rejection is None
            and found.purpose is not InvitePurpose.REBIND
            and telegram_id is not None
            and await self._is_active_member(venue_id, telegram_id=telegram_id)
        ):
            # Only a joining code is refused here. A rebind code is *meant* for an account
            # that may already be somebody — `activate_rebind_code` decides whether it is
            # the right somebody, which is a different question with a different answer.
            rejection = InviteRejection.ALREADY_MEMBER
        return InvitePreview(
            venue_id=venue_id,
            code_id=found.id,
            role=found.role,
            position=found.position,
            suggested_full_name=found.full_name,
            rejection=rejection,
            purpose=found.purpose,
            member_id=found.issued_to_member_id,
        )

    async def _is_active_member(self, venue_id: int, *, telegram_id: int) -> bool:
        """Does this Telegram account already work in this venue?

        Read through the venue's own repository, so the question cannot accidentally be
        answered about the venue next door (TZ 3.3).
        """
        user = await self.repositories.users.get_by_telegram_id(telegram_id)
        if user is None:
            return False
        member = await self.repositories.members(venue_id).get_for_user(user.id)
        return member is not None and member.is_active

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

        **A code grants access and never takes any away.** Three shapes, and the middle one
        is the fix for two live incidents:

        * nobody by that Telegram id works here — the row is created from the code;
        * an *active* member typed it — refused with `ALREADY_MEMBER`, and **the code is
          left unused**. It was issued for somebody else, and burning it destroys their
          invitation. Both incidents were this: an owner opened his own link to see that it
          worked and was demoted by it, and later spent the single use of a link he had
          already forwarded to a colleague;
        * a dismissed member came back — the row is reactivated, and the role can only go
          *up*: a returning manager does not become `staff` because the code said so.
        """
        moment = as_utc(now)
        parsed = parse_invite_code(raw_code)
        if parsed is None:
            return InviteActivation(rejection=InviteRejection.MALFORMED)
        venue_id, code = parsed

        invites = self.repositories.invites(venue_id)
        found = await invites.get_by_code(code)
        if found is None or found.purpose is InvitePurpose.REBIND:
            # A rebind code names a card that already exists; joining on it would create a
            # membership for the wrong person and spend somebody's way back into their own.
            # Answered as unknown, so that trying codes cannot tell the two kinds apart.
            return InviteActivation(rejection=InviteRejection.UNKNOWN)
        rejection = _invite_rejection(found, moment)
        if rejection is not None:
            return InviteActivation(rejection=rejection, venue_id=venue_id)

        members = self.repositories.members(venue_id)
        known = await self.repositories.users.get_by_telegram_id(telegram_id)
        seated = None if known is None else await members.get_for_user(known.id)
        if seated is not None and seated.is_active:
            # Checked before `_upsert_user`, so a refused activation writes nothing at all —
            # not the row, not the code, not the name.
            return InviteActivation(rejection=InviteRejection.ALREADY_MEMBER, venue_id=venue_id)

        user = await self._upsert_user(
            telegram_id=telegram_id,
            full_name=full_name or (found.full_name or ""),
            username=username,
        )
        existing = seated
        if existing is None:
            member = await members.add(
                user_id=user.id,
                role=found.role,
                position=found.position,
            )
            was_reinstated = False
        else:
            # TZ 5.1: a returning employee keeps the row that carries their history. The
            # role is the higher of the two: the code may promote a returning employee, and
            # may not quietly demote one (TZ 2).
            was_reinstated = True
            restored = (
                found.role if role_rank(found.role) > role_rank(existing.role) else existing.role
            )
            updated = await members.update(
                existing.id,
                role=restored,
                position=found.position or existing.position,
                is_active=True,
            )
            member = updated if updated is not None else existing

        await invites.mark_used(found.id, used_by=user.id, used_at=moment)
        if user.active_venue_id is None:
            await self.repositories.users.set_active_venue(user.id, venue_id)
        await self._record_activation(venue_id, user=user, member=member, code=found, at=moment)

        return InviteActivation(
            user=user,
            member=member,
            venue_id=venue_id,
            was_reinstated=was_reinstated,
        )

    async def _record_activation(
        self,
        venue_id: int,
        *,
        user: User,
        member: VenueMember,
        code: InviteCode,
        at: dt.datetime,
    ) -> None:
        """Write the membership and the spent code into `audit_log` (TZ 2, decision B13).

        Until this existed, the single most sensitive change in the product left no trace
        at all: who became a manager, when, and on whose invitation was answerable only by
        `invite_codes.used_by`, and a role overwritten by an activation was answerable by
        nothing. `MemberService` has recorded its own edits since it was written; this is
        the same events arriving by the other road.

        The actor is the person joining. They are the one who acted — nobody else pressed
        anything — and `audit_log.user_id` is a `users.id`, never a `telegram_id`.
        """
        trail = AuditTrail(self.repositories.audit(venue_id))
        if trail.is_silent:
            return
        actor = _context(user, member)
        await trail.created(
            actor,
            AuditEntity.MEMBER,
            member.id,
            after=snapshot(member, "user_id", "role", "position", "is_active"),
        )
        await trail.updated(
            actor,
            AuditEntity.INVITE_CODE,
            code.id,
            before={"used_at": None, "used_by": None},
            after={"used_at": at, "used_by": user.id},
            action=AuditAction.UPDATE,
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
        # `full_name` is deliberately absent from what an activation may write. The column
        # is on the global `users` row, so it is the name every venue this person works in
        # can see, and a code carrying somebody else's name would rewrite it in all of them
        # (TZ 2). Correcting the name of a person the system already knows is the manager's
        # own act — `MemberService.rename`, decision B8. Observed twice on the live stand:
        # the owner's name flipped between two codes he opened himself.
        fields: dict[str, object] = {}
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
    "BootstrapNotAllowedError",
    "Identity",
    "InviteActivation",
    "InviteCodeGenerationError",
    "InvitePreview",
    "InviteRejection",
    "NaiveMomentError",
    "PermissionDeniedError",
    "RebindOutcome",
    "SelfTargetError",
    "UnknownMembershipError",
    "VenueMismatchError",
    "as_utc",
    "format_invite_code",
    "invite_deeplink_payload",
    "issuable_roles",
    "may_act_on",
    "new_invite_secret",
    "parse_invite_code",
    "require_manager",
    "require_not_self",
    "require_outranks_target",
    "require_owner",
    "require_role",
    "require_self_or_manager",
    "require_venue",
    "role_rank",
]
