"""All enumerations of the project in one module, as named PostgreSQL types.

Every enum below is a native PG type with a fixed name, so that migrations create it once
and several tables reuse it (`bp_checklist_type` is shared by templates and runs, `bp_unit`
by products, order items and write-offs).

Type names carry the `bp_` prefix. Without it the schema would occupy `unit`,
`order_status`, `member_role` and `run_status` in `public` — names generic enough to
collide with another application when the bot is deployed into a database it does not own,
and the product is explicitly meant to be replicated (TZ 1.4#4). The prefix applies to the
type name only; the values stay exactly as TZ section 4 writes them.

Values come from TZ section 4 verbatim; the two deliberate extensions are marked in place.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence

from sqlalchemy import Enum as SAEnum


class InvitePurpose(enum.StrEnum):
    """What a code in `invite_codes` is for (TZ 5.1).

    Two codes look identical to the person typing them and mean opposite things. `JOIN`
    brings somebody new in and creates a membership. `REBIND` points at a card that already
    exists and only changes the Telegram account that opens it — no row is created, and the
    person's shifts, checklists and write-offs do not move, because they hang off `users.id`
    and that is exactly what stays put.
    """

    JOIN = "join"
    REBIND = "rebind"


class MemberRole(enum.StrEnum):
    """TZ 2 and 4.1: role is bound to the pair telegram_id + venue_id."""

    STAFF = "staff"
    MANAGER = "manager"
    OWNER = "owner"


class ShiftStatus(enum.StrEnum):
    """TZ 4.2."""

    PLANNED = "planned"
    CONFIRMED = "confirmed"
    WORKED = "worked"
    MISSED = "missed"
    CANCELLED = "cancelled"


class ShiftSource(enum.StrEnum):
    """TZ 4.2."""

    MANUAL = "manual"
    IMPORT = "import"


class ChecklistType(enum.StrEnum):
    """TZ 4.3 lists two values, TZ 5.4 describes three (decision D1)."""

    OPENING = "opening"
    CLOSING = "closing"
    STOCK = "stock"


class RunStatus(enum.StrEnum):
    """TZ 4.3."""

    SENT = "sent"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    OVERDUE = "overdue"
    SKIPPED = "skipped"


class NotificationStatus(enum.StrEnum):
    """TZ 4.7 plus `sending` (decision D5): the worker claims a row before delivery."""

    SCHEDULED = "scheduled"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProductKind(enum.StrEnum):
    """TZ 4.4."""

    ALCOHOL = "alcohol"
    NON_ALCOHOL = "non_alcohol"
    INGREDIENT = "ingredient"
    OTHER = "other"


class Unit(enum.StrEnum):
    """TZ 4.4."""

    ML = "ml"
    L = "l"
    G = "g"
    KG = "kg"
    PCS = "pcs"
    BOTTLE = "bottle"


class OrderStatus(enum.StrEnum):
    """TZ 4.4."""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    SENT = "sent"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class SupplierChannel(enum.StrEnum):
    """TZ 4.4 and 5.7: channel types are fixed by code, their content by the venue."""

    SUPPLIER = "supplier"
    MARKETPLACE = "marketplace"
    MARKET = "market"
    HOUSEHOLD = "household"


def _values(enum_type: type[enum.StrEnum]) -> Sequence[str]:
    return [member.value for member in enum_type]


def pg_enum(enum_type: type[enum.StrEnum], name: str) -> SAEnum:
    """Named native PG enum that stores the value, not the member name."""
    return SAEnum(
        enum_type,
        name=name,
        native_enum=True,
        create_constraint=False,
        validate_strings=True,
        values_callable=lambda enum_cls: list(_values(enum_cls)),
    )


member_role_enum = pg_enum(MemberRole, "bp_member_role")
invite_purpose_enum = pg_enum(InvitePurpose, "bp_invite_purpose")
shift_status_enum = pg_enum(ShiftStatus, "bp_shift_status")
shift_source_enum = pg_enum(ShiftSource, "bp_shift_source")
checklist_type_enum = pg_enum(ChecklistType, "bp_checklist_type")
run_status_enum = pg_enum(RunStatus, "bp_run_status")
notification_status_enum = pg_enum(NotificationStatus, "bp_notification_status")
product_kind_enum = pg_enum(ProductKind, "bp_product_kind")
unit_enum = pg_enum(Unit, "bp_unit")
order_status_enum = pg_enum(OrderStatus, "bp_order_status")
supplier_channel_enum = pg_enum(SupplierChannel, "bp_supplier_channel")

__all__ = [
    "ChecklistType",
    "InvitePurpose",
    "MemberRole",
    "NotificationStatus",
    "OrderStatus",
    "ProductKind",
    "RunStatus",
    "ShiftSource",
    "ShiftStatus",
    "SupplierChannel",
    "Unit",
    "checklist_type_enum",
    "invite_purpose_enum",
    "member_role_enum",
    "notification_status_enum",
    "order_status_enum",
    "pg_enum",
    "product_kind_enum",
    "run_status_enum",
    "shift_source_enum",
    "shift_status_enum",
    "supplier_channel_enum",
    "unit_enum",
]
