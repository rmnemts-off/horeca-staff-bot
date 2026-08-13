"""Nomenclature, suppliers and orders (TZ 4.4, 5.6, 5.7).

Stage 1 functionality, but the tables are created now so that the schema is not reworked
later (plan, block A). The delivery stays empty: not a single product or supplier is
inserted anywhere (TZ 1.4#6, acceptance 11.5).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base, CreatedAtMixin, IdMixin, VenueScopedMixin, fk
from src.db.models.enums import (
    OrderStatus,
    ProductKind,
    SupplierChannel,
    Unit,
    order_status_enum,
    product_kind_enum,
    supplier_channel_enum,
    unit_enum,
)

EMPTY_TEXT_ARRAY = "{}"


class ProductCategory(IdMixin, VenueScopedMixin, Base):
    """TZ 4.4: product_categories(id, venue_id, name, kind, parent_id, order_index)."""

    __tablename__ = "product_categories"
    __table_args__ = (
        # Target of the composite keys from `products` and from this table itself.
        UniqueConstraint("venue_id", "id", name="uq_product_categories_venue_id_id"),
        # A subcategory belongs to the same venue as its parent (TZ 9).
        ForeignKeyConstraint(
            ["venue_id", "parent_id"],
            ["product_categories.venue_id", "product_categories.id"],
            name="fk_product_categories_venue_parent",
            ondelete="SET NULL (parent_id)",
        ),
        Index("ix_product_categories_venue_order", "venue_id", "order_index"),
    )

    name: Mapped[str]
    kind: Mapped[ProductKind] = mapped_column(product_kind_enum, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)


class Product(IdMixin, VenueScopedMixin, CreatedAtMixin, Base):
    """TZ 4.4: products(...). Prices are optional, the venue fills what it needs."""

    __tablename__ = "products"
    __table_args__ = (
        # Target of the composite keys from `writeoffs` (and, from stage 1, from order
        # rows resolved through their order).
        UniqueConstraint("venue_id", "id", name="uq_products_venue_id_id"),
        # Category and default supplier must belong to the same venue as the product.
        ForeignKeyConstraint(
            ["venue_id", "category_id"],
            ["product_categories.venue_id", "product_categories.id"],
            name="fk_products_venue_category",
            ondelete="SET NULL (category_id)",
        ),
        ForeignKeyConstraint(
            ["venue_id", "default_supplier_id"],
            ["suppliers.venue_id", "suppliers.id"],
            name="fk_products_venue_default_supplier",
            ondelete="SET NULL (default_supplier_id)",
        ),
        Index("ix_products_venue_category", "venue_id", "category_id"),
    )

    # NULL while the venue has not sorted the position into a category yet (TZ 8.1).
    category_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str]
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, nullable=False
    )
    unit: Mapped[Unit] = mapped_column(unit_enum, nullable=False)
    bottle_volume: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    portion_volume: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    portions_per_bottle: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    menu_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    purchase_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    default_supplier_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Bar zone from the "checklist" sheet: station, fridge, cupboard (TZ 4.4, 5.4 stock).
    storage_zone: Mapped[str | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(server_default="true", nullable=False)


class Supplier(IdMixin, VenueScopedMixin, Base):
    """TZ 4.4 and 5.7: channel type is fixed by code, the content is venue data."""

    __tablename__ = "suppliers"
    __table_args__ = (
        # Target of the composite keys from `products.default_supplier_id` and `orders`.
        UniqueConstraint("venue_id", "id", name="uq_suppliers_venue_id_id"),
    )

    name: Mapped[str]
    contact_person: Mapped[str | None] = mapped_column(nullable=True)
    phone: Mapped[str | None] = mapped_column(nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(nullable=True)
    email: Mapped[str | None] = mapped_column(nullable=True)
    channel: Mapped[SupplierChannel] = mapped_column(supplier_channel_enum, nullable=False)
    # The only allowed link out of Telegram (TZ 1.4#1).
    order_url: Mapped[str | None] = mapped_column(nullable=True)
    external_app: Mapped[str | None] = mapped_column(nullable=True)
    # Venue-local time (D12): deadline for accepting orders.
    order_deadline_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    comment: Mapped[str | None] = mapped_column(nullable=True)

    supplier_products: Mapped[list[SupplierProduct]] = relationship(
        back_populates="supplier",
        cascade="all, delete-orphan",
    )


class SupplierProduct(IdMixin, Base):
    """TZ 4.4: supplier_products(id, supplier_id, product_id, sku, package_size, price).

    Child row: reached through the supplier, which carries venue_id (decision D9).
    """

    __tablename__ = "supplier_products"

    supplier_id: Mapped[int] = fk("suppliers.id", ondelete="CASCADE", index=True)
    product_id: Mapped[int] = fk("products.id", ondelete="CASCADE", index=True)
    sku: Mapped[str | None] = mapped_column(nullable=True)
    # Package size from the source table: 0.5 / 0.7 / 0.75 / 1 l - a label, hence text.
    package_size: Mapped[str | None] = mapped_column(nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    supplier: Mapped[Supplier] = relationship(back_populates="supplier_products")


class Order(IdMixin, VenueScopedMixin, CreatedAtMixin, Base):
    """TZ 4.4: orders(id, venue_id, supplier_id, user_id, status, delivery_date, comment)."""

    __tablename__ = "orders"
    __table_args__ = (
        # An order may only be placed with a supplier of its own venue (TZ 9).
        ForeignKeyConstraint(
            ["venue_id", "supplier_id"],
            ["suppliers.venue_id", "suppliers.id"],
            name="fk_orders_venue_supplier",
            ondelete="SET NULL (supplier_id)",
        ),
        Index("ix_orders_venue_status", "venue_id", "status"),
    )

    # NULL for channels without a supplier: marketplace, market, household (TZ 5.7).
    supplier_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user_id: Mapped[int] = fk("users.id", ondelete="RESTRICT")
    status: Mapped[OrderStatus] = mapped_column(
        order_status_enum,
        server_default=OrderStatus.DRAFT.value,
        nullable=False,
    )
    delivery_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    comment: Mapped[str | None] = mapped_column(nullable=True)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )


class OrderItem(IdMixin, Base):
    """TZ 4.4: order_items(id, order_id, product_id, qty, unit, comment)."""

    __tablename__ = "order_items"

    order_id: Mapped[int] = fk("orders.id", ondelete="CASCADE", index=True)
    product_id: Mapped[int] = fk("products.id", ondelete="RESTRICT")
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit: Mapped[Unit] = mapped_column(unit_enum, nullable=False)
    comment: Mapped[str | None] = mapped_column(nullable=True)

    order: Mapped[Order] = relationship(back_populates="items")
