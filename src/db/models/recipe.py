"""Recipes, preps and knowledge base (TZ 4.6, 5.5).

`recipes` and `knowledge_articles` may have `venue_id = NULL` — the shared BarPoint
library (TZ 3.3). It ships empty; only the mechanism is implemented.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, Index, Integer, Numeric, Text, column, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base, IdMixin, LibraryScopedMixin, TimestampMixin, fk
from src.db.models.enums import Unit, unit_enum

EMPTY_TEXT_ARRAY = "{}"


class Recipe(IdMixin, LibraryScopedMixin, TimestampMixin, Base):
    """TZ 4.6: recipes(...).

    Uniqueness is (venue_id, category, lower(btrim(name))) — decision D6: one name can
    legitimately occur in two categories (an espresso-based Americano and a cocktail).
    NULLS NOT DISTINCT makes the same rule work for library rows, where venue_id is NULL.
    """

    __tablename__ = "recipes"
    __table_args__ = (
        Index(
            "uq_recipes_venue_category_name",
            "venue_id",
            "category",
            func.lower(func.btrim(column("name"))),
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        # TZ 5.5: search must survive typos; pg_trgm is created by the initial migration.
        Index(
            "ix_recipes_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        # TZ 4.6 asks for search "by name, aliases and full text". The array half is a GIN
        # index with the default array operator class: it serves `aliases && ARRAY[:q]`
        # and `:q = ANY(aliases)`, i.e. the exact synonym hit. Trigram similarity over the
        # array is deliberately not indexed — it would need an expression over
        # `array_to_string`, which PostgreSQL marks STABLE and refuses to index without a
        # custom IMMUTABLE wrapper function. At the volume TZ 5.5 names (~200 rows) the
        # similarity pass over `aliases` is a scan and stays far inside the 5-second rule.
        Index("ix_recipes_aliases_gin", "aliases", postgresql_using="gin"),
    )

    name: Mapped[str]
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, nullable=False
    )
    # Classics / author / seasonal / lemonade / coffee / tea — the venue names its own.
    category: Mapped[str]
    glassware: Mapped[str | None] = mapped_column(nullable=True)
    method: Mapped[str | None] = mapped_column(nullable=True)
    ice: Mapped[str | None] = mapped_column(nullable=True)
    garnish: Mapped[str | None] = mapped_column(nullable=True)
    instruction: Mapped[str | None] = mapped_column(nullable=True)
    # TZ 5.5: seasonal positions are tied to the date they were introduced.
    season_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # TZ 5.5: lemonades and teas carry a layout for several volumes (0.4 l and 1 l).
    yield_variants: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    photo_file_id: Mapped[str | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(server_default="true", nullable=False)
    updated_by: Mapped[int | None] = fk("users.id", ondelete="SET NULL", nullable=True)

    ingredients: Mapped[list[RecipeIngredient]] = relationship(
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by="RecipeIngredient.order_index",
    )


class RecipeIngredient(IdMixin, Base):
    """TZ 4.6 plus `qty_text` (decision D4).

    Three rendering branches (plan, task 18): qty + unit, qty without unit, qty_text only —
    the source data contains "top up", "5 leaves", "2/3 dash", so qty and unit are both
    nullable and the raw text is preserved (TZ 7, "non-numeric amounts are kept as text").
    """

    __tablename__ = "recipe_ingredients"

    recipe_id: Mapped[int] = fk("recipes.id", ondelete="CASCADE", index=True)
    product_id: Mapped[int | None] = fk("products.id", ondelete="SET NULL", nullable=True)
    prep_id: Mapped[int | None] = fk("preps.id", ondelete="SET NULL", nullable=True)
    name: Mapped[str]
    qty: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    unit: Mapped[Unit | None] = mapped_column(unit_enum, nullable=True)
    qty_text: Mapped[str | None] = mapped_column(nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)

    recipe: Mapped[Recipe] = relationship(back_populates="ingredients")


class Prep(IdMixin, LibraryScopedMixin, TimestampMixin, Base):
    """TZ 4.6: preps(...) — syrups, infusions, cordials, semi-finished products.

    `venue_id` is nullable per TZ 4.6, but preps are NOT in the library whitelist of the
    repository layer (decision D9) until question C4 is answered.
    """

    __tablename__ = "preps"

    name: Mapped[str]
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, nullable=False
    )
    category: Mapped[str | None] = mapped_column(nullable=True)
    method: Mapped[str | None] = mapped_column(nullable=True)
    # Yield: 1,5 l / 1 l / 300 ml - text, exactly as the venue writes it.
    yield_amount: Mapped[str | None] = mapped_column(nullable=True)
    shelf_life: Mapped[str | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(server_default="true", nullable=False)
    updated_by: Mapped[int | None] = fk("users.id", ondelete="SET NULL", nullable=True)

    ingredients: Mapped[list[PrepIngredient]] = relationship(
        back_populates="prep",
        cascade="all, delete-orphan",
        order_by="PrepIngredient.order_index",
    )


class PrepIngredient(IdMixin, Base):
    """TZ 4.6 plus `qty_text` for the same reason as in recipe_ingredients (D4, TZ 7)."""

    __tablename__ = "prep_ingredients"

    prep_id: Mapped[int] = fk("preps.id", ondelete="CASCADE", index=True)
    product_id: Mapped[int | None] = fk("products.id", ondelete="SET NULL", nullable=True)
    name: Mapped[str]
    qty: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    unit: Mapped[Unit | None] = mapped_column(unit_enum, nullable=True)
    qty_text: Mapped[str | None] = mapped_column(nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)

    prep: Mapped[Prep] = relationship(back_populates="ingredients")


class KnowledgeArticle(IdMixin, LibraryScopedMixin, TimestampMixin, Base):
    """TZ 4.6: knowledge_articles(id, venue_id, title, aliases, category, body_md, ...)."""

    __tablename__ = "knowledge_articles"
    __table_args__ = (
        Index(
            "ix_knowledge_articles_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        # Same pair as `recipes`: trigrams on the title, array GIN on the synonyms (TZ 4.6).
        Index("ix_knowledge_articles_aliases_gin", "aliases", postgresql_using="gin"),
    )

    title: Mapped[str]
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, nullable=False
    )
    category: Mapped[str | None] = mapped_column(nullable=True)
    body_md: Mapped[str]
    updated_by: Mapped[int | None] = fk("users.id", ondelete="SET NULL", nullable=True)
