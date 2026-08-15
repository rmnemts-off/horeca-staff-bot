"""«📖 Рецептуры»: the manager's section and the form behind it (TZ 5.5, 5.8; task 30a).

Three kinds of test, and the split is the one `tests/bot/test_admin_staff.py` uses.

* **The screens are values.** `src/bot/views/recipe_form.py` is a pure function of what the
  service returned, so the empty state of TZ 8.1 is an ordinary assert — including the
  property that matters most about it, that every button on an empty screen carries a
  payload the resolver has a rule for and therefore goes somewhere real.
* **The form is walked through the real router tree.** `handlers.all_routers()` is included
  rather than this one module, and the real callback resolver sits on the dispatcher,
  because the three presses of this block ride on
  :class:`~src.bot.callbacks.TimezoneChoice` — a factory `src/bot/handlers/admin_venue.py`
  registers on first and answers for every index of its own. That the venue block does not
  swallow them is not a fact about this module's code; it is a fact about the tree, and the
  tree is what is exercised here.
* **The rights are checked where they are decided.** `OpenAdmin` is refused for `staff` by
  the resolver (TZ 9), and the buttons that ride on `TimezoneChoice` reach a handler with no
  check at all — so what is asserted for those is that the *handler* refuses them and writes
  nothing, which is the check that cannot be dropped.

`RecipeService` is faked at its own two signatures. The fake refuses a second card with the
key of decision D6 exactly as the real one does, because "a duplicate is a screen and not a
traceback" is a claim about a code path that only exists if the service can actually raise.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, Final

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import InlineKeyboardMarkup, Update
from src.bot import handlers, texts
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
    RecipeShow,
    parse,
)
from src.bot.handlers.admin_recipes import (
    FORM,
    OPTIONAL_STATES,
    Catalogue,
    CatalogueServices,
)
from src.bot.keyboards import recipe_form as keyboards
from src.bot.middlewares.auth import ACTOR_KEY
from src.bot.middlewares.resolver import RULES, CallbackResolverMiddleware, Refusal, resolve
from src.bot.middlewares.services import SERVICES_KEY, VenueServices
from src.bot.states import RecipeEditing, RecipeLookup, RecipeWizard
from src.bot.views import recipe_form as views
from src.services.access import AccessContext
from src.services.recipes import (
    MAX_SEARCH_RESULTS,
    RecipeCard,
    RecipeDraft,
    RecipeExistsError,
    RecipeField,
    RecipeHit,
    RecipeIncompleteError,
    RecipeNotFoundError,
    RecipeReadOnlyError,
    RecipeService,
    SearchPage,
    parse_ingredients,
)

from tests.bot.test_middlewares import (
    CHAT_ID,
    MANAGER,
    STAFF,
    STAFF_TELEGRAM_ID,
    VENUE_ID,
    FakeSubjects,
    make_bot,
    make_callback,
    make_message,
    make_recipe,
    session_of,
)
from tests.bot.test_views_html import HOSTILE, assert_telegram_html

#: What a manager types. Data of a test, never of the delivery (principle 1.4#6).
DRINK: Final = "Mojito"
GROUP: Final = "cocktails"
OTHER_GROUP: Final = "coffee"
COMPOSITION: Final = "White rum — 50 ml\nLime — 20 ml"

RECIPE_ID: Final = 77
EXISTING_ID: Final = 12


def make_card(
    *,
    recipe_id: int = RECIPE_ID,
    name: str = DRINK,
    category: str = GROUP,
    draft: RecipeDraft | None = None,
) -> RecipeCard:
    """A card as `RecipeService.create` returns one: the row plus its composition."""
    source = draft if draft is not None else RecipeDraft(name=name, category=category)
    return RecipeCard(
        recipe_id=recipe_id,
        name=name,
        category=category,
        is_library=False,
        glassware=source.glassware,
        method=source.method,
        ice=source.ice,
        garnish=source.garnish,
        instruction=source.instruction,
        season_date=None,
        yield_variants=None,
        photo_file_id=None,
        ingredients=parse_ingredients("\n".join(source.ingredients)),
    )


class FakeRecipes:
    """`RecipeService` as this module uses it, with every call written down.

    The duplicate of decision D6 is reproduced rather than stubbed out: the table is keyed
    by `(folded category, folded name)`, which is the key `RecipeService._twin` compares by,
    so `test_a_duplicate_is_a_screen_and_not_a_traceback` exercises the branch that exists
    in production instead of an exception this fake invented.
    """

    def __init__(self, *categories: str) -> None:
        self.groups = list(categories)
        self.rows: dict[tuple[str, str], int] = {}
        self.drafts: list[RecipeDraft] = []
        self.actors: list[AccessContext] = []
        #: The cards themselves, for the half of the section that reads them back.
        self.cards: dict[int, RecipeCard] = {}
        self.deleted: list[int] = []
        self.edits: list[tuple[int, RecipeField, str | None]] = []

    def already_holds(self, *, name: str, category: str, recipe_id: int) -> None:
        self.rows[self._key(name, category)] = recipe_id
        if category not in self.groups:
            self.groups.append(category)

    def holds_card(self, card: RecipeCard) -> RecipeCard:
        """A card that already exists, the way the section finds one to correct."""
        self.cards[card.recipe_id] = card
        self.rows[self._key(card.name, card.category)] = card.recipe_id
        if card.category not in self.groups:
            self.groups.append(card.category)
        return card

    # -- the reads the manager's half of the section makes ------------------------------

    async def search(
        self,
        query: str,
        *,
        offset: int = 0,
        limit: int = MAX_SEARCH_RESULTS,
    ) -> SearchPage:
        cleaned = " ".join(query.split())
        if not cleaned:
            return SearchPage(query="", hits=(), offset=offset, limit=limit, has_next=False)
        found = [card for card in self.cards.values() if cleaned.casefold() in card.name.casefold()]
        return self._page(cleaned, found, offset=offset, limit=limit)

    async def browse(
        self,
        category: str,
        *,
        offset: int = 0,
        limit: int = MAX_SEARCH_RESULTS,
    ) -> SearchPage:
        cleaned = " ".join(category.split())
        found = [
            card for card in self.cards.values() if card.category.casefold() == cleaned.casefold()
        ]
        return self._page(cleaned, found, offset=offset, limit=limit)

    async def card(self, recipe_id: int) -> RecipeCard | None:
        return self.cards.get(recipe_id)

    # -- and the two writes -------------------------------------------------------------

    async def update(
        self,
        actor: AccessContext,
        recipe_id: int,
        field: RecipeField,
        value: str | None,
    ) -> RecipeCard:
        """`RecipeService.update`, with every refusal the real one has (CLAUDE.md).

        A fake that promised more than the service does would make the screens below green
        about branches that do not exist — the duplicate of decision D6 above is the same
        arrangement and the reason for it.
        """
        assert actor.is_manager, "the real service refuses anybody below manager (TZ 5.8)"
        card = self.cards.get(recipe_id)
        if card is None:
            raise RecipeNotFoundError(recipe_id)
        if card.is_library:
            raise RecipeReadOnlyError(recipe_id)
        cleaned = (value or "").strip() or None
        if field in {RecipeField.NAME, RecipeField.CATEGORY} and cleaned is None:
            raise RecipeIncompleteError(str(field))
        name = cleaned if field is RecipeField.NAME else card.name
        category = cleaned if field is RecipeField.CATEGORY else card.category
        assert name is not None and category is not None
        taken = self.rows.get(self._key(name, category))
        if taken is not None and taken != recipe_id:
            raise RecipeExistsError(name=name, category=category, recipe_id=taken)

        self.edits.append((recipe_id, field, value))
        self.rows.pop(self._key(card.name, card.category), None)
        written = replace(card, name=name, category=category, **self._written(field, cleaned))
        return self.holds_card(written)

    async def delete(self, actor: AccessContext, recipe_id: int) -> str:
        assert actor.is_manager, "the real service refuses anybody below manager (TZ 5.8)"
        card = self.cards.get(recipe_id)
        if card is None:
            raise RecipeNotFoundError(recipe_id)
        if card.is_library:
            raise RecipeReadOnlyError(recipe_id)
        self.deleted.append(recipe_id)
        del self.cards[recipe_id]
        self.rows.pop(self._key(card.name, card.category), None)
        self.groups = [
            group
            for group in self.groups
            if any(row.category == group for row in self.cards.values())
        ]
        return card.name

    @staticmethod
    def _written(field: RecipeField, cleaned: str | None) -> dict[str, Any]:
        """The one column an edit touches, as `dataclasses.replace` takes it."""
        columns = {
            RecipeField.GLASSWARE: "glassware",
            RecipeField.METHOD: "method",
            RecipeField.ICE: "ice",
            RecipeField.GARNISH: "garnish",
            RecipeField.INSTRUCTION: "instruction",
        }
        column = columns.get(field)
        if column is None:
            return (
                {"ingredients": parse_ingredients(cleaned or "")}
                if field is (RecipeField.COMPOSITION)
                else {}
            )
        return {column: cleaned}

    @staticmethod
    def _page(
        query: str,
        found: Sequence[RecipeCard],
        *,
        offset: int,
        limit: int,
    ) -> SearchPage:
        window = min(max(limit, 1), MAX_SEARCH_RESULTS)
        visible = found[offset : offset + window]
        return SearchPage(
            query=query,
            hits=tuple(
                RecipeHit(
                    recipe_id=card.recipe_id,
                    name=card.name,
                    category=card.category,
                    is_library=card.is_library,
                )
                for card in visible
            ),
            offset=offset,
            limit=window,
            has_next=len(found) > offset + window,
        )

    async def categories(self) -> tuple[str, ...]:
        return tuple(self.groups)

    async def create(self, actor: AccessContext, draft: RecipeDraft) -> RecipeCard:
        assert actor.is_manager, "the real service refuses anybody below manager (TZ 5.8)"
        key = self._key(draft.name, draft.category)
        taken = self.rows.get(key)
        if taken is not None:
            raise RecipeExistsError(
                name=draft.name.strip(),
                category=draft.category.strip(),
                recipe_id=taken,
            )
        self.actors.append(actor)
        self.drafts.append(draft)
        self.rows[key] = RECIPE_ID
        if draft.category not in self.groups:
            self.groups.append(draft.category)
        return make_card(name=draft.name.strip(), category=draft.category.strip(), draft=draft)

    @staticmethod
    def _key(name: str, category: str) -> tuple[str, str]:
        return (category.strip().casefold(), name.strip().casefold())


class FakeCatalogue:
    """`data["services"]`, narrowed to what these screens read.

    `repositories` is here because the resolver reads it off the bundle before any handler
    runs; nothing in this module may touch it (`tests/bot/test_handler_boundary.py`).
    """

    def __init__(self, recipes: FakeRecipes) -> None:
        self.recipes = recipes
        self.repositories = FakeSubjects(VENUE_ID)


def _the_real_services_satisfy_the_protocols(
    services: VenueServices,
    recipes: RecipeService,
) -> tuple[CatalogueServices, Catalogue]:
    """mypy is the assertion here, and it is the point of the protocols.

    The handlers are typed against `CatalogueServices` and `Catalogue` so that a fake can be
    built without a session; that only stays honest while the real services still satisfy
    them. Nothing calls this — it fails at type-check time or not at all.
    """
    return services, recipes


# --------------------------------------------------------------------------------------
# The stand: the real router tree, the real resolver, a fake service
# --------------------------------------------------------------------------------------


class Stand:
    """Every router of the bot, in production order, over one fake catalogue.

    The whole tree and not this module alone: the presses of this block travel on a factory
    the venue block registers on first, so "the payload reaches this handler" is only true
    of the tree that actually runs.
    """

    def __init__(
        self,
        *,
        actor: AccessContext | None = MANAGER,
        recipes: FakeRecipes | None = None,
    ) -> None:
        self.bot: Bot = make_bot()
        storage = MemoryStorage()
        self.dispatcher = Dispatcher(storage=storage)
        self.dispatcher.callback_query.outer_middleware(CallbackResolverMiddleware())
        for router in handlers.all_routers():
            self.dispatcher.include_router(router)
        self.recipes = recipes if recipes is not None else FakeRecipes()
        self.services = FakeCatalogue(self.recipes)
        self.context: dict[str, Any] = {
            ACTOR_KEY: actor,
            SERVICES_KEY: self.services,
        }
        self.state = FSMContext(
            storage=storage,
            key=StorageKey(bot_id=self.bot.id, chat_id=CHAT_ID, user_id=STAFF_TELEGRAM_ID),
        )

    async def types(self, text: str) -> None:
        await self.dispatcher.feed_update(
            self.bot,
            Update(update_id=1, message=make_message(text)),
            **self.context,
        )

    async def presses(self, payload: str) -> None:
        await self.dispatcher.feed_update(
            self.bot,
            Update(update_id=1, callback_query=make_callback(payload)),
            **self.context,
        )

    def knows(self, card: RecipeCard) -> RecipeCard:
        """A card the venue already has — in the service *and* in the resolver's table.

        Both halves are needed and they are different checks: the service is what the screens
        read, and the row is what `src/bot/middlewares/resolver.py` fetches to prove the card
        is this venue's own before a handler runs (TZ 9).
        """
        self.recipes.holds_card(card)
        self.services.repositories.tables.recipes.append(
            make_recipe(card.recipe_id, venue_id=None if card.is_library else VENUE_ID)
        )
        return card

    async def sends_a_photo(self) -> None:
        """A message with no text in it — what a held mic button behind the bar produces."""
        message = make_message("caption")
        await self.dispatcher.feed_update(
            self.bot,
            Update(update_id=1, message=message.model_copy(update={"text": None})),
            **self.context,
        )

    async def presses_caption(self, caption: str) -> None:
        """Press the button of the last screen that reads `caption` — a press, not a poke."""
        markup = self.keyboard()
        found = [
            button.callback_data or ""
            for row in markup.inline_keyboard
            for button in row
            if button.text == caption
        ]
        assert found, f"no button reads {caption!r} on the screen that is up"
        await self.presses(found[0])

    # -- reading the bot back -----------------------------------------------------------

    def alerts(self) -> list[str]:
        return [
            str(call.text)
            for call in session_of(self.bot).calls
            if isinstance(call, AnswerCallbackQuery) and call.text
        ]

    def _last(self) -> SendMessage | EditMessageText:
        shown = [
            call
            for call in session_of(self.bot).calls
            if isinstance(call, SendMessage | EditMessageText)
        ]
        assert shown, "nothing was shown at all"
        return shown[-1]

    def screen(self) -> str:
        return str(self._last().text)

    def keyboard(self) -> InlineKeyboardMarkup:
        markup = self._last().reply_markup
        assert isinstance(markup, InlineKeyboardMarkup)
        return markup

    def payloads(self) -> list[str]:
        return payloads_of(self.keyboard())

    def captions(self) -> list[str]:
        return captions_of(self.keyboard())


def captions_of(markup: InlineKeyboardMarkup | None) -> list[str]:
    assert markup is not None
    return [button.text for row in markup.inline_keyboard for button in row]


def payloads_of(markup: InlineKeyboardMarkup | None) -> list[str]:
    assert markup is not None
    return [button.callback_data or "" for row in markup.inline_keyboard for button in row]


async def walk_to_composition(stand: Stand, *, category: str = GROUP) -> None:
    """«Добавить» → название → категория (typed) → три пропуска, up to the composition."""
    await stand.presses(AdminCommand(action=AdminAction.RECIPE_NEW).pack())
    await stand.types(DRINK)
    await stand.types(category)
    for _ in range(3):
        await stand.presses_caption(texts.INVITE_SKIP_BUTTON)


# --------------------------------------------------------------------------------------
# The empty state (TZ 8.1) — what every venue sees in this section on day one
# --------------------------------------------------------------------------------------


def test_the_empty_section_says_so_and_offers_the_button_that_ends_it() -> None:
    screen = views.section(())
    assert texts.CARD_EDITOR_EMPTY in screen.text
    assert texts.CARD_EDITOR_TITLE in screen.text
    assert texts.CARD_ADD_BUTTON in captions_of(screen.markup)


def test_no_button_of_the_empty_section_leads_nowhere() -> None:
    """TZ 8.1: «ни одной кнопки в никуда» — every payload parses and has a rule.

    A rule in the resolver is what makes a payload reachable at all: a factory without one
    is refused before any handler sees it, so a button carrying it is decoration.
    """
    for payload in payloads_of(views.section(()).markup):
        assert type(parse(payload)) in RULES, payload


def test_the_section_shows_what_the_venue_has_entered() -> None:
    screen = views.section((GROUP, OTHER_GROUP))
    assert GROUP in screen.text
    assert OTHER_GROUP in screen.text
    assert texts.CARD_EDITOR_EMPTY not in screen.text


def test_the_category_step_of_a_venue_with_nothing_is_a_question_and_no_dead_button() -> None:
    """The empty state inside the form: there is no category until a first card names one,
    so the step asks for the word instead of drawing a button that offers nothing."""
    screen = views.category_step(())
    assert texts.CARD_GROUP_PROMPT in screen.text
    for payload in payloads_of(screen.markup):
        assert type(parse(payload)) in RULES, payload


async def test_the_add_button_of_the_empty_section_actually_opens_the_form() -> None:
    """An empty state is only done if its one button works — pressed, and not read."""
    stand = Stand()
    await stand.presses(OpenAdmin(section=AdminSection.CATALOGUE).pack())
    assert texts.CARD_EDITOR_EMPTY in stand.screen()

    await stand.presses_caption(texts.CARD_ADD_BUTTON)
    assert await stand.state.get_state() == RecipeWizard.name.state
    assert texts.CARD_NAME_PROMPT in stand.screen()


# --------------------------------------------------------------------------------------
# The payloads this block draws
# --------------------------------------------------------------------------------------


def test_a_category_button_carries_its_place_in_the_page_and_nothing_else() -> None:
    """Decision D14: a category is a word the venue typed, so what travels is an index.

    The page it indexes into lives in the FSM state; a category in the button would be
    free text, which rule 2 of `src/bot/callbacks.py` refuses outright.
    """
    for offered in range(keyboards.CATEGORY_LIMIT):
        payload = parse(RecipeCategory(index=offered).pack())
        assert isinstance(payload, RecipeCategory)
        assert payload.index == offered


# --------------------------------------------------------------------------------------
# The form (TZ 5.5, decision B5)
# --------------------------------------------------------------------------------------


async def test_the_minimal_recipe_is_saved_with_everything_else_skipped() -> None:
    """Decision B5: a name, a category and a composition are a card (TZ 5.5, TZ 8.1).

    Everything TZ 5.5 renders a card without is skipped with the button, and what reaches
    the service is a draft with those fields absent rather than blank.
    """
    stand = Stand()
    await walk_to_composition(stand)
    assert await stand.state.get_state() == RecipeWizard.composition.state

    await stand.types(COMPOSITION)
    await stand.presses_caption(texts.INVITE_SKIP_BUTTON)  # garnish
    await stand.presses_caption(texts.INVITE_SKIP_BUTTON)  # instruction

    assert len(stand.recipes.drafts) == 1, "one call, at the last step and not before"
    draft = stand.recipes.drafts[0]
    assert (draft.name, draft.category) == (DRINK, GROUP)
    assert list(draft.ingredients) == [COMPOSITION], "the lines are the service's to parse"
    assert (draft.glassware, draft.method, draft.ice) == (None, None, None)
    assert (draft.garnish, draft.instruction) == (None, None)
    assert stand.recipes.actors == [MANAGER]

    assert texts.CARD_SAVED in stand.screen()
    assert DRINK in stand.screen()
    assert RecipeShow(recipe_id=RECIPE_ID).pack() in stand.payloads()
    assert await stand.state.get_state() is None, "the form is over the moment the card exists"


async def test_the_composition_reaches_the_card_through_the_service_parser() -> None:
    """The handler never splits a line: TZ 3.2 and decision D4 put that in the service."""
    stand = Stand()
    await walk_to_composition(stand)
    await stand.types(COMPOSITION)
    await stand.presses_caption(texts.INVITE_SKIP_BUTTON)
    await stand.presses_caption(texts.INVITE_SKIP_BUTTON)
    assert "White rum" in stand.screen()
    assert "Lime" in stand.screen()


async def test_the_categories_of_the_venue_are_offered_as_buttons() -> None:
    """TZ 5.5 and principle 1.4#5: what exists is pressed, and nothing is hard-coded."""
    stand = Stand(recipes=FakeRecipes(GROUP, OTHER_GROUP))
    await stand.presses(AdminCommand(action=AdminAction.RECIPE_NEW).pack())
    await stand.types(DRINK)
    assert await stand.state.get_state() == RecipeWizard.category.state
    assert GROUP in stand.captions()
    assert OTHER_GROUP in stand.captions()

    await stand.presses_caption(OTHER_GROUP)
    assert await stand.state.get_state() == RecipeWizard.glassware.state
    assert (await stand.state.get_data())["category"] == OTHER_GROUP


async def test_a_category_the_venue_does_not_have_yet_is_typed_over_the_buttons() -> None:
    stand = Stand(recipes=FakeRecipes(GROUP))
    await stand.presses(AdminCommand(action=AdminAction.RECIPE_NEW).pack())
    await stand.types(DRINK)
    await stand.types(OTHER_GROUP)
    assert (await stand.state.get_data())["category"] == OTHER_GROUP
    assert await stand.state.get_state() == RecipeWizard.glassware.state


async def test_an_index_that_names_no_offered_category_redraws_the_step() -> None:
    """A stale keyboard must not save a category nobody chose (TZ 9)."""
    stand = Stand(recipes=FakeRecipes(GROUP))
    await stand.presses(AdminCommand(action=AdminAction.RECIPE_NEW).pack())
    await stand.types(DRINK)
    await stand.presses(RecipeCategory(index=keyboards.CATEGORY_LIMIT + 5).pack())
    assert await stand.state.get_state() == RecipeWizard.category.state
    assert texts.CARD_GROUP_PROMPT in stand.screen()
    assert "category" not in await stand.state.get_data()


async def test_a_blank_name_is_asked_for_again_and_nothing_is_remembered() -> None:
    """`RecipeService.create` refuses a card with no name, so the form does not carry one
    eight steps forward to be refused there (decision B5)."""
    stand = Stand()
    await stand.presses(AdminCommand(action=AdminAction.RECIPE_NEW).pack())
    await stand.types("   ")
    assert await stand.state.get_state() == RecipeWizard.name.state
    assert texts.CARD_NAME_PROMPT in stand.screen()
    assert await stand.state.get_data() == {}


async def test_an_optional_step_may_be_typed_as_well_as_skipped() -> None:
    stand = Stand()
    await stand.presses(AdminCommand(action=AdminAction.RECIPE_NEW).pack())
    await stand.types(DRINK)
    await stand.types(GROUP)
    await stand.types("highball")
    assert (await stand.state.get_data())["glassware"] == "highball"
    assert await stand.state.get_state() == RecipeWizard.method.state


# --------------------------------------------------------------------------------------
# The duplicate of decision D6
# --------------------------------------------------------------------------------------


async def test_a_duplicate_is_a_screen_and_not_a_traceback() -> None:
    """Decision D6: the venue already has this name in this category.

    Nothing is overwritten — the card that is in the way is offered instead, which is why
    `RecipeExistsError` carries its id at all.
    """
    recipes = FakeRecipes()
    recipes.already_holds(name=DRINK, category=GROUP, recipe_id=EXISTING_ID)
    stand = Stand(recipes=recipes)

    await walk_to_composition(stand)
    await stand.types(COMPOSITION)
    await stand.presses_caption(texts.INVITE_SKIP_BUTTON)
    await stand.presses_caption(texts.INVITE_SKIP_BUTTON)

    assert stand.recipes.drafts == [], "a refused draft is not written"
    assert texts.ERROR_SOMETHING_WENT_WRONG not in stand.screen()
    assert DRINK in stand.screen()
    assert GROUP in stand.screen()
    assert RecipeShow(recipe_id=EXISTING_ID).pack() in stand.payloads()
    assert await stand.state.get_state() is None


def test_the_duplicate_screen_names_both_halves_of_the_key() -> None:
    screen = views.exists(name=DRINK, category=GROUP, recipe_id=EXISTING_ID)
    assert screen.text == texts.CARD_EXISTS_TEMPLATE.format(name=DRINK, category=GROUP)
    for payload in payloads_of(screen.markup):
        assert type(parse(payload)) in RULES, payload


# --------------------------------------------------------------------------------------
# Rights (TZ 2, TZ 9)
# --------------------------------------------------------------------------------------


async def test_staff_is_refused_the_section_by_the_resolver() -> None:
    resolution = await resolve(
        OpenAdmin(section=AdminSection.CATALOGUE).pack(),
        actor=STAFF,
        repositories=FakeSubjects(VENUE_ID),
    )
    assert resolution.refusal is Refusal.FORBIDDEN
    assert resolution.payload is None


async def test_a_manager_gets_through_to_the_section() -> None:
    """The other half of the check above: the refusal is about the role and not about the
    section being unreachable for everybody."""
    resolution = await resolve(
        OpenAdmin(section=AdminSection.CATALOGUE).pack(),
        actor=MANAGER,
        repositories=FakeSubjects(VENUE_ID),
    )
    assert resolution.is_allowed


async def test_staff_cannot_open_the_form() -> None:
    """`TimezoneChoice` is `needs_actor=False`, so the resolver lets `CARD_ADD_BUTTON`
    through and this handler is the only check there is (TZ 9)."""
    stand = Stand(actor=STAFF)
    await stand.presses(AdminCommand(action=AdminAction.RECIPE_NEW).pack())
    assert await stand.state.get_state() is None, "no form was started"
    assert stand.recipes.drafts == []
    assert stand.alerts() == [texts.ERROR_NOT_ALLOWED]


@pytest.mark.parametrize(
    ("step", "payload"),
    [
        (RecipeWizard.category, RecipeCategory(index=0).pack()),
        (RecipeWizard.composition, AdminCommand(action=AdminAction.STEP_SKIP).pack()),
    ],
    ids=["category", "skip"],
)
async def test_staff_inside_the_form_is_refused_before_the_handler(
    step: State, payload: str
) -> None:
    """The other half: a form left open by somebody who is no longer a manager.

    The refusal comes from the resolver — `AdminCommand` and `RecipeCategory` both carry
    `minimum_role=MANAGER` — so no handler of this module runs at all, which is the point:
    the check is not something each of these screens has to remember.

    The half-filled draft is left where it was, and that is deliberate rather than
    overlooked. It leads nowhere: every further press is refused the same way, the next
    typed line is refused by :func:`take_answer` (which does clear it, because no resolver
    runs on a message), and the store forgets it within the day of `FSM_TTL`. Clearing it
    from here would mean letting a refused press change state, which is the one thing a
    refusal must not do.
    """
    stand = Stand(actor=STAFF, recipes=FakeRecipes(GROUP))
    await stand.state.set_state(step)
    await stand.state.update_data({"name": DRINK, "category": GROUP})
    await stand.presses(payload)
    assert stand.alerts() == [texts.ERROR_NOT_ALLOWED]
    assert stand.recipes.drafts == []


async def test_staff_typing_into_an_open_form_is_refused_too() -> None:
    """No resolver runs on a message at all, so the check on a typed step is this one."""
    stand = Stand(actor=STAFF)
    await stand.state.set_state(RecipeWizard.name)
    await stand.types(DRINK)
    assert stand.screen() == texts.ERROR_NOT_ALLOWED
    assert await stand.state.get_state() is None


async def test_an_update_with_no_venue_behind_it_reaches_no_screen() -> None:
    """Somebody with no membership yet has no venue-scoped service (TZ 5.1)."""
    stand = Stand()
    stand.context[SERVICES_KEY] = None
    await stand.presses(OpenAdmin(section=AdminSection.CATALOGUE).pack())
    assert stand.alerts() == [texts.ERROR_NOT_ALLOWED]


# --------------------------------------------------------------------------------------
# Leaving the form, and pressing a step that is no longer open
# --------------------------------------------------------------------------------------


async def test_opening_the_section_ends_a_half_filled_form() -> None:
    """Arriving at the board is leaving the scenario; otherwise the next line typed would
    be read as somebody's garnish (TZ 8.2).

    What replaces it is the lookup of TZ 5.8 and not an absence of state: from the section a
    typed line is a search for a card to correct. The draft itself is gone, which is the
    half that matters — nothing of the abandoned form survives into it.
    """
    stand = Stand()
    await stand.presses(AdminCommand(action=AdminAction.RECIPE_NEW).pack())
    await stand.types(DRINK)
    await stand.presses(OpenAdmin(section=AdminSection.CATALOGUE).pack())
    assert await stand.state.get_state() == RecipeLookup.query.state
    assert handlers.admin_recipes.NAME_FIELD not in await stand.state.get_data()


async def test_a_skip_pressed_outside_the_form_is_answered_and_saves_nothing() -> None:
    """TZ 9: an action answers in a second, even when the answer is «this screen is old»."""
    stand = Stand()
    await stand.presses(AdminCommand(action=AdminAction.STEP_SKIP).pack())
    assert stand.alerts() == [texts.ERROR_OUTDATED_SCREEN]
    assert stand.recipes.drafts == []


async def test_a_category_press_outside_the_category_step_is_answered() -> None:
    stand = Stand()
    await stand.presses(AdminCommand(action=AdminAction.RECIPE_NEW).pack())
    await stand.presses(RecipeCategory(index=0).pack())
    assert stand.alerts()[-1] == texts.ERROR_OUTDATED_SCREEN
    assert await stand.state.get_state() == RecipeWizard.name.state


# --------------------------------------------------------------------------------------
# The shape of the form itself
# --------------------------------------------------------------------------------------


def test_the_form_asks_the_steps_of_decision_b5_in_order() -> None:
    assert [step.state for step in FORM] == [
        RecipeWizard.name,
        RecipeWizard.category,
        RecipeWizard.glassware,
        RecipeWizard.method,
        RecipeWizard.ice,
        RecipeWizard.composition,
        RecipeWizard.garnish,
        RecipeWizard.instruction,
    ]
    assert [step.state for step in FORM if step.is_required] == [
        RecipeWizard.name,
        RecipeWizard.category,
    ]
    assert set(OPTIONAL_STATES) == {step.state for step in FORM if not step.is_required}


def test_every_step_of_the_form_has_a_screen_with_a_way_out() -> None:
    """TZ 8.2 wants a cancel on every step of a scenario, and TZ 8.1 no button in nowhere."""
    screens = (
        views.name_step(),
        views.category_step((GROUP,)),
        views.glassware_step(),
        views.method_step(),
        views.ice_step(),
        views.composition_step(),
        views.garnish_step(),
        views.instruction_step(),
    )
    for screen in screens:
        assert screen.text.strip()
        assert texts.CANCEL_BUTTON in captions_of(screen.markup)
        for payload in payloads_of(screen.markup):
            assert type(parse(payload)) in RULES, payload


def test_every_screen_survives_a_venue_that_types_html() -> None:
    """A category called «Rum & Cola» must not take the whole message down (TZ 8.2).

    The parse mode is set for the process (`src/bot/dispatcher.py`), so an unquoted `<`
    means Telegram refuses the message and the manager is shown nothing at all. The check
    itself is `tests/bot/test_views_html.py`'s, borrowed here rather than copied — these
    screens belong in that file's list too, and adding them there is the one edit this task
    could not make (see the report).
    """
    hostile_draft = RecipeDraft(
        name=HOSTILE,
        category=HOSTILE,
        glassware=HOSTILE,
        method=HOSTILE,
        ice=HOSTILE,
        garnish=HOSTILE,
        instruction=HOSTILE,
        ingredients=(f"{HOSTILE} — {HOSTILE}",),
    )
    screens = (
        views.section((HOSTILE, HOSTILE)),
        views.category_step((HOSTILE,)),
        views.saved(make_card(name=HOSTILE, category=HOSTILE, draft=hostile_draft)),
        views.exists(name=HOSTILE, category=HOSTILE, recipe_id=EXISTING_ID),
    )
    for screen in screens:
        assert_telegram_html(screen.text)


def test_the_router_is_named_and_registers_the_screens() -> None:
    instance = handlers.admin_recipes.router()
    assert instance.name == "admin_recipes"
    assert len(instance.callback_query.handlers) == 13
    assert len(instance.message.handlers) == 3


# --------------------------------------------------------------------------------------
# Finding a card to correct (TZ 5.8)
# --------------------------------------------------------------------------------------


async def open_section(stand: Stand) -> None:
    await stand.presses(OpenAdmin(section=AdminSection.CATALOGUE).pack())


async def test_the_section_invites_a_search_once_there_is_something_to_find() -> None:
    stand = Stand(recipes=FakeRecipes(GROUP))

    await open_section(stand)

    assert texts.CARD_FIND_PROMPT in stand.screen()
    assert await stand.state.get_state() == RecipeLookup.query.state


async def test_an_empty_venue_is_not_invited_to_search_at_all() -> None:
    """TZ 8.1: a prompt that answers every line with «nothing found» is a dead end."""
    stand = Stand()

    await open_section(stand)

    assert texts.CARD_EDITOR_EMPTY in stand.screen()
    assert texts.CARD_FIND_PROMPT not in stand.screen()


async def test_a_typed_name_finds_the_card() -> None:
    stand = Stand()
    stand.knows(make_card(recipe_id=RECIPE_ID, name=DRINK))
    await open_section(stand)

    await stand.types("moj")

    assert texts.TTK_FOUND_TEMPLATE.format(count=1) in stand.screen()
    assert RecipeManage(recipe_id=RECIPE_ID).pack() in stand.payloads()


async def test_a_name_nothing_matches_says_so_and_leaves_a_way_on() -> None:
    stand = Stand(recipes=FakeRecipes(GROUP))
    await open_section(stand)

    await stand.types("negroni")

    assert texts.TTK_NOTHING_FOUND_TEMPLATE.format(query="negroni") in stand.screen()
    assert texts.CARD_ADD_BUTTON in stand.captions()


async def test_a_category_button_opens_what_it_holds() -> None:
    stand = Stand()
    stand.knows(make_card(recipe_id=RECIPE_ID, name=DRINK, category=GROUP))
    stand.knows(make_card(recipe_id=EXISTING_ID, name="Espresso", category=OTHER_GROUP))
    await open_section(stand)

    await stand.presses_caption(GROUP)

    assert RecipeManage(recipe_id=RECIPE_ID).pack() in stand.payloads()
    assert RecipeManage(recipe_id=EXISTING_ID).pack() not in stand.payloads()


async def test_the_listing_pages_through_a_long_category() -> None:
    """`CataloguePage` and not `RecipePage`: that one opens the bartender's screen."""
    stand = Stand()
    for index in range(1, 13):
        stand.knows(make_card(recipe_id=index, name=f"Drink {index}", category=GROUP))
    await open_section(stand)
    await stand.presses_caption(GROUP)

    assert CataloguePage(offset=MAX_SEARCH_RESULTS).pack() in stand.payloads()
    await stand.presses_caption(texts.TTK_MORE_BUTTON)

    assert RecipeManage(recipe_id=11).pack() in stand.payloads()
    assert texts.TTK_PAGE_TEMPLATE.format(page=2) in stand.screen()


# --------------------------------------------------------------------------------------
# Correcting one field (TZ 5.8)
# --------------------------------------------------------------------------------------


async def open_card(stand: Stand, card: RecipeCard) -> None:
    stand.knows(card)
    await stand.presses(RecipeManage(recipe_id=card.recipe_id).pack())


async def test_the_card_offers_a_button_per_field_and_the_deletion() -> None:
    stand = Stand()

    await open_card(stand, make_card(name=DRINK))

    captions = stand.captions()
    assert texts.CARD_NAME_BUTTON in captions
    assert texts.CARD_COMPOSITION_BUTTON in captions
    assert texts.CARD_DELETE_BUTTON in captions
    assert DRINK in stand.screen()


async def test_a_field_is_retyped_and_the_card_comes_back_changed() -> None:
    stand = Stand()
    await open_card(stand, make_card(name="Mojjito"))

    await stand.presses_caption(texts.CARD_NAME_BUTTON)
    assert await stand.state.get_state() == RecipeEditing.value.state
    await stand.types(DRINK)

    assert stand.recipes.edits == [(RECIPE_ID, RecipeField.NAME, DRINK)]
    assert texts.CARD_UPDATED in stand.screen()
    assert DRINK in stand.screen()
    assert await stand.state.get_state() == RecipeLookup.query.state


async def test_an_optional_field_offers_a_button_that_empties_it() -> None:
    """Telegram refuses to send an empty message, so clearing has to be a press."""
    stand = Stand()
    await open_card(stand, make_card(name=DRINK, category=GROUP))
    await stand.presses_caption(texts.CARD_GLASSWARE_BUTTON)

    assert texts.CARD_CLEAR_BUTTON in stand.captions()
    await stand.presses_caption(texts.CARD_CLEAR_BUTTON)

    assert stand.recipes.edits == [(RECIPE_ID, RecipeField.GLASSWARE, None)]
    assert texts.CARD_UPDATED in stand.screen()


async def test_the_name_offers_no_clearing_that_would_be_refused() -> None:
    """Decision D6 keeps the name and the category, so the button would be a lie."""
    stand = Stand()
    await open_card(stand, make_card(name=DRINK))

    await stand.presses_caption(texts.CARD_NAME_BUTTON)

    assert texts.CARD_CLEAR_BUTTON not in stand.captions()


async def test_blanking_the_name_asks_the_same_field_again() -> None:
    """TZ 8.1: no dead end. The manager is looking at the card, so a refusal explains nothing."""
    stand = Stand()
    await open_card(stand, make_card(name=DRINK))
    await stand.presses_caption(texts.CARD_NAME_BUTTON)

    await stand.types("   ")

    assert stand.recipes.edits == []
    assert await stand.state.get_state() == RecipeEditing.value.state
    assert texts.CARD_EDIT_PROMPT_TEMPLATE.format(field=texts.CARD_NAME_BUTTON) in stand.screen()


async def test_renaming_onto_another_card_is_a_screen_and_not_a_traceback() -> None:
    stand = Stand()
    stand.knows(make_card(recipe_id=EXISTING_ID, name="Daiquiri", category=GROUP))
    await open_card(stand, make_card(recipe_id=RECIPE_ID, name=DRINK, category=GROUP))
    await stand.presses_caption(texts.CARD_NAME_BUTTON)

    await stand.types("daiquiri")

    assert texts.CARD_EXISTS_TEMPLATE.format(name="daiquiri", category=GROUP) in stand.screen()


async def test_the_composition_step_says_how_a_line_is_written() -> None:
    """Decision D4: «Содовая — топ» is a valid line, and the prompt has to say so."""
    stand = Stand()
    await open_card(stand, make_card(name=DRINK))

    await stand.presses_caption(texts.CARD_COMPOSITION_BUTTON)

    assert texts.CARD_COMPOSITION_PROMPT in stand.screen()


async def test_a_line_typed_after_the_state_expired_is_answered() -> None:
    """A deploy or a TTL between the button and the answer (TZ 9: never silence)."""
    stand = Stand()
    await open_card(stand, make_card(name=DRINK))
    await stand.presses_caption(texts.CARD_NAME_BUTTON)
    await stand.state.set_data({})

    await stand.types("anything")

    assert stand.recipes.edits == []
    assert texts.ERROR_OUTDATED_SCREEN in stand.screen()


# --------------------------------------------------------------------------------------
# Deleting a card (TZ 5.8)
# --------------------------------------------------------------------------------------


async def test_a_deletion_is_asked_about_before_it_happens() -> None:
    """Nothing in the schema keeps a removed card, so the question is not optional."""
    stand = Stand()
    await open_card(stand, make_card(name=DRINK))

    await stand.presses_caption(texts.CARD_DELETE_BUTTON)

    assert texts.CARD_DELETE_CONFIRM_TEMPLATE.format(name=DRINK) in stand.screen()
    assert stand.recipes.deleted == []


async def test_the_second_press_deletes_and_lands_back_in_the_section() -> None:
    stand = Stand()
    await open_card(stand, make_card(name=DRINK))
    await stand.presses_caption(texts.CARD_DELETE_BUTTON)

    await stand.presses_caption(texts.CARD_DELETE_YES_BUTTON)

    assert stand.recipes.deleted == [RECIPE_ID]
    assert texts.CARD_DELETED_TEMPLATE.format(name=DRINK) in stand.screen()
    assert texts.CARD_ADD_BUTTON in stand.captions()


async def test_the_way_out_of_the_question_is_the_card_it_asked_about() -> None:
    stand = Stand()
    await open_card(stand, make_card(name=DRINK))
    await stand.presses_caption(texts.CARD_DELETE_BUTTON)

    await stand.presses_caption(texts.BACK_BUTTON)

    assert stand.recipes.deleted == []
    assert texts.CARD_DELETE_BUTTON in stand.captions()


# --------------------------------------------------------------------------------------
# Who may do any of it (TZ 9, acceptance 11.3)
# --------------------------------------------------------------------------------------


async def test_a_bartender_cannot_open_the_card_of_the_manager_s_section() -> None:
    """The rule is the resolver's, and it is checked on the assembled tree."""
    stand = Stand(actor=STAFF)
    stand.knows(make_card(name=DRINK))

    await stand.presses(RecipeManage(recipe_id=RECIPE_ID).pack())

    assert stand.alerts() == [texts.ERROR_NOT_ALLOWED]


#: The three presses of this block that name a card, and therefore the three rules that have
#: to hold. Parametrised rather than asserted on one of them: each factory carries its own
#: `Rule` in the resolver, and a test of one leaves the other two free to be declared
#: `LIBRARY` — which is exactly the mutation that survived when this was a single test.
CARD_PRESSES: Final = [
    pytest.param(lambda: RecipeManage(recipe_id=RECIPE_ID).pack(), id="open"),
    pytest.param(lambda: RecipeEdit(recipe_id=RECIPE_ID, field=RecipeField.NAME).pack(), id="edit"),
    pytest.param(lambda: RecipeDrop(recipe_id=RECIPE_ID, is_confirmed=True).pack(), id="delete"),
]


@pytest.mark.parametrize("press", CARD_PRESSES)
async def test_a_card_of_another_venue_is_refused_before_any_handler_runs(
    press: Callable[[], str],
) -> None:
    """Acceptance 11.3: a forged id addresses nothing, and says nothing about what exists."""
    stand = Stand()
    stand.recipes.holds_card(make_card(name=DRINK))
    stand.services.repositories.tables.recipes.append(make_recipe(RECIPE_ID, venue_id=VENUE_ID + 1))

    await stand.presses(press())

    assert stand.alerts() == [texts.ERROR_NOT_ALLOWED]
    assert stand.recipes.edits == []
    assert stand.recipes.deleted == []


@pytest.mark.parametrize("press", CARD_PRESSES)
async def test_a_library_card_is_refused_the_same_way(press: Callable[[], str]) -> None:
    """TZ 3.3: the shared library is read by every venue and edited by none (question C4).

    Refused at the resolver and not at the service, which is a screen earlier on purpose: a
    card whose every button would be answered with «cannot be changed» is not a card to
    open. The service refuses one too (`RecipeReadOnlyError`), and that is the line that
    holds if this rule is ever relaxed.
    """
    stand = Stand()
    stand.recipes.holds_card(make_card(name=DRINK))
    stand.services.repositories.tables.recipes.append(make_recipe(RECIPE_ID, venue_id=None))

    await stand.presses(press())

    assert stand.alerts() == [texts.ERROR_NOT_ALLOWED]
    assert stand.recipes.edits == []
    assert stand.recipes.deleted == []


# --------------------------------------------------------------------------------------
# Screens that outlived their scenario (TZ 9)
# --------------------------------------------------------------------------------------


async def test_a_pager_from_an_older_screen_does_not_hijack_the_form() -> None:
    """Both halves of one defect: the wrong page, and the wizard dropped to draw it.

    The section's listing and the recipe form are two scenarios over one FSM storage, and
    the form *replaces* the data the pager rebuilds its listing from. Unfiltered, the pager
    ran a lookup of an empty query — and set the state back to the section, so the manager's
    half-typed card was gone with it.
    """
    stand = Stand()
    for index in range(1, 13):
        stand.knows(make_card(recipe_id=index, name=f"Drink {index}", category=GROUP))
    await open_section(stand)
    await stand.types("drink")
    assert CataloguePage(offset=MAX_SEARCH_RESULTS).pack() in stand.payloads()

    await stand.presses(AdminCommand(action=AdminAction.RECIPE_NEW).pack())
    await stand.types(DRINK)
    await stand.presses(CataloguePage(offset=MAX_SEARCH_RESULTS).pack())

    assert stand.alerts() == [texts.ERROR_OUTDATED_SCREEN]
    assert await stand.state.get_state() == RecipeWizard.category.state
    assert (await stand.state.get_data())[handlers.admin_recipes.NAME_FIELD] == DRINK


async def test_a_category_of_the_section_is_not_the_forms_answer() -> None:
    """A wrong-screen failure, and the reason `CatalogueCategory` is its own factory.

    While the form waits for a category, a press on a section screen left further up the
    chat used to arrive as *the answer to that question*: the manager reached for «show me
    the coffees» and set the category of the card they were entering.
    """
    stand = Stand(recipes=FakeRecipes(GROUP, OTHER_GROUP))
    await open_section(stand)
    await stand.presses(AdminCommand(action=AdminAction.RECIPE_NEW).pack())
    await stand.types(DRINK)
    assert await stand.state.get_state() == RecipeWizard.category.state

    await stand.presses(CatalogueCategory(index=1).pack())

    assert stand.alerts() == [texts.ERROR_OUTDATED_SCREEN]
    assert stand.recipes.drafts == []
    assert await stand.state.get_state() == RecipeWizard.category.state


async def test_the_forms_own_category_button_still_answers_it() -> None:
    """The other half: the two factories are told apart, not both refused."""
    stand = Stand(recipes=FakeRecipes(GROUP, OTHER_GROUP))
    await open_section(stand)
    await stand.presses(AdminCommand(action=AdminAction.RECIPE_NEW).pack())
    await stand.types(DRINK)

    await stand.presses_caption(OTHER_GROUP)

    assert await stand.state.get_state() == RecipeWizard.glassware.state


# --------------------------------------------------------------------------------------
# A message that is not a line of text (TZ 8.1)
# --------------------------------------------------------------------------------------


async def test_a_photo_sent_to_a_field_prompt_does_not_wipe_the_field() -> None:
    """`Message.text` is `None` for a photo, a sticker and a voice note.

    Passed on, that is indistinguishable from «clear this field», and the manager would be
    told «Готово» about a garnish they never meant to touch.
    """
    stand = Stand()
    await open_card(stand, make_card(name=DRINK, category=GROUP))
    await stand.presses_caption(texts.CARD_GARNISH_BUTTON)

    await stand.sends_a_photo()

    assert stand.recipes.edits == []
    assert await stand.state.get_state() == RecipeEditing.value.state
    assert texts.CARD_EDIT_PROMPT_TEMPLATE.format(field=texts.CARD_GARNISH_BUTTON) in (
        stand.screen()
    )


# --------------------------------------------------------------------------------------
# The shared library in the manager's listing (TZ 3.3, 8.1)
# --------------------------------------------------------------------------------------


async def test_a_library_card_is_not_offered_in_the_managers_listing() -> None:
    """The search reads the venue's rows *and* BarPoint's; only one of them is manageable.

    Every button of that screen ends in a write, which a library row does not allow
    (question C4), so the resolver would refuse the press — and a button whose only possible
    answer is a refusal is the dead button TZ 8.1 forbids.
    """
    stand = Stand()
    stand.knows(make_card(recipe_id=RECIPE_ID, name=DRINK, category=GROUP))
    shared = replace(make_card(recipe_id=EXISTING_ID, name="Negroni"), is_library=True)
    stand.recipes.holds_card(shared)
    await open_section(stand)

    await stand.types("o")

    assert RecipeManage(recipe_id=RECIPE_ID).pack() in stand.payloads()
    assert RecipeManage(recipe_id=EXISTING_ID).pack() not in stand.payloads()
