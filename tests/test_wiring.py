"""The seam between the repositories and the services, asserted by mypy.

**Why this file exists.** The data layer and the service layer were written against
`src/db/repositories/protocols.py` in parallel (CLAUDE.md: the shared contract is the
models and the repository interfaces, fixed first so the waves can go on independently).
A `Protocol` is checked structurally, and only where a value of the concrete type is
actually assigned to it — and until this file no line of the project made that assignment.
`NotificationRepo` never met `NotificationRepository`, `ShiftRepo` never met
`ShiftRepository`, and every service was assembled, in tests, out of fakes written from the
same protocol. So each half agreed with the contract, the contract agreed with itself, mypy
had nothing to compare, and the one comparison that mattered — the real repository against
the real service — was never made.

It had already drifted. `NotificationRepo.mark_sent` and `mark_failed` require the
`claimed_at` claim token that closes the capture race of acceptance 11.4, while the
protocol still declared the older two-argument form; `NotificationService` could not have
been built from the real repository at all. Nothing was red, and the first place that would
have said so was production.

**What this file does.** Two bindings, and neither needs to run:

* every concrete repository of `src/db/repositories/` is assigned to the protocol it
  implements (:class:`Repositories`), so a signature that drifts on either side is an error
  at the assignment;
* every service of `src/services/` is constructed from those real repositories
  (:class:`Services`), so a service asking for something no repository provides is an error
  at the call.

`files = ["src", "tests"]` in `pyproject.toml` is what makes that enough: mypy reads this
file on every run, with or without pytest and with or without a database. The tests below
do execute — they build the objects on a session with no bind, so nothing connects and no
statement is issued — and the two completeness guards are the part that genuinely needs the
run time: they walk `src/db/repositories/` and `src/services/` and fail when something
found there is not bound above. Without them this file would only ever cover what existed
the day it was written, and the next repository would be as unchecked as `NotificationRepo`
was.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass, fields
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
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
    ChecklistRunRepository,
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
from src.services.access import AccessRepositories, AccessService
from src.services.audit import AuditSink, AuditTrail
from src.services.checklists import ChecklistService
from src.services.members import MemberRepositories, MemberService
from src.services.notifications import NotificationService
from src.services.overdue import OverdueService
from src.services.recipes import RecipeService
from src.services.reports import ReportService
from src.services.shifts import ShiftService
from src.services.templates import TemplateService
from src.services.venues import VenueCreationRepositories, VenueService

from tests.repo_scan import REPOSITORIES_DIR, SRC_DIR, python_files, relative

#: Any positive id: nothing is executed here, and `BaseRepository` refuses a non-positive
#: one at construction time (TZ 3.3).
VENUE_ID = 1

#: An IANA name for `ShiftService`, which resolves it through `src/services/timezones.py`.
TIMEZONE = "Europe/Moscow"

REPOSITORIES_PACKAGE = "src.db.repositories"
SERVICES_PACKAGE = "src.services"
SERVICES_DIR = SRC_DIR / "services"

#: How the two layers are named: `NotificationRepo` implements `NotificationRepository`,
#: `NotificationService` is built from it. The completeness guards discover classes by
#: these suffixes, so a new one cannot escape by being missing from a list somebody had to
#: remember to edit.
REPOSITORY_SUFFIX = "Repo"
SERVICE_SUFFIX = "Service"

#: Modules of `src/db/repositories/` that declare no concrete repository.
NON_REPOSITORY_MODULES = frozenset({"__init__", "base", "protocols"})

#: Resolved like `REPO_ROOT` in `tests/repo_scan.py`, so `relative()` works whatever
#: symlink the checkout is reached through.
THIS_FILE = relative(Path(__file__).resolve())


def _session() -> AsyncSession:
    """A session with no bind.

    Everything below is a real object rather than a mock, but no statement is ever issued:
    what is being asserted is the type of each one, and that is settled at construction.
    A bind would only drag a database into a test that has no use for one (CLAUDE.md: the
    suite outside `@pytest.mark.db` runs with no PostgreSQL at all).
    """
    return AsyncSession()


def _classes_in(
    package: str,
    directory: Path,
    suffix: str,
    skip: frozenset[str],
) -> dict[str, type]:
    """Classes *declared* under `directory` whose name ends with `suffix`.

    Declared, not imported: a class counts for the module it is defined in, so one
    re-exported elsewhere is not counted twice.
    """
    found: dict[str, type] = {}
    for path in python_files(directory):
        if path.stem in skip:
            continue
        module = importlib.import_module(f"{package}.{path.stem}")
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if name.endswith(suffix) and cls.__module__ == module.__name__:
                found[name] = cls
    return found


# --------------------------------------------------------------------------------------
# (a) Every repository against the protocol it implements
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Repositories:
    """One field per protocol of `src/db/repositories/protocols.py` that has an
    implementation.

    The annotations are the assertion: building this class out of the concrete
    repositories makes mypy check each one structurally against the protocol it is handed
    over as. The protocols of TZ 4.4-4.6 that nothing implements yet — products, suppliers,
    orders, write-offs, preps, knowledge articles, import jobs — are absent on purpose, and
    :func:`test_every_repository_class_is_bound_to_a_protocol` is what makes their
    implementations join this list the day they are written.
    """

    venues: VenueRepository
    settings: VenueSettingsRepository
    users: UserRepository
    members: VenueMemberRepository
    invites: InviteCodeRepository
    shifts: ShiftRepository
    templates: ChecklistTemplateRepository
    items: ChecklistItemRepository
    runs: ChecklistRunRepository
    run_items: ChecklistRunItemRepository
    recipes: RecipeRepository
    ingredients: RecipeIngredientRepository
    notifications: NotificationRepository
    audit: AuditLogRepository


def build_repositories(session: AsyncSession) -> Repositories:
    """The whole data layer of stage 0, every repository seen as its protocol."""
    return Repositories(
        venues=VenueRepo(session),
        settings=VenueSettingsRepo(session, VENUE_ID),
        users=UserRepo(session),
        members=VenueMemberRepo(session, VENUE_ID),
        invites=InviteCodeRepo(session, VENUE_ID),
        shifts=ShiftRepo(session, VENUE_ID),
        templates=ChecklistTemplateRepo(session, VENUE_ID),
        items=ChecklistItemRepo(session, VENUE_ID),
        runs=ChecklistRunRepo(session, VENUE_ID),
        run_items=ChecklistRunItemRepo(session, VENUE_ID),
        recipes=RecipeRepo(session, VENUE_ID),
        ingredients=RecipeIngredientRepo(session, VENUE_ID),
        notifications=NotificationRepo(session, VENUE_ID),
        audit=AuditLogRepo(session, VENUE_ID),
    )


def repository_classes() -> dict[str, type]:
    return _classes_in(
        REPOSITORIES_PACKAGE,
        REPOSITORIES_DIR,
        REPOSITORY_SUFFIX,
        NON_REPOSITORY_MODULES,
    )


def test_every_repository_satisfies_its_protocol() -> None:
    """Read by mypy, which is the half that matters; pytest only proves it constructs."""
    repositories = build_repositories(_session())
    assert len(fields(repositories)) == len(repository_classes())


def test_every_repository_class_is_bound_to_a_protocol() -> None:
    """A repository written later is bound here too, or this fails.

    The guard that keeps the class above from ageing into a snapshot of one afternoon.
    """
    repositories = build_repositories(_session())
    bound = {type(getattr(repositories, field.name)) for field in fields(Repositories)}
    unbound = sorted(name for name, cls in repository_classes().items() if cls not in bound)
    assert not unbound, (
        f"{', '.join(unbound)} implements a protocol that nothing assigns it to; add a "
        f"field to {THIS_FILE}::Repositories so mypy compares the signatures"
    )


# --------------------------------------------------------------------------------------
# (b) Every service out of those repositories
# --------------------------------------------------------------------------------------


class RepositoryBundle:
    """`AccessRepositories`, `MemberRepositories` and `VenueCreationRepositories`.

    Those three services take their repositories through a small protocol instead of one by
    one: `users` and `venues` are global, while `venue_members` and `invite_codes` are
    venue-scoped and therefore arrive as factories — the venue is part of the repository
    object (TZ 3.3), and access is the code that works out *which* venue in the first
    place. `VenueService` is the sharpest case of the same shape: it runs before the venue
    it is creating exists, so nothing can be scoped for it in advance (decision A3). The
    return annotations below are the protocol types, so every `return` here is one more
    assignment mypy checks.
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

    def settings(self, venue_id: int) -> VenueSettingsRepository:
        return VenueSettingsRepo(self._session, venue_id)

    def templates(self, venue_id: int) -> ChecklistTemplateRepository:
        return ChecklistTemplateRepo(self._session, venue_id)

    def audit(self, venue_id: int) -> AuditSink:
        return AuditLogRepo(self._session, venue_id)


@dataclass(frozen=True, slots=True)
class Services:
    """Every service of `src/services/`, as the bot layer will hold them."""

    access: AccessService
    venues: VenueService
    members: MemberService
    notifications: NotificationService
    checklists: ChecklistService
    overdue: OverdueService
    templates: TemplateService
    recipes: RecipeService
    shifts: ShiftService
    reports: ReportService


def build_services(session: AsyncSession) -> Services:
    """The assembly the bot layer does at start-up, written once where mypy can read it.

    `NotificationService` goes to the three services that have something to tell somebody:
    it is the implementation of `ChecklistNotifier`, `RecipeNotifier` and `ShiftNotifier`,
    and `AccessService` is the implementation of `ManagerRecipients` (answer A4). Those
    seams are exactly what a fake hides, so they are wired here from the real objects and
    from nothing else.
    """
    bundle = RepositoryBundle(session)
    access_repositories: AccessRepositories = bundle
    member_repositories: MemberRepositories = bundle
    # `VenueService` runs before its venue exists (decision A3), so it takes the same
    # factory shape as the two above rather than a repository already scoped to a venue.
    venue_repositories: VenueCreationRepositories = bundle

    # One trail per venue: `audit_log.venue_id` is the repository's own column, so the
    # services below cannot record into another venue even by mistake (TZ 2, 3.3).
    trail = AuditTrail(AuditLogRepo(session, VENUE_ID))
    access = AccessService(access_repositories)
    notifications = NotificationService(
        venue_id=VENUE_ID,
        venues=VenueRepo(session),
        settings=VenueSettingsRepo(session, VENUE_ID),
        users=UserRepo(session),
        notifications=NotificationRepo(session, VENUE_ID),
        recipients=access,
    )

    checklists = ChecklistService(
        templates=ChecklistTemplateRepo(session, VENUE_ID),
        items=ChecklistItemRepo(session, VENUE_ID),
        runs=ChecklistRunRepo(session, VENUE_ID),
        run_items=ChecklistRunItemRepo(session, VENUE_ID),
        notifier=notifications,
    )
    return Services(
        access=access,
        venues=VenueService(venue_repositories),
        members=MemberService(member_repositories, audit=trail),
        notifications=notifications,
        checklists=checklists,
        overdue=OverdueService(
            timezone=TIMEZONE,
            runs=ChecklistRunRepo(session, VENUE_ID),
            shifts=ShiftRepo(session, VENUE_ID),
            settings=VenueSettingsRepo(session, VENUE_ID),
            checklists=checklists,
        ),
        templates=TemplateService(
            templates=ChecklistTemplateRepo(session, VENUE_ID),
            items=ChecklistItemRepo(session, VENUE_ID),
            audit=trail,
        ),
        recipes=RecipeService(
            recipes=RecipeRepo(session, VENUE_ID),
            ingredients=RecipeIngredientRepo(session, VENUE_ID),
            notifier=notifications,
            audit=trail,
        ),
        shifts=ShiftService(
            venue_id=VENUE_ID,
            timezone=TIMEZONE,
            shifts=ShiftRepo(session, VENUE_ID),
            users=UserRepo(session),
            members=VenueMemberRepo(session, VENUE_ID),
            notifier=notifications,
            audit=trail,
        ),
        reports=ReportService(
            venue="PIMS",
            shifts=ShiftRepo(session, VENUE_ID),
            checklists=checklists,
            # `users` and not the roster: a dismissed employee keeps their row (TZ 5.1) and
            # the report of the month they worked still has to say their name.
            users=UserRepo(session),
        ),
    )


def service_classes() -> dict[str, type]:
    return _classes_in(SERVICES_PACKAGE, SERVICES_DIR, SERVICE_SUFFIX, frozenset())


def test_every_service_is_built_from_the_real_repositories() -> None:
    """The other half of the seam: what the services ask for, against what exists."""
    services = build_services(_session())
    assert len(fields(services)) == len(service_classes())


def test_every_service_class_is_assembled() -> None:
    """A service written later is assembled here too, or this fails."""
    services = build_services(_session())
    assembled = {type(getattr(services, field.name)) for field in fields(Services)}
    missing = sorted(name for name, cls in service_classes().items() if cls not in assembled)
    assert not missing, (
        f"{', '.join(missing)} is never built from the real repositories; assemble it in "
        f"{THIS_FILE} so mypy checks what it asks its repositories for"
    )
