"""The checklist template editor (plan, task 28; TZ 5.4, 5.8; decisions B3, B6, D2).

This is the manager's half of the checklist module: the screen behind the checklist section
of the management menu. The employee's half — running a checklist — lives in
``src/services/checklists.py`` and never writes a template; this module never touches a run.
The two meet at exactly one point, :meth:`ChecklistTemplateRepository.is_referenced_by_runs`,
and that point is the whole of decision B3.

**Editing is copy-on-write, and the fork is per edit, not per item (B3).** TZ 4.3 says an
edit creates a new version. Read literally, entering a real checklist — 8 groups, ~40 lines
(TZ 5.4) — would leave 40 versions behind and a version list nobody can read. So: while no
``checklist_run`` points at the template, edits are applied in place; from the first
reference on, an edit first forks the template (``version + 1``, the old one deactivated,
every item copied) and is then applied to the copy. Runs keep pointing at the version they
were created from, which is what makes TZ 4.3's "last month's report does not move" true.

One edit is one fork. :meth:`TemplateService.bulk_add` adds forty items through a single
:meth:`_open`, so a manager pasting the whole checklist in one message (decision B6) creates
one new version, not forty. And an edit that changes nothing forks nothing: renaming a group
to the name it already has, or ticking "critical" on an item that is already critical, is not
a change, and a version created by it would be a version nobody asked for.

**Old versions are read-only.** Every entry point resolves the *active* template and refuses
an inactive one (:class:`InactiveTemplateError`). Not defensive decoration: an old version is
what the reports of TZ 5.9 read, forking it would collide with the version numbers that came
after it, and a `callback_data` still sitting in an old message is exactly how a manager would
get there (TZ 9).

**A group is not a row (decision D2).** ``checklist_items`` carries ``group_index`` and
``group_name``, so a group exists precisely while it has items: adding an item to a name that
is not there yet creates the group, deleting the last item of a group removes it, and there is
no "create empty group" operation to offer. Renaming a group is an update of its items.

**The empty state is the normal state (TZ 8.1).** A venue is delivered without a single
checklist line, so :meth:`TemplateService.view` answers with a :class:`TemplateView` whose
``template_id`` is ``None`` rather than with ``None``: the screen renders the same way with
zero groups and with eight. The template row itself is created lazily by the first write —
it holds no content of its own (its ``name`` is the technical type, the caption comes from
``src/bot/texts/``), so creating it early would still leave the venue empty, and creating it
in a migration would be seed data (CLAUDE.md).

Wording lives in ``src/bot/texts/``; this module returns structures and raises typed errors.
The transaction belongs to the caller (``session_scope``): nothing here commits.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.db.models import ChecklistItem, ChecklistTemplate, ChecklistType
from src.db.repositories.protocols import (
    ChecklistItemRepository,
    ChecklistTemplateRepository,
)
from src.services.access import (
    AccessContext,
    AccessError,
    require_manager,
    require_venue,
)
from src.services.audit import AuditEntity, AuditTrail, snapshot

#: Opens a group in the bulk syntax of decision B6: ``# Station``. The space is optional —
#: a header typed without it is still a header, not a checklist line starting with a hash.
GROUP_PREFIX = "#"

#: The columns of an item that the audit log carries (TZ 2). Deliberately not "every column":
#: a diff is meant to be read, and `template_id` is in it only because a fork moves the item.
AUDITED_ITEM_FIELDS = (
    "template_id",
    "group_index",
    "group_name",
    "order_index",
    "text",
    "is_critical",
    "requires_photo",
    "requires_comment",
)


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class TemplateError(AccessError):
    """Base class for the refusals of this module.

    Rooted in :class:`src.services.access.AccessError` like the roster and the schedule: a
    handler catches one family for "the service said no" and renders a text from
    ``src/bot/texts/``, while the error middleware keeps everything else as a bug.
    """


class TemplateNotFoundError(TemplateError):
    """No such template in the actor's venue.

    A template of another venue lands here too, and on purpose: the repository is scoped, so
    a forged `callback_data` resolves to nothing rather than to the neighbour's checklist
    (TZ 9, acceptance 11.3).
    """

    def __init__(self, template_id: int, venue_id: int) -> None:
        super().__init__(f"checklist template {template_id} does not belong to venue {venue_id}")
        self.template_id = template_id
        self.venue_id = venue_id


class TemplateItemNotFoundError(TemplateError):
    """No such item in the actor's venue.

    ``checklist_items`` has no ``venue_id`` (decision D9): the repository reaches the venue
    through a join to ``checklist_templates``, so an item id from another venue is not
    "refused" but simply absent, and it arrives here as the same error as a deleted one.
    """

    def __init__(self, item_id: int, venue_id: int) -> None:
        super().__init__(f"checklist item {item_id} does not belong to venue {venue_id}")
        self.item_id = item_id
        self.venue_id = venue_id


class TemplateGroupNotFoundError(TemplateError):
    """No group with that index in the template — it has no items, so it does not exist (D2)."""

    def __init__(self, template_id: int, group_index: int) -> None:
        super().__init__(f"checklist template {template_id} has no group {group_index}")
        self.template_id = template_id
        self.group_index = group_index


class InactiveTemplateError(TemplateError):
    """An edit addressed to a superseded version (decision B3, TZ 4.3).

    Old versions are what the runs and the reports of a past month read. Editing one would
    change history and would collide with the version numbers issued after it, so a stale
    button in an old message stops here.
    """

    def __init__(self, template_id: int, version: int) -> None:
        super().__init__(
            f"checklist template {template_id} is version {version} and is no longer the "
            "current one; only the active version can be edited"
        )
        self.template_id = template_id
        self.version = version


class BlankTextError(TemplateError):
    """Blank input is absent input: a line of spaces is not a checklist item."""


# --------------------------------------------------------------------------------------
# Render model
# --------------------------------------------------------------------------------------


class MoveDirection(enum.StrEnum):
    """Which way :meth:`TemplateService.move_item` shifts a line inside its group."""

    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class TemplateItemView:
    """One line of the template as the editing screen shows it."""

    item_id: int
    text: str
    group_index: int
    group_name: str | None
    order_index: int
    is_critical: bool
    requires_photo: bool
    requires_comment: bool


@dataclass(frozen=True, slots=True)
class TemplateGroupView:
    """A group of lines (TZ 5.4: a flat list of forty items is unreadable on a phone).

    ``index`` is ``group_index`` and orders the groups; it never addresses one, because
    deleting the last item of a group leaves a hole in the numbering (decision D2).
    """

    index: int
    name: str | None
    items: tuple[TemplateItemView, ...]

    @property
    def total_count(self) -> int:
        return len(self.items)


@dataclass(frozen=True, slots=True)
class TemplateView:
    """The whole editing screen, empty state included (TZ 8.1).

    ``template_id is None`` means the venue has never had a checklist of this type — the
    normal state of a freshly delivered venue, not an error. Everything else on the screen
    reads the same in both cases: zero groups, zero items, "add the first line".
    """

    template_id: int | None
    checklist_type: ChecklistType
    name: str | None
    version: int | None
    is_active: bool
    groups: tuple[TemplateGroupView, ...]

    @property
    def exists(self) -> bool:
        return self.template_id is not None

    @property
    def items(self) -> tuple[TemplateItemView, ...]:
        return tuple(item for group in self.groups for item in group.items)

    @property
    def total_items(self) -> int:
        return len(self.items)

    @property
    def group_count(self) -> int:
        return len(self.groups)

    @property
    def is_empty(self) -> bool:
        """TZ 8.1 and TZ 5.4: a template without items is never sent to anybody."""
        return not self.groups


# --------------------------------------------------------------------------------------
# Bulk input (decision B6)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedGroup:
    """One group of a bulk message: the header and the lines that followed it."""

    name: str | None
    items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BulkPlan:
    """What :func:`parse_bulk` read out of one message, before anything is written."""

    groups: tuple[ParsedGroup, ...]

    @property
    def group_count(self) -> int:
        return len(self.groups)

    @property
    def item_count(self) -> int:
        return sum(len(group.items) for group in self.groups)

    @property
    def is_empty(self) -> bool:
        return not self.groups


def parse_bulk(text: str) -> BulkPlan:
    """Read a bulk message into groups and lines (decision B6). Pure, and testable alone.

    The syntax is three rules and nothing else:

    * a line starting with ``#`` opens a new group and the rest of it is the group name;
    * any other line is a checklist item;
    * a blank line is ignored — a message typed on a phone is full of them.

    Items typed before the first header go into a group with no name (``group_name=None``),
    which is a real shape: a short checklist has no groups at all.

    **Continuation lines** are the fourth rule, and it is a decision rather than a reading of
    the TZ. Real checklists contain a line broken across two — "sliced fruit," continued by
    "berries" on the next one — and the tempting fix is a heuristic: glue a line onto the
    previous one when the previous ended in a comma, or when this one starts lowercase.
    Every such rule guesses, and guesses wrong on the day somebody writes a line that happens
    to end in a comma. So the rule here is explicit and typed by hand: **a line beginning with
    a space or a tab continues the previous item**, joined with a single space. It is syntax
    the manager is told about in the hint next to the input field (``src/bot/texts/``), not
    something the parser infers, and it doubles as the escape for a line that has to start
    with ``#``: indent it and it is text.

    A header with no items under it produces no group: a group does not exist without items
    (decision D2), so there is nothing to create and nothing to show.
    """
    parsed: list[tuple[str | None, list[str]]] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Checked on the raw line, before stripping — the indent *is* the marker.
        if raw[:1] in {" ", "\t"} and parsed and parsed[-1][1]:
            parsed[-1][1][-1] = f"{parsed[-1][1][-1]} {line}"
            continue
        if line.startswith(GROUP_PREFIX):
            parsed.append((_clean(line[len(GROUP_PREFIX) :]), []))
            continue
        if not parsed:
            parsed.append((None, []))
        parsed[-1][1].append(line)

    return BulkPlan(
        groups=tuple(ParsedGroup(name=name, items=tuple(items)) for name, items in parsed if items)
    )


# --------------------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EditResult:
    """The screen to redraw, plus the two facts the wording depends on.

    ``forked`` is what lets the bot say "a new version of the checklist has been created"
    (TZ 5.8 promises it in so many words), and ``changed`` is ``False`` for an edit that
    asked for the state the row was already in — no write, no version, nothing to announce.
    """

    view: TemplateView
    forked: bool
    item_id: int | None = None
    changed: bool = True


@dataclass(frozen=True, slots=True)
class BulkResult:
    """Outcome of one bulk message (decision B6): what was added, and at what cost."""

    view: TemplateView
    forked: bool
    added_items: int
    added_groups: int

    @property
    def changed(self) -> bool:
        return self.added_items > 0


@dataclass(slots=True)
class _Draft:
    """The template an edit is actually being applied to, after copy-on-write.

    ``remap`` is empty for an in-place edit and maps every old item id to its copy after a
    fork, so the caller keeps addressing "the item the manager tapped" without caring which
    version it now lives in.
    """

    template: ChecklistTemplate
    items: list[ChecklistItem]
    forked: bool
    remap: dict[int, int] = field(default_factory=dict)

    def track(self, item_id: int) -> int:
        return self.remap.get(item_id, item_id)


# --------------------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------------------


class TemplateService:
    """Editing the checklist of one venue (TZ 5.8). Holds no Telegram types, no session.

    Both repositories are already scoped to a venue (TZ 3.3), so no method takes a
    ``venue_id``: the venue comes from the :class:`AccessContext` and from nowhere else.
    Every method is a manager's (TZ 2) and says so with :func:`require_manager`, on the
    server, before it looks at anything — hiding a button is not a permission check.
    """

    def __init__(
        self,
        *,
        templates: ChecklistTemplateRepository,
        items: ChecklistItemRepository,
        audit: AuditTrail,
    ) -> None:
        self._templates = templates
        self._items = items
        self._audit = audit

    # ---------------------------------------------------------------------------------
    # Reading
    # ---------------------------------------------------------------------------------

    async def view(self, actor: AccessContext, checklist_type: ChecklistType) -> TemplateView:
        """The grouped editing screen, or its empty shape when nothing exists yet (TZ 8.1)."""
        require_manager(actor)
        template = await self._templates.get_active(checklist_type)
        if template is None:
            return _empty_view(checklist_type)
        return await self._view(template)

    # ---------------------------------------------------------------------------------
    # Items
    # ---------------------------------------------------------------------------------

    async def add_item(
        self,
        actor: AccessContext,
        *,
        checklist_type: ChecklistType,
        text: str,
        group_name: str | None,
        is_critical: bool = False,
        requires_photo: bool = False,
    ) -> EditResult:
        """Append a line to the end of a group, creating that group if it is not there (D2).

        The group is addressed by name because that is what the manager sees; an unknown name
        is not an error but the ordinary way a group comes into existence, since a group
        without items does not exist and cannot be created on its own.
        """
        require_manager(actor)
        body = _require_text(text, "a checklist item cannot be empty")
        label = _clean(group_name)

        template = await self._active_or_create(actor, checklist_type)
        draft = await self._open(actor, template)
        group_index = _find_group(draft.items, label)
        if group_index is None:
            group_index = _next_group_index(draft.items)
            order_index = 0
        else:
            order_index = _next_order_index(draft.items, group_index)

        return await self._append(
            actor,
            draft,
            text=body,
            group_name=label,
            group_index=group_index,
            order_index=order_index,
            is_critical=is_critical,
            requires_photo=requires_photo,
        )

    async def add_to_new_group(
        self,
        actor: AccessContext,
        *,
        checklist_type: ChecklistType,
        group_name: str,
        text: str,
    ) -> EditResult:
        """Open a new group at the end of the checklist with its first line (D2).

        Separate from :meth:`add_item` on purpose: there a repeated name means "the same
        group", here it means "another group with the same name", and the manager has said
        which one they meant instead of the service guessing from the name.
        """
        require_manager(actor)
        body = _require_text(text, "a checklist item cannot be empty")
        label = _require_text(group_name, "a group name cannot be empty")

        template = await self._active_or_create(actor, checklist_type)
        draft = await self._open(actor, template)
        return await self._append(
            actor,
            draft,
            text=body,
            group_name=label,
            group_index=_next_group_index(draft.items),
            order_index=0,
        )

    async def edit_text(self, actor: AccessContext, item_id: int, text: str) -> EditResult:
        """Reword one line (TZ 5.8)."""
        require_manager(actor)
        body = _require_text(text, "a checklist item cannot be empty")
        return await self._change_item(actor, item_id, {"text": body})

    async def set_critical(self, actor: AccessContext, item_id: int, is_set: bool) -> EditResult:
        """TZ 5.4: a critical line escalates to the manager the moment it is skipped."""
        require_manager(actor)
        return await self._change_item(actor, item_id, {"is_critical": is_set})

    async def set_photo(self, actor: AccessContext, item_id: int, is_set: bool) -> EditResult:
        """TZ 5.4: a line with ``requires_photo`` is not ticked until a photo arrives."""
        require_manager(actor)
        return await self._change_item(actor, item_id, {"requires_photo": is_set})

    async def delete_item(self, actor: AccessContext, item_id: int) -> EditResult:
        """Remove one line; if it was the last of its group, the group goes with it (D2).

        Nothing renumbers the groups that follow. ``group_index`` orders them and never
        addresses them, so the hole is invisible on the screen, while renumbering would
        rewrite every remaining row for no gain — and after a fork those rows are a fresh
        copy of the whole checklist already.
        """
        require_manager(actor)
        template, _ = await self._resolve_item(actor, item_id)
        draft = await self._open(actor, template)
        target = draft.track(item_id)
        copy = _find_item(draft.items, target)
        if copy is None or not await self._items.delete(target):
            raise TemplateItemNotFoundError(target, actor.venue_id)

        await self._audit.deleted(
            actor,
            AuditEntity.CHECKLIST_ITEM,
            target,
            before=snapshot(copy, *AUDITED_ITEM_FIELDS),
        )
        return await self._result(actor, draft)

    async def move_item(
        self,
        actor: AccessContext,
        item_id: int,
        direction: MoveDirection,
    ) -> EditResult:
        """Swap a line with its neighbour inside the group (TZ 5.8: reordering the lines).

        A line never leaves its group: dragging the first item of one group up into the one above
        by pressing an arrow twice is not what anybody meant by "order". At the edge of a
        group the answer is ``changed=False`` — and, because that decision is taken before
        :meth:`_open`, a button that can do nothing does not create a template version either.
        """
        require_manager(actor)
        template, item = await self._resolve_item(actor, item_id)
        peers = [
            row
            for row in sorted(await self._items.list_for_template(template.id), key=_item_order)
            if row.group_index == item.group_index
        ]
        position = next(index for index, row in enumerate(peers) if row.id == item.id)
        target = position - 1 if direction is MoveDirection.UP else position + 1
        if not 0 <= target < len(peers):
            return EditResult(
                view=await self._view(template),
                forked=False,
                item_id=item_id,
                changed=False,
            )

        draft = await self._open(actor, template)
        order = [draft.track(row.id) for row in peers]
        order[position], order[target] = order[target], order[position]
        await self._renumber(actor, draft, order)
        return await self._result(actor, draft, item_id=draft.track(item_id))

    # ---------------------------------------------------------------------------------
    # Groups
    # ---------------------------------------------------------------------------------

    async def rename_group(
        self,
        actor: AccessContext,
        *,
        template_id: int,
        group_index: int,
        name: str,
    ) -> EditResult:
        """Rename a group, i.e. update the ``group_name`` of all of its items (D2).

        Takes the template explicitly because the button lives on a screen that already knows
        which template it is drawing, and a group has no id of its own to name it by.
        """
        require_manager(actor)
        label = _require_text(name, "a group name cannot be empty")
        template = await self._require_template(actor, template_id)
        items = sorted(await self._items.list_for_template(template.id), key=_item_order)
        current = next((row for row in items if row.group_index == group_index), None)
        if current is None:
            raise TemplateGroupNotFoundError(template_id, group_index)
        if current.group_name == label:
            return EditResult(view=await self._view(template), forked=False, changed=False)

        # Read before the write: `current` is a live row and the update reaches it, so an
        # audit "before" taken afterwards would say the name changed from itself to itself.
        previous = current.group_name
        draft = await self._open(actor, template)
        await self._items.rename_group(draft.template.id, group_index, label)
        # A group is not a row, so the record hangs off the template and carries the index
        # inside the diff — otherwise the log would say "a name changed" and not say which.
        await self._audit.updated(
            actor,
            AuditEntity.CHECKLIST_TEMPLATE,
            draft.template.id,
            before={"group": {"index": group_index, "name": previous}},
            after={"group": {"index": group_index, "name": label}},
        )
        return await self._result(actor, draft)

    # ---------------------------------------------------------------------------------
    # Bulk input (decision B6)
    # ---------------------------------------------------------------------------------

    async def bulk_add(
        self,
        actor: AccessContext,
        *,
        checklist_type: ChecklistType,
        text: str,
    ) -> BulkResult:
        """Append a whole message worth of groups and lines in one edit (decision B6).

        The point of B6 is the eight groups and forty lines of a real checklist arriving in
        minutes instead of an hour, and the point of doing it here rather than as forty calls
        to :meth:`add_item` is the version count: one :meth:`_open`, one fork, one new
        version, one ``INSERT`` of every line.

        Every header opens a *new* group even when a group of that name already exists.
        Merging by name is not in the TZ, and it is the kind of "reasonable default" that is
        wrong in an operating bar: two zones can legitimately share a name, and a message that
        silently poured forty lines into existing groups would be much harder to undo than one
        that appended eight groups the manager can see and rename.

        An empty message writes nothing at all — no items, no template, and no version.
        """
        require_manager(actor)
        plan = parse_bulk(text)
        if plan.is_empty:
            existing = await self._templates.get_active(checklist_type)
            view = _empty_view(checklist_type) if existing is None else await self._view(existing)
            return BulkResult(view=view, forked=False, added_items=0, added_groups=0)

        template = await self._active_or_create(actor, checklist_type)
        draft = await self._open(actor, template)
        first_group = _next_group_index(draft.items)
        rows: list[dict[str, Any]] = [
            {
                "text": body,
                "group_name": group.name,
                "group_index": first_group + offset,
                "order_index": order,
            }
            for offset, group in enumerate(plan.groups)
            for order, body in enumerate(group.items)
        ]
        created = await self._items.add_many(draft.template.id, rows)

        # One record, not forty. The manager performed one action — TZ 2 asks for who changed
        # what, and forty rows saying "an item appeared" answer that worse than one saying the
        # checklist went from 0 lines in 0 groups to 40 in 8.
        await self._audit.updated(
            actor,
            AuditEntity.CHECKLIST_TEMPLATE,
            draft.template.id,
            before={
                "item_count": len(draft.items),
                "group_count": _group_count(draft.items),
            },
            after={
                "item_count": len(draft.items) + len(created),
                "group_count": _group_count(draft.items) + plan.group_count,
            },
        )
        result = await self._result(actor, draft)
        return BulkResult(
            view=result.view,
            forked=result.forked,
            added_items=len(created),
            added_groups=plan.group_count,
        )

    # ---------------------------------------------------------------------------------
    # Copy-on-write (decision B3)
    # ---------------------------------------------------------------------------------

    async def _open(self, actor: AccessContext, template: ChecklistTemplate) -> _Draft:
        """The template this edit will be applied to — the original, or a fresh version.

        Called once per edit, never per item. Until a run points at the template there is no
        history to protect and the edit goes in place; from the first reference on, the copy
        is made here and everything after it writes into the copy, so the runs of last month
        keep reading exactly the rows they were created from (TZ 4.3).

        The old version is deactivated *before* the new one is inserted: D7 puts a partial
        unique index on ``(venue_id, type) WHERE is_active``, and two active templates of a
        type do not exist even for the length of one statement.
        """
        items = sorted(await self._items.list_for_template(template.id), key=_item_order)
        if not await self._templates.is_referenced_by_runs(template.id):
            return _Draft(template=template, items=list(items), forked=False)

        await self._templates.deactivate(template.id)
        await self._audit.set_active(
            actor, AuditEntity.CHECKLIST_TEMPLATE, template.id, is_active=False
        )
        fork = await self._templates.create(
            checklist_type=template.type,
            name=template.name,
            version=template.version + 1,
            updated_by=actor.user_id,
        )
        copies = await self._items.add_many(fork.id, [_copy_of(item) for item in items])
        await self._audit.created(
            actor,
            AuditEntity.CHECKLIST_TEMPLATE,
            fork.id,
            after={
                **snapshot(fork, "type", "name", "version", "is_active"),
                "forked_from": template.id,
                "item_count": len(copies),
            },
        )
        return _Draft(
            template=fork,
            items=list(copies),
            forked=True,
            remap={old.id: new.id for old, new in zip(items, copies, strict=True)},
        )

    # ---------------------------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------------------------

    async def _active_or_create(
        self,
        actor: AccessContext,
        checklist_type: ChecklistType,
    ) -> ChecklistTemplate:
        """The active template of a type, created empty on first use (TZ 8.1, CLAUDE.md).

        The row carries no content — ``name`` is the technical type, the caption on the screen
        comes from ``src/bot/texts/`` — so creating it lazily keeps the delivered system
        genuinely empty while giving the first item somewhere to go.
        """
        template = await self._templates.get_active(checklist_type)
        if template is not None:
            require_venue(actor, template.venue_id)
            return template

        created = await self._templates.create(
            checklist_type=checklist_type,
            name=checklist_type.value,
            version=1,
            updated_by=actor.user_id,
        )
        await self._audit.created(
            actor,
            AuditEntity.CHECKLIST_TEMPLATE,
            created.id,
            after=snapshot(created, "type", "name", "version", "is_active"),
        )
        return created

    async def _require_template(
        self,
        actor: AccessContext,
        template_id: int,
    ) -> ChecklistTemplate:
        template = await self._templates.get(template_id)
        if template is None:
            raise TemplateNotFoundError(template_id, actor.venue_id)
        require_venue(actor, template.venue_id)
        if not template.is_active:
            raise InactiveTemplateError(template.id, template.version)
        return template

    async def _resolve_item(
        self,
        actor: AccessContext,
        item_id: int,
    ) -> tuple[ChecklistTemplate, ChecklistItem]:
        """The item and the template it belongs to, both proven to be this venue's (TZ 9)."""
        item = await self._items.get(item_id)
        if item is None:
            raise TemplateItemNotFoundError(item_id, actor.venue_id)
        return await self._require_template(actor, item.template_id), item

    async def _append(
        self,
        actor: AccessContext,
        draft: _Draft,
        *,
        text: str,
        group_name: str | None,
        group_index: int,
        order_index: int,
        is_critical: bool = False,
        requires_photo: bool = False,
    ) -> EditResult:
        item = await self._items.add(
            template_id=draft.template.id,
            text=text,
            group_name=group_name,
            group_index=group_index,
            order_index=order_index,
            is_critical=is_critical,
            requires_photo=requires_photo,
        )
        await self._audit.created(
            actor,
            AuditEntity.CHECKLIST_ITEM,
            item.id,
            after=snapshot(item, *AUDITED_ITEM_FIELDS),
        )
        return await self._result(actor, draft, item_id=item.id)

    async def _change_item(
        self,
        actor: AccessContext,
        item_id: int,
        fields: Mapping[str, Any],
    ) -> EditResult:
        """One field write, with the "nothing moved, nothing happens" rule in front of it.

        The comparison is made against the row as it stands *before* the fork, so ticking
        "critical" on a line that is already critical costs neither a version nor an audit
        record — the audit module refuses an empty diff for the same reason.
        """
        template, item = await self._resolve_item(actor, item_id)
        before = snapshot(item, *fields)
        if all(before[name] == value for name, value in fields.items()):
            return EditResult(
                view=await self._view(template),
                forked=False,
                item_id=item_id,
                changed=False,
            )

        draft = await self._open(actor, template)
        target = draft.track(item_id)
        updated = await self._items.update(target, **fields)
        if updated is None:
            raise TemplateItemNotFoundError(target, actor.venue_id)
        await self._audit.updated(
            actor,
            AuditEntity.CHECKLIST_ITEM,
            target,
            before=before,
            after=snapshot(updated, *fields),
        )
        return await self._result(actor, draft, item_id=target)

    async def _renumber(
        self,
        actor: AccessContext,
        draft: _Draft,
        order: Sequence[int],
    ) -> None:
        """Write ``order_index`` as the position in ``order``, skipping the rows that match.

        Renumbering rather than swapping two values: the positions are then always ``0..n-1``
        whatever the history of the group, and an adjacent move still writes exactly the two
        rows that moved.
        """
        rows = {row.id: row for row in draft.items}
        for position, item_id in enumerate(order):
            row = rows.get(item_id)
            if row is None:
                raise TemplateItemNotFoundError(item_id, actor.venue_id)
            if row.order_index == position:
                continue
            # Read before the write, for the reason `rename_group` gives: the update lands on
            # this very row, so the old value has to be taken while it is still the old value.
            previous = row.order_index
            if await self._items.update(item_id, order_index=position) is None:
                raise TemplateItemNotFoundError(item_id, actor.venue_id)
            await self._audit.updated(
                actor,
                AuditEntity.CHECKLIST_ITEM,
                item_id,
                before={"order_index": previous},
                after={"order_index": position},
            )

    async def _result(
        self,
        actor: AccessContext,
        draft: _Draft,
        *,
        item_id: int | None = None,
    ) -> EditResult:
        if not draft.forked:
            # TZ 4.3 keeps `updated_by` on the template; a fork was created with it already.
            await self._templates.update(draft.template.id, updated_by=actor.user_id)
        return EditResult(
            view=await self._view(draft.template),
            forked=draft.forked,
            item_id=item_id,
        )

    async def _view(self, template: ChecklistTemplate) -> TemplateView:
        items = sorted(await self._items.list_for_template(template.id), key=_item_order)
        return TemplateView(
            template_id=template.id,
            checklist_type=template.type,
            name=template.name,
            version=template.version,
            is_active=template.is_active,
            groups=_group(_item_view(item) for item in items),
        )


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    """Blank input is absent input — an unnamed group and a group named " " are one thing."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _require_text(value: str, message: str) -> str:
    cleaned = _clean(value)
    if cleaned is None:
        raise BlankTextError(message)
    return cleaned


def _item_order(item: ChecklistItem) -> tuple[int, int, int]:
    """(group, position, id) — the order TZ 5.4 renders a checklist in (decision D2)."""
    return (item.group_index, item.order_index, item.id)


def _copy_of(item: ChecklistItem) -> dict[str, Any]:
    """The payload that reproduces one item in a forked version (decision B3)."""
    return {
        "text": item.text,
        "group_name": item.group_name,
        "group_index": item.group_index,
        "order_index": item.order_index,
        "requires_photo": item.requires_photo,
        "requires_comment": item.requires_comment,
        "is_critical": item.is_critical,
    }


def _find_item(items: Iterable[ChecklistItem], item_id: int) -> ChecklistItem | None:
    return next((item for item in items if item.id == item_id), None)


def _find_group(items: Iterable[ChecklistItem], group_name: str | None) -> int | None:
    """The index of the group carrying that name, or ``None`` when there is no such group."""
    return next((item.group_index for item in items if item.group_name == group_name), None)


def _next_group_index(items: Iterable[ChecklistItem]) -> int:
    return max((item.group_index for item in items), default=-1) + 1


def _next_order_index(items: Iterable[ChecklistItem], group_index: int) -> int:
    return (
        max((item.order_index for item in items if item.group_index == group_index), default=-1) + 1
    )


def _group_count(items: Iterable[ChecklistItem]) -> int:
    return len({item.group_index for item in items})


def _item_view(item: ChecklistItem) -> TemplateItemView:
    return TemplateItemView(
        item_id=item.id,
        text=item.text,
        group_index=item.group_index,
        group_name=item.group_name,
        order_index=item.order_index,
        is_critical=item.is_critical,
        requires_photo=item.requires_photo,
        requires_comment=item.requires_comment,
    )


def _group(views: Iterable[TemplateItemView]) -> tuple[TemplateGroupView, ...]:
    """Fold an already ordered sequence into groups (decision D2: groups are not rows).

    The twin of ``_group()`` in ``src/services/checklists.py`` and deliberately not shared
    with it: that one folds run items and this one folds template items, and the only thing
    they would share is four lines of loop. A common helper over a protocol would tie the
    editor and the runner together for no gain.
    """
    groups: list[TemplateGroupView] = []
    bucket: list[TemplateItemView] = []
    index: int | None = None
    name: str | None = None

    for view in views:
        if index is not None and view.group_index != index:
            groups.append(TemplateGroupView(index=index, name=name, items=tuple(bucket)))
            bucket = []
        index, name = view.group_index, view.group_name
        bucket.append(view)

    if index is not None:
        groups.append(TemplateGroupView(index=index, name=name, items=tuple(bucket)))
    return tuple(groups)


def _empty_view(checklist_type: ChecklistType) -> TemplateView:
    """TZ 8.1: a venue that has never had a checklist of this type still has a screen."""
    return TemplateView(
        template_id=None,
        checklist_type=checklist_type,
        name=None,
        version=None,
        is_active=False,
        groups=(),
    )


__all__ = [
    "AUDITED_ITEM_FIELDS",
    "GROUP_PREFIX",
    "BlankTextError",
    "BulkPlan",
    "BulkResult",
    "EditResult",
    "InactiveTemplateError",
    "MoveDirection",
    "ParsedGroup",
    "TemplateError",
    "TemplateGroupNotFoundError",
    "TemplateGroupView",
    "TemplateItemNotFoundError",
    "TemplateItemView",
    "TemplateNotFoundError",
    "TemplateService",
    "TemplateView",
    "parse_bulk",
]
