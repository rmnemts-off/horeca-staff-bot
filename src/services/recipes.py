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

**Filling the section is the other half** (decision B5, plan task 30a). Without a way to
enter a recipe from the bot the TTK section of a venue stays empty forever and TZ 5.5 cannot
be walked through at all. :class:`RecipeDraft` is what the wizard collects,
:func:`parse_ingredients` turns the composition — which arrives as text, because a
line-by-line wizard for eight ingredients is an hour of typing (the same reasoning as
decision B6) — into rows, and :meth:`RecipeService.create` refuses a second card carrying the
key of decision D6 instead of quietly overwriting the first.

**Correcting it is the third** (TZ 5.8: the reference data of a venue is its manager's).
A card entered by hand has a typo in it sooner or later, and until :meth:`RecipeService.update`
existed the only answer was a second card next to the first — which is the one thing
decision D6 sets out to prevent. One field at a time, because that is what the screen edits;
:meth:`RecipeService.delete` removes a card whole, and the composition goes with it through
the foreign key rather than through a second call.

**The inline answer needs whole cards, not names** (part IV of the stage 1 spec).
:meth:`RecipeService.search_cards` is :meth:`RecipeService.search` with the compositions
already attached — one query for the rows and one for every composition on the page, because
the alternative is ten round trips for every letter a bartender types.

Units are the one thing this module has no words for. `Unit` is a closed set fixed by code
(TZ 4.4), but its short forms are interface language and language lives in
``src/bot/texts/``. So the vocabulary is handed in (``units=``) and an amount whose unit is
not in it is kept verbatim in `qty_text` rather than guessed at — decision D4 and TZ 7 keep
non-numeric amounts as they were typed, and a unit this layer invented would be worse than
no unit at all.

No wording lives in this module: it returns structures, and the renderer turns them into a
message (plan, task 21).
"""

from __future__ import annotations

import datetime as dt
import enum
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, Protocol

from src.db.models import Recipe, RecipeIngredient, Unit
from src.db.repositories.protocols import RecipeIngredientRepository, RecipeRepository
from src.services.access import AccessContext, AccessError, require_manager
from src.services.audit import SILENT, AuditEntity, AuditTrail, snapshot

#: TZ 5.5: "at most 10, with pagination".
MAX_SEARCH_RESULTS = 10

#: Key the composition is recorded under in `audit_log` (TZ 2). Not a column: the diff of
#: an edited composition is about rows of `recipe_ingredients`, and a reader of the log
#: wants the lines that changed rather than the number of them.
COMPOSITION_KEY: Final = "ingredients"

#: A service given no unit vocabulary: every amount then stays text (decision D4). The words
#: themselves are interface language and live in `src/bot/texts/recipes.py`.
NO_UNITS: Final[Mapping[str, Unit]] = MappingProxyType({})

#: The columns `create()` writes, and therefore the ones it records. The audit snapshot is
#: explicit on purpose (see `src/services/audit.py`): a dump of the whole row would carry
#: fields the edit never touched.
_AUDITED_FIELDS: Final = (
    "name",
    "category",
    "glassware",
    "method",
    "ice",
    "garnish",
    "instruction",
)

_WHITESPACE = re.compile(r"\s+")

#: Between an ingredient and its amount. An em or en dash is punctuation and never stands
#: inside a word, so it separates on its own; a hyphen is a letter of a compound name
#: (Coca-Cola, cold-brew) and separates only when it stands apart from the word before it.
#: The *first* separator splits the line, so an amount written as a range ("40 - 50 ml")
#: survives whole in `qty_text` instead of being cut in two. The dashes are spelled as
#: escapes because an en dash in a literal is a character nobody can tell from a hyphen.
_SEPARATOR = re.compile("\\s+[-\u2013\u2014]\\s*|[\u2013\u2014]")

#: An amount that is nothing but a number: "45", "45.0", "1,5" (decision D4, branch two).
_BARE_NUMBER = re.compile(r"^\d+(?:[.,]\d+)?$")

#: A number followed by one token, which is a unit only if the vocabulary says so.
_NUMBER_AND_UNIT = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(\S+)$")


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class RecipeError(AccessError):
    """Base class for the refusals of this module.

    Rooted in :class:`src.services.access.AccessError` like the roster and schedule
    services: a handler catches one family for "the service said no" and renders a text
    from ``src/bot/texts/``, while the error middleware keeps everything else a bug.
    """


class RecipeIncompleteError(RecipeError):
    """A card needs a name and a category before it can exist (TZ 5.5, decision B5).

    Everything else is optional — a card with nothing but a name and a composition is the
    normal state of a venue that has just started typing (TZ 8.1) — but the category is not
    decoration: it is half the key of decision D6 and the caption that tells two drinks of
    the same name apart.
    """

    def __init__(self, field_name: str) -> None:
        super().__init__(f"a recipe cannot be created without {field_name}")
        self.field_name = field_name


class RecipeExistsError(RecipeError):
    """The venue already has this recipe in this category (decision D6).

    Its own error rather than a silent overwrite: the manager typed a whole card, and
    writing it over the existing one would lose the ingredients of a recipe nobody asked to
    change. The caller renders "this one is already there" and offers the card that exists —
    which is why `recipe_id` is carried here.
    """

    def __init__(self, *, name: str, category: str, recipe_id: int) -> None:
        super().__init__(f"{name!r} already exists in category {category!r} as recipe {recipe_id}")
        self.name = name
        self.category = category
        self.recipe_id = recipe_id


class RecipeNotFoundError(RecipeError):
    """The card is not there — deleted, or never this venue's (TZ 9).

    One error for both, deliberately: a row of the bar next door is invisible to a
    venue-scoped repository exactly as a deleted one is, and telling the two apart is how a
    forged id becomes a way to find out what exists.
    """

    def __init__(self, recipe_id: int) -> None:
        super().__init__(f"recipe {recipe_id} is not available in this venue")
        self.recipe_id = recipe_id


class RecipeReadOnlyError(RecipeError):
    """A row of the shared BarPoint library, which a venue reads and does not write.

    TZ 3.3 has `recipes.venue_id = NULL` mean "everybody's", and question C4 — may a venue
    edit one — is open. Until it is answered the repository's writes are `for_venue()`, so
    an edit of a library row would silently do nothing; this says so instead.
    """

    def __init__(self, recipe_id: int) -> None:
        super().__init__(f"recipe {recipe_id} belongs to the shared library and is read-only")
        self.recipe_id = recipe_id


class RecipeField(enum.StrEnum):
    """Which part of an existing card an edit is aimed at (TZ 5.8).

    The values are one letter because this enum travels in `callback_data`, where the whole
    budget is 64 bytes (`src/bot/callbacks.py`). Nothing persists them — an `audit_log` row
    names the *column*, not this — so they are a routing key and not data.

    :attr:`COMPOSITION` is the odd one and is handled apart everywhere: it is not a column of
    `recipes` at all but the rows of `recipe_ingredients`, rewritten whole.
    """

    NAME = "n"
    CATEGORY = "c"
    GLASSWARE = "g"
    METHOD = "m"
    ICE = "i"
    GARNISH = "r"
    INSTRUCTION = "s"
    COMPOSITION = "p"


#: Which column of `recipes` each field writes. :attr:`RecipeField.COMPOSITION` is absent
#: because it has none — see the enum's docstring.
_COLUMNS: Final[Mapping[RecipeField, str]] = MappingProxyType(
    {
        RecipeField.NAME: "name",
        RecipeField.CATEGORY: "category",
        RecipeField.GLASSWARE: "glassware",
        RecipeField.METHOD: "method",
        RecipeField.ICE: "ice",
        RecipeField.GARNISH: "garnish",
        RecipeField.INSTRUCTION: "instruction",
    }
)

#: The two fields a card cannot lose: they are the key of decision D6, and blanking either
#: would leave a row no listing can caption and no duplicate check can compare.
#:
#: Public because a *screen* has to know it: the step that edits an optional field offers a
#: way to clear it, and offering that on these two would promise what :meth:`RecipeService.update`
#: refuses. One set, read by both, rather than two that agree until somebody edits one.
REQUIRED_FIELDS: Final = frozenset({RecipeField.NAME, RecipeField.CATEGORY})


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
class RecipeDraft:
    """What the wizard of decision B5 collected, before anything is written.

    Only `name` and `category` are required, and that is the whole rule of the minimal form:
    TZ 5.5 draws a card whose glassware, method, ice, garnish and instruction are absent on
    most classic recipes, and a form that demanded them would stop a manager from entering
    the drink they actually have. The category is not in that list — it is half the key of
    decision D6 and the caption that tells two drinks of the same name apart.

    `ingredients` is text, not rows: the composition arrives the way a person writes it,
    "name — amount" per line, and :func:`parse_ingredients` reads it. Several elements or one
    element with newlines are the same input — the wizard may hand over the message it got
    without splitting it first.
    """

    name: str
    category: str
    glassware: str | None = None
    method: str | None = None
    ice: str | None = None
    garnish: str | None = None
    instruction: str | None = None
    ingredients: Sequence[str] = ()


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
class CardPage:
    """A page of whole cards, where :class:`SearchPage` is a page of captions.

    The inline answer of part IV needs both halves of every hit at once: the dropdown row is
    a name, and what is sent when it is tapped is the card. Two structures rather than one
    because the screens that draw a keyboard need no compositions and must not pay for them
    (TZ 5.5 measures this in seconds).
    """

    query: str
    cards: tuple[RecipeCard, ...]
    offset: int
    limit: int
    has_next: bool

    @property
    def is_empty(self) -> bool:
        return not self.cards

    @property
    def next_offset(self) -> int | None:
        return self.offset + self.limit if self.has_next else None


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


def parse_ingredients(
    text: str,
    *,
    units: Mapping[str, Unit] = NO_UNITS,
) -> tuple[IngredientLine, ...]:
    """Read a typed composition into rows (decision B5, D4; TZ 7).

    One line is one ingredient. The amount is whatever follows the dash; a line without a
    dash is an ingredient named without an amount, which is a valid line — TZ 5.5 shows
    "Soda — top" and the reference book has rows with no quantity at all. Blank lines and a
    line that names nothing are dropped rather than stored as an empty ingredient.

    The amount lands in one of the three branches of :func:`classify_amount`, and the branch
    is decided by what can be read, never by what could be assumed:

    * a number and a unit the caller's vocabulary knows -> `qty` + `unit`, the measured form;
    * a bare number -> `qty` alone. The reference book stores "45.0" with the unit implied by
      the ingredient, and TZ 7 keeps it that way;
    * anything else -> `qty_text`, **exactly as it was typed**. "top up", "5 leaves",
      "2/3 dash", and equally "45 ml" when the vocabulary has no word for millilitres: a unit
      this layer guessed at would be a number the venue never wrote (decision D4).

    Pure on purpose — no repository, no venue, no clock — so that the wizard can show the
    manager what it understood before anything is saved.
    """
    vocabulary = {word.strip().casefold(): unit for word, unit in units.items()}
    lines: list[IngredientLine] = []
    for raw in text.splitlines():
        parsed = _ingredient_from_line(raw, len(lines), vocabulary)
        if parsed is not None:
            lines.append(parsed)
    return tuple(lines)


class RecipeService:
    """Business logic of TZ 5.5. Returns structures; wording is task 21."""

    def __init__(
        self,
        *,
        recipes: RecipeRepository,
        ingredients: RecipeIngredientRepository,
        notifier: RecipeNotifier,
        units: Mapping[str, Unit] = NO_UNITS,
        audit: AuditTrail = SILENT,
    ) -> None:
        self._recipes = recipes
        self._ingredients = ingredients
        self._notifier = notifier
        #: Unit words the venue's interface speaks; see the module docstring for why they
        #: are handed in instead of being written here.
        self._units = units
        self._audit = audit

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

    async def search_cards(
        self,
        query: str,
        *,
        offset: int = 0,
        limit: int = MAX_SEARCH_RESULTS,
    ) -> CardPage:
        """The same search as :meth:`search`, with every composition already attached.

        Two queries and not eleven: the rows, then all their ingredients at once
        (`list_for_recipes`). The inline answer of part IV builds a whole page for every
        letter typed, and a round trip per card is the difference between a list that keeps
        up with a thumb and one that lags a word behind it.

        The paging arithmetic deliberately repeats :meth:`search` rather than calling it: what
        that method returns is captions, and the rows themselves — which a card is assembled
        from — are gone by then. The one page-shaped fact both share is `limit + 1`, asking
        for one row more than fits to learn whether another page follows.
        """
        cleaned = _normalise_query(query)
        window = _window(limit)
        start = max(offset, 0)
        if not cleaned:
            return CardPage(query="", cards=(), offset=start, limit=window, has_next=False)

        found = _prefer_local(await self._recipes.search(cleaned, limit=window + 1, offset=start))
        rows = found[:window]
        compositions = _by_recipe(
            await self._ingredients.list_for_recipes([row.id for row in rows])
        )
        return CardPage(
            query=cleaned,
            cards=tuple(_card(row, compositions.get(row.id, ())) for row in rows),
            offset=start,
            limit=window,
            has_next=len(found) > window,
        )

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
        return _card(recipe, rows)

    async def create(self, actor: AccessContext, draft: RecipeDraft) -> RecipeCard:
        """Enter a recipe from the bot (decision B5, plan task 30a).

        TZ 5.8 puts the reference data of a venue in the hands of its manager, so
        :func:`require_manager` guards the write. No venue is taken from the draft: the
        repository is scoped to one and the actor works in one, and those are the same venue
        by construction (TZ 3.3) — a venue id in a wizard's payload would be a second answer
        to a question that already has one.

        The card is returned rather than the row: the wizard shows the manager what was
        saved, and it is the same structure :meth:`card` renders, so the confirmation screen
        and the card are the same screen.

        Order matters. The duplicate of decision D6 is refused before anything is written,
        because the alternative is a half-created recipe rolled back by an index violation
        the manager cannot read.
        """
        require_manager(actor)
        name = _clean(draft.name)
        if name is None:
            raise RecipeIncompleteError("name")
        category = _clean(draft.category)
        if category is None:
            raise RecipeIncompleteError("category")

        existing = await self._twin(name=name, category=category)
        if existing is not None:
            raise RecipeExistsError(name=name, category=category, recipe_id=existing.id)

        lines = parse_ingredients("\n".join(draft.ingredients), units=self._units)
        recipe = await self._recipes.create(
            name=name,
            category=category,
            glassware=_clean(draft.glassware),
            method=_clean(draft.method),
            ice=_clean(draft.ice),
            garnish=_clean(draft.garnish),
            instruction=_clean(draft.instruction),
            updated_by=actor.user_id,
        )
        rows = await self._ingredients.replace_all(
            recipe.id,
            [_ingredient_row(line) for line in lines],
        )
        await self._audit.created(
            actor,
            AuditEntity.RECIPE,
            recipe.id,
            after=snapshot(recipe, *_AUDITED_FIELDS),
        )
        return _card(recipe, rows)

    async def update(
        self,
        actor: AccessContext,
        recipe_id: int,
        field: RecipeField,
        value: str | None,
    ) -> RecipeCard:
        """Correct one field of a card that already exists (TZ 5.8).

        One field per call, because that is the shape of the screen: a manager who spotted a
        typo in a name is not asked the other seven questions again. The card comes back
        assembled, so what is shown after the edit is drawn by the same code as the card the
        shift is reading — the confirmation and the card are one screen, as they are after a
        create.

        Three refusals, and each is a screen rather than a traceback:

        * a card that is not this venue's is :class:`RecipeNotFoundError`, indistinguishable
          from one that was deleted (TZ 9);
        * a row of the shared library is :class:`RecipeReadOnlyError` — the repository writes
          `for_venue()` only, so without this the edit would report success and change
          nothing (question C4);
        * a new name or category that collides with another card of the venue is
          :class:`RecipeExistsError`, refused *before* the write for the same reason
          :meth:`create` refuses it there: the alternative is an index violation the manager
          cannot read. The card being edited is not its own twin.
        """
        require_manager(actor)
        recipe = await self._writable(recipe_id)
        cleaned = _clean(value)
        if field in REQUIRED_FIELDS and cleaned is None:
            raise RecipeIncompleteError(_COLUMNS[field])
        if field is RecipeField.COMPOSITION:
            return await self._rewrite_composition(actor, recipe, value or "")

        column = _COLUMNS[field]
        if field in REQUIRED_FIELDS and cleaned is not None:
            await self._refuse_twin(recipe, field, cleaned)
        # Read before the write and not after: `update()` returns the same object out of the
        # identity map, so a snapshot taken afterwards would record the new value as the old
        # one and the diff would come out empty (TZ 2).
        before = snapshot(recipe, column)
        # `updated_by` travels with every write, the way `TemplateService` maintains it: the
        # column answers "who last touched this row", and a create that stamped it followed
        # by edits that did not would have it name the wrong person from the second edit on.
        written = await self._recipes.update(
            recipe_id,
            **{column: cleaned},
            updated_by=actor.user_id,
        )
        if written is None:
            # Gone between the read and the write; the repository refuses to say more.
            raise RecipeNotFoundError(recipe_id)
        await self._audit.updated(
            actor,
            AuditEntity.RECIPE,
            written.id,
            before=before,
            after=snapshot(written, column),
        )
        return _card(written, await self._ingredients.list_for_recipe(written.id))

    async def delete(self, actor: AccessContext, recipe_id: int) -> str:
        """Remove a card whole, and answer with the name it had (TZ 5.8).

        The composition goes with it through `ON DELETE CASCADE` rather than through a
        second call: two writes where the schema already promises one is a way to leave
        orphans behind when the second fails.

        A card is deleted and not switched off, and that is a decision. `is_active` would
        keep the row inside the unique index of decision D6, so a venue that removed a drink
        by mistake could never enter it again — the index would refuse the new card against a
        row no screen shows. Nothing in the schema points at `recipes`, so there is
        no history to preserve except the `audit_log` record this writes.

        The name is read before the row goes, and returned rather than looked up again:
        after the delete there is nothing left to ask.
        """
        require_manager(actor)
        recipe = await self._writable(recipe_id)
        before = snapshot(recipe, *_AUDITED_FIELDS)
        name = recipe.name.strip()
        if not await self._recipes.delete(recipe_id):
            raise RecipeNotFoundError(recipe_id)
        await self._audit.deleted(actor, AuditEntity.RECIPE, recipe_id, before=before)
        return name

    async def _writable(self, recipe_id: int) -> Recipe:
        """The card this venue may change, or the refusal that says why it may not."""
        recipe = await self._recipes.get(recipe_id)
        if recipe is None:
            raise RecipeNotFoundError(recipe_id)
        if recipe.venue_id is None:
            raise RecipeReadOnlyError(recipe_id)
        return recipe

    async def _refuse_twin(self, recipe: Recipe, field: RecipeField, value: str) -> None:
        """Decision D6 for an edit: the key this change would produce must still be free."""
        name = value if field is RecipeField.NAME else recipe.name
        category = value if field is RecipeField.CATEGORY else recipe.category
        twin = await self._twin(name=name, category=category, besides=recipe.id)
        if twin is not None:
            raise RecipeExistsError(name=name, category=category, recipe_id=twin.id)

    async def _rewrite_composition(
        self,
        actor: AccessContext,
        recipe: Recipe,
        typed: str,
    ) -> RecipeCard:
        """Replace the composition with what was typed (decision D4 reads the amounts).

        A blank message is a valid answer and clears it: a card is valid with nothing but a
        name (TZ 5.5), and a manager who entered a composition by mistake must be able to
        take it away without deleting the card.
        """
        before = _composition_digest(await self._ingredients.list_for_recipe(recipe.id))
        lines = parse_ingredients(typed, units=self._units)
        rows = await self._ingredients.replace_all(
            recipe.id,
            [_ingredient_row(line) for line in lines],
        )
        # The composition is a child table, so the card's own row is untouched by the write
        # above — and it is still the card that changed. Same column, same reason as above.
        await self._recipes.update(recipe.id, updated_by=actor.user_id)
        await self._audit.updated(
            actor,
            AuditEntity.RECIPE,
            recipe.id,
            before={COMPOSITION_KEY: before},
            after={COMPOSITION_KEY: _composition_digest(rows)},
        )
        return _card(recipe, rows)

    async def _twin(self, *, name: str, category: str, besides: int | None = None) -> Recipe | None:
        """The row decision D6 says this name would collide with, or `None`.

        The key is `(venue_id, category, lower(btrim(name)))` — the unique index of
        `recipes`, and the same one :func:`_prefer_local` overlays by, so a venue can hold
        an Americano the cocktail next to an Americano the coffee.

        Three things about it are worth spelling out:

        * **library rows are not a collision.** The index treats `venue_id IS NULL` as its
          own scope, and a venue writing its own version of a shared recipe is exactly what
          :func:`_prefer_local` exists for. Refusing it here would make the overlay
          unreachable (TZ 3.3). `find_by_key` is `for_venue()` and answers that by itself;
        * **the index is asked, not a listing.** This used to page through
          `list_by_category` until it ended, which was correct and blind in one place: that
          listing filters on `is_active`, and the index does not. A card put away and its
          name entered again would have passed the check and died on the constraint;
        * **the row being edited is not its own twin** (`besides`). Correcting the case of a
          name is a change of spelling, not a duplicate.
        """
        found = await self._recipes.find_by_key(name=name, category=category)
        if found is None or found.id == besides:
            return None
        return found

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


def _by_recipe(rows: Sequence[RecipeIngredient]) -> dict[int, list[RecipeIngredient]]:
    """One bulk read, split back into the cards it belongs to; order is preserved."""
    grouped: dict[int, list[RecipeIngredient]] = {}
    for row in rows:
        grouped.setdefault(row.recipe_id, []).append(row)
    return grouped


def _composition_digest(rows: Sequence[RecipeIngredient]) -> tuple[str, ...]:
    """The composition as `audit_log` records it: one string per line, in order (TZ 2).

    Enough for a reader of the log to see *which* line moved, where a count would say only
    that something did — and swapping one ingredient for another does not change a count, so
    the record would have been skipped entirely.

    A unit is written by its schema value and not by its interface word: this is a record and
    not a screen, and the words live in `src/bot/texts/`.
    """
    return tuple(_digest_line(row) for row in sorted(rows, key=_ingredient_order))


def _digest_line(row: RecipeIngredient) -> str:
    name = row.name.strip()
    amount = _digest_amount(row)
    return name if amount is None else f"{name} {amount}"


def _digest_amount(row: RecipeIngredient) -> str | None:
    """The amount of one line, exactly as the three branches of decision D4 hold it."""
    match classify_amount(row.qty, row.unit, row.qty_text):
        case AmountKind.MEASURED if row.qty is not None and row.unit is not None:
            return f"{_digest_number(row.qty)} {row.unit.value}"
        case AmountKind.NUMERIC if row.qty is not None:
            return _digest_number(row.qty)
        case AmountKind.TEXT:
            return _clean(row.qty_text)
        case _:
            return None


def _digest_number(value: Decimal) -> str:
    """«50», never «50.000» — the scale a `Numeric(12,3)` column hands back.

    Without this the digest compares two different renderings of one number: `before` is
    read from the database, where 50 came back as `Decimal("50.000")`, and `after` is built
    from rows that were just inserted and never re-read, where it is still `Decimal("50")`.
    Every measured line would then read as changed — including on an edit that rewrote the
    composition with the very same text, which `AuditTrail.updated` exists to skip.
    """
    whole = value.to_integral_value()
    return f"{whole:f}" if value == whole else f"{value.normalize():f}"


def _key(name: str, category: str) -> tuple[str, str]:
    """Decision D6: a recipe is identified by its category plus its folded name.

    The category is folded too, where the index compares it as written. That is stricter
    than the index by exactly one case: a second "Coffee" next to an existing "coffee". The
    index would take it, `list_by_category` folds and would show both rows in one listing,
    and two identical captions in one list is the thing decision D6 set out to prevent.
    """
    return (category.strip().casefold(), name.strip().casefold())


def _overlay_key(recipe: Recipe) -> tuple[str, str]:
    """The key of :func:`_key`, read off a row."""
    return _key(recipe.name, recipe.category)


def _card(recipe: Recipe, rows: Sequence[RecipeIngredient]) -> RecipeCard:
    """Assemble a card from a row and its composition.

    Shared by :meth:`RecipeService.card` and :meth:`RecipeService.create` so that what a
    manager sees right after saving is assembled by the same code as what a bartender opens
    tomorrow — a confirmation screen built separately is a screen that drifts.
    """
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


def _ingredient_from_line(
    raw: str,
    index: int,
    vocabulary: Mapping[str, Unit],
) -> IngredientLine | None:
    """One typed line, or `None` when there is no ingredient in it."""
    line = _WHITESPACE.sub(" ", raw).strip()
    if not line:
        return None
    separator = _SEPARATOR.search(line)
    name = line if separator is None else line[: separator.start()].strip()
    amount = "" if separator is None else line[separator.end() :].strip()
    if not name:
        return None
    qty, unit, qty_text = _parse_amount(amount, vocabulary)
    return IngredientLine(
        name=name,
        kind=classify_amount(qty, unit, qty_text),
        qty=qty,
        unit=unit,
        qty_text=qty_text,
        product_id=None,
        prep_id=None,
        order_index=index,
    )


def _parse_amount(
    amount: str,
    vocabulary: Mapping[str, Unit],
) -> tuple[Decimal | None, Unit | None, str | None]:
    """Split a typed amount into the three columns of decision D4.

    Nothing is rewritten on the way: the only text that becomes a number is text that is
    nothing but a number, and the only word that becomes a `Unit` is a word the vocabulary
    already holds. Everything else is stored as it was typed.
    """
    if not amount:
        return (None, None, None)
    if _BARE_NUMBER.match(amount):
        return (_decimal(amount), None, None)
    measured = _NUMBER_AND_UNIT.match(amount)
    if measured is not None:
        unit = vocabulary.get(measured.group(2).casefold())
        if unit is not None:
            return (_decimal(measured.group(1)), unit, None)
    return (None, None, amount)


def _decimal(number: str) -> Decimal:
    """A comma is a decimal point here: the venue writes "1,5" and means one and a half."""
    return Decimal(number.replace(",", "."))


def _ingredient_row(line: IngredientLine) -> dict[str, Any]:
    """One parsed line as `RecipeIngredientRepository.replace_all` takes it."""
    return {
        "name": line.name,
        "qty": line.qty,
        "unit": line.unit,
        "qty_text": line.qty_text,
        "order_index": line.order_index,
    }


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
    "COMPOSITION_KEY",
    "MAX_SEARCH_RESULTS",
    "NO_UNITS",
    "REQUIRED_FIELDS",
    "AmountKind",
    "CardPage",
    "IngredientLine",
    "MissingRecipeAlert",
    "RecipeCard",
    "RecipeDraft",
    "RecipeError",
    "RecipeExistsError",
    "RecipeField",
    "RecipeHit",
    "RecipeIncompleteError",
    "RecipeNotFoundError",
    "RecipeNotifier",
    "RecipeReadOnlyError",
    "RecipeService",
    "SearchPage",
    "classify_amount",
    "parse_ingredients",
]
