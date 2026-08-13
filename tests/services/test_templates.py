"""Checklist template editor (plan, task 28; TZ 5.4, 5.8; decisions B3, B6, D2).

The two fakes below are copies of the contract of ``src/db/repositories/checklists.py``, not
stand-ins for "a database", and three details are copied deliberately because the service is
built on top of them:

* ``checklist_templates`` owns ``venue_id``, so every statement of ``ChecklistTemplateRepo``
  carries ``AND venue_id = :venue_id`` — :meth:`FakeTemplates._scoped` is that predicate;
* ``checklist_items`` owns none (decision D9): ``ChecklistItemRepo`` is a ``ChildRepository``
  and reaches its venue through a join to ``checklist_templates``, including in ``get()``,
  which addresses a row by its own id. :meth:`FakeItems._parent` is that join, and the write
  side raises ``NoResultFound`` on a foreign template exactly as ``_owned_template`` does —
  a fake that answered by bare id would make ``test_item_of_another_venue_is_invisible`` pass
  against a service that has no isolation at all;
* D7 puts a partial unique index on ``(venue_id, type) WHERE is_active``, so
  :meth:`FakeTemplates.create` refuses a second active template of a type. That is what makes
  "deactivate the old version *before* inserting the new one" a rule the tests can check
  rather than a comment in the service.

Both fakes hold the *whole* table, every venue in it, and :meth:`FakeTemplates.neighbour`
hands the same two tables to another venue's repositories — the bar next door writes real
rows into the shared tables and none of them is reachable from here (TZ 3.3, TZ 9,
acceptance 11.3).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError, NoResultFound
from src.db.models import (
    Base,
    ChecklistItem,
    ChecklistTemplate,
    ChecklistType,
    MemberRole,
)
from src.services.access import AccessContext, PermissionDeniedError
from src.services.audit import AuditTrail
from src.services.templates import (
    BlankTextError,
    InactiveTemplateError,
    MoveDirection,
    TemplateGroupNotFoundError,
    TemplateItemNotFoundError,
    TemplateNotFoundError,
    TemplateService,
    parse_bulk,
)

VENUE_ID = 1
#: The bar next door: its rows live in the same tables and must be unreachable from here.
OTHER_VENUE_ID = 2
MANAGER_ID = 9
STAFF_ID = 7

#: 8 groups, 40 lines — the volume TZ 5.4 names and decision B6 is about.
BULK_MESSAGE = "\n".join(
    line
    for group in range(1, 9)
    for line in (f"# Group {group}", *(f"line {group}.{number}" for number in range(1, 6)))
)


def manager(venue_id: int = VENUE_ID) -> AccessContext:
    return AccessContext(
        user_id=MANAGER_ID,
        telegram_id=1000 + MANAGER_ID,
        venue_id=venue_id,
        member_id=MANAGER_ID,
        role=MemberRole.MANAGER,
        full_name="manager",
    )


def staff(venue_id: int = VENUE_ID) -> AccessContext:
    return AccessContext(
        user_id=STAFF_ID,
        telegram_id=1000 + STAFF_ID,
        venue_id=venue_id,
        member_id=STAFF_ID,
        role=MemberRole.STAFF,
        full_name="bartender",
    )


# --------------------------------------------------------------------------------------
# Model factories (declared here on purpose: tests/factories.py belongs to task 5a)
# --------------------------------------------------------------------------------------


def make_template(
    template_id: int,
    *,
    version: int = 1,
    is_active: bool = True,
    checklist_type: ChecklistType = ChecklistType.OPENING,
    venue_id: int = VENUE_ID,
) -> ChecklistTemplate:
    return ChecklistTemplate(
        id=template_id,
        venue_id=venue_id,
        type=checklist_type,
        name=checklist_type.value,
        version=version,
        is_active=is_active,
    )


def make_item(
    item_id: int,
    template_id: int,
    *,
    text: str,
    group_index: int = 0,
    group_name: str | None = None,
    order_index: int = 0,
    is_critical: bool = False,
    requires_photo: bool = False,
    requires_comment: bool = False,
) -> ChecklistItem:
    return ChecklistItem(
        id=item_id,
        template_id=template_id,
        group_index=group_index,
        group_name=group_name,
        order_index=order_index,
        text=text,
        requires_photo=requires_photo,
        requires_comment=requires_comment,
        is_critical=is_critical,
    )


# --------------------------------------------------------------------------------------
# Fake repositories
# --------------------------------------------------------------------------------------


class FakeItems:
    """`ChecklistItemRepository` — a child of `checklist_templates` (decision D9).

    Rows come back in insertion order, unsorted on purpose: sorting by
    ``(group_index, order_index)`` is the service's job and a fake that did it would hide a
    service that forgot.
    """

    def __init__(
        self,
        items: Sequence[ChecklistItem] = (),
        *,
        venue_id: int = VENUE_ID,
        table: list[ChecklistItem] | None = None,
    ) -> None:
        #: The whole `checklist_items` table, every venue in it: the scope is the `WHERE`
        #: and not the storage, so another venue's repository may adopt the same list.
        self.items: list[ChecklistItem] = list(items) if table is None else table
        self.venue_id = venue_id
        #: The templates table this child hangs off; `FakeTemplates` owns it and hands it over.
        self._templates: dict[int, ChecklistTemplate] = {}

    def bind_templates(self, templates: dict[int, ChecklistTemplate]) -> None:
        self._templates = templates

    @property
    def parent(self) -> type[Base]:
        return ChecklistTemplate

    @property
    def parent_fk(self) -> str:
        return "template_id"

    def new_id(self) -> int:
        """Ids are unique across the table, which every venue shares."""
        return max((item.id for item in self.items), default=0) + 1

    def _parent(self, template_id: int) -> ChecklistTemplate | None:
        """`JOIN checklist_templates ON ... WHERE checklist_templates.venue_id = :vid` (D9).

        `None` means the template is unknown *or* belongs to another venue — one answer for
        both, which is what makes a forged `template_id` address nothing (TZ 9).
        """
        template = self._templates.get(template_id)
        if template is None or template.venue_id != self.venue_id:
            return None
        return template

    def _owned(self, template_id: int) -> int:
        """`ChecklistItemRepo._owned_template`: a write into a foreign template raises."""
        if self._parent(template_id) is None:
            raise NoResultFound(
                f"checklist template {template_id} does not belong to venue {self.venue_id}"
            )
        return template_id

    def _scoped(self, item_id: int) -> ChecklistItem | None:
        item = next((row for row in self.items if row.id == item_id), None)
        if item is None or self._parent(item.template_id) is None:
            return None
        return item

    async def get(self, item_id: int) -> ChecklistItem | None:
        return self._scoped(item_id)

    async def list_for_template(self, template_id: int) -> Sequence[ChecklistItem]:
        if self._parent(template_id) is None:
            return []
        return [item for item in self.items if item.template_id == template_id]

    async def count_for_template(self, template_id: int) -> int:
        return len(await self.list_for_template(template_id))

    async def add(
        self,
        *,
        template_id: int,
        text: str,
        group_name: str | None,
        group_index: int,
        order_index: int,
        requires_photo: bool = False,
        requires_comment: bool = False,
        is_critical: bool = False,
    ) -> ChecklistItem:
        self._owned(template_id)
        item = make_item(
            self.new_id(),
            template_id,
            text=text,
            group_name=group_name,
            group_index=group_index,
            order_index=order_index,
            requires_photo=requires_photo,
            requires_comment=requires_comment,
            is_critical=is_critical,
        )
        self.items.append(item)
        return item

    async def add_many(
        self,
        template_id: int,
        items: Sequence[dict[str, Any]],
    ) -> Sequence[ChecklistItem]:
        self._owned(template_id)
        created = [
            make_item(
                self.new_id() + offset,
                template_id,
                text=str(payload["text"]),
                group_name=payload.get("group_name"),
                group_index=int(payload.get("group_index", 0)),
                order_index=int(payload.get("order_index", 0)),
                requires_photo=bool(payload.get("requires_photo", False)),
                requires_comment=bool(payload.get("requires_comment", False)),
                is_critical=bool(payload.get("is_critical", False)),
            )
            for offset, payload in enumerate(items)
        ]
        self.items.extend(created)
        return created

    async def update(self, item_id: int, **fields: Any) -> ChecklistItem | None:
        item = self._scoped(item_id)
        if item is None:
            return None
        for name, value in _writable(ChecklistItem, fields).items():
            setattr(item, name, value)
        return item

    async def delete(self, item_id: int) -> bool:
        item = self._scoped(item_id)
        if item is None:
            return False
        self.items.remove(item)
        return True

    async def rename_group(self, template_id: int, group_index: int, name: str) -> int:
        renamed = [
            item
            for item in await self.list_for_template(template_id)
            if item.group_index == group_index
        ]
        for item in renamed:
            item.group_name = name
        return len(renamed)


class FakeTemplates:
    """`ChecklistTemplateRepository`, scoped to one venue over a shared table (TZ 3.3).

    `is_referenced_by_runs` is the real predicate too: the statement counts
    ``checklist_runs`` of *this* venue, so a template of the bar next door is never "in use"
    from here — it is simply not visible at all.
    """

    def __init__(
        self,
        items: FakeItems,
        templates: Sequence[ChecklistTemplate] = (),
        *,
        venue_id: int = VENUE_ID,
        table: dict[int, ChecklistTemplate] | None = None,
        runs: list[tuple[int, int]] | None = None,
    ) -> None:
        #: The whole `checklist_templates` table, every venue in it.
        self.templates = (
            {template.id: template for template in templates} if table is None else table
        )
        #: `checklist_runs` reduced to what B3 needs: (venue_id, template_id) of each run.
        self.runs: list[tuple[int, int]] = [] if runs is None else runs
        self.venue_id = venue_id
        self._items = items
        items.bind_templates(self.templates)

    @property
    def items(self) -> FakeItems:
        """The child repository bound to this one — same tables, same venue."""
        return self._items

    def neighbour(self, venue_id: int = OTHER_VENUE_ID) -> FakeTemplates:
        """The same two tables seen through another venue's repositories (11.3)."""
        items = FakeItems(venue_id=venue_id, table=self._items.items)
        return FakeTemplates(items, venue_id=venue_id, table=self.templates, runs=self.runs)

    def new_id(self) -> int:
        return max(self.templates, default=0) + 1

    def _scoped(self, template_id: int) -> ChecklistTemplate | None:
        """`WHERE id = :template_id AND venue_id = :venue_id`, in every statement."""
        template = self.templates.get(template_id)
        if template is None or template.venue_id != self.venue_id:
            return None
        return template

    async def get(self, template_id: int) -> ChecklistTemplate | None:
        return self._scoped(template_id)

    async def get_active(self, checklist_type: ChecklistType) -> ChecklistTemplate | None:
        for template in self.templates.values():
            if template.venue_id != self.venue_id:
                continue
            if template.type is checklist_type and template.is_active:
                return template
        return None

    async def list_versions(self, checklist_type: ChecklistType) -> Sequence[ChecklistTemplate]:
        return sorted(
            (
                template
                for template in self.templates.values()
                if template.venue_id == self.venue_id and template.type is checklist_type
            ),
            key=lambda template: template.version,
            reverse=True,
        )

    async def create(
        self,
        *,
        checklist_type: ChecklistType,
        name: str,
        version: int,
        updated_by: int | None = None,
    ) -> ChecklistTemplate:
        # Decision D7: `uq_checklist_templates_active` is a partial unique index, so a second
        # active template of a type does not exist even for the length of one statement.
        if await self.get_active(checklist_type) is not None:
            raise IntegrityError(
                "INSERT INTO checklist_templates",
                None,
                Exception("uq_checklist_templates_active"),
            )
        template = ChecklistTemplate(
            id=self.new_id(),
            venue_id=self.venue_id,
            type=checklist_type,
            name=name,
            version=version,
            is_active=True,
            updated_by=updated_by,
        )
        self.templates[template.id] = template
        return template

    async def update(self, template_id: int, **fields: Any) -> ChecklistTemplate | None:
        template = self._scoped(template_id)
        if template is None:
            return None
        for name, value in _writable(ChecklistTemplate, fields).items():
            setattr(template, name, value)
        return template

    async def deactivate(self, template_id: int) -> ChecklistTemplate | None:
        return await self.update(template_id, is_active=False)

    async def is_referenced_by_runs(self, template_id: int) -> bool:
        return any(
            venue_id == self.venue_id and referenced == template_id
            for venue_id, referenced in self.runs
        )


class FakeAudit:
    """`AuditSink`: the trail appends and never reads, so the fake is a list."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(
        self,
        *,
        user_id: int | None,
        entity: str,
        entity_id: int | None,
        action: str,
        diff: dict[str, Any] | None = None,
    ) -> None:
        self.records.append(
            {
                "user_id": user_id,
                "entity": entity,
                "entity_id": entity_id,
                "action": action,
                "diff": diff,
            }
        )

    def of(self, entity: str, action: str) -> list[dict[str, Any]]:
        return [
            record
            for record in self.records
            if record["entity"] == entity and record["action"] == action
        ]


def _writable(model: type[Any], fields: dict[str, Any]) -> dict[str, Any]:
    """`_writable` of the real module: unknown keys and the identity columns are dropped."""
    columns = model.__table__.c
    return {
        name: value
        for name, value in fields.items()
        if name in columns and name not in {"id", "venue_id"}
    }


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def audit() -> FakeAudit:
    return FakeAudit()


@pytest.fixture
def repos() -> FakeTemplates:
    """An empty venue — the state every venue is delivered in (TZ 8.1)."""
    return FakeTemplates(FakeItems())


def build(repos: FakeTemplates, audit: FakeAudit | None = None) -> TemplateService:
    return TemplateService(
        templates=repos,
        items=repos.items,
        audit=AuditTrail(audit),
    )


def texts(service_view: Any) -> list[str]:
    return [item.text for item in service_view.items]


# --------------------------------------------------------------------------------------
# parse_bulk — a pure function, tested without a service (decision B6)
# --------------------------------------------------------------------------------------


def test_parse_bulk_reads_eight_groups_and_forty_lines() -> None:
    plan = parse_bulk(BULK_MESSAGE)

    assert plan.group_count == 8
    assert plan.item_count == 40
    assert [group.name for group in plan.groups] == [f"Group {number}" for number in range(1, 9)]
    assert plan.groups[0].items[0] == "line 1.1"


def test_parse_bulk_ignores_blank_lines_and_puts_a_lead_in_an_unnamed_group() -> None:
    plan = parse_bulk("\n\nturn the lights on\n   \n# Station\nice\n\nsyrups\n")

    assert [(group.name, group.items) for group in plan.groups] == [
        (None, ("turn the lights on",)),
        ("Station", ("ice", "syrups")),
    ]


def test_parse_bulk_joins_an_indented_line_to_the_previous_item() -> None:
    """The continuation rule of task 44: syntax the manager is told, never a guess."""
    plan = parse_bulk("# Station\nfruit slices,\n    berries\ngarnishes")

    assert plan.groups[0].items == ("fruit slices, berries", "garnishes")


def test_parse_bulk_treats_an_indented_hash_as_text() -> None:
    """The same rule doubles as the escape for a line that has to start with a hash."""
    plan = parse_bulk("# Station\nice\n  # 3 buckets")

    assert plan.group_count == 1
    assert plan.groups[0].items == ("ice # 3 buckets",)


def test_parse_bulk_drops_a_header_without_items() -> None:
    """A group does not exist without items (decision D2), so there is nothing to create."""
    plan = parse_bulk("# Station\n# Glassware\nglasses")

    assert [(group.name, group.items) for group in plan.groups] == [("Glassware", ("glasses",))]


def test_parse_bulk_of_an_empty_message_is_empty() -> None:
    assert parse_bulk("   \n\n  ").is_empty


# --------------------------------------------------------------------------------------
# The empty state (TZ 8.1)
# --------------------------------------------------------------------------------------


async def test_view_of_a_venue_without_checklists_is_a_screen_not_a_none(
    repos: FakeTemplates,
) -> None:
    view = await build(repos).view(manager(), ChecklistType.OPENING)

    assert view.exists is False
    assert view.is_empty is True
    assert view.groups == ()
    assert view.total_items == 0


async def test_the_first_item_creates_the_template_and_its_group(
    repos: FakeTemplates,
    audit: FakeAudit,
) -> None:
    result = await build(repos, audit).add_item(
        manager(),
        checklist_type=ChecklistType.OPENING,
        text="turn the lights on",
        group_name="Visual",
    )

    assert result.view.exists is True
    assert result.view.version == 1
    assert result.forked is False
    assert [(group.name, group.total_count) for group in result.view.groups] == [("Visual", 1)]
    assert audit.of("checklist_templates", "create")
    assert audit.of("checklist_items", "create")


# --------------------------------------------------------------------------------------
# Groups (decision D2)
# --------------------------------------------------------------------------------------


async def test_add_item_appends_to_a_group_that_already_exists(repos: FakeTemplates) -> None:
    service = build(repos)
    actor = manager()
    await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="ice", group_name="Station"
    )
    result = await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="syrups", group_name="Station"
    )

    assert result.view.group_count == 1
    assert texts(result.view) == ["ice", "syrups"]
    assert [item.order_index for item in result.view.items] == [0, 1]


async def test_add_to_new_group_opens_another_group_with_the_same_name(
    repos: FakeTemplates,
) -> None:
    """B6 and D2: the manager says which group they meant; the service never merges by name."""
    service = build(repos)
    actor = manager()
    await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="ice", group_name="Station"
    )
    result = await service.add_to_new_group(
        actor, checklist_type=ChecklistType.OPENING, group_name="Station", text="second station"
    )

    assert result.view.group_count == 2
    assert [group.index for group in result.view.groups] == [0, 1]


async def test_deleting_the_last_item_of_a_group_removes_the_group(
    repos: FakeTemplates,
    audit: FakeAudit,
) -> None:
    service = build(repos, audit)
    actor = manager()
    await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="ice", group_name="Station"
    )
    kept = await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="glasses", group_name="Glassware"
    )
    lonely = await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="syrups", group_name="Bar"
    )
    assert lonely.item_id is not None

    result = await service.delete_item(actor, lonely.item_id)

    assert [group.name for group in result.view.groups] == ["Station", "Glassware"]
    assert kept.item_id in [item.item_id for item in result.view.items]
    assert audit.of("checklist_items", "delete")[0]["diff"]["text"]["to"] == "syrups"


async def test_rename_group_renames_every_item_of_it(repos: FakeTemplates) -> None:
    service = build(repos)
    actor = manager()
    await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="ice", group_name="Station"
    )
    added = await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="syrups", group_name="Station"
    )
    assert added.view.template_id is not None

    result = await service.rename_group(
        actor, template_id=added.view.template_id, group_index=0, name="Bar station"
    )

    assert [group.name for group in result.view.groups] == ["Bar station"]
    assert {item.group_name for item in result.view.items} == {"Bar station"}


async def test_rename_of_an_unknown_group_is_refused(repos: FakeTemplates) -> None:
    service = build(repos)
    actor = manager()
    added = await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="ice", group_name="Station"
    )
    assert added.view.template_id is not None

    with pytest.raises(TemplateGroupNotFoundError):
        await service.rename_group(
            actor, template_id=added.view.template_id, group_index=4, name="Bar"
        )


# --------------------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------------------


async def test_move_item_swaps_with_its_neighbour_inside_the_group(
    repos: FakeTemplates,
    audit: FakeAudit,
) -> None:
    service = build(repos, audit)
    actor = manager()
    for text in ("ice", "syrups", "garnishes"):
        await service.add_item(
            actor, checklist_type=ChecklistType.OPENING, text=text, group_name="Station"
        )
    view = await service.view(actor, ChecklistType.OPENING)
    audit.records.clear()

    result = await service.move_item(actor, view.items[2].item_id, MoveDirection.UP)

    assert texts(result.view) == ["ice", "garnishes", "syrups"]
    assert [item.order_index for item in result.view.items] == [0, 1, 2]
    # Exactly the two rows that moved, each with the position it came from.
    moves = audit.of("checklist_items", "update")
    assert [record["diff"]["order_index"] for record in moves] == [
        {"from": 2, "to": 1},
        {"from": 1, "to": 2},
    ]


async def test_move_item_never_leaves_its_group(repos: FakeTemplates) -> None:
    service = build(repos)
    actor = manager()
    await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="ice", group_name="Station"
    )
    lower = await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="glasses", group_name="Glassware"
    )
    assert lower.item_id is not None

    result = await service.move_item(actor, lower.item_id, MoveDirection.UP)

    assert result.changed is False
    assert [group.name for group in result.view.groups] == ["Station", "Glassware"]


async def test_move_at_the_edge_of_a_group_changes_nothing_and_forks_nothing(
    repos: FakeTemplates,
) -> None:
    service = build(repos)
    actor = manager()
    added = await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="ice", group_name="Station"
    )
    assert added.item_id is not None
    repos.runs.append((VENUE_ID, added.view.template_id or 0))

    result = await service.move_item(actor, added.item_id, MoveDirection.UP)

    assert result.changed is False
    assert result.forked is False
    assert len(repos.templates) == 1


# --------------------------------------------------------------------------------------
# Copy-on-write (decision B3) — the point of the whole module
# --------------------------------------------------------------------------------------


async def test_an_edit_without_runs_stays_in_place(
    repos: FakeTemplates,
    audit: FakeAudit,
) -> None:
    service = build(repos, audit)
    actor = manager()
    added = await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="ice", group_name="Station"
    )
    assert added.item_id is not None

    result = await service.edit_text(actor, added.item_id, "ice, full bucket")

    assert result.forked is False
    assert result.view.template_id == added.view.template_id
    assert result.view.version == 1
    assert len(repos.templates) == 1
    assert texts(result.view) == ["ice, full bucket"]


async def test_an_edit_of_a_referenced_template_forks_and_the_old_version_keeps_its_items(
    repos: FakeTemplates,
    audit: FakeAudit,
) -> None:
    """The main test of decision B3: last month's report does not move (TZ 4.3)."""
    service = build(repos, audit)
    actor = manager()
    for text in ("ice", "syrups"):
        await service.add_item(
            actor, checklist_type=ChecklistType.OPENING, text=text, group_name="Station"
        )
    before = await service.view(actor, ChecklistType.OPENING)
    assert before.template_id is not None
    # A run of a past shift now points at this version, exactly as `create_run` leaves it.
    repos.runs.append((VENUE_ID, before.template_id))
    frozen = [(item.item_id, item.text) for item in before.items]

    result = await service.edit_text(actor, before.items[0].item_id, "ice, full bucket")

    # The edit landed on a new version...
    assert result.forked is True
    assert result.view.template_id != before.template_id
    assert result.view.version == 2
    assert texts(result.view) == ["ice, full bucket", "syrups"]
    # ...the old one was deactivated and is still there, with its own rows untouched, so the
    # run created from it renders exactly what the employee ticked.
    old = repos.templates[before.template_id]
    assert old.is_active is False
    assert [(item.id, item.text) for item in repos.items.items if item.template_id == old.id] == (
        frozen
    )
    assert audit.of("checklist_templates", "create")[-1]["diff"]["forked_from"]["to"] == old.id


async def test_a_second_edit_of_the_new_version_does_not_fork_again(
    repos: FakeTemplates,
) -> None:
    """Only a *referenced* template forks; the fresh version has no runs of its own yet."""
    service = build(repos)
    actor = manager()
    added = await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="ice", group_name="Station"
    )
    assert added.item_id is not None and added.view.template_id is not None
    repos.runs.append((VENUE_ID, added.view.template_id))

    first = await service.edit_text(actor, added.item_id, "ice, full bucket")
    assert first.item_id is not None
    second = await service.edit_text(actor, first.item_id, "ice, two buckets")

    assert first.forked is True
    assert second.forked is False
    assert len(repos.templates) == 2
    assert texts(second.view) == ["ice, two buckets"]


async def test_an_edit_that_changes_nothing_neither_forks_nor_records(
    repos: FakeTemplates,
    audit: FakeAudit,
) -> None:
    service = build(repos, audit)
    actor = manager()
    added = await service.add_item(
        actor,
        checklist_type=ChecklistType.OPENING,
        text="ice",
        group_name="Station",
        is_critical=True,
    )
    assert added.item_id is not None
    repos.runs.append((VENUE_ID, added.view.template_id or 0))
    audit.records.clear()

    result = await service.set_critical(actor, added.item_id, True)

    assert result.changed is False
    assert result.forked is False
    assert len(repos.templates) == 1
    assert audit.records == []


async def test_an_old_version_cannot_be_edited(repos: FakeTemplates) -> None:
    """A stale button from an old message must not rewrite what a report already shows."""
    service = build(repos)
    actor = manager()
    added = await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="ice", group_name="Station"
    )
    assert added.item_id is not None and added.view.template_id is not None
    stale_template = added.view.template_id
    stale_item = added.item_id
    repos.runs.append((VENUE_ID, stale_template))
    await service.edit_text(actor, stale_item, "ice, full bucket")

    with pytest.raises(InactiveTemplateError):
        await service.edit_text(actor, stale_item, "ice, three buckets")
    with pytest.raises(InactiveTemplateError):
        await service.rename_group(
            actor, template_id=stale_template, group_index=0, name="Bar station"
        )


# --------------------------------------------------------------------------------------
# Bulk input (decision B6)
# --------------------------------------------------------------------------------------


async def test_bulk_add_writes_forty_lines_in_eight_groups_at_once(
    repos: FakeTemplates,
    audit: FakeAudit,
) -> None:
    service = build(repos, audit)

    result = await service.bulk_add(
        manager(), checklist_type=ChecklistType.OPENING, text=BULK_MESSAGE
    )

    assert (result.added_items, result.added_groups) == (40, 8)
    assert result.view.group_count == 8
    assert result.view.total_items == 40
    assert [group.name for group in result.view.groups] == [f"Group {n}" for n in range(1, 9)]
    assert [item.order_index for item in result.view.groups[0].items] == [0, 1, 2, 3, 4]
    # One record for one action, not forty (TZ 2).
    updates = audit.of("checklist_templates", "update")
    assert len(updates) == 1
    assert updates[0]["diff"]["item_count"] == {"from": 0, "to": 40}


async def test_bulk_add_forks_the_referenced_template_exactly_once(
    repos: FakeTemplates,
    audit: FakeAudit,
) -> None:
    """Forty lines, one version: the literal reading of TZ 4.3 would have made forty (B3)."""
    service = build(repos, audit)
    actor = manager()
    added = await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="ice", group_name="Station"
    )
    assert added.view.template_id is not None
    repos.runs.append((VENUE_ID, added.view.template_id))
    audit.records.clear()

    result = await service.bulk_add(actor, checklist_type=ChecklistType.OPENING, text=BULK_MESSAGE)

    assert result.forked is True
    assert result.view.version == 2
    assert len(repos.templates) == 2
    assert len(audit.of("checklist_templates", "create")) == 1
    # The copy carries the line that was already there, and the new groups follow it.
    assert result.view.total_items == 41
    assert result.view.groups[0].name == "Station"
    assert result.view.group_count == 9


async def test_bulk_add_of_an_empty_message_writes_nothing_at_all(
    repos: FakeTemplates,
    audit: FakeAudit,
) -> None:
    result = await build(repos, audit).bulk_add(
        manager(), checklist_type=ChecklistType.OPENING, text="   \n\n"
    )

    assert (result.added_items, result.added_groups) == (0, 0)
    assert result.view.exists is False
    assert repos.templates == {}
    assert audit.records == []


# --------------------------------------------------------------------------------------
# Rights (TZ 2)
# --------------------------------------------------------------------------------------


async def test_staff_may_not_read_or_edit_the_template(repos: FakeTemplates) -> None:
    service = build(repos)
    seeded = await service.add_item(
        manager(), checklist_type=ChecklistType.OPENING, text="ice", group_name="Station"
    )
    assert seeded.item_id is not None and seeded.view.template_id is not None
    bartender = staff()

    with pytest.raises(PermissionDeniedError):
        await service.view(bartender, ChecklistType.OPENING)
    with pytest.raises(PermissionDeniedError):
        await service.add_item(
            bartender, checklist_type=ChecklistType.OPENING, text="x", group_name=None
        )
    with pytest.raises(PermissionDeniedError):
        await service.add_to_new_group(
            bartender, checklist_type=ChecklistType.OPENING, group_name="g", text="x"
        )
    with pytest.raises(PermissionDeniedError):
        await service.edit_text(bartender, seeded.item_id, "x")
    with pytest.raises(PermissionDeniedError):
        await service.set_critical(bartender, seeded.item_id, True)
    with pytest.raises(PermissionDeniedError):
        await service.set_photo(bartender, seeded.item_id, True)
    with pytest.raises(PermissionDeniedError):
        await service.delete_item(bartender, seeded.item_id)
    with pytest.raises(PermissionDeniedError):
        await service.move_item(bartender, seeded.item_id, MoveDirection.DOWN)
    with pytest.raises(PermissionDeniedError):
        await service.rename_group(
            bartender, template_id=seeded.view.template_id, group_index=0, name="g"
        )
    with pytest.raises(PermissionDeniedError):
        await service.bulk_add(bartender, checklist_type=ChecklistType.OPENING, text=BULK_MESSAGE)

    assert texts(await service.view(manager(), ChecklistType.OPENING)) == ["ice"]


async def test_blank_input_is_refused(repos: FakeTemplates) -> None:
    service = build(repos)
    actor = manager()

    with pytest.raises(BlankTextError):
        await service.add_item(
            actor, checklist_type=ChecklistType.OPENING, text="   ", group_name="Station"
        )
    assert repos.templates == {}


# --------------------------------------------------------------------------------------
# Venue isolation (TZ 3.3, TZ 9, acceptance 11.3)
# --------------------------------------------------------------------------------------


async def test_the_checklist_of_another_venue_is_invisible(repos: FakeTemplates) -> None:
    """Mutation-checked: drop the join in `FakeItems._parent` and this is what fails.

    The neighbour's rows are really in the shared tables — written through their own
    repositories, with ids a forged `callback_data` could carry — and not one of them is
    reachable from here, while the neighbour reads them perfectly well.
    """
    here = build(repos)
    next_door_repos = repos.neighbour()
    next_door = build(next_door_repos)

    theirs = await next_door.add_item(
        manager(OTHER_VENUE_ID),
        checklist_type=ChecklistType.OPENING,
        text="their ice",
        group_name="Their station",
    )
    assert theirs.item_id is not None and theirs.view.template_id is not None
    actor = manager()

    # Reads see nothing of it.
    assert (await here.view(actor, ChecklistType.OPENING)).exists is False
    # Writes addressed at it, by item id and by template id alike, find nothing.
    with pytest.raises(TemplateItemNotFoundError):
        await here.edit_text(actor, theirs.item_id, "mine now")
    with pytest.raises(TemplateItemNotFoundError):
        await here.set_critical(actor, theirs.item_id, True)
    with pytest.raises(TemplateItemNotFoundError):
        await here.delete_item(actor, theirs.item_id)
    with pytest.raises(TemplateItemNotFoundError):
        await here.move_item(actor, theirs.item_id, MoveDirection.DOWN)
    with pytest.raises(TemplateNotFoundError):
        await here.rename_group(
            actor, template_id=theirs.view.template_id, group_index=0, name="mine now"
        )

    # Nothing moved, and the venue it belongs to still reads it.
    assert texts(await next_door.view(manager(OTHER_VENUE_ID), ChecklistType.OPENING)) == [
        "their ice"
    ]


async def test_two_venues_edit_their_own_checklists_side_by_side(repos: FakeTemplates) -> None:
    """The shared table is a `WHERE`, not two storages: both venues work, neither leaks."""
    here = build(repos)
    next_door = build(repos.neighbour())

    await here.bulk_add(manager(), checklist_type=ChecklistType.OPENING, text=BULK_MESSAGE)
    await next_door.bulk_add(
        manager(OTHER_VENUE_ID), checklist_type=ChecklistType.OPENING, text="# Their\ntheir line"
    )

    mine = await here.view(manager(), ChecklistType.OPENING)
    theirs = await next_door.view(manager(OTHER_VENUE_ID), ChecklistType.OPENING)

    assert mine.total_items == 40
    assert theirs.total_items == 1
    assert len(repos.items.items) == 41


# --------------------------------------------------------------------------------------
# Audit (TZ 2, acceptance 11.3 / plan task 36)
# --------------------------------------------------------------------------------------


async def test_every_edit_is_recorded_with_the_actor_and_the_diff(
    repos: FakeTemplates,
    audit: FakeAudit,
) -> None:
    service = build(repos, audit)
    actor = manager()
    added = await service.add_item(
        actor, checklist_type=ChecklistType.OPENING, text="ice", group_name="Station"
    )
    assert added.item_id is not None and added.view.template_id is not None
    audit.records.clear()

    await service.edit_text(actor, added.item_id, "ice, full bucket")
    await service.set_photo(actor, added.item_id, True)
    await service.rename_group(
        actor, template_id=added.view.template_id, group_index=0, name="Bar station"
    )

    assert [record["user_id"] for record in audit.records] == [MANAGER_ID] * 3
    edits = audit.of("checklist_items", "update")
    assert edits[0]["entity_id"] == added.item_id
    assert edits[0]["diff"] == {"text": {"from": "ice", "to": "ice, full bucket"}}
    assert edits[1]["diff"] == {"requires_photo": {"from": False, "to": True}}
    assert audit.of("checklist_templates", "update")[0]["diff"]["group"]["to"] == {
        "index": 0,
        "name": "Bar station",
    }


async def test_a_service_without_an_audit_repository_is_silent_not_broken(
    repos: FakeTemplates,
) -> None:
    """`AuditTrail(None)`: the venue wizard and these tests both need it (audit module)."""
    result = await build(repos).bulk_add(
        manager(), checklist_type=ChecklistType.OPENING, text=BULK_MESSAGE
    )

    assert result.added_items == 40
