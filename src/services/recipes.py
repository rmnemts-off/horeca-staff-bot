"""Recipe search and card assembly (TZ 5.5, plan task 18).

TZ 5.5 is one sentence long in spirit: a bartender in the middle of a shift needs one
cocktail in five seconds. Everything here follows from that.

* **Search, not a dump.** Typo- and case-tolerant matching over name and synonyms is the
  repository's job (plan, task 13, `similarity()` over a trigram index); this service caps
  a page at ten hits and pages through the rest, exactly as TZ 5.5 asks.
* **Every hit carries its category.** A bar legitimately has two rows called *Americano* —
  the cocktail and the coffee (decision D6 makes that the uniqueness key). A list of two
  identical captions is a list the bartender cannot choose from.
* **Empty fields disappear from the card.** `instruction`, `ice`, `garnish` and
  `glassware` are absent on most classic recipes in the reference book, and a line reading
  "Garnish:" with nothing after the colon is worse than no line. The service therefore
  normalises blank strings to ``None`` and the renderer simply skips them.
* **Three branches for the amount** (decision D4, TZ 7): a measured amount (50 ml), a bare
  number with no unit (45), and free text (top up, 5 leaves). The branch is decided here
  and named in :class:`AmountKind`; the words for it belong to task 21.

The card must survive a row that has nothing but a name and its ingredients — that is the
normal state of a venue that has just started typing its recipes in (TZ 8.1).

No wording lives in this module: it returns structures, and the renderer turns them into a
message (plan, task 21).
"""

from __future__ import annotations

import datetime as dt
import enum
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from src.db.models import Recipe, RecipeIngredient, Unit
from src.db.repositories.protocols import RecipeIngredientRepository, RecipeRepository

#: TZ 5.5: "at most 10, with pagination".
MAX_SEARCH_RESULTS = 10

_WHITESPACE = re.compile(r"\s+")


class AmountKind(enum.StrEnum):
    """Which of the three renderings a line needs (plan, task 18)."""

    #: `qty` and `unit` are both set: "50" + the word for millilitres.
    MEASURED = "measured"
    #: `qty` without a unit: the reference book stores plain "45" for some rows.
    NUMERIC = "numeric"
    #: Free text only: "top up", "5 leaves", "2/3 dash" (decision D4, TZ 7).
    TEXT = "text"
    #: Nothing at all — an ingredient named without an amount. Still a valid line.
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class IngredientLine:
    """One line of the composition, with the branch already chosen."""

    name: str
    kind: AmountKind
    qty: Decimal | None
    unit: Unit | None
    qty_text: str | None
    product_id: int | None
    prep_id: int | None
    order_index: int

    @property
    def links_to_prep(self) -> bool:
        """TZ 5.5: from a cocktail card, jump to the card of a prep it contains."""
        return self.prep_id is not None


@dataclass(frozen=True, slots=True)
class RecipeCard:
    """A card that is valid with nothing but a name and a list of ingredients.

    Every optional text field is ``None`` when the source is empty or blank, which is the
    signal to the renderer that the line does not exist rather than that it is empty.
    """

    recipe_id: int
    name: str
    category: str
    is_library: bool
    glassware: str | None
    method: str | None
    ice: str | None
    garnish: str | None
    instruction: str | None
    season_date: dt.date | None
    yield_variants: dict[str, Any] | None
    photo_file_id: str | None
    ingredients: tuple[IngredientLine, ...]

    @property
    def has_ingredients(self) -> bool:
        return bool(self.ingredients)

    @property
    def has_serving_line(self) -> bool:
        """Whether the glassware / method / ice line has anything to show at all."""
        return any((self.glassware, self.method, self.ice))

    @property
    def is_seasonal(self) -> bool:
        return self.season_date is not None


@dataclass(frozen=True, slots=True)
class RecipeHit:
    """One search result. `category` is not decoration — see the module docstring."""

    recipe_id: int
    name: str
    category: str
    is_library: bool


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of results, with everything the pager buttons need.

    There is no total: the repository would have to count separately, and the count would
    stop matching the page as soon as a local recipe shadows a library one. The page is
    built by asking for one row more than fits, which answers "is there a next page"
    exactly, and TZ 5.5 asks for pagination, not for a result counter.
    """

    query: str
    hits: tuple[RecipeHit, ...]
    offset: int
    limit: int
    has_next: bool

    @property
    def is_empty(self) -> bool:
        return not self.hits

    @property
    def has_previous(self) -> bool:
        return self.offset > 0

    @property
    def next_offset(self) -> int | None:
        return self.offset + self.limit if self.has_next else None

    @property
    def previous_offset(self) -> int | None:
        return max(self.offset - self.limit, 0) if self.has_previous else None

    @property
    def page_number(self) -> int:
        return self.offset // self.limit + 1


@dataclass(frozen=True, slots=True)
class MissingRecipeAlert:
    """TZ 5.5: "Report that the recipe is missing" -> a notification to the manager."""

    query: str
    reported_by: int


class RecipeNotifier(Protocol):
    """The one thing this service tells a manager (TZ 5.5, plan task 19)."""

    async def recipe_missing(self, alert: MissingRecipeAlert) -> None: ...


def classify_amount(
    qty: Decimal | None,
    unit: Unit | None,
    qty_text: str | None,
) -> AmountKind:
    """Pick the rendering branch for one ingredient (decision D4).

    Order of precedence, and why:

    1. ``qty`` **and** ``unit`` -> :attr:`AmountKind.MEASURED`. The most precise form wins.
    2. free text -> :attr:`AmountKind.TEXT`. When there is no unit, the text a venue typed
       carries strictly more than a bare number does: "5 leaves" against "5". That is the
       whole reason TZ 7 keeps non-numeric amounts instead of discarding them.
    3. ``qty`` alone -> :attr:`AmountKind.NUMERIC`. The reference book has rows like "45.0"
       with the unit implied by the ingredient.
    4. nothing -> :attr:`AmountKind.ABSENT`. Still a valid line: the ingredient is named.
    """
    if qty is not None and unit is not None:
        return AmountKind.MEASURED
    if _clean(qty_text) is not None:
        return AmountKind.TEXT
    if qty is not None:
        return AmountKind.NUMERIC
    return AmountKind.ABSENT


class RecipeService:
    """Business logic of TZ 5.5. Returns structures; wording is task 21."""

    def __init__(
        self,
        *,
        recipes: RecipeRepository,
        ingredients: RecipeIngredientRepository,
        notifier: RecipeNotifier,
    ) -> None:
        self._recipes = recipes
        self._ingredients = ingredients
        self._notifier = notifier

    async def search(
        self,
        query: str,
        *,
        offset: int = 0,
        limit: int = MAX_SEARCH_RESULTS,
    ) -> SearchPage:
        """Find recipes by name or synonym, at most ten per page (TZ 5.5).

        A blank query is not a search for everything: TZ 5.5 rules out dumping the whole
        table, so it returns an empty page and never reaches the database.
        """
        cleaned = _normalise_query(query)
        window = _window(limit)
        start = max(offset, 0)
        if not cleaned:
            return SearchPage(query="", hits=(), offset=start, limit=window, has_next=False)

        found = await self._recipes.search(cleaned, limit=window + 1, offset=start)
        return _page(cleaned, found, offset=start, limit=window)

    async def browse(
        self,
        category: str,
        *,
        offset: int = 0,
        limit: int = MAX_SEARCH_RESULTS,
    ) -> SearchPage:
        """The other entry of TZ 5.5: pick a category instead of typing a name."""
        cleaned = _normalise_query(category)
        window = _window(limit)
        start = max(offset, 0)
        if not cleaned:
            return SearchPage(query="", hits=(), offset=start, limit=window, has_next=False)

        found = await self._recipes.list_by_category(cleaned, limit=window + 1, offset=start)
        return _page(cleaned, found, offset=start, limit=window)

    async def categories(self) -> tuple[str, ...]:
        """Categories a venue has actually used. Empty until it enters its first recipe."""
        return tuple(await self._recipes.list_categories())

    async def card(self, recipe_id: int) -> RecipeCard | None:
        """Assemble the card, or ``None`` for an unknown or foreign recipe (TZ 9).

        Blank optional fields become ``None`` so that the renderer drops the line entirely
        instead of printing a colon with nothing behind it.
        """
        recipe = await self._recipes.get(recipe_id)
        if recipe is None:
            return None

        rows = await self._ingredients.list_for_recipe(recipe.id)
        return RecipeCard(
            recipe_id=recipe.id,
            name=recipe.name.strip(),
            category=recipe.category,
            is_library=recipe.venue_id is None,
            glassware=_clean(recipe.glassware),
            method=_clean(recipe.method),
            ice=_clean(recipe.ice),
            garnish=_clean(recipe.garnish),
            instruction=_clean(recipe.instruction),
            season_date=recipe.season_date,
            yield_variants=recipe.yield_variants,
            photo_file_id=recipe.photo_file_id,
            ingredients=tuple(_ingredient_line(row) for row in sorted(rows, key=_ingredient_order)),
        )

    async def report_missing(self, *, query: str, reported_by: int) -> MissingRecipeAlert | None:
        """TZ 5.5: the employee says the recipe is not there, the manager hears about it.

        A blank query tells the manager nothing, so nothing is sent.
        """
        cleaned = _normalise_query(query)
        if not cleaned:
            return None
        alert = MissingRecipeAlert(query=cleaned, reported_by=reported_by)
        await self._notifier.recipe_missing(alert)
        return alert


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    """Blank is absent: an empty or whitespace-only field must not render as a line."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _normalise_query(query: str) -> str:
    """Trim and collapse whitespace. Case is left to the repository, which folds it in SQL."""
    return _WHITESPACE.sub(" ", query).strip()


def _window(limit: int) -> int:
    """A page is between one and ten rows, whatever the caller asked for (TZ 5.5)."""
    return min(max(limit, 1), MAX_SEARCH_RESULTS)


def _ingredient_order(row: RecipeIngredient) -> tuple[int, int]:
    return (row.order_index, row.id)


def _ingredient_line(row: RecipeIngredient) -> IngredientLine:
    return IngredientLine(
        name=row.name.strip(),
        kind=classify_amount(row.qty, row.unit, row.qty_text),
        qty=row.qty,
        unit=row.unit,
        qty_text=_clean(row.qty_text),
        product_id=row.product_id,
        prep_id=row.prep_id,
        order_index=row.order_index,
    )


def _overlay_key(recipe: Recipe) -> tuple[str, str]:
    """Decision D6: a recipe is identified by its category plus its folded name."""
    return (recipe.category.strip().casefold(), recipe.name.strip().casefold())


def _prefer_local(found: Sequence[Recipe]) -> list[Recipe]:
    """Drop a library row that the venue has its own version of (TZ 3.3, decision D6).

    The repository returns the union of the venue's rows and the shared library; deciding
    that a local row overrides a global one with the same key is a service-layer call, and
    this is where it is made. The library ships empty (TZ 3.3), so today this is a no-op
    that will start doing work the moment BarPoint publishes its first shared recipe.
    """
    shadowed = {_overlay_key(row) for row in found if row.venue_id is not None}
    return [row for row in found if row.venue_id is not None or _overlay_key(row) not in shadowed]


def _page(query: str, found: Sequence[Recipe], *, offset: int, limit: int) -> SearchPage:
    """Turn `limit + 1` rows into a page of `limit` plus the answer about the next one."""
    rows = _prefer_local(found)
    has_next = len(rows) > limit
    return SearchPage(
        query=query,
        hits=tuple(_hit(row) for row in rows[:limit]),
        offset=offset,
        limit=limit,
        has_next=has_next,
    )


def _hit(recipe: Recipe) -> RecipeHit:
    return RecipeHit(
        recipe_id=recipe.id,
        name=recipe.name.strip(),
        category=recipe.category,
        is_library=recipe.venue_id is None,
    )


__all__ = [
    "MAX_SEARCH_RESULTS",
    "AmountKind",
    "IngredientLine",
    "MissingRecipeAlert",
    "RecipeCard",
    "RecipeHit",
    "RecipeNotifier",
    "RecipeService",
    "SearchPage",
    "classify_amount",
]
