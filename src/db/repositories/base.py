"""Base repositories: exactly three query shapes (decision D9, TZ 3.3, acceptance 11.3).

Tables that own ``venue_id`` use :class:`BaseRepository`:

* ``for_venue()``  -> ``WHERE venue_id = :vid``
* ``library()``    -> ``WHERE venue_id = :vid OR venue_id IS NULL``

The second shape is available only to the tables of the shared BarPoint library.
Everything else — including ``preps`` — must use ``for_venue()``: TZ 3.3 names recipes and
knowledge articles as the only global entities, and question C4 (editing a library row) is
still open.

Child rows (checklist items, run items, order items, ingredients, supplier products) carry
no ``venue_id``; they use :class:`ChildRepository`, whose ``for_venue()`` is a join to the
venue-scoped parent. That is the third shape, and it is the only way a child row may be
addressed — including a lookup by its own id, which goes through ``by_id()`` and therefore
still carries the join. Without it a forged ``callback_data`` would read another venue's
checklist item (acceptance 11.3, TZ 9).

Any ``select()`` in this package must be venue filtered; the static test
``tests/test_static_repos.py`` enforces it. A query over a genuinely global table (``users``)
must carry an inline ``# venue-exempt: <reason>`` comment.

What the venue *is* is not a subclass decision: ``venue_column`` and ``scope_model`` are
computed from ``model`` / ``parent`` here and sealed (``SEALED_SCOPE_ATTRIBUTES``), and the
column is re-checked on every query shape (``_checked_venue_column``). Both halves exist
because the property used to be a plain override point: a repository returning
``Product.__table__.c.id`` from it got ``for_venue()``, ``library()`` and ``by_id()`` that
filtered on the primary key and read every venue in the database.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from sqlalchemy import ColumnElement, Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.base import Base

#: Only these tables may be queried with `library()` (decision D9).
LIBRARY_TABLES = frozenset({"recipes", "knowledge_articles"})

#: Attributes that answer "what is the venue of this repository". They are derived from
#: `model` (or `parent`) by the two classes below and are sealed against subclasses: a
#: repository that returns another column from `venue_column` keeps `for_venue()`,
#: `library()` and `by_id()` while none of them filters by venue any more.
SEALED_SCOPE_ATTRIBUTES = frozenset({"venue_column", "scope_model"})

#: Schema-level map of the child tables: table -> (parent table, foreign key on the child).
#: It is the machine-readable form of decision D9 ("child rows are reachable only through a
#: join to their parent") and is checked against every `ChildRepository` subclass at import
#: time. Not content: these are table names fixed by TZ section 4, not venue data.
CHILD_PARENTS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "checklist_items": ("checklist_templates", "template_id"),
        "checklist_run_items": ("checklist_runs", "run_id"),
        "order_items": ("orders", "order_id"),
        "prep_ingredients": ("preps", "prep_id"),
        "recipe_ingredients": ("recipes", "recipe_id"),
        "supplier_products": ("suppliers", "supplier_id"),
    }
)


class LibraryScopeError(RuntimeError):
    """Raised when `library()` is called for a table outside the whitelist."""

    def __init__(self, tablename: str) -> None:
        super().__init__(
            f"table {tablename!r} is not part of the shared library; "
            f"allowed: {sorted(LIBRARY_TABLES)}"
        )
        self.tablename = tablename


class VenueScopeError(RuntimeError):
    """Raised when a repository is built without a usable venue scope (TZ 3.3).

    A missing venue must fail here and not later: `for_venue()` with `venue_id = None`
    would render `venue_id IS NULL`, which on `recipes`, `knowledge_articles` and `preps`
    means "the whole shared library" instead of "nothing".
    """

    def __init__(self, venue_id: object) -> None:
        super().__init__(
            f"venue_id must be a positive integer, got {venue_id!r}; the venue-context "
            "middleware has to resolve the venue before a repository is created"
        )
        self.venue_id = venue_id


class RepositoryScopeError(TypeError):
    """Raised at class-definition time when a repository is wired to the wrong base."""


class VenueColumnError(RuntimeError):
    """Raised when the column a repository filters on is not its model's ``venue_id``.

    ``venue_column`` is a property, and a property can be overridden. A subclass that
    returned any other column from it — ``Product.__table__.c.id`` is enough — kept
    ``for_venue()``, ``library()`` and ``by_id()`` unchanged, and all three then built a
    filter that is not a venue filter, while every caller went on believing the scope was
    part of the object (TZ 3.3, acceptance 11.3).
    """

    def __init__(self, repository: type, column: object, expected: object) -> None:
        super().__init__(
            f"{repository.__name__}.venue_column is {column}, not {expected}: a repository "
            "filters on the venue_id column of its own model (for a child row, of its "
            "parent) and on nothing else — declare model/parent instead of overriding it"
        )
        self.repository = repository
        self.column = column
        self.expected = expected


def _venue_column(model: type[Base]) -> ColumnElement[Any]:
    table = model.__table__
    if "venue_id" not in table.c:
        tablename: str = model.__tablename__
        raise RepositoryScopeError(
            f"model {tablename!r} has no venue_id column; a child row is addressed through "
            "its parent — declare the repository as ChildRepository[Model, Parent] "
            f"(see CHILD_PARENTS for the parent of {tablename!r})"
        )
    return table.c.venue_id


class BaseRepository[ModelT: Base]:
    """Venue-scoped data access. Subclasses set `model` and add read/write methods."""

    model: type[ModelT]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._check_sealed_scope()
        cls._check_scope()

    @classmethod
    def _check_sealed_scope(cls) -> None:
        """What "the venue" is, is decided here and not by a subclass.

        Both attributes are computed from `model` / `parent` by the two classes of this
        module. A subclass that redefines one of them changes the column every query is
        filtered on while keeping the methods that promise a venue scope, so the answer is
        no, at class-definition time, with the reason spelled out.
        """
        if cls.__module__ == __name__:  # BaseRepository -> ChildRepository, by design
            return
        sealed = sorted(SEALED_SCOPE_ATTRIBUTES & set(vars(cls)))
        if sealed:
            raise RepositoryScopeError(
                f"{cls.__name__} redefines {', '.join(sealed)}; the venue scope comes from "
                "`model` (or `parent` for a child row) and nowhere else. A query over a "
                "genuinely global table belongs in a method with a registered "
                "`# venue-exempt:` comment, not in a rewritten scope (TZ 3.3)"
            )

    @classmethod
    def _check_scope(cls) -> None:
        """Fail on import, not on the first query in production."""
        model: type[Base] | None = getattr(cls, "model", None)
        if model is None:  # intermediate class, e.g. ChildRepository itself
            return
        _venue_column(model)

    def __init__(self, session: AsyncSession, venue_id: int) -> None:
        if not isinstance(venue_id, int) or isinstance(venue_id, bool) or venue_id <= 0:
            raise VenueScopeError(venue_id)
        self.session = session
        self.venue_id = venue_id

    @classmethod
    def scope_model(cls) -> type[Base]:
        """The model whose `venue_id` scopes this repository."""
        return cls.model

    @property
    def venue_column(self) -> ColumnElement[Any]:
        return _venue_column(self.scope_model())

    def _checked_venue_column(self) -> ColumnElement[Any]:
        """`venue_column`, verified rather than trusted — see :class:`VenueColumnError`.

        `__init_subclass__` already refuses a subclass that redefines the property; this
        is the second half of the same guarantee, and it also holds for a class patched
        after it was created. Every query shape below goes through here.
        """
        column = self.venue_column
        expected = _venue_column(self.scope_model())
        if column is not expected:
            raise VenueColumnError(type(self), column, expected)
        return column

    def for_venue(self) -> Select[tuple[ModelT]]:
        """Rows of the active venue only."""
        venue_column = self._checked_venue_column()
        return select(self.model).where(venue_column == self.venue_id)

    def library(self) -> Select[tuple[ModelT]]:
        """Rows of the active venue plus the shared library (`venue_id IS NULL`).

        Overriding a global row by a local one with the same key (D6) is a service-layer
        decision; the repository only returns the union.
        """
        tablename: str = self.model.__tablename__
        if tablename not in LIBRARY_TABLES:
            raise LibraryScopeError(tablename)
        column = self._checked_venue_column()
        return select(self.model).where(or_(column == self.venue_id, column.is_(None)))

    def by_id(self, row_id: int) -> Select[tuple[ModelT]]:
        """One row by id, still scoped: a foreign id must return nothing, not a row (TZ 9).

        `session.get()` is never an alternative — it ignores every filter.
        """
        table = self.model.__table__
        if "id" not in table.c:
            tablename: str = self.model.__tablename__
            raise RepositoryScopeError(
                f"table {tablename!r} has no id column; address it by its own key"
            )
        return self.for_venue().where(table.c.id == row_id)


class ChildRepository[ModelT: Base, ParentT: Base](BaseRepository[ModelT]):
    """Third query shape: a child row is reachable only through its venue-scoped parent.

    Subclasses declare the parent model and the foreign key that points at it::

        class ChecklistItemRepo(ChildRepository[ChecklistItem, ChecklistTemplate]):
            model = ChecklistItem
            parent = ChecklistTemplate
            parent_fk = "template_id"

    Both `for_venue()` and `by_id()` then carry the join, so no method of a child
    repository can return a row of another venue.
    """

    parent: type[ParentT]
    parent_fk: str

    @classmethod
    def _check_scope(cls) -> None:
        model: type[Base] | None = getattr(cls, "model", None)
        parent: type[Base] | None = getattr(cls, "parent", None)
        parent_fk: str | None = getattr(cls, "parent_fk", None)
        if model is None and parent is None and parent_fk is None:
            return  # intermediate class
        if model is None or parent is None or not parent_fk:
            raise RepositoryScopeError(
                f"{cls.__name__} must declare model, parent and parent_fk (decision D9)"
            )
        table = model.__table__
        tablename: str = model.__tablename__
        if "venue_id" in table.c:
            raise RepositoryScopeError(
                f"model {tablename!r} owns venue_id; use BaseRepository, not ChildRepository"
            )
        if parent_fk not in table.c:
            raise RepositoryScopeError(
                f"{tablename!r} has no column {parent_fk!r} to join {parent.__tablename__!r}"
            )
        _venue_column(parent)
        declared = CHILD_PARENTS.get(tablename)
        if declared is not None and declared != (parent.__tablename__, parent_fk):
            raise RepositoryScopeError(
                f"{tablename!r} is declared as a child of {declared} in CHILD_PARENTS, "
                f"but {cls.__name__} joins ({parent.__tablename__!r}, {parent_fk!r})"
            )

    @classmethod
    def scope_model(cls) -> type[Base]:
        """The venue lives on the parent — that is the whole point of this class."""
        return cls.parent

    def _through_parent(self, venue_id_filter: ColumnElement[bool]) -> Select[tuple[ModelT]]:
        parent_table = self.parent.__table__
        return (
            select(self.model)
            .join(parent_table, self.model.__table__.c[self.parent_fk] == parent_table.c.id)
            .where(venue_id_filter)
        )

    def for_venue(self) -> Select[tuple[ModelT]]:
        """Child rows whose parent belongs to the active venue."""
        venue_column = self._checked_venue_column()
        return self._through_parent(venue_column == self.venue_id)

    def library(self) -> Select[tuple[ModelT]]:
        """Child rows of the active venue plus those of the shared library.

        Allowed only when the parent itself is a library table — in practice
        `recipe_ingredients`, so that a bartender can read a global recipe card (TZ 3.3).
        """
        tablename: str = self.parent.__tablename__
        if tablename not in LIBRARY_TABLES:
            raise LibraryScopeError(tablename)
        column = self._checked_venue_column()
        return self._through_parent(or_(column == self.venue_id, column.is_(None)))

    def for_parent(self, parent_id: int) -> Select[tuple[ModelT]]:
        """Rows of one parent, with the venue check still in place."""
        return self.for_venue().where(self.model.__table__.c[self.parent_fk] == parent_id)
