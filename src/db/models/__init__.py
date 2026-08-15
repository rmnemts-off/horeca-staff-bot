"""Every model of the project.

Importing this package registers all tables in `Base.metadata`; Alembic and the metadata
test rely on that. Table composition follows TZ section 4; every addition on top of it is
listed with its TZ reference in `docs/schema-notes.md`.
"""

from __future__ import annotations

from src.db.models.base import (
    Base,
    CreatedAtMixin,
    IdMixin,
    LibraryScopedMixin,
    TimestampMixin,
    VenueScopedMixin,
)
from src.db.models.catalog import (
    Order,
    OrderItem,
    Product,
    ProductCategory,
    Supplier,
    SupplierProduct,
)
from src.db.models.checklist import (
    ChecklistItem,
    ChecklistRun,
    ChecklistRunItem,
    ChecklistTemplate,
)
from src.db.models.enums import (
    ChecklistType,
    InvitePurpose,
    MemberRole,
    NotificationStatus,
    OrderStatus,
    ProductKind,
    RunStatus,
    ShiftSource,
    ShiftStatus,
    SupplierChannel,
    Unit,
)
from src.db.models.recipe import (
    KnowledgeArticle,
    Prep,
    PrepIngredient,
    Recipe,
    RecipeIngredient,
)
from src.db.models.service import AuditLog, ImportJob, Notification
from src.db.models.shift import Shift
from src.db.models.user import InviteCode, User, VenueMember
from src.db.models.venue import Venue, VenueSettings
from src.db.models.writeoff import Writeoff, WriteoffReason

__all__ = [
    "AuditLog",
    "Base",
    "ChecklistItem",
    "ChecklistRun",
    "ChecklistRunItem",
    "ChecklistTemplate",
    "ChecklistType",
    "CreatedAtMixin",
    "IdMixin",
    "ImportJob",
    "InviteCode",
    "InvitePurpose",
    "KnowledgeArticle",
    "LibraryScopedMixin",
    "MemberRole",
    "Notification",
    "NotificationStatus",
    "Order",
    "OrderItem",
    "OrderStatus",
    "Prep",
    "PrepIngredient",
    "Product",
    "ProductCategory",
    "ProductKind",
    "Recipe",
    "RecipeIngredient",
    "RunStatus",
    "Shift",
    "ShiftSource",
    "ShiftStatus",
    "Supplier",
    "SupplierChannel",
    "SupplierProduct",
    "TimestampMixin",
    "Unit",
    "User",
    "Venue",
    "VenueMember",
    "VenueScopedMixin",
    "VenueSettings",
    "Writeoff",
    "WriteoffReason",
]
