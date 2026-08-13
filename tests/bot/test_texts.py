"""`src/bot/texts/` and the main-menu registry (plan task 21).

Three properties, and each of them is one that a review catches once and then stops
catching:

* **the rules of TZ 8.2 hold for every string** — a button is at most 20 characters, a
  template says so in its name and a plain message contains no stray `{`;
* **the namespace is complete** — a constant that never reaches `src.bot.texts` is a
  constant no rule above applies to, and a key another module already looks up by name
  (`ERROR_SOMETHING_WENT_WRONG`, the notification types) resolves;
* **the menu is decision B7** — `staff` sees three buttons, `manager` and `owner` four, and
  the sections stage 0 does not build are not drawn for anybody.

The wording itself is not asserted. The customer rewords these strings without a developer
(TZ 8.2), so pinning phrases here would turn every edit of a message into a red build.
"""

from __future__ import annotations

import importlib
import pkgutil
import string
from types import ModuleType
from typing import Final

import pytest
from aiogram.types import InlineKeyboardButton
from src.bot import texts
from src.bot.callbacks import MenuAction, Nav, NavTarget, parse
from src.bot.keyboards.menu import (
    MAIN_MENU,
    action_for,
    entries_for,
    main_menu,
    submenu,
    wizard,
)
from src.bot.middlewares.errors import ERROR_MESSAGE_KEY, apology_text
from src.bot.texts.admin import role_label
from src.bot.texts.checklists import checklist_title
from src.bot.texts.recipes import unit_label
from src.db.models import ChecklistType, MemberRole, Unit
from src.services.notifications import NotificationType

#: TZ 8.2: «Текст кнопки — до 20 символов».
BUTTON_LIMIT: Final = 20

TEXT_MODULES: Final[tuple[ModuleType, ...]] = tuple(
    importlib.import_module(f"{texts.__name__}.{info.name}")
    for info in pkgutil.iter_modules(texts.__path__)
)

#: (module, name, value) for every string constant the package exports.
CONSTANTS: Final[tuple[tuple[str, str, str], ...]] = tuple(
    (module.__name__.rsplit(".", 1)[-1], name, getattr(module, name))
    for module in TEXT_MODULES
    for name in module.__all__
    if isinstance(getattr(module, name), str)
)

CONSTANT_IDS: Final[list[str]] = [f"{where}.{name}" for where, name, _ in CONSTANTS]


def placeholders(value: str) -> list[str]:
    """Named fields of a `str.format` template, in order of appearance."""
    return [
        field
        for _, field, _, _ in string.Formatter().parse(value)
        if field is not None and field != ""
    ]


def literal_length(value: str) -> int:
    """Length of everything in a template that is not a placeholder."""
    return sum(len(literal) for literal, _, _, _ in string.Formatter().parse(value))


# --------------------------------------------------------------------------------------
# The scope of these tests must not collapse quietly
# --------------------------------------------------------------------------------------


def test_the_package_is_populated() -> None:
    assert len(TEXT_MODULES) >= 7, (
        f"only {len(TEXT_MODULES)} text modules; the package is meant to hold one per "
        "functional block (onboarding, menu, shift, checklist, recipes, errors, admin)"
    )
    assert len(CONSTANTS) > 100, f"only {len(CONSTANTS)} strings; the scope collapsed"
    assert any(name.endswith("_BUTTON") for _, name, _ in CONSTANTS), (
        "no button caption is exported, so the 20-character rule of TZ 8.2 checks nothing"
    )


# --------------------------------------------------------------------------------------
# TZ 8.2: the rules every string obeys
# --------------------------------------------------------------------------------------


#: Glue rather than wording: a separator and a mark are appended to a line, so their
#: padding is the point of them («... · открытие»). Everything else is a message or a
#: caption, where a stray space is a typo the renderer would faithfully send on.
GLUE_SUFFIXES: Final = ("_SEPARATOR", "_MARK")


@pytest.mark.parametrize(("where", "name", "value"), CONSTANTS, ids=CONSTANT_IDS)
def test_a_text_is_a_non_empty_stripped_string(where: str, name: str, value: str) -> None:
    assert value.strip(), f"{where}.{name} is empty"
    if not name.endswith(GLUE_SUFFIXES):
        assert value == value.strip(), (
            f"{where}.{name} has leading or trailing whitespace; padding a message is the "
            "renderer's business, not the wording's"
        )


@pytest.mark.parametrize(
    ("where", "name", "value"),
    [row for row in CONSTANTS if row[1].endswith("_BUTTON")],
    ids=[f"{where}.{name}" for where, name, _ in CONSTANTS if name.endswith("_BUTTON")],
)
def test_a_button_caption_fits_the_limit(where: str, name: str, value: str) -> None:
    """TZ 8.2: «Текст кнопки — до 20 символов»."""
    assert len(value) <= BUTTON_LIMIT, (
        f"{where}.{name} is {len(value)} characters: {value!r}. A caption longer than "
        f"{BUTTON_LIMIT} wraps or is cut on a phone (TZ 8.2)"
    )


@pytest.mark.parametrize(
    ("where", "name", "value"),
    [row for row in CONSTANTS if row[1].endswith("_BUTTON_TEMPLATE")],
    ids=[f"{where}.{name}" for where, name, _ in CONSTANTS if name.endswith("_BUTTON_TEMPLATE")],
)
def test_a_button_template_leaves_room_for_its_values(where: str, name: str, value: str) -> None:
    """The fixed part of a caption template already has to fit — what is substituted is
    venue wording, and the renderer shortens it, but no shortening saves a template whose
    own text is over the limit."""
    assert literal_length(value) <= BUTTON_LIMIT, (
        f"{where}.{name} spells {literal_length(value)} characters before anything is "
        f"substituted; the caption can never fit {BUTTON_LIMIT} (TZ 8.2)"
    )


@pytest.mark.parametrize(("where", "name", "value"), CONSTANTS, ids=CONSTANT_IDS)
def test_templates_are_named_templates(where: str, name: str, value: str) -> None:
    """A `{placeholder}` in a constant that is not called `_TEMPLATE` is a message that
    will be sent with braces in it, because nothing told the caller to format it."""
    found = placeholders(value)
    if name.endswith("_TEMPLATE"):
        assert found, f"{where}.{name} is named a template but has no placeholder"
    else:
        assert "{" not in value and "}" not in value, (
            f"{where}.{name} carries a placeholder but is not named `*_TEMPLATE`, so a "
            f"caller has no reason to format it: {value!r}"
        )


@pytest.mark.parametrize(
    ("where", "name", "value"),
    [row for row in CONSTANTS if row[1].endswith("_TEMPLATE")],
    ids=[f"{where}.{name}" for where, name, _ in CONSTANTS if name.endswith("_TEMPLATE")],
)
def test_a_template_uses_named_placeholders(where: str, name: str, value: str) -> None:
    """Named, so that a renderer reads what it is filling in and cannot swap two arguments
    of the same type — `{start}` and `{end}` are a shift window, `{0}` and `{1}` are not."""
    found = placeholders(value)
    for field in found:
        assert field.isidentifier(), f"{where}.{name}: {field!r} is not a name"
    assert value.format(**dict.fromkeys(found, "x")), f"{where}.{name} does not format"


# --------------------------------------------------------------------------------------
# The namespace other modules already depend on
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("where", "name", "value"), CONSTANTS, ids=CONSTANT_IDS)
def test_every_constant_reaches_the_package_namespace(where: str, name: str, value: str) -> None:
    """`src.bot.texts` is what the rest of the bot imports; a constant that stops at its
    module is invisible to every caller and to every rule above."""
    assert getattr(texts, name, None) == value, (
        f"{where}.{name} is not re-exported from src.bot.texts"
    )
    assert name in texts.__all__, f"{where}.{name} is missing from src.bot.texts.__all__"


def test_the_error_middleware_finds_its_apology() -> None:
    """`src/bot/middlewares/errors.py` was written before this package and named the key it
    would read instead of the phrase (TZ 8.2). The two must still agree, or the user gets a
    silent failure and the log gets a warning nobody reads."""
    assert ERROR_MESSAGE_KEY == "ERROR_SOMETHING_WENT_WRONG"
    assert apology_text() == texts.ERROR_SOMETHING_WENT_WRONG


@pytest.mark.parametrize("notification_type", list(NotificationType), ids=str)
def test_every_notification_type_has_wording(notification_type: NotificationType) -> None:
    """The types are declared by `src/services/notifications.py`; a row queued for a type
    with no wording would be delivered as an empty message (TZ 6, plan task 31)."""
    headline = texts.notification_headline(notification_type)
    assert headline.strip(), f"{notification_type} has no headline"


@pytest.mark.parametrize("checklist_type", list(ChecklistType), ids=str)
def test_every_checklist_type_has_a_title(checklist_type: ChecklistType) -> None:
    assert checklist_title(checklist_type).strip()


@pytest.mark.parametrize("unit", list(Unit), ids=str)
def test_every_unit_has_a_label(unit: Unit) -> None:
    """TZ 4.4 fixes the units in code, so their short forms are wording and must be
    complete — a card rendering «50 <Unit.ML: 'ml'>» is what an incomplete map produces."""
    assert unit_label(unit).strip()


@pytest.mark.parametrize("role", list(MemberRole), ids=str)
def test_every_role_has_a_label(role: MemberRole) -> None:
    assert role_label(role).strip()


# --------------------------------------------------------------------------------------
# The main menu: decision B7 and TZ 5.2
# --------------------------------------------------------------------------------------


def test_staff_sees_exactly_three_sections() -> None:
    """Decision B7: the shift, the schedule and the recipe book — and nothing else. The
    write-off, the order and the knowledge base are not drawn at all, not drawn as stubs."""
    assert [entry.action for entry in entries_for(MemberRole.STAFF)] == [
        MenuAction.MY_SHIFT,
        MenuAction.SCHEDULE,
        MenuAction.CATALOGUE,
    ]


@pytest.mark.parametrize("role", [MemberRole.MANAGER, MemberRole.OWNER])
def test_management_is_added_for_manager_and_owner(role: MemberRole) -> None:
    """TZ 5.2, and TZ 2: an `owner` has every right a `manager` has."""
    actions = [entry.action for entry in entries_for(role)]
    assert actions[-1] is MenuAction.MANAGEMENT
    assert actions[:-1] == [entry.action for entry in entries_for(MemberRole.STAFF)]


@pytest.mark.parametrize("role", list(MemberRole), ids=str)
def test_unbuilt_sections_are_not_rendered_for_anybody(role: MemberRole) -> None:
    """TZ 8.1 forbids a button that leads nowhere; decision B7 turns them on with the flag
    of the registry, not with an edit of this keyboard."""
    unavailable = {entry.action for entry in MAIN_MENU if not entry.is_available}
    assert unavailable, "nothing is switched off, so this rule proves nothing"
    assert unavailable.isdisjoint({entry.action for entry in entries_for(role)})
    for entry in MAIN_MENU:
        if not entry.is_available:
            assert action_for(entry.caption) is None, (
                f"{entry.action} is not rendered, so its caption must not be recognised "
                "either: answering a button that does not exist is TZ 8.1 backwards"
            )


def test_the_registry_covers_every_section_of_the_menu() -> None:
    """A section that TZ 5.2 names but the registry omits cannot be switched on by a flag."""
    assert {entry.action for entry in MAIN_MENU} == set(MenuAction)
    captions = [entry.caption for entry in MAIN_MENU]
    assert len(set(captions)) == len(captions), "two sections share a caption"


@pytest.mark.parametrize("role", list(MemberRole), ids=str)
def test_a_menu_press_is_recognised_by_its_caption(role: MemberRole) -> None:
    """TZ 5.2 makes the menu a reply keyboard, so the router matches text, not payloads."""
    for entry in entries_for(role):
        assert action_for(entry.caption) is entry.action
    assert action_for("мохито") is None


@pytest.mark.parametrize("role", list(MemberRole), ids=str)
def test_the_menu_is_a_persistent_reply_keyboard(role: MemberRole) -> None:
    """TZ 5.2: «постоянная reply-клавиатура снизу, чтобы не терялась в переписке»."""
    markup = main_menu(role)
    assert markup.is_persistent is True
    assert markup.resize_keyboard is True
    assert markup.one_time_keyboard is False
    rendered = [button.text for row in markup.keyboard for button in row]
    assert rendered == [entry.caption for entry in entries_for(role)]
    assert all(len(row) <= 2 for row in markup.keyboard), "TZ 5.2 draws the menu two by two"


# --------------------------------------------------------------------------------------
# Navigation: TZ 5.2 and 8.2 require it of every screen, so the constructor supplies it
# --------------------------------------------------------------------------------------


def test_a_submenu_always_offers_back_and_home() -> None:
    """TZ 5.2: every submenu carries a back button and a home button."""
    markup = submenu()
    captions = [button.text for row in markup.inline_keyboard for button in row]
    assert captions == [texts.BACK_BUTTON, texts.HOME_BUTTON]
    targets = [
        parse(button.callback_data or "") for row in markup.inline_keyboard for button in row
    ]
    assert targets == [Nav(target=NavTarget.BACK), Nav(target=NavTarget.HOME)]


def test_a_wizard_step_always_offers_cancel() -> None:
    """TZ 8.2: «в любом пошаговом сценарии есть „Отмена", возвращающая в меню без
    сохранения»."""
    markup = wizard()
    captions = [button.text for row in markup.inline_keyboard for button in row]
    assert texts.CANCEL_BUTTON in captions
    assert texts.BACK_BUTTON in captions


def test_navigation_is_appended_after_the_screens_own_rows() -> None:
    """A screen hands over its own rows; it never spells the navigation itself, which is
    what makes «every submenu has them» a property rather than a habit."""
    own = [
        InlineKeyboardButton(
            text=texts.STAFF_ADD_BUTTON,
            callback_data=Nav(target=NavTarget.HOME).pack(),
        )
    ]
    markup = submenu(own)
    assert [button.text for button in markup.inline_keyboard[0]] == [texts.STAFF_ADD_BUTTON]
    assert [button.text for button in markup.inline_keyboard[-1]] == [
        texts.BACK_BUTTON,
        texts.HOME_BUTTON,
    ]
    assert [button.text for button in wizard(own).inline_keyboard[-1]] == [
        texts.BACK_BUTTON,
        texts.CANCEL_BUTTON,
    ]
