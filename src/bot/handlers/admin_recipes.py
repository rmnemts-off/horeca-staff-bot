"""Entering a recipe by hand (TZ 5.5, decision B5; plan task 30a).

Owner: plan task 30a. Without this module the catalogue of a venue stays empty forever and
TZ 5.5 cannot be walked through at all — a bartender's search over a table nobody can write
to is the whole of the section. Two screens and one wizard: the manager's half of the
catalogue (TZ 5.8), and the eight steps of decision B5 — name, category, glassware, method,
ice, composition, garnish, instruction.

Every handler is the shape TZ 3.2 requires: take the update, call a service, render a view.
The wording is `src/bot/texts/admin.py`, the screens are `src/bot/views/recipe_form.py`, and
the composition is read by `RecipeService.parse_ingredients` and never here — a line whose
amount is a word rather than a number is decision D4's business, not a handler's.

**The form is a table, not eight copies of one handler.** :data:`FORM` lists the steps in
order with the field each fills and whether it may be skipped; one message handler and one
skip handler read the current FSM state out of :data:`STEPS_BY_STATE` and advance. The point
is not brevity: the order of the steps, which of them are optional and which key each writes
are then one thing to read and one thing to change, instead of eight places where the seventh
is the one that drifts.

**Every press of this block is vetted before it arrives; every typed step is not.**
`AdminCommand` (open the form, skip a step), `RecipeCategory` (a category of the page) and
`OpenAdmin` all carry `minimum_role=MANAGER` in `src/bot/middlewares/resolver.py`, so a
press that reaches a handler below has already been vetted. The handlers ask again anyway,
and that is a second line rather than a leftover: the answer is one attribute read and the
cost of it being missing is a manager's screen shown to a bartender. :func:`take_answer`
asks because it *has* to — no resolver runs on a message at all — and :func:`_save` reads
the actor it hands the service (TZ 9, TZ 2).

**Nothing is written before the last step.** The draft lives in the FSM state until
:func:`_save` calls `RecipeService.create` once, so a cancelled form leaves the database as
it was; and the duplicate of decision D6 comes back as `RecipeExistsError`, which is a screen
offering the card that is already there rather than an apology from the error middleware.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message

from src.bot import texts
from src.bot.callbacks import (
    AdminAction,
    AdminCommand,
    AdminSection,
    CatalogueCategory,
    CataloguePage,
    OpenAdmin,
    RecipeCategory,
    RecipeDrop,
    RecipeEdit,
    RecipeManage,
)
from src.bot.handlers.admin import BOT_KEY, Event, deliver, manager_of, refuse
from src.bot.middlewares.menu import STATE_KEY
from src.bot.middlewares.resolver import PAYLOAD_KEY
from src.bot.middlewares.services import SERVICES_KEY
from src.bot.states import RecipeEditing, RecipeLookup, RecipeWizard
from src.bot.views import Screen
from src.bot.views import recipe_form as screens
from src.services.access import AccessContext
from src.services.recipes import (
    RecipeCard,
    RecipeDraft,
    RecipeExistsError,
    RecipeField,
    RecipeIncompleteError,
    RecipeNotFoundError,
    RecipeReadOnlyError,
    SearchPage,
)

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "admin_recipes"

#: Keys of `state.get_data()`, named after the fields of `RecipeDraft` they fill. Free text
#: never travels in a button (the 64-byte budget of `src/bot/callbacks.py`), so the whole
#: draft lives here until the one call that writes it.
NAME_FIELD: Final = "name"
CATEGORY_FIELD: Final = "category"
GLASSWARE_FIELD: Final = "glassware"
METHOD_FIELD: Final = "method"
ICE_FIELD: Final = "ice"
COMPOSITION_FIELD: Final = "composition"
GARNISH_FIELD: Final = "garnish"
INSTRUCTION_FIELD: Final = "instruction"

#: Keys of the lookup — what the listing is showing, so the pager can rebuild it. Named
#: apart from the draft's own keys above: the section and the form are two scenarios over
#: one storage, and a key they shared would be a category chosen in one arriving in the
#: other.
LOOKUP_QUERY: Final = "lookup_query"
LOOKUP_CATEGORY: Final = "lookup_category"

#: Keys of an edit in progress: a message carries neither the card nor the field, and the
#: button that started the step carried both.
EDITED_RECIPE: Final = "edited_recipe"
EDITED_FIELD: Final = "edited_field"

#: The categories the category step actually drew, kept beside the answers for the reason
#: `src/bot/handlers/admin_venue.py::ZONES_KEY` gives: an index is resolved against *the page
#: that was shown*, so a card added in another chat between the draw and the press cannot
#: turn a button into a different category.
OFFERED_KEY: Final = "offered_categories"


class Catalogue(Protocol):
    """What this module asks of `RecipeService`, and no more.

    Narrower than the service on purpose, like `Roster` in `src/bot/handlers/admin_staff.py`:
    a test of these screens would otherwise have to build a session to build two repositories
    to build the service. `tests/bot/test_admin_recipes.py` checks with mypy that the real
    `VenueServices` still satisfies this.
    """

    async def categories(self) -> tuple[str, ...]: ...

    async def create(self, actor: AccessContext, draft: RecipeDraft) -> RecipeCard: ...

    async def search(self, query: str, *, offset: int = ..., limit: int = ...) -> SearchPage: ...

    async def browse(self, category: str, *, offset: int = ..., limit: int = ...) -> SearchPage: ...

    async def card(self, recipe_id: int) -> RecipeCard | None: ...

    async def update(
        self,
        actor: AccessContext,
        recipe_id: int,
        field: RecipeField,
        value: str | None,
    ) -> RecipeCard: ...

    async def delete(self, actor: AccessContext, recipe_id: int) -> str: ...


class CatalogueServices(Protocol):
    """The one service of `data["services"]` these screens use."""

    @property
    def recipes(self) -> Catalogue: ...


@dataclass(frozen=True, slots=True)
class Step:
    """One question of the form: which state asks it, which field it fills, is it required.

    `is_required` is decision B5 in a column. TZ 5.5 draws a card whose glassware, method,
    ice, garnish and instruction are absent on most classic recipes, so those steps offer a
    skip; the name and the category are the two `RecipeService.create` refuses without, and
    a blank answer to either is asked again rather than carried forward into a refusal the
    manager would read eight steps later.
    """

    state: State
    field: str
    is_required: bool


#: The order of decision B5, and the only place it is written.
FORM: Final[tuple[Step, ...]] = (
    Step(RecipeWizard.name, NAME_FIELD, is_required=True),
    Step(RecipeWizard.category, CATEGORY_FIELD, is_required=True),
    Step(RecipeWizard.glassware, GLASSWARE_FIELD, is_required=False),
    Step(RecipeWizard.method, METHOD_FIELD, is_required=False),
    Step(RecipeWizard.ice, ICE_FIELD, is_required=False),
    Step(RecipeWizard.composition, COMPOSITION_FIELD, is_required=False),
    Step(RecipeWizard.garnish, GARNISH_FIELD, is_required=False),
    Step(RecipeWizard.instruction, INSTRUCTION_FIELD, is_required=False),
)

#: What `state.get_state()` answers -> the step it is. `None` is not a key of this map, so a
#: press arriving outside the form reads as "no step" rather than as the first one.
STEPS_BY_STATE: Final[Mapping[str | None, Step]] = {step.state.state: step for step in FORM}

#: The steps a skip button may be pressed on.
OPTIONAL_STATES: Final[tuple[State, ...]] = tuple(
    step.state for step in FORM if not step.is_required
)

#: One screen per step, except the category — that one needs the venue's own categories and
#: is drawn by :func:`_draw`.
STEP_SCREENS: Final[Mapping[str | None, Callable[[], Screen]]] = {
    RecipeWizard.name.state: screens.name_step,
    RecipeWizard.glassware.state: screens.glassware_step,
    RecipeWizard.method.state: screens.method_step,
    RecipeWizard.ice.state: screens.ice_step,
    RecipeWizard.composition.state: screens.composition_step,
    RecipeWizard.garnish.state: screens.garnish_step,
    RecipeWizard.instruction.state: screens.instruction_step,
}


# --------------------------------------------------------------------------------------
# The section (TZ 5.8, 8.1)
# --------------------------------------------------------------------------------------


async def open_catalogue(event: CallbackQuery, **data: Any) -> None:
    """The manager's half of the catalogue, empty state included (TZ 5.8, 8.1).

    The state is dropped first, for the reason `admin_staff.open_staff` gives: this screen is
    the section's board, and arriving at it from a form that was interrupted must not leave a
    half-filled draft waiting for the manager's next line. What replaces it is the lookup:
    from here a typed line is a search for a card, not an answer to a question.
    """
    services = _catalogue(data)
    if manager_of(data) is None or services is None:
        await refuse(event)
        return
    await _open_section(event, data, services)


# --------------------------------------------------------------------------------------
# The form (TZ 5.5, decision B5)
# --------------------------------------------------------------------------------------


async def start_form(event: CallbackQuery, **data: Any) -> None:
    """`CARD_ADD_BUTTON`: the draft starts empty and nothing is written until the last step.

    No role check here: `AdminCommand` is `minimum_role=MANAGER` in the resolver, so a
    bartender replaying this payload is refused before the form is opened (TZ 9).
    """
    state: FSMContext = data[STATE_KEY]
    await state.set_state(RecipeWizard.name)
    await state.set_data({})
    await deliver(event, screens.name_step(), bot=data[BOT_KEY])


async def take_answer(event: Message, **data: Any) -> None:
    """Any step of the form answered by typing.

    A blank answer to a required step re-asks it and remembers nothing; a blank answer to an
    optional one is the skip written out by hand, which is the same fact and is stored as the
    same absence (`RecipeService` turns a blank field into `None` in any case).

    The role is checked here and can be nowhere else: aiogram runs the resolver on callback
    queries only, so a form left open by somebody who has since been demoted is stopped by
    this line (TZ 9).
    """
    state: FSMContext = data[STATE_KEY]
    if manager_of(data) is None:
        await state.clear()
        await refuse(event)
        return
    step = STEPS_BY_STATE.get(await state.get_state())
    if step is None:
        # Registered by state, so the only way here is a state that went away underneath the
        # message. Nothing is stored, and the line is answered rather than swallowed (TZ 9).
        await event.answer(texts.ERROR_OUTDATED_SCREEN)
        return
    typed = (event.text or "").strip()
    if step.is_required and not typed:
        await _draw(event, data, step)
        return
    await _advance(event, data, step=step, value=typed or None)


async def choose_category(event: CallbackQuery, **data: Any) -> None:
    """A category the venue already uses, pressed instead of typed (principle 1.4#5).

    The index is resolved against the page held in the state, and an index that names nothing
    in it re-draws the step rather than saving a category nobody chose.
    """
    services = _catalogue(data)
    state: FSMContext = data[STATE_KEY]
    if manager_of(data) is None or services is None:
        # The same shape as the two steps below: a person who may not be here does not keep
        # a live scenario waiting for their next line either.
        await state.clear()
        await refuse(event)
        return
    payload = data[PAYLOAD_KEY]
    offered = await _offered(state, services)
    chosen = _category_at(offered, payload.index)
    if chosen is None:
        await _draw(event, data, FORM[1])
        return
    await _advance(event, data, step=FORM[1], value=chosen)


async def skip_step(event: CallbackQuery, **data: Any) -> None:
    """`INVITE_SKIP_BUTTON` on an optional step (TZ 5.5: most cards have none of them).

    Which step is skipped is the FSM state's answer, not the payload's: the scheme has one
    factory to carry this press and a step spelled into it would be a second answer to a
    question the state already answers.
    """
    state: FSMContext = data[STATE_KEY]
    if manager_of(data) is None:
        await state.clear()
        await refuse(event)
        return
    step = STEPS_BY_STATE.get(await state.get_state())
    if step is None:
        await stale_step(event)
        return
    await _advance(event, data, step=step, value=None)


# --------------------------------------------------------------------------------------
# Correcting a card that already exists (TZ 5.8)
# --------------------------------------------------------------------------------------


async def find_card(event: Message, **data: Any) -> None:
    """A line typed into the section is a search for a card to correct (TZ 5.8).

    The role is checked here and can be nowhere else, for the reason :func:`take_answer`
    gives: no resolver runs on a message, so a section left open by somebody who has since
    been demoted is stopped by this line (TZ 9).
    """
    services = _catalogue(data)
    state: FSMContext = data[STATE_KEY]
    if manager_of(data) is None or services is None:
        await state.clear()
        await refuse(event)
        return
    await state.set_data({LOOKUP_QUERY: (event.text or "").strip()})
    await _draw_listing(event, data, services, offset=0)


async def browse_category(event: CallbackQuery, **data: Any) -> None:
    """One of the categories the section offered, opened as a list of its cards.

    The index is resolved against the page held in the state, exactly as in the form's own
    category step: a card added in another chat between the draw and the press must not turn
    a button into a different category.

    The press is a `CatalogueCategory` and not the form's `RecipeCategory`, which is what
    keeps a section screen left further up the chat from answering the form's question
    instead of browsing (see that class's docstring).
    """
    services = _catalogue(data)
    state: FSMContext = data[STATE_KEY]
    if manager_of(data) is None or services is None:
        await state.clear()
        await refuse(event)
        return
    chosen = _category_at(await _offered(state, services), data[PAYLOAD_KEY].index)
    if chosen is None:
        await _open_section(event, data, services)
        return
    await state.update_data({LOOKUP_CATEGORY: chosen, LOOKUP_QUERY: ""})
    await _draw_listing(event, data, services, offset=0)


async def turn_page(event: CallbackQuery, **data: Any) -> None:
    """The pager of the listing: the same lookup, a different window (TZ 5.5)."""
    services = _catalogue(data)
    if manager_of(data) is None or services is None:
        await refuse(event)
        return
    await _draw_listing(event, data, services, offset=max(data[PAYLOAD_KEY].offset, 0))


async def open_card(event: CallbackQuery, **data: Any) -> None:
    """One card, with a button per field and the deletion (TZ 5.8).

    The resolver has already proved the row is this venue's own and that the presser may
    manage it; the service is asked again because a card is the row *and* its composition,
    and assembling that is not a handler's job.
    """
    services = _catalogue(data)
    state: FSMContext = data[STATE_KEY]
    if manager_of(data) is None or services is None:
        await refuse(event)
        return
    await state.set_state(RecipeLookup.query)
    await _draw_card(event, data, services, data[PAYLOAD_KEY].recipe_id)


async def start_edit(event: CallbackQuery, **data: Any) -> None:
    """A field of the open card: asked for, or — with `is_clear` — taken off (TZ 5.8).

    Which card and which field are remembered in the state: the answer arrives as a message,
    and a message carries neither. The clearing half needs no state at all, because the
    press already says everything: this card, this field, empty.
    """
    services = _catalogue(data)
    actor = manager_of(data)
    state: FSMContext = data[STATE_KEY]
    if actor is None or services is None:
        await refuse(event)
        return
    payload = data[PAYLOAD_KEY]
    if payload.is_clear:
        await _write(event, data, services, actor, payload.recipe_id, payload.field, None)
        return
    card = await services.recipes.card(payload.recipe_id)
    if card is None:
        await _gone(event, data, services)
        return
    await state.set_state(RecipeEditing.value)
    await state.set_data({EDITED_RECIPE: card.recipe_id, EDITED_FIELD: str(payload.field)})
    await deliver(event, screens.field_step(card, payload.field), bot=data[BOT_KEY])


async def take_value(event: Message, **data: Any) -> None:
    """The new value of one field, as it was typed (TZ 5.8).

    A message with no text in it — a photo, a sticker, a voice note held down by mistake
    behind the bar — is **not an answer and least of all an empty one**. `event.text` is
    `None` for all of those, and passing that on would clear the field and report success:
    the manager loses a garnish they never meant to touch. Taking a field away is the button
    on this screen (`RecipeEdit(is_clear=True)`), so anything unreadable re-asks the step.
    """
    services = _catalogue(data)
    actor = manager_of(data)
    state: FSMContext = data[STATE_KEY]
    if actor is None or services is None:
        await state.clear()
        await refuse(event)
        return
    collected = await state.get_data()
    recipe_id = collected.get(EDITED_RECIPE)
    field = _field_of(collected.get(EDITED_FIELD))
    if not isinstance(recipe_id, int) or field is None:
        # The state went away underneath the message (a TTL, a deploy). Nothing is written,
        # and the line is answered rather than swallowed (TZ 9).
        await state.clear()
        await event.answer(texts.ERROR_OUTDATED_SCREEN)
        return

    if event.text is None:
        await _ask_again(event, data, services, recipe_id=recipe_id, field=field)
        return

    await _write(event, data, services, actor, recipe_id, field, event.text)


async def drop_card(event: CallbackQuery, **data: Any) -> None:
    """The deletion: the question, and — on the second press — the deletion itself.

    One handler for both because they are one decision, and the payload says which half of
    it this press is. Nothing in the schema keeps a removed card, so the question is not
    optional (TZ 8.2).
    """
    services = _catalogue(data)
    actor = manager_of(data)
    state: FSMContext = data[STATE_KEY]
    if actor is None or services is None:
        await refuse(event)
        return
    payload = data[PAYLOAD_KEY]
    if not payload.is_confirmed:
        card = await services.recipes.card(payload.recipe_id)
        if card is None:
            await _gone(event, data, services)
            return
        await deliver(event, screens.delete_question(card), bot=data[BOT_KEY])
        return

    await state.set_state(RecipeLookup.query)
    try:
        name = await services.recipes.delete(actor, payload.recipe_id)
    except RecipeReadOnlyError:
        await _refuse_with(event, data, texts.CARD_READ_ONLY)
        return
    except RecipeNotFoundError:
        await _gone(event, data, services)
        return
    categories = await services.recipes.categories()
    await state.update_data({OFFERED_KEY: list(categories), LOOKUP_CATEGORY: "", LOOKUP_QUERY: ""})
    await deliver(event, screens.deleted(name, categories), bot=data[BOT_KEY])


async def stale_step(event: CallbackQuery, **data: Any) -> None:
    """A form button pressed in a state it does not belong to (TZ 9: always answer).

    The screens of this form are edited in place, so the only way here is a message that was
    superseded — a copy of a step left open while something else was started. Nothing is
    saved, and the spinner is closed with the truth instead of hanging for a minute.
    """
    await event.answer(texts.ERROR_OUTDATED_SCREEN, show_alert=True)


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------


def _catalogue(data: dict[str, Any]) -> CatalogueServices | None:
    """The venue's services, or `None` for an update with no venue behind it (TZ 5.1).

    Not hypothetical: the gate lets through a person with no membership yet, and nothing
    venue-scoped exists for them (`src/bot/middlewares/venue.py`).
    """
    services: CatalogueServices | None = data.get(SERVICES_KEY)
    return services


def _field_of(remembered: object) -> RecipeField | None:
    """The field an interrupted step was about, or `None` when the state has lost it.

    Anything that is not one of the eight reads as absent — a value left in Redis by a
    version of this module that named its fields differently is a state to restart from, not
    a column to write into.
    """
    if not isinstance(remembered, str):
        return None
    try:
        return RecipeField(remembered)
    except ValueError:
        return None


async def _open_section(event: Event, data: dict[str, Any], services: CatalogueServices) -> None:
    """The section, on a clean lookup: the previous search is not this one's."""
    state: FSMContext = data[STATE_KEY]
    categories = await services.recipes.categories()
    await state.set_state(RecipeLookup.query)
    await state.set_data({OFFERED_KEY: list(categories)})
    await deliver(event, screens.section(categories), bot=data[BOT_KEY])


async def _draw_listing(
    event: Event,
    data: dict[str, Any],
    services: CatalogueServices,
    *,
    offset: int,
) -> None:
    """Re-run the remembered lookup at one window and put the result on the screen.

    The page is rebuilt from the state rather than held in it, for the reason
    `src/bot/handlers/recipes.py` gives: a page kept in the state stops matching the database
    the moment a card is edited, and this is the very screen that edits them.
    """
    state: FSMContext = data[STATE_KEY]
    collected = await state.get_data()
    category = _remembered(collected, LOOKUP_CATEGORY)
    if category:
        page = await services.recipes.browse(category, offset=offset)
    else:
        page = await services.recipes.search(_remembered(collected, LOOKUP_QUERY), offset=offset)
    await state.set_state(RecipeLookup.query)
    await deliver(event, screens.listing(page), bot=data[BOT_KEY])


async def _draw_card(
    event: Event,
    data: dict[str, Any],
    services: CatalogueServices,
    recipe_id: int,
    *,
    note: str = "",
) -> None:
    """The managed card, or the screen for a card that is no longer there."""
    card = await services.recipes.card(recipe_id)
    if card is None:
        await _gone(event, data, services)
        return
    await deliver(event, screens.managed(card, note=note), bot=data[BOT_KEY])


async def _write(
    event: Event,
    data: dict[str, Any],
    services: CatalogueServices,
    actor: AccessContext,
    recipe_id: int,
    field: RecipeField,
    value: str | None,
) -> None:
    """The one write of the editor, and the four refusals it can come back with (TZ 5.8).

    Shared by the typed answer and by the clear button, so both end on the same screen and
    both are refused the same way — two copies of this would be two sets of branches, and
    the second would be the one that forgets `RecipeExistsError`.
    """
    state: FSMContext = data[STATE_KEY]
    await state.set_state(RecipeLookup.query)
    try:
        card = await services.recipes.update(actor, recipe_id, field, value)
    except RecipeExistsError as clash:
        await deliver(
            event,
            screens.exists(name=clash.name, category=clash.category, recipe_id=clash.recipe_id),
            bot=data[BOT_KEY],
        )
        return
    except RecipeIncompleteError:
        # The name or the category, blanked. Decision D6 keeps both, so the step is asked
        # again instead of being answered with a refusal (TZ 8.1: no dead end).
        await _ask_again(event, data, services, recipe_id=recipe_id, field=field)
        return
    except RecipeReadOnlyError:
        await _refuse_with(event, data, texts.CARD_READ_ONLY)
        return
    except RecipeNotFoundError:
        await _gone(event, data, services)
        return
    await deliver(event, screens.managed(card, note=texts.CARD_UPDATED), bot=data[BOT_KEY])


async def _ask_again(
    event: Event,
    data: dict[str, Any],
    services: CatalogueServices,
    *,
    recipe_id: int,
    field: RecipeField,
) -> None:
    """The same field, asked once more — a blank answer to one that cannot be blank."""
    state: FSMContext = data[STATE_KEY]
    card = await services.recipes.card(recipe_id)
    if card is None:
        await _gone(event, data, services)
        return
    await state.set_state(RecipeEditing.value)
    await state.update_data({EDITED_RECIPE: recipe_id, EDITED_FIELD: str(field)})
    await deliver(event, screens.field_step(card, field), bot=data[BOT_KEY])


async def _gone(event: Event, data: dict[str, Any], services: CatalogueServices) -> None:
    """The card went away between the screen being drawn and the button being pressed."""
    await _refuse_with(event, data, texts.ERROR_GONE)
    await _open_section(event, data, services)


async def _refuse_with(event: Event, data: dict[str, Any], text: str) -> None:
    """Say why, in the one place a press can be answered without redrawing anything."""
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
        return
    await event.answer(text)


def _next(step: Step) -> Step | None:
    """The step after this one, or `None` when the form is over."""
    following = FORM.index(step) + 1
    return FORM[following] if following < len(FORM) else None


def _category_at(offered: Sequence[str], index: int | None) -> str | None:
    """The category a press named, or `None` when it named none of the offered ones."""
    if index is None or not 0 <= index < len(offered):
        return None
    return offered[index]


async def _offered(state: FSMContext, services: CatalogueServices) -> tuple[str, ...]:
    """The categories the step drew, or the current ones when the state has lost them.

    The fallback is not a guess about *which* category was pressed — `_category_at` may well
    answer `None` against a different list, and then the step is drawn again. It is here so
    that a state expired past its TTL re-asks rather than raises.
    """
    remembered = (await state.get_data()).get(OFFERED_KEY)
    if isinstance(remembered, list) and remembered:
        return tuple(str(name) for name in remembered)
    return await services.recipes.categories()


async def _draw(event: Event, data: dict[str, Any], step: Step) -> None:
    """Put one step of the form on the screen (TZ 8.2: a press edits, a line typed sends)."""
    state: FSMContext = data[STATE_KEY]
    if step.state is RecipeWizard.category:
        services = _catalogue(data)
        if services is None:
            await refuse(event)
            return
        offered = await services.recipes.categories()
        await state.update_data({OFFERED_KEY: list(offered)})
        await deliver(event, screens.category_step(offered), bot=data[BOT_KEY])
        return
    await deliver(event, STEP_SCREENS[step.state.state](), bot=data[BOT_KEY])


async def _advance(event: Event, data: dict[str, Any], *, step: Step, value: str | None) -> None:
    """Remember what this step answered and ask the next one, or save (decision B5)."""
    state: FSMContext = data[STATE_KEY]
    if value is not None:
        await state.update_data({step.field: value})
    following = _next(step)
    if following is None:
        await _save(event, data)
        return
    await state.set_state(following.state)
    await _draw(event, data, following)


async def _save(event: Event, data: dict[str, Any]) -> None:
    """The one write of this module (TZ 5.8, decision D6).

    The state is cleared before the service is called and not after, for the reason
    `admin_staff._issue` gives: the form is over the moment the card exists, and a failure
    while rendering must not leave the manager in a step that would write a second one.

    A draft that has lost its required fields — a state expired past its TTL, a deploy in the
    middle of the form — restarts at the first step instead of reaching a service that would
    refuse it with a message about a field the manager never saw.
    """
    actor = manager_of(data)
    services = _catalogue(data)
    state: FSMContext = data[STATE_KEY]
    if actor is None or services is None:
        await state.clear()
        await refuse(event)
        return

    collected = await state.get_data()
    name = _remembered(collected, NAME_FIELD)
    category = _remembered(collected, CATEGORY_FIELD)
    if not (name and category):
        await state.set_state(RecipeWizard.name)
        await deliver(event, screens.name_step(), bot=data[BOT_KEY])
        return

    draft = _draft(collected, name=name, category=category)
    await state.clear()
    try:
        card = await services.recipes.create(actor, draft)
    except RecipeExistsError as clash:
        # Decision D6: the venue already has this name in this category. A screen, and the
        # card that is in the way — not a traceback for the error middleware to apologise for.
        await deliver(
            event,
            screens.exists(name=clash.name, category=clash.category, recipe_id=clash.recipe_id),
            bot=data[BOT_KEY],
        )
        return
    await deliver(event, screens.saved(card), bot=data[BOT_KEY])


def _draft(collected: dict[str, Any], *, name: str, category: str) -> RecipeDraft:
    """The answers of the form as the service takes them.

    The composition is handed over as the manager wrote it: `RecipeService` splits the lines
    and reads each amount (decision D4), and a handler that pre-split them would be the
    second parser TZ 3.2 exists to prevent.
    """
    composition = _remembered(collected, COMPOSITION_FIELD)
    return RecipeDraft(
        name=name,
        category=category,
        glassware=_remembered(collected, GLASSWARE_FIELD) or None,
        method=_remembered(collected, METHOD_FIELD) or None,
        ice=_remembered(collected, ICE_FIELD) or None,
        garnish=_remembered(collected, GARNISH_FIELD) or None,
        instruction=_remembered(collected, INSTRUCTION_FIELD) or None,
        ingredients=(composition,) if composition else (),
    )


def _remembered(collected: dict[str, Any], key: str) -> str:
    """One answer of the form, as a string. Anything else in the state reads as absent."""
    value = collected.get(key)
    return value.strip() if isinstance(value, str) else ""


def router() -> Router:
    """The screens of this block (see the module docstring).

    Order matters in one place: the two state-filtered handlers sit before the catch-alls,
    so a press made inside the step it belongs to never reaches the apology meant for a
    press made outside it.

    `CARD_ADD_BUTTON` is registered without a state filter on purpose: it means "start a
    card" from the section and equally from a form left half-filled, and the first thing it
    does is reset the draft.
    """
    instance = Router(name=ROUTER_NAME)
    instance.callback_query.register(
        open_catalogue,
        OpenAdmin.filter(F.section == AdminSection.CATALOGUE),
    )
    instance.callback_query.register(
        start_form,
        AdminCommand.filter(F.action == AdminAction.RECIPE_NEW),
    )
    instance.callback_query.register(
        choose_category,
        RecipeCategory.filter(),
        StateFilter(RecipeWizard.category),
    )
    instance.callback_query.register(
        skip_step,
        AdminCommand.filter(F.action == AdminAction.STEP_SKIP),
        StateFilter(*OPTIONAL_STATES),
    )
    # The same two presses arriving outside the step they belong to: a button from a screen
    # the scenario has already left. Two registrations because they are two factories.
    instance.callback_query.register(
        stale_step,
        AdminCommand.filter(F.action == AdminAction.STEP_SKIP),
    )
    instance.callback_query.register(stale_step, RecipeCategory.filter())
    # The two presses of the *section* — browse a category, turn the page of a listing. Both
    # are state-filtered and both have the same apology behind them, and that pair is a rule
    # rather than symmetry: each rebuilds its screen out of what the FSM state remembers
    # (`LOOKUP_QUERY`, `LOOKUP_CATEGORY`, `OFFERED_KEY`), and the wizard and the field editor
    # both *replace* that data. Without the filter a pager button left on an older message
    # would page a lookup that is no longer there — and drag the manager out of the form they
    # are in the middle of, since drawing a listing sets the state back.
    instance.callback_query.register(
        browse_category,
        CatalogueCategory.filter(),
        StateFilter(RecipeLookup.query),
    )
    instance.callback_query.register(
        turn_page,
        CataloguePage.filter(),
        StateFilter(RecipeLookup.query),
    )
    instance.callback_query.register(stale_step, CatalogueCategory.filter())
    instance.callback_query.register(stale_step, CataloguePage.filter())
    # Correcting a card (TZ 5.8). None of these is state-filtered: a card is opened from a
    # listing, from a screen left open an hour ago and from the confirmation of an edit, and
    # each of those is a legitimate press. What they are *not* allowed to do is reach another
    # venue's row, and that is the resolver's rule rather than a state.
    instance.callback_query.register(open_card, RecipeManage.filter())
    instance.callback_query.register(start_edit, RecipeEdit.filter())
    instance.callback_query.register(drop_card, RecipeDrop.filter())
    instance.message.register(take_answer, StateFilter(*(step.state for step in FORM)))
    instance.message.register(take_value, StateFilter(RecipeEditing.value))
    instance.message.register(find_card, StateFilter(RecipeLookup.query), F.text)
    return instance


__all__ = [
    "CATEGORY_FIELD",
    "COMPOSITION_FIELD",
    "EDITED_FIELD",
    "EDITED_RECIPE",
    "FORM",
    "GARNISH_FIELD",
    "GLASSWARE_FIELD",
    "ICE_FIELD",
    "INSTRUCTION_FIELD",
    "LOOKUP_CATEGORY",
    "LOOKUP_QUERY",
    "METHOD_FIELD",
    "NAME_FIELD",
    "OFFERED_KEY",
    "OPTIONAL_STATES",
    "ROUTER_NAME",
    "STEPS_BY_STATE",
    "STEP_SCREENS",
    "Catalogue",
    "CatalogueServices",
    "Step",
    "browse_category",
    "choose_category",
    "drop_card",
    "find_card",
    "open_card",
    "open_catalogue",
    "router",
    "skip_step",
    "stale_step",
    "start_edit",
    "start_form",
    "take_answer",
    "take_value",
    "turn_page",
]
