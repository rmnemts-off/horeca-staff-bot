"""Checklist repositories (plan, task 12).

Three kinds of test live here on purpose. The shape tests compile the statements against
the PostgreSQL dialect and read them: they need no server, so the three conditional updates
the whole checklist screen rests on — `IS DISTINCT FROM`, `completed_at IS NULL`,
`started_at IS NULL` — are checked on every machine, in every run. The behaviour tests
carry `@pytest.mark.db` and prove the same rules against a real database.

Between the two sits the wiring check at the end of the shape section. `ChecklistService`
is typed on protocols, so nothing at runtime ever notices that a repository is missing a
method the service calls — the failure is a type error, and it only appears where the two
halves are actually put together. Assembling the service out of the concrete repositories
in a file mypy reads is that place.

The last test is the only one that proves anything about *concurrency*. Calling `set_done`
twice in a row on one session shows that the second call matches no row — a statement about
the `WHERE` clause and nothing else, because one session cannot race itself. Two taps in a
bar arrive on two connections, and what has to hold there is that the loser of the row lock
re-evaluates the condition after the winner commits and still changes nothing. That needs
two sessions, two repositories and `asyncio.gather`, exactly as the queue test in
`tests/db/test_repositories_notifications.py` does it — so those rows are committed on
purpose and removed afterwards instead of riding the rolled-back `session` fixture.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Iterable
from typing import Any, cast

import pytest
from sqlalchemy import delete
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.db.models import (
    ChecklistRun,
    ChecklistRunItem,
    ChecklistTemplate,
    ChecklistType,
    RunStatus,
    User,
    Venue,
)
from src.db.repositories.checklists import (
    ChecklistItemRepo,
    ChecklistRunItemRepo,
    ChecklistRunRepo,
    ChecklistTemplateRepo,
)
from src.db.repositories.protocols import ChecklistRunRepository
from src.services.checklists import (
    ChecklistService,
    EmptyTemplateAlert,
    PendingItemsAlert,
    ReopenableRunRepository,
)

from tests.factories import (
    create_checklist_item,
    create_checklist_template,
    create_shift,
    create_user,
    create_venue,
)

MOMENT = dt.datetime(2026, 1, 1, 6, 0, tzinfo=dt.UTC)
LATER = MOMENT + dt.timedelta(hours=1)
VENUE_ID = 7


# --------------------------------------------------------------------------------------
# A session that records statements instead of running them
# --------------------------------------------------------------------------------------


class Rows:
    """The part of `ScalarResult` these repositories use."""

    def __init__(self, *rows: Any) -> None:
        self._rows = list(rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class Recorder:
    """Stand-in for `AsyncSession`: keeps every statement, executes nothing."""

    def __init__(self, *results: Any) -> None:
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self._results = list(results)

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        return self._record(statement)

    async def scalars(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        return self._record(statement)

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        self.statements.append(statement)
        return self._results.pop(0) if self._results else None

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    def add_all(self, instances: Iterable[Any]) -> None:
        self.added.extend(instances)

    async def flush(self, *args: Any, **kwargs: Any) -> None:
        return None

    def _record(self, statement: Any) -> Any:
        self.statements.append(statement)
        return self._results.pop(0) if self._results else Rows()

    def sql(self, index: int = 0) -> str:
        return str(self.statements[index].compile(dialect=postgresql.dialect()))


def as_session(recorder: Recorder) -> AsyncSession:
    return cast(AsyncSession, recorder)


# --------------------------------------------------------------------------------------
# Shape of the statements — no database
# --------------------------------------------------------------------------------------


async def test_set_done_is_one_conditional_update() -> None:
    """The double tap is a no-op because the row has to differ from the target state."""
    recorder = Recorder(Rows(1))
    repo = ChecklistRunItemRepo(as_session(recorder), VENUE_ID)

    await repo.set_done(run_id=3, item_id=5, is_done=True, moment=MOMENT)

    statement = recorder.sql()
    assert statement.startswith("UPDATE checklist_run_items")
    assert "IS DISTINCT FROM" in statement
    # The venue travels through the join to `checklist_runs` (decision D9).
    assert "JOIN checklist_runs" in statement
    assert "checklist_runs.venue_id" in statement


async def test_complete_only_touches_an_unfinished_run() -> None:
    recorder = Recorder(Rows(1))
    repo = ChecklistRunRepo(as_session(recorder), VENUE_ID)

    await repo.complete(
        3,
        moment=MOMENT,
        status=RunStatus.COMPLETED,
        completed_by=11,
        skip_comment=None,
    )

    statement = recorder.sql()
    assert "checklist_runs.completed_at IS NULL" in statement
    assert "checklist_runs.venue_id" in statement


async def test_mark_started_only_fires_once_and_moves_the_status() -> None:
    recorder = Recorder()
    repo = ChecklistRunRepo(as_session(recorder), VENUE_ID)

    await repo.mark_started(3, MOMENT)

    statement = recorder.sql()
    assert "checklist_runs.started_at IS NULL" in statement
    assert "CASE WHEN" in statement


async def test_mark_overdue_leaves_a_finished_run_alone() -> None:
    recorder = Recorder(Rows(1))
    repo = ChecklistRunRepo(as_session(recorder), VENUE_ID)

    await repo.mark_overdue(3, MOMENT)

    statement = recorder.sql()
    assert "checklist_runs.completed_at IS NULL" in statement
    assert "checklist_runs.status !=" in statement


async def test_reopen_only_touches_a_finished_run() -> None:
    """The mirror of `complete()`: there has to be a completion to undo (TZ 5.4)."""
    recorder = Recorder(Rows(1))
    repo = ChecklistRunRepo(as_session(recorder), VENUE_ID)

    await repo.reopen(3)

    statement = recorder.sql()
    assert statement.startswith("UPDATE checklist_runs")
    assert "checklist_runs.completed_at IS NOT NULL" in statement
    assert "checklist_runs.venue_id" in statement
    # `completed_by` and `skip_comment` stay: the completion happened, and reopening the
    # run does not unhappen it (schema note Q4 gives the fact no columns of its own).
    assert "completed_by" not in statement
    assert "skip_comment" not in statement


async def test_refresh_counters_recomputes_instead_of_incrementing() -> None:
    recorder = Recorder(Rows(1))
    repo = ChecklistRunRepo(as_session(recorder), VENUE_ID)

    await repo.refresh_counters(3)

    statement = recorder.sql()
    assert "done_items=(SELECT count" in statement.replace(" =", "=").replace("= ", "=")
    assert "FROM checklist_run_items" in statement
    assert "done_items + " not in statement


async def test_a_child_query_always_joins_its_parent() -> None:
    recorder = Recorder()
    repo = ChecklistItemRepo(as_session(recorder), VENUE_ID)

    await repo.list_for_template(4)

    statement = recorder.sql()
    assert "JOIN checklist_templates" in statement
    assert "checklist_templates.venue_id" in statement


async def test_a_write_for_a_foreign_template_is_refused_before_it_is_written() -> None:
    """`add()` promises a row back, so ownership has to raise rather than return None."""
    recorder = Recorder()
    repo = ChecklistItemRepo(as_session(recorder), VENUE_ID)

    with pytest.raises(NoResultFound, match="does not belong to venue"):
        await repo.add(
            template_id=4,
            text="Item",
            group_name=None,
            group_index=0,
            order_index=0,
        )
    assert not recorder.added


# --------------------------------------------------------------------------------------
# Wiring — the service has to be buildable out of these repositories
# --------------------------------------------------------------------------------------


class SilentNotifier:
    """The three alerts `ChecklistService` knows how to raise, and nothing else.

    A real notifier is plan task 19; here the point is only that the argument type-checks,
    so the wiring below is about the four repositories and not about notifications.
    """

    async def checklist_template_empty(self, alert: EmptyTemplateAlert) -> None:
        return None

    async def checklist_finished_with_skips(self, alert: PendingItemsAlert) -> None:
        return None

    async def checklist_overdue(self, alert: PendingItemsAlert) -> None:
        return None


def assemble(session: AsyncSession, venue_id: int) -> ChecklistService:
    """The wiring a handler will do (plan, task 24), written where mypy reads it.

    `ChecklistService` takes protocols, so a repository that is missing a method the
    service calls is invisible at runtime until the call is made — and the service tests
    pass fakes, which have every method by construction. Only the concrete classes prove
    the contract is whole, and only a type checker can see it: this function never needs to
    run for the check to bite.
    """
    return ChecklistService(
        templates=ChecklistTemplateRepo(session, venue_id),
        items=ChecklistItemRepo(session, venue_id),
        runs=ChecklistRunRepo(session, venue_id),
        run_items=ChecklistRunItemRepo(session, venue_id),
        notifier=SilentNotifier(),
    )


def as_reopenable(runs: ChecklistRunRepository) -> ReopenableRunRepository:
    """The frozen contract already satisfies the service's local protocol.

    `ReopenableRunRepository` was declared inside `src/services/checklists.py` because
    `reopen` was missing from `ChecklistRunRepository`. It is there now, so the two
    protocols describe the same interface and this conversion needs no cast — which is
    exactly the statement "the local protocol is redundant", checked rather than asserted
    in a comment. The day `reopen` is dropped from the frozen contract, mypy fails here.
    """
    return runs


def test_the_service_assembles_out_of_the_real_repositories() -> None:
    service = assemble(as_session(Recorder()), VENUE_ID)

    assert isinstance(service, ChecklistService)


def test_the_run_repository_satisfies_both_protocols() -> None:
    runs = ChecklistRunRepo(as_session(Recorder()), VENUE_ID)

    assert as_reopenable(runs) is runs


# --------------------------------------------------------------------------------------
# Behaviour — against a real PostgreSQL
# --------------------------------------------------------------------------------------


@pytest.mark.db
async def test_active_template_and_run_by_shift(session: AsyncSession) -> None:
    venue = await create_venue(session)
    user = await create_user(session)
    template = await create_checklist_template(session, venue)
    shift = await create_shift(session, venue, user, is_opener=True)
    item = await create_checklist_item(session, template)

    templates = ChecklistTemplateRepo(session, venue.id)
    runs = ChecklistRunRepo(session, venue.id)
    items = ChecklistItemRepo(session, venue.id)
    run_items = ChecklistRunItemRepo(session, venue.id)

    active = await templates.get_active(ChecklistType.OPENING)
    assert active is not None
    assert active.id == template.id
    assert await items.count_for_template(template.id) == 1

    run = await runs.create(
        shift_id=shift.id,
        user_id=user.id,
        template_id=template.id,
        checklist_type=ChecklistType.OPENING,
        total_items=1,
        sent_at=MOMENT,
    )
    assert await run_items.create_for_run(run.id, [item.id]) == 1

    found = await runs.get_by_shift(shift.id, ChecklistType.OPENING)
    assert found is not None
    assert found.id == run.id
    assert await runs.get_by_shift(shift.id, ChecklistType.CLOSING) is None
    assert await templates.is_referenced_by_runs(template.id) is True


@pytest.mark.db
async def test_a_repeated_tap_changes_nothing_and_the_counter_holds(
    session: AsyncSession,
) -> None:
    venue = await create_venue(session)
    user = await create_user(session)
    template = await create_checklist_template(session, venue)
    first_item = await create_checklist_item(session, template, order_index=1)
    second_item = await create_checklist_item(session, template, order_index=2)

    runs = ChecklistRunRepo(session, venue.id)
    run_items = ChecklistRunItemRepo(session, venue.id)
    run = await runs.create(
        shift_id=None,
        user_id=user.id,
        template_id=template.id,
        checklist_type=ChecklistType.STOCK,
        total_items=2,
        sent_at=MOMENT,
    )
    await run_items.create_for_run(run.id, [first_item.id, second_item.id])

    ticked = await run_items.set_done(
        run_id=run.id, item_id=first_item.id, is_done=True, moment=MOMENT
    )
    again = await run_items.set_done(
        run_id=run.id, item_id=first_item.id, is_done=True, moment=LATER
    )

    assert ticked is not None
    assert ticked.is_done is True
    assert ticked.done_at is not None
    assert again is None, "the second tap must find no row to flip"
    assert await run_items.count_done(run.id) == 1

    await runs.mark_started(run.id, MOMENT)
    await runs.mark_started(run.id, LATER)
    refreshed = await runs.refresh_counters(run.id)
    assert refreshed is not None
    assert refreshed.done_items == 1
    assert refreshed.total_items == 2
    assert refreshed.status is RunStatus.IN_PROGRESS
    assert refreshed.started_at == MOMENT, "the first tap wins"

    untick = await run_items.set_done(
        run_id=run.id, item_id=first_item.id, is_done=False, moment=LATER
    )
    assert untick is not None
    assert untick.is_done is False
    assert untick.done_at is None
    assert await run_items.count_done(run.id) == 0


@pytest.mark.db
async def test_one_completion_even_when_done_is_pressed_twice(session: AsyncSession) -> None:
    venue = await create_venue(session)
    user = await create_user(session)
    template = await create_checklist_template(session, venue)
    item = await create_checklist_item(session, template)

    runs = ChecklistRunRepo(session, venue.id)
    run_items = ChecklistRunItemRepo(session, venue.id)
    run = await runs.create(
        shift_id=None,
        user_id=user.id,
        template_id=template.id,
        checklist_type=ChecklistType.STOCK,
        total_items=1,
        sent_at=MOMENT,
    )
    await run_items.create_for_run(run.id, [item.id])

    finished = await runs.complete(
        run.id,
        moment=MOMENT,
        status=RunStatus.SKIPPED,
        completed_by=user.id,
        skip_comment="reason",
    )
    second = await runs.complete(
        run.id,
        moment=LATER,
        status=RunStatus.COMPLETED,
        completed_by=user.id,
    )

    assert finished is not None
    assert finished.status is RunStatus.SKIPPED
    assert finished.completed_at == MOMENT
    assert second is None, "one completion is one manager notification"


@pytest.mark.db
async def test_overdue_is_writable_and_only_once(session: AsyncSession) -> None:
    """Contract addition: `list_by_status(OVERDUE)` needs something that sets the status."""
    venue = await create_venue(session)
    user = await create_user(session)
    template = await create_checklist_template(session, venue)

    runs = ChecklistRunRepo(session, venue.id)
    run = await runs.create(
        shift_id=None,
        user_id=user.id,
        template_id=template.id,
        checklist_type=ChecklistType.OPENING,
        total_items=0,
        sent_at=MOMENT,
    )

    marked = await runs.mark_overdue(run.id, LATER)
    assert marked is not None
    assert marked.status is RunStatus.OVERDUE
    assert await runs.mark_overdue(run.id, LATER) is None

    listed = await runs.list_by_status(RunStatus.OVERDUE)
    assert [row.id for row in listed] == [run.id]

    await runs.complete(run.id, moment=LATER, status=RunStatus.COMPLETED, completed_by=user.id)
    assert await runs.mark_overdue(run.id, LATER) is None, "a finished run is never overdue"


@pytest.mark.db
async def test_reopen_undoes_one_completion_and_only_the_first_call_wins(
    session: AsyncSession,
) -> None:
    """TZ 5.4: a manager may reopen a finished run, and reopening it twice is one reopen."""
    venue = await create_venue(session)
    stranger = await create_venue(session)
    user = await create_user(session)
    template = await create_checklist_template(session, venue)

    runs = ChecklistRunRepo(session, venue.id)
    run = await runs.create(
        shift_id=None,
        user_id=user.id,
        template_id=template.id,
        checklist_type=ChecklistType.OPENING,
        total_items=0,
        sent_at=MOMENT,
    )
    await runs.mark_started(run.id, MOMENT)
    await runs.complete(
        run.id,
        moment=LATER,
        status=RunStatus.SKIPPED,
        completed_by=user.id,
        skip_comment="reason",
    )

    assert await ChecklistRunRepo(session, stranger.id).reopen(run.id) is None, (
        "a run of another venue is not reopenable (acceptance 11.3)"
    )

    reopened = await runs.reopen(run.id)
    assert reopened is not None
    assert reopened.completed_at is None
    assert reopened.status is RunStatus.IN_PROGRESS
    assert reopened.started_at == MOMENT, "the ticks and their start are left where they were"
    assert reopened.completed_by == user.id, "who finished it is history, not state"
    assert reopened.skip_comment == "reason"

    assert await runs.reopen(run.id) is None, "two managers pressing at once reopen it once"

    finished_again = await runs.complete(
        run.id,
        moment=LATER,
        status=RunStatus.COMPLETED,
        completed_by=user.id,
    )
    assert finished_again is not None
    assert finished_again.status is RunStatus.COMPLETED


@pytest.mark.db
async def test_nothing_of_another_venue_is_reachable(session: AsyncSession) -> None:
    """Acceptance 11.3: a foreign id resolves to nothing, not to a row."""
    venue = await create_venue(session)
    stranger = await create_venue(session)
    user = await create_user(session)
    template = await create_checklist_template(session, venue)
    item = await create_checklist_item(session, template)

    runs = ChecklistRunRepo(session, venue.id)
    run_items = ChecklistRunItemRepo(session, venue.id)
    run = await runs.create(
        shift_id=None,
        user_id=user.id,
        template_id=template.id,
        checklist_type=ChecklistType.STOCK,
        total_items=1,
        sent_at=MOMENT,
    )
    await run_items.create_for_run(run.id, [item.id])

    foreign_templates = ChecklistTemplateRepo(session, stranger.id)
    foreign_items = ChecklistItemRepo(session, stranger.id)
    foreign_runs = ChecklistRunRepo(session, stranger.id)
    foreign_run_items = ChecklistRunItemRepo(session, stranger.id)

    assert await foreign_templates.get(template.id) is None
    assert await foreign_items.get(item.id) is None
    assert await foreign_items.list_for_template(template.id) == []
    assert await foreign_runs.get(run.id) is None
    assert await foreign_run_items.list_for_run(run.id) == []
    assert await foreign_run_items.create_for_run(run.id, [item.id]) == 0
    assert (
        await foreign_run_items.set_done(
            run_id=run.id, item_id=item.id, is_done=True, moment=MOMENT
        )
        is None
    )
    assert (
        await foreign_runs.complete(
            run.id, moment=MOMENT, status=RunStatus.COMPLETED, completed_by=user.id
        )
        is None
    )
    assert await run_items.count_done(run.id) == 0, "the foreign tap changed nothing"


@pytest.mark.db
async def test_bulk_entry_and_group_rename(session: AsyncSession) -> None:
    """Decision B6: forty lines arrive in one message, groups are plain columns (D2)."""
    venue = await create_venue(session)
    template = await create_checklist_template(session, venue)
    items = ChecklistItemRepo(session, venue.id)

    created = await items.add_many(
        template.id,
        [
            {"text": "a", "group_index": 0, "order_index": 1},
            {"text": "b", "group_index": 0, "order_index": 2},
            {"text": "c", "group_index": 1, "order_index": 1},
        ],
    )
    assert len(created) == 3
    assert await items.rename_group(template.id, 0, "Bar") == 2

    listed = await items.list_for_template(template.id)
    assert [row.text for row in listed] == ["a", "b", "c"]
    assert [row.group_name for row in listed] == ["Bar", "Bar", None]

    assert await items.delete(listed[2].id) is True
    assert await items.count_for_template(template.id) == 2


# --------------------------------------------------------------------------------------
# Concurrency — two connections on one row
# --------------------------------------------------------------------------------------


@pytest.mark.db
async def test_two_sessions_never_tick_or_finish_the_same_run_twice(engine: AsyncEngine) -> None:
    """The double tap and the double "Done", as they actually arrive: on two connections.

    Both races run against rows that are committed, so the second statement really has to
    wait for the first transaction to end. Under READ COMMITTED the waiting statement then
    re-evaluates its `WHERE` against the row the winner left behind — `is_done` is already
    `true`, `completed_at` is no longer `NULL` — and updates nothing. That is the whole of
    "one flip, one completion, one manager notification" (TZ 5.4, plan task 17), and a
    sequential test cannot show it: one session never waits for itself.

    The commit lives inside each coroutine for the same reason; if both sessions held their
    transactions open until `gather` returned, the loser would wait for a lock nobody ever
    releases.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    venue_id: int | None = None
    user_id: int | None = None
    try:
        async with factory() as setup:
            venue = await create_venue(setup)
            user = await create_user(setup)
            template = await create_checklist_template(setup, venue)
            item = await create_checklist_item(setup, template)
            run = await ChecklistRunRepo(setup, venue.id).create(
                shift_id=None,
                user_id=user.id,
                template_id=template.id,
                checklist_type=ChecklistType.STOCK,
                total_items=1,
                sent_at=MOMENT,
            )
            await ChecklistRunItemRepo(setup, venue.id).create_for_run(run.id, [item.id])
            await setup.commit()
            venue_id, user_id = venue.id, user.id
            scope, run_id, item_id, who = venue.id, run.id, item.id, user.id

        async def tap(session: AsyncSession, moment: dt.datetime) -> ChecklistRunItem | None:
            repo = ChecklistRunItemRepo(session, scope)
            flipped = await repo.set_done(
                run_id=run_id, item_id=item_id, is_done=True, moment=moment
            )
            await session.commit()
            return flipped

        async def press_done(session: AsyncSession, moment: dt.datetime) -> ChecklistRun | None:
            repo = ChecklistRunRepo(session, scope)
            finished = await repo.complete(
                run_id, moment=moment, status=RunStatus.COMPLETED, completed_by=who
            )
            await session.commit()
            return finished

        async with factory() as first, factory() as second:
            taps = await asyncio.gather(tap(first, MOMENT), tap(second, LATER))

        assert [row is not None for row in taps].count(True) == 1, (
            "both connections flipped the same line: the counter would now count it twice"
        )

        async with factory() as after_taps:
            assert await ChecklistRunItemRepo(after_taps, scope).count_done(run_id) == 1
            counted = await ChecklistRunRepo(after_taps, scope).refresh_counters(run_id)
            assert counted is not None
            assert counted.done_items == 1
            assert counted.total_items == 1
            await after_taps.commit()

        async with factory() as third, factory() as fourth:
            presses = await asyncio.gather(press_done(third, MOMENT), press_done(fourth, LATER))

        assert [row is not None for row in presses].count(True) == 1, (
            "two completions are two manager notifications for one checklist"
        )

        async with factory() as check:
            stored = await ChecklistRunRepo(check, scope).get(run_id)
            assert stored is not None
            assert stored.status is RunStatus.COMPLETED
            assert stored.completed_at in (MOMENT, LATER), "the winner's moment, and only it"
    finally:
        if venue_id is not None:
            async with factory() as cleanup:
                # Runs first: they reference the template with RESTRICT and the user with
                # RESTRICT, and the run items go with them (CASCADE).
                await cleanup.execute(delete(ChecklistRun).where(ChecklistRun.venue_id == venue_id))
                await cleanup.execute(
                    delete(ChecklistTemplate).where(ChecklistTemplate.venue_id == venue_id)
                )
                await cleanup.execute(delete(Venue).where(Venue.id == venue_id))
                if user_id is not None:
                    await cleanup.execute(delete(User).where(User.id == user_id))
                await cleanup.commit()
