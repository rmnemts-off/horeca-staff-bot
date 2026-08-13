"""One session per update, and everything that is built on it (TZ 3.2, 3.3).

This is the composition root of the bot process. `tests/test_wiring.py` already assembles
every repository and every service once, for mypy; this module is the same assembly at run
time, and the two are deliberately the same shape — a service that gains a dependency
fails there before it fails here.

**Why a container and not a middleware per service.** A repository is scoped to one venue
by construction (TZ 3.3): the venue is a field of the object, not an argument of the query.
So nothing venue-scoped can exist before the auth middleware has said who is asking and the
venue middleware has said where they work. The container is what carries the *session*
across that gap: it is created for the update, hands out `AccessService` immediately (which
answers "who" and "where" and therefore cannot be scoped), and builds the venue-scoped half
only once a venue is known.

**One transaction per update.** The session lives inside `session_scope()`, which commits
when the handler returns and rolls back when it raises. A handler therefore never commits
and never has to remember to; and an update that fails halfway leaves nothing behind.

**Handlers get services, not repositories.** `data["services"]` is the venue's service
bundle. The repositories hanging off it are there for the callback resolver, which has to
fetch the object a button names before any handler sees it; `tests/bot/test_handler_boundary.py`
is what keeps a handler from reaching for them (TZ 3.2: a handler takes an update, calls a
service and renders the reply).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Final

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Venue
from src.db.repositories.audit import AuditLogRepo
from src.db.repositories.checklists import (
    ChecklistItemRepo,
    ChecklistRunItemRepo,
    ChecklistRunRepo,
    ChecklistTemplateRepo,
)
from src.db.repositories.invites import InviteCodeRepo
from src.db.repositories.members import VenueMemberRepo
from src.db.repositories.notifications import NotificationRepo
from src.db.repositories.protocols import (
    AuditLogRepository,
    ChecklistItemRepository,
    ChecklistRunItemRepository,
    ChecklistTemplateRepository,
    InviteCodeRepository,
    NotificationRepository,
    RecipeIngredientRepository,
    RecipeRepository,
    ShiftRepository,
    UserRepository,
    VenueMemberRepository,
    VenueRepository,
    VenueSettingsRepository,
)
from src.db.repositories.recipes import RecipeIngredientRepo, RecipeRepo
from src.db.repositories.shifts import ShiftRepo
from src.db.repositories.users import UserRepo
from src.db.repositories.venues import VenueRepo, VenueSettingsRepo
from src.db.session import session_scope
from src.services.access import AccessService
from src.services.checklists import ChecklistService, ReopenableRunRepository
from src.services.members import MemberService
from src.services.notifications import NotificationService
from src.services.recipes import RecipeService
from src.services.shifts import ShiftService

#: Handler-context key of the container itself. Only the middlewares of this package read
#: it; a handler that needs something from it takes `services` instead.
CONTAINER_KEY: Final = "container"

#: Handler-context key of `AccessService`, which exists before a venue does (TZ 5.1).
ACCESS_KEY: Final = "access"

#: Handler-context key of the venue's service bundle, set by the venue middleware.
SERVICES_KEY: Final = "services"

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]
Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True, slots=True)
class VenueRepositories:
    """Every repository of one venue, seen as the protocol it implements.

    Same list as `Repositories` in `tests/test_wiring.py`, and for the same reason: the
    annotations are what makes mypy compare the concrete class against the contract the
    services were written against.
    """

    venues: VenueRepository
    settings: VenueSettingsRepository
    users: UserRepository
    members: VenueMemberRepository
    invites: InviteCodeRepository
    shifts: ShiftRepository
    templates: ChecklistTemplateRepository
    items: ChecklistItemRepository
    runs: ReopenableRunRepository
    run_items: ChecklistRunItemRepository
    recipes: RecipeRepository
    ingredients: RecipeIngredientRepository
    notifications: NotificationRepository
    audit: AuditLogRepository


@dataclass(frozen=True, slots=True)
class VenueServices:
    """What a handler of this venue may call (TZ 3.2: logic lives in the services)."""

    venue: Venue
    repositories: VenueRepositories
    members: MemberService
    checklists: ChecklistService
    recipes: RecipeService
    shifts: ShiftService
    notifications: NotificationService

    @property
    def venue_id(self) -> int:
        return self.venue.id


class RepositoryBundle:
    """`AccessRepositories` and `MemberRepositories` over one session.

    Those two services are the exception documented in `src/services/access.py`: they work
    out *which* venue in the first place, so they take factories rather than a repository
    already scoped to one.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def users(self) -> UserRepository:
        return UserRepo(self._session)

    @property
    def venues(self) -> VenueRepository:
        return VenueRepo(self._session)

    def members(self, venue_id: int) -> VenueMemberRepository:
        return VenueMemberRepo(self._session, venue_id)

    def invites(self, venue_id: int) -> InviteCodeRepository:
        return InviteCodeRepo(self._session, venue_id)


class Container:
    """Everything one update may need, over one session and one transaction."""

    def __init__(self, session: AsyncSession, *, bootstrap_owner_ids: frozenset[int]) -> None:
        self._session = session
        self._bundle = RepositoryBundle(session)
        self.access = AccessService(self._bundle, bootstrap_owner_ids=bootstrap_owner_ids)

    @property
    def users(self) -> UserRepository:
        """Global, like the `users` table: a person exists before any venue does."""
        return self._bundle.users

    @property
    def venues(self) -> VenueRepository:
        return self._bundle.venues

    def repositories_for(self, venue_id: int) -> VenueRepositories:
        session = self._session
        return VenueRepositories(
            venues=VenueRepo(session),
            settings=VenueSettingsRepo(session, venue_id),
            users=UserRepo(session),
            members=VenueMemberRepo(session, venue_id),
            invites=InviteCodeRepo(session, venue_id),
            shifts=ShiftRepo(session, venue_id),
            templates=ChecklistTemplateRepo(session, venue_id),
            items=ChecklistItemRepo(session, venue_id),
            runs=ChecklistRunRepo(session, venue_id),
            run_items=ChecklistRunItemRepo(session, venue_id),
            recipes=RecipeRepo(session, venue_id),
            ingredients=RecipeIngredientRepo(session, venue_id),
            notifications=NotificationRepo(session, venue_id),
            audit=AuditLogRepo(session, venue_id),
        )

    def for_venue(self, venue: Venue) -> VenueServices:
        """The services of one venue, built from repositories scoped to it (TZ 3.3).

        `NotificationService` is passed to the three services that have something to tell
        somebody: handlers never call the scheduler and never send a message of their own
        (plan, tasks 16 and 24).
        """
        repositories = self.repositories_for(venue.id)
        notifications = NotificationService(
            venue_id=venue.id,
            venues=repositories.venues,
            settings=repositories.settings,
            users=repositories.users,
            notifications=repositories.notifications,
            recipients=self.access,
        )
        return VenueServices(
            venue=venue,
            repositories=repositories,
            members=MemberService(self._bundle),
            checklists=ChecklistService(
                templates=repositories.templates,
                items=repositories.items,
                runs=repositories.runs,
                run_items=repositories.run_items,
                notifier=notifications,
            ),
            recipes=RecipeService(
                recipes=repositories.recipes,
                ingredients=repositories.ingredients,
                notifier=notifications,
            ),
            shifts=ShiftService(
                venue_id=venue.id,
                timezone=venue.timezone,
                shifts=repositories.shifts,
                users=repositories.users,
                members=repositories.members,
                notifier=notifications,
            ),
            notifications=notifications,
        )


class ServicesMiddleware(BaseMiddleware):
    """Opens the session of the update and puts the container into the context."""

    def __init__(
        self,
        *,
        sessions: Sessions = session_scope,
        bootstrap_owner_ids: frozenset[int] = frozenset(),
    ) -> None:
        self._sessions = sessions
        self._bootstrap_owner_ids = bootstrap_owner_ids

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._sessions() as session:
            container = Container(session, bootstrap_owner_ids=self._bootstrap_owner_ids)
            data[CONTAINER_KEY] = container
            data[ACCESS_KEY] = container.access
            return await handler(event, data)


def setup(
    dispatcher: Any,
    *,
    sessions: Sessions = session_scope,
    bootstrap_owner_ids: frozenset[int] = frozenset(),
) -> ServicesMiddleware:
    """Register the container middleware on updates (called by `src/bot/dispatcher.py`)."""
    middleware = ServicesMiddleware(sessions=sessions, bootstrap_owner_ids=bootstrap_owner_ids)
    dispatcher.update.outer_middleware(middleware)
    return middleware


__all__ = [
    "ACCESS_KEY",
    "CONTAINER_KEY",
    "SERVICES_KEY",
    "Container",
    "RepositoryBundle",
    "ServicesMiddleware",
    "Sessions",
    "VenueRepositories",
    "VenueServices",
    "setup",
]
