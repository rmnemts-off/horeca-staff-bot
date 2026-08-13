"""A main-menu press outranks the scenario it interrupts (TZ 5.2, plan task 20).

TZ 5.2, last rule of the section: an unfinished FSM scenario must not block the user --
pressing any main-menu button drops the state, and asks "interrupt the order?" **only when
there is unsaved data**. Two requirements hide in that sentence:

1. the press must be seen **before** the handler waiting for the next line of the wizard,
   or the caption is read as the answer to "what is your name?" and the person is stuck in
   the scenario they were trying to leave;
2. it must not be a confirmation prompt every time, or abandoning an empty first step costs
   two taps for nothing.

**Why this is a middleware and not a router.** The plan says "a router with priority above
the FSM handlers", and a router cannot do it. aiogram resolves the FSM state **once per
update**, in `FSMContextMiddleware`, and hands the result down as `raw_state`; `StateFilter`
reads that value and nothing else. A router handler that clears the state and steps aside
with `SkipHandler` therefore leaves `raw_state` behind it unchanged, the wizard's own
state-filtered handler still matches, and the caption is swallowed exactly as in failure 1
above -- with the state now cleared as well, which is the worst of both. An outer
middleware of `Dispatcher.update` holds the context dictionary the whole routing pass is
fed from, so it can clear the state *and* correct `raw_state`, and it does so before any
router is consulted at all. That is a stronger guarantee than router order: a router
included in the wrong place cannot get in front of it.

**The confirmation is an ordinary `OpenSection` button.** "Interrupt" carries the section
the person was heading for, so pressing it produces the callback the section's own handler
already answers, and this middleware clears the scenario on its way past. Nothing is
replayed and there is no second way into a screen. "Carry on" is
:class:`~src.bot.callbacks.MenuKeep`, which takes the question away and leaves the state
where it was. Either answer takes the question out of the chat: a question that has been
answered is not a screen, and left hanging it is a live keyboard pointing at a scenario
that no longer exists.

**Both roads into the menu are the same road.** A section is opened by a caption on the
reply keyboard *and* by an inline `OpenSection` button, and TZ 5.2 does not distinguish
them: unsaved data is asked about, an untouched scenario is dropped in silence. So the
button press is checked exactly as the caption is — otherwise half of the scenarios of
TZ 5.8 lose what was typed into them without a word, depending only on which of the two
buttons the person happened to reach for. The one press that is *not* checked again is the
answer to the question itself: it comes from the question's own message
(:func:`is_interrupt_question`), and asking it again would be a loop rather than a rule.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from src.bot import texts
from src.bot.callbacks import CallbackError, MenuAction, MenuKeep, OpenSection, parse
from src.bot.keyboards.menu import action_for
from src.bot.middlewares.errors import inner_event

#: aiogram's own key for the FSM context of the update.
STATE_KEY: Final = "state"

#: aiogram's own key for the state `StateFilter` matches on. Written once per update by
#: `FSMContextMiddleware`; see the module docstring for why this module rewrites it.
RAW_STATE_KEY: Final = "raw_state"

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def interrupt_keyboard(action: MenuAction) -> InlineKeyboardMarkup:
    """Interrupt (which is the section itself) and carry on (which is nothing)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.INTERRUPT_YES_BUTTON,
                    callback_data=OpenSection(section=action).pack(),
                ),
                InlineKeyboardButton(
                    text=texts.INTERRUPT_NO_BUTTON,
                    callback_data=MenuKeep().pack(),
                ),
            ]
        ]
    )


def is_interrupt_question(screen: Message | None) -> bool:
    """Whether this screen is the question above — i.e. the press on it is the answer.

    Recognised by the `MenuKeep` button, which nothing else in the project draws. The
    confirmation is an ordinary `OpenSection` (see the module docstring), so the message it
    was pressed on is the only thing that tells "yes, interrupt" apart from a fresh menu
    press made while some other screen was open.
    """
    markup = screen.reply_markup if screen is not None else None
    if not isinstance(markup, InlineKeyboardMarkup):
        return False
    keep = MenuKeep().pack()
    return any(button.callback_data == keep for row in markup.inline_keyboard for button in row)


def menu_action_of(message: Message) -> MenuAction | None:
    """The section a message means, or `None` when it is not a main-menu caption.

    `action_for` only knows the captions of sections stage 0 actually draws (decision B7),
    so a declared-but-hidden one is not recognised here either — it is ordinary text, and a
    wizard waiting for a line receives it as one.
    """
    return action_for(message.text or "")


class MenuInterceptMiddleware(BaseMiddleware):
    """Outer middleware of `Dispatcher.update`, after the gate; see the module docstring."""

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state = data.get(STATE_KEY)
        if not isinstance(state, FSMContext):
            return await handler(event, data)

        target = inner_event(event)
        if isinstance(target, Message):
            action = menu_action_of(target)
            if action is not None:
                return await self._menu_pressed(handler, event, data, target, state, action)
        elif isinstance(target, CallbackQuery):
            return await self._button_pressed(handler, event, data, target, state)
        return await handler(event, data)

    async def _menu_pressed(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
        message: Message,
        state: FSMContext,
        action: MenuAction,
    ) -> Any:
        if data.get(RAW_STATE_KEY) is None:
            return await handler(event, data)
        if await state.get_data():
            # TZ 5.2: ask, and only now. The scenario is left exactly as it was, so
            # "carry on" needs to restore nothing.
            await message.answer(
                texts.INTERRUPT_QUESTION,
                reply_markup=interrupt_keyboard(action),
            )
            return None
        # A scenario with nothing typed into it yet: there is nothing to ask about.
        await self._drop(state, data)
        return await handler(event, data)

    async def _button_pressed(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
        callback: CallbackQuery,
        state: FSMContext,
    ) -> Any:
        payload = self._payload(callback)
        screen = callback.message if isinstance(callback.message, Message) else None
        if isinstance(payload, MenuKeep):
            await callback.answer()
            await self._close_question(screen)
            return None
        if not isinstance(payload, OpenSection):
            return await handler(event, data)
        if data.get(RAW_STATE_KEY) is None:
            # No scenario is open, so there is nothing to interrupt and nothing to clear.
            return await handler(event, data)
        if is_interrupt_question(screen):
            # The press *is* the answer: the question was asked and confirmed. It stops
            # being a screen the moment it is answered, so it does not stay in the chat.
            await self._drop(state, data)
            await self._close_question(screen)
            return await handler(event, data)
        if screen is not None and await state.get_data():
            # The same rule as the caption road (TZ 5.2), and the same question.
            await callback.answer()
            await screen.answer(
                texts.INTERRUPT_QUESTION,
                reply_markup=interrupt_keyboard(payload.section),
            )
            return None
        # Nothing typed in yet — or a screen too old to reply in, where the question could
        # not be asked anyway (`callback.message` is `InaccessibleMessage` after 48 hours).
        await self._drop(state, data)
        return await handler(event, data)

    @staticmethod
    async def _close_question(screen: Message | None) -> None:
        """Take the answered question out of the chat; see the module docstring."""
        if screen is not None:
            await screen.delete()

    @staticmethod
    def _payload(callback: CallbackQuery) -> object | None:
        """The typed callback, through the one parser of the project (D14)."""
        if callback.data is None:
            return None
        try:
            return parse(callback.data)
        except CallbackError:
            # Not ours to answer: the resolver refuses it a moment later, in one place.
            return None

    @staticmethod
    async def _drop(state: FSMContext, data: dict[str, Any]) -> None:
        await state.clear()
        # The correction the whole module exists for: `StateFilter` downstream reads this
        # value, not the storage.
        data[RAW_STATE_KEY] = None


def setup(dispatcher: Any) -> MenuInterceptMiddleware:
    """Register the interception on updates, after the gate and the venue context."""
    middleware = MenuInterceptMiddleware()
    dispatcher.update.outer_middleware(middleware)
    return middleware


__all__ = [
    "RAW_STATE_KEY",
    "STATE_KEY",
    "MenuInterceptMiddleware",
    "interrupt_keyboard",
    "is_interrupt_question",
    "menu_action_of",
    "setup",
]
