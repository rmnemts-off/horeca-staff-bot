"""`src/bot/callbacks.py`: the 64-byte budget of decision D14, proved and provoked.

The failure this file exists for is silent. Telegram does not reject a payload over 64
bytes — it truncates it, and the button then arrives as a shorter string that matches
another factory or none, so the bartender gets the wrong screen instead of an error. A test
that packs one plausible id proves nothing about the day `checklist_run.id` passes a
million, which is why every factory here is packed with a **full-width `BIGINT` in every
field** and the longest member of every enum.

The second half is the teeth. A budget that is only checked by a test is a budget the next
factory can walk past; `src/bot/callbacks.py` measures a subclass while it is being
declared, so the tests at the bottom build the factories that would break it and assert the
declaration itself fails. Without them the enforcement could be deleted and this file would
stay green. Two of those factories are declared with aiogram's `sep=`, because a separator
is a byte per field and the class keyword that sets it is the one lever a subclass has over
arithmetic written in the module.
"""

from __future__ import annotations

import enum
import typing
from typing import Any, Final

import pytest
from src.bot import callbacks
from src.bot.callbacks import (
    CALLBACK_BUDGET,
    MAX_PREFIX_LENGTH,
    REGISTRY,
    SEPARATOR,
    Callback,
    CallbackBudgetError,
    CallbackFieldTypeError,
    ChecklistToggle,
    DuplicatePrefixError,
    ForeignSeparatorError,
    MalformedCallbackError,
    MenuAction,
    Nav,
    NavTarget,
    UnknownCallbackError,
    factory_for,
    parse,
    registered,
    worst_case_length,
)

#: The widest a `BIGINT` primary key can be written (decision D18: no UUIDs, and this is
#: the number that made that decision).
WIDEST_ID: Final = -(2**63)

FACTORIES: Final[tuple[type[Callback], ...]] = registered()
FACTORY_IDS: Final[list[str]] = [factory.__name__ for factory in FACTORIES]


def widest_value(annotation: Any) -> Any:
    """The value of this annotation that produces the longest payload."""
    origin = typing.get_origin(annotation)
    if origin is not None:
        arguments = [
            argument for argument in typing.get_args(annotation) if argument is not type(None)
        ]
        return widest_value(arguments[0])
    if annotation is bool:
        return True
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return max(annotation, key=lambda member: len(str(member.value)))
    if annotation is int:
        return WIDEST_ID
    raise AssertionError(f"no widest value for {annotation!r}")


def widest_instance(factory: type[Callback]) -> Callback:
    """The factory filled with the largest thing every one of its fields can hold."""
    return factory(
        **{name: widest_value(field.annotation) for name, field in factory.model_fields.items()}
    )


# --------------------------------------------------------------------------------------
# The scope
# --------------------------------------------------------------------------------------


def test_the_registry_is_populated() -> None:
    assert len(FACTORIES) >= 20, f"only {len(FACTORIES)} factories; the scope collapsed"


def test_every_factory_declared_in_the_module_is_registered() -> None:
    """Registration is a side effect of subclassing, and this is what says so out loud: a
    factory that somehow escaped the base class would be unparseable by `parse` and
    unmeasured by the budget."""
    declared = {
        value
        for value in vars(callbacks).values()
        if isinstance(value, type) and issubclass(value, Callback) and value is not Callback
    }
    assert declared == set(FACTORIES)


# --------------------------------------------------------------------------------------
# Decision D14: 64 bytes, whatever the ids
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("factory", FACTORIES, ids=FACTORY_IDS)
def test_a_full_width_payload_fits_the_budget(factory: type[Callback]) -> None:
    payload = widest_instance(factory).pack()
    assert len(payload.encode()) <= CALLBACK_BUDGET, (
        f"{factory.__name__} packs {len(payload.encode())} bytes with full-width ids: "
        f"{payload!r}. Telegram truncates past {CALLBACK_BUDGET} and the truncated string "
        "routes somewhere else instead of failing (decision D14)"
    )


@pytest.mark.parametrize("factory", FACTORIES, ids=FACTORY_IDS)
def test_the_declared_worst_case_is_not_optimistic(factory: type[Callback]) -> None:
    """The arithmetic that guards the declaration must not promise less than a real pack
    produces, or the guard would pass a factory that cannot be built."""
    assert worst_case_length(factory) >= len(widest_instance(factory).pack().encode())


def test_the_checklist_button_carries_two_full_width_ids() -> None:
    """The case decision D14 was written for: TZ 5.4 puts a `run_id` and an `item_id` into
    one button, which is 72 bytes with two UUIDs and fits with two `BIGINT`s."""
    payload = ChecklistToggle(run_id=WIDEST_ID, item_id=WIDEST_ID).pack()
    assert len(payload.encode()) <= CALLBACK_BUDGET


@pytest.mark.parametrize("factory", FACTORIES, ids=FACTORY_IDS)
def test_a_payload_survives_the_round_trip(factory: type[Callback]) -> None:
    original = widest_instance(factory)
    assert parse(original.pack()) == original


def test_prefixes_are_unique_and_short() -> None:
    prefixes = [factory.__prefix__ for factory in FACTORIES]
    assert sorted(prefixes) == sorted(set(prefixes))
    assert set(REGISTRY) == set(prefixes)
    for prefix in prefixes:
        assert 1 <= len(prefix) <= MAX_PREFIX_LENGTH, prefix
        assert SEPARATOR not in prefix


@pytest.mark.parametrize("factory", FACTORIES, ids=FACTORY_IDS)
def test_every_factory_packs_with_the_scheme_separator(factory: type[Callback]) -> None:
    """`parse` finds the owner of a payload by splitting on `SEPARATOR`; a factory that
    packed with its own would build a button no handler could ever be reached from."""
    assert factory.__separator__ == SEPARATOR, factory.__name__


# --------------------------------------------------------------------------------------
# Parsing lives here and nowhere else
# --------------------------------------------------------------------------------------


def test_parse_returns_the_typed_object() -> None:
    assert parse(Nav(target=NavTarget.CANCEL).pack()) == Nav(target=NavTarget.CANCEL)


# --------------------------------------------------------------------------------------
# «Назад» knows where it goes
# --------------------------------------------------------------------------------------


def test_back_carries_its_destination() -> None:
    """The decision recorded in :class:`Nav`, held here so the next screen cannot quietly
    unmake it: nothing in this framework remembers where a press came from — no screen
    stack, no breadcrumbs, and a callback query names the button and not the screen — so a
    back button without a destination could only be answered by guessing, and a guess is
    the wrong-screen failure this module exists to prevent."""
    assert "section" in Nav.model_fields, (
        "Nav.section is gone, so «Назад» no longer says where it returns to; whoever "
        "handles it can only invent a destination (decision D10 and the class docstring)"
    )
    back = Nav(target=NavTarget.BACK, section=MenuAction.MANAGEMENT)
    restored = parse(back.pack())
    assert restored == back
    assert isinstance(restored, Nav)
    assert restored.section is MenuAction.MANAGEMENT


def test_back_without_a_section_means_the_main_menu() -> None:
    """The other half of the same decision, and the reason the field is allowed to be
    empty: decision D10 caps navigation at two levels, so a screen one step from the menu
    has exactly one place to go back to and names nothing."""
    restored = parse(Nav(target=NavTarget.BACK).pack())
    assert isinstance(restored, Nav)
    assert restored.section is None


@pytest.mark.parametrize("target", [NavTarget.HOME, NavTarget.CANCEL])
def test_home_and_cancel_need_no_destination(target: NavTarget) -> None:
    """Home is the main menu by definition and cancel drops the scenario in hand (TZ 8.2);
    neither is a screen-to-screen move, so neither carries one."""
    assert parse(Nav(target=target).pack()) == Nav(target=target)


def test_an_unknown_prefix_is_refused() -> None:
    """An old keyboard, or a string somebody typed. TZ 9 wants neither to reach a handler."""
    with pytest.raises(UnknownCallbackError):
        parse("zzzz:1:2")
    with pytest.raises(UnknownCallbackError):
        factory_for("")


def test_a_malformed_payload_is_refused() -> None:
    """The prefix is ours, the rest is not: too few parts, or a word where an id belongs."""
    with pytest.raises(MalformedCallbackError):
        parse(f"{ChecklistToggle.__prefix__}{SEPARATOR}1")
    with pytest.raises(MalformedCallbackError):
        parse(f"{ChecklistToggle.__prefix__}{SEPARATOR}1{SEPARATOR}мохито")


def test_packing_refuses_a_value_wider_than_the_schema() -> None:
    """Python integers are unbounded, so the arithmetic of the declaration is not the last
    word — `pack` is, and it fails loudly rather than handing Telegram something to cut."""
    with pytest.raises(CallbackBudgetError):
        ChecklistToggle(run_id=10**40, item_id=10**40).pack()


# --------------------------------------------------------------------------------------
# The teeth: what the base class refuses to let anybody declare
# --------------------------------------------------------------------------------------


def test_a_factory_over_the_budget_cannot_be_declared() -> None:
    """Three `BIGINT`s and a four-byte prefix are 67 bytes. The screen that needs them
    needs a different design, and it must find that out on import, not in production."""
    with pytest.raises(CallbackBudgetError):

        class TooWide(Callback, prefix="wide"):
            first: int
            second: int
            third: int

    assert "wide" not in REGISTRY


def test_free_text_cannot_travel_in_a_button() -> None:
    """A search query has no worst case, so accepting one would turn the budget into a
    guess; the query lives in the FSM state and the button carries an id."""
    with pytest.raises(CallbackFieldTypeError):

        class WithText(Callback, prefix="q1"):
            query: str

    assert "q1" not in REGISTRY


def test_two_factories_cannot_share_a_prefix() -> None:
    """Each would unpack the other's payload, and the result is a wrong screen rather than
    an error — the same class of failure truncation causes."""
    with pytest.raises(DuplicatePrefixError):

        class Shadow(Callback, prefix=Nav.__prefix__):
            target: NavTarget

    assert REGISTRY[Nav.__prefix__] is Nav


def test_a_wide_separator_is_counted_against_the_budget() -> None:
    """The one lever a subclass has over arithmetic written in the module: aiogram takes a
    `sep=` class keyword and packs with it, so a budget counting the module's single byte
    would wave through a factory whose real payloads are 68 bytes. Two `BIGINT`s and a
    thirteen-byte separator are exactly that; with the separator counted from the class
    the declaration cannot happen."""
    with pytest.raises(CallbackBudgetError):

        class Wide(Callback, prefix="w1", sep="~" * 13):
            run_id: int
            item_id: int

    assert "w1" not in REGISTRY


def test_a_factory_cannot_pack_with_its_own_separator() -> None:
    """A narrow separator fits the budget and still breaks the scheme: `factory_for`
    splits a raw payload on `SEPARATOR` to find its owner, so `w2|1` would be routed by
    nobody and the button would do nothing at all."""
    with pytest.raises(ForeignSeparatorError):

        class Piped(Callback, prefix="w2", sep="|"):
            run_id: int

    assert "w2" not in REGISTRY


def test_a_long_prefix_cannot_be_declared() -> None:
    """Every byte of a prefix is a byte an id cannot use."""
    with pytest.raises(CallbackBudgetError):

        class Verbose(Callback, prefix="checklist_toggle"):
            run_id: int

    assert "checklist_toggle" not in REGISTRY
