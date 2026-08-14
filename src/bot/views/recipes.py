"""Screens of the catalogue: the section, the hit list, the card (TZ 5.5; plan task 25).

Pure functions of what the recipe service returned, as the package docstring requires: no
sending, no editing, no repository. That is what lets the card be asserted as a string in
`tests/bot/test_recipes_screen.py` without a bot anywhere near it.

**An absent field is absent from the card.** `instruction`, `ice`, `garnish` and
`glassware` are empty on most classic recipes in the reference book, and the service has
already turned "" into `None` (`src/services/recipes.py`); here that means the line is not
rendered at all — no heading, no dash, no colon with nothing behind it. A card that has
nothing but a name and a composition is therefore a *valid* card, which is the normal state
of a venue that has just started typing its recipes in (TZ 8.1).

**The three branches of an amount are the service's decision, not this module's** (decision
D4, TZ 7): `AmountKind` names which of "50 ml", a bare "45" and "top up" a line is, and
:func:`_amount` prints the one it was handed. Nothing is guessed here — a unit this layer
invented would be a number the venue never wrote.

**Two empty states, and they are different questions.** A venue with no recipes at all sees
`TTK_EMPTY` (:func:`section`, TZ 8.1) — a fact about the venue, with no
button pretending otherwise, because entering recipes is the manager's screen (TZ 5.8) and
this one is opened by everybody. A search that matched nothing sees `TTK_NOTHING_FOUND`
(:func:`results`) with the button of TZ 5.5 that tells the manager about it. The first is
answered by filling the section, the second by one person being told one word.

**Venue text is quoted.** `TTK_CARD_NAME_TEMPLATE` is `<b>{name}</b>`, so these screens are
HTML, and a drink called «Rum & Cola» must not arrive as a broken entity. See the module
docstring of `src/bot/handlers/recipes.py` for the half of that which is not here yet.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal

from src.bot import texts
from src.bot.keyboards import recipes as keyboards
from src.bot.views import Screen, quoted
from src.services.recipes import AmountKind, IngredientLine, RecipeCard, RecipeHit, SearchPage


def section(categories: Sequence[str]) -> Screen:
    """The screen the catalogue section opens on: an invitation to search, or the truth.

    Emptiness is read off the categories a venue has actually used, because that is the one
    question the service answers cheaply and it is the same one: a venue with no category
    has no recipe, and until it has one there is nothing to search (TZ 8.1).
    """
    body = texts.TTK_SEARCH_PROMPT if categories else texts.TTK_EMPTY
    return Screen(text=_joined((texts.TTK_TITLE, body)), markup=keyboards.section())


def results(page: SearchPage, *, suggestions: Sequence[RecipeHit] = ()) -> Screen:
    """A page of hits, or the screen for having found none (TZ 5.5).

    One function and not two: which of the two a search deserves is decided by the page the
    service returned, and a handler that chose between two views would be the branch TZ 8.1
    asks to keep out of handlers.

    A page with no query at all is neither: it is what a blank line typed into the search
    produces, and the answer to that is the invitation again rather than a report that
    nothing matched nothing.
    """
    if not page.query:
        return Screen(text=texts.TTK_SEARCH_PROMPT, markup=keyboards.section())
    if page.is_empty:
        return _nothing_found(page.query, suggestions)
    return Screen(text=_found(page), markup=keyboards.hits(page))


def card(recipe: RecipeCard) -> Screen:
    """The card of TZ 5.5, with every empty part missing rather than blank."""
    return Screen(
        text=_joined(
            (
                _name(recipe),
                _serving(recipe),
                _composition(recipe.ingredients),
                _optional(texts.TTK_CARD_INSTRUCTION_TEMPLATE, recipe.instruction),
                _optional(texts.TTK_CARD_GARNISH_TEMPLATE, recipe.garnish),
            )
        ),
        markup=keyboards.card(),
    )


# --------------------------------------------------------------------------------------
# The hit list
# --------------------------------------------------------------------------------------


def _found(page: SearchPage) -> str:
    """`TTK_FOUND_TEMPLATE`, plus which page of them this is."""
    lines = [texts.TTK_FOUND_TEMPLATE.format(count=len(page.hits))]
    if page.has_previous or page.has_next:
        lines.append(texts.TTK_PAGE_TEMPLATE.format(page=page.page_number))
    return _joined(lines)


def _nothing_found(query: str, suggestions: Sequence[RecipeHit]) -> Screen:
    """TZ 5.5: "not found, did you mean ..." and the button that reports it."""
    lines = [texts.TTK_NOTHING_FOUND_TEMPLATE.format(query=_quoted(query))]
    if suggestions:
        lines.append(texts.TTK_MAYBE_TITLE)
    return Screen(text=_joined(lines), markup=keyboards.nothing_found(suggestions))


# --------------------------------------------------------------------------------------
# The card
# --------------------------------------------------------------------------------------


def _name(recipe: RecipeCard) -> str:
    """The name, and the seasonal mark when there is one (TZ 5.5)."""
    line = texts.TTK_CARD_NAME_TEMPLATE.format(name=_quoted(recipe.name))
    if not recipe.is_seasonal:
        return line
    return texts.TTK_CARD_SEPARATOR.join((line, texts.TTK_CARD_SEASONAL_MARK))


def _serving(recipe: RecipeCard) -> str:
    """The glassware / method / ice line, of whichever of the three are filled in."""
    parts = (
        _optional(texts.TTK_CARD_GLASSWARE_TEMPLATE, recipe.glassware),
        _optional(texts.TTK_CARD_METHOD_TEMPLATE, recipe.method),
        _optional(texts.TTK_CARD_ICE_TEMPLATE, recipe.ice),
    )
    return texts.TTK_CARD_SEPARATOR.join(part for part in parts if part)


def _composition(lines: Sequence[IngredientLine]) -> str:
    """The heading and the lines, or nothing at all when there is no composition."""
    if not lines:
        return ""
    return _joined((texts.TTK_CARD_COMPOSITION_TITLE, *(_ingredient(line) for line in lines)))


def _ingredient(line: IngredientLine) -> str:
    """One line of the composition.

    A line that points at a prep (`links_to_prep`) is printed exactly like any other: the
    preps section is TZ 5.5's own and stage 0 does not build it, so there is nowhere for a
    link to lead yet.
    """
    amount = _amount(line)
    if amount is None:
        return texts.TTK_CARD_LINE_PLAIN_TEMPLATE.format(name=_quoted(line.name))
    return texts.TTK_CARD_LINE_TEMPLATE.format(name=_quoted(line.name), amount=amount)


def _amount(line: IngredientLine) -> str | None:
    """Print the branch the service chose, or `None` for an ingredient with no amount.

    The guards are not a second opinion about the branch: they are what makes an optional
    field of the dataclass printable at all, and a line whose fields contradict its own kind
    falls through to «no amount», which is a line the card can still show.
    """
    match line.kind:
        case AmountKind.MEASURED if line.qty is not None and line.unit is not None:
            return texts.TTK_CARD_AMOUNT_TEMPLATE.format(
                qty=_number(line.qty),
                unit=texts.unit_label(line.unit),
            )
        case AmountKind.NUMERIC if line.qty is not None:
            return _number(line.qty)
        case AmountKind.TEXT if line.qty_text is not None:
            return _quoted(line.qty_text)
        case _:
            return None


def _number(value: Decimal) -> str:
    """«50», not «50.00»: a venue writes an amount, not a precision (TZ 7)."""
    whole = value.to_integral_value()
    return f"{whole:f}" if value == whole else f"{value.normalize():f}"


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _optional(template: str, value: str | None) -> str:
    """A line, or nothing — never a heading with an empty value behind it."""
    return "" if value is None else template.format(value=_quoted(value))


def _quoted(value: str) -> str:
    """Venue text, safe to put inside the HTML these templates are written in.

    The rule belongs to the package (`src/bot/views/__init__.py::quoted`); this alias only
    keeps the call sites below short.
    """
    return quoted(value)


def _joined(blocks: Iterable[str]) -> str:
    """Whatever is present, one per line; an absent part leaves no blank line behind."""
    return "\n".join(block for block in blocks if block)


__all__ = ["card", "results", "section"]
