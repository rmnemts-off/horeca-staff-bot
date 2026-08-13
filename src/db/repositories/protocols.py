"""Repository interfaces — signatures only, no implementation (plan, task 4).

The contract is frozen here so that the service, bot and scheduler waves can be written
against it in parallel. Implementations land in wave C (plan, tasks 11-14).

Conventions for every protocol below:

* a repository is created for one venue (`session`, `venue_id`) and never receives
  `venue_id` in method arguments — the scope is part of the object (TZ 3.3);
* a repository returns `None` instead of raising when a row is missing or belongs to
  another venue, so that a foreign `callback_data` dies in the service layer (TZ 9);
* nothing here decides business rules: no auto-creation, no status transitions, no
  notification side effects. That is the service layer (TZ 3.2);
* the six protocols of child tables extend `ChildScopedRepository`: their rows have no
  `venue_id` of their own, so the implementation must be a `ChildRepository` and every
  query joins the parent (decision D9, acceptance 11.3).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Protocol

from src.db.models import (
    AuditLog,
    Base,
    ChecklistItem,
    ChecklistRun,
    ChecklistRunItem,
    ChecklistTemplate,
    ChecklistType,
    ImportJob,
    InviteCode,
    KnowledgeArticle,
    MemberRole,
    Notification,
    Order,
    OrderItem,
    Prep,
    PrepIngredient,
    Product,
    ProductCategory,
    Recipe,
    RecipeIngredient,
    RunStatus,
    Shift,
    Supplier,
    SupplierProduct,
    Unit,
    User,
    Venue,
    VenueMember,
    VenueSettings,
    Writeoff,
    WriteoffReason,
)

# --------------------------------------------------------------------------------------
# Shared contract of the child tables (decision D9)
# --------------------------------------------------------------------------------------


class ChildScopedRepository(Protocol):
    """Rows without `venue_id`, reachable only through their venue-scoped parent.

    Six tables of TZ section 4 are built this way: `checklist_items`,
    `checklist_run_items`, `order_items`, `recipe_ingredients`, `prep_ingredients`,
    `supplier_products`. Their repositories are implemented on
    `src.db.repositories.base.ChildRepository`, which turns `for_venue()` into a join to
    the parent and makes `by_id()` carry that join too — a child row of another venue is
    therefore not addressable at all, not even by a guessed id (TZ 9, acceptance 11.3).

    The two attributes below are what the base class needs: `parent` is the parent model,
    `parent_fk` is the foreign key column on the child. `CHILD_PARENTS` in the base module
    holds the same pairs at schema level and is checked against every implementation.
    """

    @property
    def parent(self) -> type[Base]: ...

    @property
    def parent_fk(self) -> str: ...


# --------------------------------------------------------------------------------------
# TZ 4.1 — organisation and people
# --------------------------------------------------------------------------------------


class VenueRepository(Protocol):
    """`venues` is the scope itself, so this repository is addressed by venue id."""

    async def get(self, venue_id: int) -> Venue | None: ...

    async def list_for_user(self, user_id: int) -> Sequence[Venue]: ...

    #: Every switched-on venue. The notification worker has no membership to derive a scope
    #: from, so it walks the venues and claims inside each one (plan, task 31).
    async def list_active(self) -> Sequence[Venue]: ...

    async def create(self, *, name: str, city: str, timezone: str) -> Venue: ...

    async def update(self, venue_id: int, **fields: Any) -> Venue | None: ...


class VenueSettingsRepository(Protocol):
    async def get(self) -> VenueSettings | None: ...

    async def create(
        self,
        *,
        default_shift_start: dt.time,
        default_shift_end: dt.time,
        **fields: Any,
    ) -> VenueSettings: ...

    async def update(self, **fields: Any) -> VenueSettings | None: ...


class UserRepository(Protocol):
    """`users` is global (a person may work in several venues, TZ 2)."""

    async def get(self, user_id: int) -> User | None: ...

    async def get_by_telegram_id(self, telegram_id: int) -> User | None: ...

    async def create(
        self,
        *,
        telegram_id: int,
        full_name: str,
        username: str | None = None,
        phone: str | None = None,
    ) -> User: ...

    async def update(self, user_id: int, **fields: Any) -> User | None: ...

    async def touch_last_seen(self, user_id: int, moment: dt.datetime) -> None: ...

    async def set_active_venue(self, user_id: int, venue_id: int | None) -> None: ...

    async def set_bot_blocked(self, user_id: int, *, blocked: bool) -> None: ...


class VenueMemberRepository(Protocol):
    async def get(self, member_id: int) -> VenueMember | None: ...

    async def get_for_user(self, user_id: int) -> VenueMember | None: ...

    async def list_active(self) -> Sequence[VenueMember]: ...

    async def list_by_role(self, role: MemberRole) -> Sequence[VenueMember]: ...

    async def add(
        self,
        *,
        user_id: int,
        role: MemberRole,
        position: str | None = None,
    ) -> VenueMember: ...

    async def update(self, member_id: int, **fields: Any) -> VenueMember | None: ...

    async def set_active(self, member_id: int, *, is_active: bool) -> VenueMember | None: ...


class InviteCodeRepository(Protocol):
    async def get_by_code(self, code: str) -> InviteCode | None: ...

    async def list_pending(self) -> Sequence[InviteCode]: ...

    async def create(
        self,
        *,
        code: str,
        role: MemberRole,
        expires_at: dt.datetime,
        position: str | None = None,
        full_name: str | None = None,
        created_by: int | None = None,
    ) -> InviteCode: ...

    async def mark_used(
        self,
        code_id: int,
        *,
        used_by: int,
        used_at: dt.datetime,
    ) -> InviteCode | None: ...

    async def revoke(self, code_id: int, moment: dt.datetime) -> InviteCode | None: ...


# --------------------------------------------------------------------------------------
# TZ 4.2 — schedule
# --------------------------------------------------------------------------------------


class ShiftRepository(Protocol):
    async def get(self, shift_id: int) -> Shift | None: ...

    async def list_for_date(self, shift_date: dt.date) -> Sequence[Shift]: ...

    async def list_for_user(
        self,
        user_id: int,
        *,
        date_from: dt.date,
        date_to: dt.date,
    ) -> Sequence[Shift]: ...

    async def list_between(self, *, date_from: dt.date, date_to: dt.date) -> Sequence[Shift]: ...

    async def get_opener(self, shift_date: dt.date) -> Shift | None: ...

    async def get_closer(self, shift_date: dt.date) -> Shift | None: ...

    async def create(self, **fields: Any) -> Shift: ...

    async def update(self, shift_id: int, **fields: Any) -> Shift | None: ...

    async def delete(self, shift_id: int) -> bool: ...


# --------------------------------------------------------------------------------------
# TZ 4.3 — checklists
# --------------------------------------------------------------------------------------


class ChecklistTemplateRepository(Protocol):
    async def get(self, template_id: int) -> ChecklistTemplate | None: ...

    async def get_active(self, checklist_type: ChecklistType) -> ChecklistTemplate | None: ...

    async def list_versions(self, checklist_type: ChecklistType) -> Sequence[ChecklistTemplate]: ...

    async def create(
        self,
        *,
        checklist_type: ChecklistType,
        name: str,
        version: int,
        updated_by: int | None = None,
    ) -> ChecklistTemplate: ...

    async def update(self, template_id: int, **fields: Any) -> ChecklistTemplate | None: ...

    async def deactivate(self, template_id: int) -> ChecklistTemplate | None: ...

    async def is_referenced_by_runs(self, template_id: int) -> bool: ...


class ChecklistItemRepository(ChildScopedRepository, Protocol):
    """Child of `checklist_templates`; the venue is resolved through `template_id`.

    `get(item_id)` is safe only because of that join: the id comes from `callback_data`
    of a checklist button, so a foreign id must resolve to `None` (TZ 9).

    Implementation: `ChildRepository[ChecklistItem, ChecklistTemplate]`, `parent_fk` is
    `template_id`.
    """

    async def get(self, item_id: int) -> ChecklistItem | None: ...

    async def list_for_template(self, template_id: int) -> Sequence[ChecklistItem]: ...

    async def count_for_template(self, template_id: int) -> int: ...

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
    ) -> ChecklistItem: ...

    async def add_many(
        self,
        template_id: int,
        items: Sequence[dict[str, Any]],
    ) -> Sequence[ChecklistItem]: ...

    async def update(self, item_id: int, **fields: Any) -> ChecklistItem | None: ...

    async def delete(self, item_id: int) -> bool: ...

    async def rename_group(self, template_id: int, group_index: int, name: str) -> int: ...


class ChecklistRunRepository(Protocol):
    async def get(self, run_id: int) -> ChecklistRun | None: ...

    async def get_by_shift(
        self,
        shift_id: int,
        checklist_type: ChecklistType,
    ) -> ChecklistRun | None: ...

    async def list_for_user(
        self,
        user_id: int,
        *,
        date_from: dt.date,
        date_to: dt.date,
    ) -> Sequence[ChecklistRun]: ...

    async def list_by_status(self, status: RunStatus) -> Sequence[ChecklistRun]: ...

    async def create(
        self,
        *,
        shift_id: int | None,
        user_id: int,
        template_id: int,
        checklist_type: ChecklistType,
        total_items: int,
        sent_at: dt.datetime | None = None,
    ) -> ChecklistRun: ...

    async def set_message(self, run_id: int, *, chat_id: int, message_id: int) -> None: ...

    async def mark_started(self, run_id: int, moment: dt.datetime) -> None: ...

    async def complete(
        self,
        run_id: int,
        *,
        moment: dt.datetime,
        status: RunStatus,
        completed_by: int,
        skip_comment: str | None = None,
    ) -> ChecklistRun | None: ...

    # Records that an unfinished run passed its deadline (TZ 4.3, 5.4). Without it
    # `list_by_status(RunStatus.OVERDUE)` above could never see a row: `complete()` is the
    # only other status write, and it requires `completed_by` and stamps `completed_at`,
    # while an overdue run is by definition not finished. The value is in the enum of
    # TZ 4.3 and TZ 5.4 asks for it, so it has to be writable. Returns `None` when the run
    # is unknown, already finished or already marked, so that the manager is told once —
    # the same rule as `complete()`.
    async def mark_overdue(self, run_id: int, moment: dt.datetime) -> ChecklistRun | None: ...

    # Undoes a completion (TZ 5.4, last block of rules: a checklist cannot be taken twice,
    # but a manager may reopen a run when the person got it wrong). Every other status
    # write in this protocol moves *forward* — `complete()` stamps `completed_at`,
    # `mark_overdue()` refuses a finished run — so the rule cannot be spelled with them.
    # The mirror of `complete()`: `SET completed_at = NULL, status = 'in_progress' WHERE
    # completed_at IS NOT NULL`, and `None` when there was nothing to undo, so two managers
    # pressing the button at once reopen the run once. Schema note Q4: reopening gets no
    # columns of its own, the fact goes to `audit_log` and the caller writes it.
    async def reopen(self, run_id: int) -> ChecklistRun | None: ...

    async def refresh_counters(self, run_id: int) -> ChecklistRun | None: ...


class ChecklistRunItemRepository(ChildScopedRepository, Protocol):
    """Child of `checklist_runs`; implementation `ChildRepository[ChecklistRunItem,
    ChecklistRun]` with `parent_fk = "run_id"`.

    `set_done` takes both ids: `run_id` carries the venue through the join, `item_id`
    identifies the row inside the run (plan, task 17 — the toggle is idempotent).
    """

    async def list_for_run(self, run_id: int) -> Sequence[ChecklistRunItem]: ...

    async def create_for_run(self, run_id: int, item_ids: Sequence[int]) -> int: ...

    async def set_done(
        self,
        *,
        run_id: int,
        item_id: int,
        is_done: bool,
        moment: dt.datetime,
        photo_file_id: str | None = None,
        comment: str | None = None,
    ) -> ChecklistRunItem | None: ...

    async def count_done(self, run_id: int) -> int: ...


# --------------------------------------------------------------------------------------
# TZ 4.4 — nomenclature, suppliers, orders
# --------------------------------------------------------------------------------------


class ProductCategoryRepository(Protocol):
    async def get(self, category_id: int) -> ProductCategory | None: ...

    async def list_all(self) -> Sequence[ProductCategory]: ...

    async def create(self, **fields: Any) -> ProductCategory: ...

    async def update(self, category_id: int, **fields: Any) -> ProductCategory | None: ...

    async def delete(self, category_id: int) -> bool: ...


class ProductRepository(Protocol):
    async def get(self, product_id: int) -> Product | None: ...

    async def list_by_category(
        self,
        category_id: int,
        *,
        limit: int,
        offset: int,
    ) -> Sequence[Product]: ...

    async def search(self, query: str, *, limit: int) -> Sequence[Product]: ...

    async def create(self, **fields: Any) -> Product: ...

    async def update(self, product_id: int, **fields: Any) -> Product | None: ...


class SupplierRepository(Protocol):
    async def get(self, supplier_id: int) -> Supplier | None: ...

    async def list_all(self) -> Sequence[Supplier]: ...

    async def create(self, **fields: Any) -> Supplier: ...

    async def update(self, supplier_id: int, **fields: Any) -> Supplier | None: ...


class SupplierProductRepository(ChildScopedRepository, Protocol):
    """Child of `suppliers`; `ChildRepository[SupplierProduct, Supplier]`,
    `parent_fk = "supplier_id"`."""

    async def list_for_supplier(self, supplier_id: int) -> Sequence[SupplierProduct]: ...

    async def add(
        self,
        *,
        supplier_id: int,
        product_id: int,
        sku: str | None = None,
        package_size: str | None = None,
        price: Decimal | None = None,
    ) -> SupplierProduct: ...

    async def delete(self, supplier_product_id: int) -> bool: ...


class OrderRepository(Protocol):
    async def get(self, order_id: int) -> Order | None: ...

    async def get_draft(self, *, user_id: int, supplier_id: int | None) -> Order | None: ...

    async def list_for_period(
        self,
        *,
        date_from: dt.datetime,
        date_to: dt.datetime,
    ) -> Sequence[Order]: ...

    async def create(self, **fields: Any) -> Order: ...

    async def update(self, order_id: int, **fields: Any) -> Order | None: ...


class OrderItemRepository(ChildScopedRepository, Protocol):
    """Child of `orders`; `ChildRepository[OrderItem, Order]`, `parent_fk = "order_id"`."""

    async def list_for_order(self, order_id: int) -> Sequence[OrderItem]: ...

    async def set_qty(
        self,
        *,
        order_id: int,
        product_id: int,
        qty: Decimal,
        unit: Unit,
        comment: str | None = None,
    ) -> OrderItem | None: ...

    async def delete(self, order_item_id: int) -> bool: ...


# --------------------------------------------------------------------------------------
# TZ 4.5 — write-offs
# --------------------------------------------------------------------------------------


class WriteoffReasonRepository(Protocol):
    async def get(self, reason_id: int) -> WriteoffReason | None: ...

    async def list_active(self) -> Sequence[WriteoffReason]: ...

    async def create(self, **fields: Any) -> WriteoffReason: ...

    async def update(self, reason_id: int, **fields: Any) -> WriteoffReason | None: ...


class WriteoffRepository(Protocol):
    async def get(self, writeoff_id: int) -> Writeoff | None: ...

    async def list_for_period(
        self,
        *,
        date_from: dt.datetime,
        date_to: dt.datetime,
    ) -> Sequence[Writeoff]: ...

    async def create(self, **fields: Any) -> Writeoff: ...

    async def cancel(
        self,
        writeoff_id: int,
        *,
        moment: dt.datetime,
        cancelled_by: int,
    ) -> Writeoff | None: ...


# --------------------------------------------------------------------------------------
# TZ 4.6 — recipes, preps, knowledge base
# --------------------------------------------------------------------------------------


class RecipeRepository(Protocol):
    """The only entity besides knowledge articles that may read the shared library (D9)."""

    async def get(self, recipe_id: int) -> Recipe | None: ...

    async def search(self, query: str, *, limit: int, offset: int = 0) -> Sequence[Recipe]: ...

    async def count_search(self, query: str) -> int: ...

    async def list_by_category(
        self,
        category: str,
        *,
        limit: int,
        offset: int,
    ) -> Sequence[Recipe]: ...

    async def list_categories(self) -> Sequence[str]: ...

    async def create(self, **fields: Any) -> Recipe: ...

    async def update(self, recipe_id: int, **fields: Any) -> Recipe | None: ...


class RecipeIngredientRepository(ChildScopedRepository, Protocol):
    """Child of `recipes`; `ChildRepository[RecipeIngredient, Recipe]`,
    `parent_fk = "recipe_id"`.

    The only child repository that may also use `library()`: the card of a global recipe
    has to render its ingredients (TZ 3.3).
    """

    async def list_for_recipe(self, recipe_id: int) -> Sequence[RecipeIngredient]: ...

    async def replace_all(
        self,
        recipe_id: int,
        ingredients: Sequence[dict[str, Any]],
    ) -> Sequence[RecipeIngredient]: ...


class PrepRepository(Protocol):
    """Venue scoped only: `preps` is deliberately out of the library whitelist (D9, C4)."""

    async def get(self, prep_id: int) -> Prep | None: ...

    async def search(self, query: str, *, limit: int, offset: int = 0) -> Sequence[Prep]: ...

    async def create(self, **fields: Any) -> Prep: ...

    async def update(self, prep_id: int, **fields: Any) -> Prep | None: ...


class PrepIngredientRepository(ChildScopedRepository, Protocol):
    """Child of `preps`; `ChildRepository[PrepIngredient, Prep]`, `parent_fk = "prep_id"`.

    `preps` are outside the library whitelist (D9, C4), so the join stays strict:
    `library()` on this repository raises `LibraryScopeError`.
    """

    async def list_for_prep(self, prep_id: int) -> Sequence[PrepIngredient]: ...

    async def replace_all(
        self,
        prep_id: int,
        ingredients: Sequence[dict[str, Any]],
    ) -> Sequence[PrepIngredient]: ...


class KnowledgeArticleRepository(Protocol):
    async def get(self, article_id: int) -> KnowledgeArticle | None: ...

    async def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int = 0,
    ) -> Sequence[KnowledgeArticle]: ...

    async def list_by_category(self, category: str) -> Sequence[KnowledgeArticle]: ...

    async def create(self, **fields: Any) -> KnowledgeArticle: ...

    async def update(self, article_id: int, **fields: Any) -> KnowledgeArticle | None: ...


# --------------------------------------------------------------------------------------
# TZ 4.7 — service tables
# --------------------------------------------------------------------------------------


class NotificationRepository(Protocol):
    """The notifications table is the only queue (D5); the worker owns claim and release.

    **A claim is concluded only by the worker that holds it** (acceptance 11.4). `sending`
    is one status shared by every worker, so it cannot answer "is this delivery still
    mine": a worker whose claim `reclaim_stale` has already handed on would otherwise
    stamp `sent` over a message not yet sent, or the terminal `failed` over one still in
    flight — a lost notification. `claim_batch` therefore writes the instant of the claim
    into `claimed_at` and returns the rows carrying it; the caller keeps that value while
    it talks to Telegram and hands it back to `mark_sent` / `mark_failed`, which match on
    it in addition to the status. Everything that ends a claim clears the column, so a late
    report from a superseded worker simply changes nothing.

    That token is part of the contract, not of the implementation: a repository whose
    `mark_sent` ignored it would satisfy a protocol without it while quietly reopening the
    race criterion 11.4 closes. `tests/test_wiring.py` binds the concrete repository to
    this protocol so that the two cannot drift apart unnoticed again.
    """

    async def get(self, notification_id: int) -> Notification | None: ...

    async def upsert_scheduled(
        self,
        *,
        dedup_key: str,
        notification_type: str,
        scheduled_at: dt.datetime,
        user_id: int | None,
        chat_id: int | None,
        payload: dict[str, Any],
        entity: str | None = None,
        entity_id: int | None = None,
    ) -> Notification: ...

    async def claim_batch(self, *, now: dt.datetime, limit: int) -> Sequence[Notification]: ...

    async def reclaim_stale(self, *, older_than: dt.datetime) -> int: ...

    # `claimed_at` is the token `claim_batch` stamped on the row and handed back on it;
    # keyword-only because it is a fence, not an optional detail — a caller that has lost
    # the token must say so explicitly rather than let it default away. Passing `None` is
    # not a way to skip the check: it matches only a row with no claim at all, which
    # `claim_batch` never leaves behind, so such a call concludes nothing and the row is
    # reclaimed instead of being wrongly closed.
    async def mark_sent(
        self,
        notification_id: int,
        moment: dt.datetime,
        *,
        claimed_at: dt.datetime | None,
    ) -> None: ...

    async def mark_failed(
        self,
        notification_id: int,
        error: str,
        *,
        claimed_at: dt.datetime | None,
    ) -> None: ...

    async def reschedule(self, notification_id: int, scheduled_at: dt.datetime) -> None: ...

    async def cancel_for_entity(self, *, entity: str, entity_id: int) -> int: ...

    async def list_for_entity(self, *, entity: str, entity_id: int) -> Sequence[Notification]: ...


class AuditLogRepository(Protocol):
    async def record(
        self,
        *,
        user_id: int | None,
        entity: str,
        entity_id: int | None,
        action: str,
        diff: dict[str, Any] | None = None,
    ) -> AuditLog: ...

    async def list_for_entity(
        self,
        *,
        entity: str,
        entity_id: int,
        limit: int,
    ) -> Sequence[AuditLog]: ...


class ImportJobRepository(Protocol):
    async def get(self, job_id: int) -> ImportJob | None: ...

    async def list_recent(self, limit: int) -> Sequence[ImportJob]: ...

    async def create(
        self,
        *,
        user_id: int | None,
        job_type: str,
        file_id: str | None,
        status: str,
    ) -> ImportJob: ...

    async def update(self, job_id: int, **fields: Any) -> ImportJob | None: ...


__all__ = [
    "AuditLogRepository",
    "ChecklistItemRepository",
    "ChecklistRunItemRepository",
    "ChecklistRunRepository",
    "ChecklistTemplateRepository",
    "ChildScopedRepository",
    "ImportJobRepository",
    "InviteCodeRepository",
    "KnowledgeArticleRepository",
    "NotificationRepository",
    "OrderItemRepository",
    "OrderRepository",
    "PrepIngredientRepository",
    "PrepRepository",
    "ProductCategoryRepository",
    "ProductRepository",
    "RecipeIngredientRepository",
    "RecipeRepository",
    "ShiftRepository",
    "SupplierProductRepository",
    "SupplierRepository",
    "UserRepository",
    "VenueMemberRepository",
    "VenueRepository",
    "VenueSettingsRepository",
    "WriteoffReasonRepository",
    "WriteoffRepository",
]
