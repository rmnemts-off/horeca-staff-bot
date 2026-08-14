"""The opening checklist as the employee sees it (TZ 5.4; plan task 24).

The central screen of the product, and the one with the least code in it. Everything that
decides anything lives elsewhere: `src/services/checklists.py` flips a line, refuses a
finished run and tells the manager; `src/bot/views/checklist.py` turns what it answered into
a text and a keyboard; `src/bot/safe_edit.py` puts that on the screen. What is left here is
the routing — which is the whole of TZ 3.2's rule about handlers, and the reason a web admin
panel can later press the same buttons.

**One run is one message, and it is edited in place** (TZ 5.4, 8.2). The `chat_id` and
`message_id` live on the run, not in the update: the shift section re-opens the checklist
from a completely different screen, and that press must rewrite the checklist's own message
rather than paint a checklist over the shift screen. So every draw goes through :func:`_draw`,
which reads the pair off the run and falls back to sending — and writing down — a new
message only when the run has none yet. That is also what happens when the employee swiped
the old one away: `safe_edit` sends a new one and calls back with its id (TZ 5.4 mentions
the case in so many words).

**A tap in a stale message changes nothing.** The service answers `RUN_FINISHED` /
`ALREADY_FINISHED`, and the handler shows a toast and edits *nothing*: the message that was
tapped may be an hour old and the run's real screen is somewhere else entirely. Overwriting
it would be the wrong-screen failure the whole callback scheme is built to avoid.

**Nobody is notified from here.** Finishing with lines unticked ends in
`services.checklists.complete(..., skip_comment=…)`, and the service puts a row into
`notifications` through `NotificationService` (TZ 6, plan task 19). There is no
`bot.send_message` to a manager in this module and there must never be one: a handler that
delivers its own notification has no retry, no dedup key and no record.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiogram import Bot, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot import texts
from src.bot.callbacks import (
    ChecklistFinish,
    ChecklistGroup,
    ChecklistShow,
    ChecklistSkipAccept,
    ChecklistToggle,
)
from src.bot.middlewares.auth import ACTOR_KEY
from src.bot.middlewares.resolver import PAYLOAD_KEY, SUBJECT_KEY
from src.bot.middlewares.services import SERVICES_KEY, VenueServices
from src.bot.safe_edit import CallbackAnswer, safe_edit
from src.bot.states import ChecklistSkip
from src.bot.views import Screen
from src.bot.views.checklist import (
    group_index_of,
    render_finished,
    render_run,
    render_skip_prompt,
    render_skip_question,
)
from src.db.models import ChecklistRun
from src.services.access import AccessContext
from src.services.checklists import ChecklistService, CompletionOutcome, ToggleOutcome
from src.services.timezones import utc_now

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "checklist"

#: Where the run being explained lives while `ChecklistSkip.comment` is open. The state,
#: not a button: the next thing the employee sends is a line of text, and a message carries
#: no payload (`src/bot/states/__init__.py`).
RUN_ID_KEY: Final = "run_id"


# --------------------------------------------------------------------------------------
# The screen (TZ 5.4, decision D11)
# --------------------------------------------------------------------------------------


async def show(event: CallbackQuery, bot: Bot, **data: Any) -> None:
    """Re-open the checklist from the shift screen, or come back to it from the question.

    TZ 5.4 in so many words: the employee may open the current checklist at any moment from
    the shift section, in case he swiped the message away. The word is *open*, not *send* —
    the run already has a message and this press edits it, which is why the ids come off the
    run and not off the update that arrived.
    """
    payload: ChecklistShow = data[PAYLOAD_KEY]
    services: VenueServices = data[SERVICES_KEY]

    view = await services.checklists.view(payload.run_id)
    if view is None:
        await event.answer(texts.ERROR_OUTDATED_SCREEN, show_alert=True)
        return
    if view.is_finished:
        await event.answer(texts.CHECKLIST_ALREADY_FINISHED, show_alert=True)
        return

    await _draw(
        bot=bot,
        checklists=services.checklists,
        run_id=view.run_id,
        screen=render_run(view),
        chat_id=view.chat_id,
        message_id=view.message_id,
        here=_chat_of(event),
        answer=event.answer,
    )


async def switch_group(event: CallbackQuery, bot: Bot, **data: Any) -> None:
    """Move the markers to another group without leaving the message (decision D11)."""
    payload: ChecklistGroup = data[PAYLOAD_KEY]
    services: VenueServices = data[SERVICES_KEY]

    view = await services.checklists.view(payload.run_id)
    if view is None:
        await event.answer(texts.ERROR_OUTDATED_SCREEN, show_alert=True)
        return
    if view.is_finished:
        await event.answer(texts.CHECKLIST_ALREADY_FINISHED, show_alert=True)
        return

    await _draw(
        bot=bot,
        checklists=services.checklists,
        run_id=view.run_id,
        screen=render_run(view, group_index=payload.group_index),
        chat_id=view.chat_id,
        message_id=view.message_id,
        here=_chat_of(event),
        answer=event.answer,
    )


async def toggle(event: CallbackQuery, bot: Bot, **data: Any) -> None:
    """`[ ]` -> `[✓]` on one line, and redraw the message (TZ 5.4).

    The counter is never computed here. `ToggleResult.view` is rebuilt from
    `checklist_run_items` after the flip, so two taps that raced produce one flip and one
    count; a handler that added one to a number it had drawn earlier would produce two.
    """
    payload: ChecklistToggle = data[PAYLOAD_KEY]
    services: VenueServices = data[SERVICES_KEY]

    result = await services.checklists.toggle(
        run_id=payload.run_id,
        item_id=payload.item_id,
        moment=utc_now(),
    )
    if result.outcome is ToggleOutcome.RUN_FINISHED:
        # A press inside a message the run has moved on from: nothing changed, and the
        # screen it was pressed on is not ours to rewrite.
        await event.answer(texts.CHECKLIST_ALREADY_FINISHED, show_alert=True)
        return
    view = result.view
    if view is None:
        await event.answer(texts.ERROR_OUTDATED_SCREEN, show_alert=True)
        return

    await _draw(
        bot=bot,
        checklists=services.checklists,
        run_id=view.run_id,
        screen=render_run(view, group_index=group_index_of(view, payload.item_id)),
        chat_id=view.chat_id,
        message_id=view.message_id,
        here=_chat_of(event),
        answer=event.answer,
    )


# --------------------------------------------------------------------------------------
# Finishing (TZ 5.4)
# --------------------------------------------------------------------------------------


async def finish(event: CallbackQuery, bot: Bot, **data: Any) -> None:
    """The finish button: either the shift is open, or the bot asks what was left.

    Both endings are the service's answer and neither is decided here — which lines count
    as pending, whether a reason is needed and whether the run had already been finished by
    a second tap are all `complete()`'s to say (TZ 5.4).
    """
    payload: ChecklistFinish = data[PAYLOAD_KEY]
    run: ChecklistRun = data[SUBJECT_KEY]
    actor: AccessContext = data[ACTOR_KEY]
    services: VenueServices = data[SERVICES_KEY]

    result = await services.checklists.complete(
        run_id=payload.run_id,
        completed_by=actor.user_id,
        moment=utc_now(),
    )
    if result.outcome is CompletionOutcome.NOT_FOUND:
        await event.answer(texts.ERROR_OUTDATED_SCREEN, show_alert=True)
        return
    if result.outcome is CompletionOutcome.ALREADY_FINISHED:
        await event.answer(texts.CHECKLIST_ALREADY_FINISHED, show_alert=True)
        return

    if result.outcome is CompletionOutcome.NEEDS_REASON:
        screen = render_skip_question(payload.run_id, result.pending)
    else:
        screen = render_finished(
            run.type,
            skipped=result.outcome is CompletionOutcome.SKIPPED,
        )
    await _draw(
        bot=bot,
        checklists=services.checklists,
        run_id=payload.run_id,
        screen=screen,
        chat_id=run.chat_id,
        message_id=run.message_id,
        here=_chat_of(event),
        answer=event.answer,
    )


async def accept_skip(
    event: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    **data: Any,
) -> None:
    """Send it as it is — TZ 5.4 asks for the reason before it sends anything."""
    payload: ChecklistSkipAccept = data[PAYLOAD_KEY]
    run: ChecklistRun = data[SUBJECT_KEY]
    services: VenueServices = data[SERVICES_KEY]

    if run.completed_at is not None:
        # The question is still on somebody's screen, the run is not. Asking for a reason
        # nobody can use would be worse than saying so.
        await event.answer(texts.CHECKLIST_ALREADY_FINISHED, show_alert=True)
        return

    await state.set_state(ChecklistSkip.comment)
    await state.update_data({RUN_ID_KEY: payload.run_id})
    await _draw(
        bot=bot,
        checklists=services.checklists,
        run_id=payload.run_id,
        screen=render_skip_prompt(),
        chat_id=run.chat_id,
        message_id=run.message_id,
        here=_chat_of(event),
        answer=event.answer,
    )


async def skip_comment(
    event: Message,
    bot: Bot,
    state: FSMContext,
    **data: Any,
) -> None:
    """The reason, typed. The manager hears about it from the service, not from here.

    `complete()` writes the run, builds the alert with the critical lines first and hands it
    to `NotificationService`; all this handler does afterwards is close the checklist's own
    message. The run id comes out of the FSM state, and it is safe there for the same reason
    it is safe in a button: the services of this update are scoped to the actor's venue, so
    a run id left over from anywhere else is simply not found.
    """
    services: VenueServices = data[SERVICES_KEY]
    actor: AccessContext = data[ACTOR_KEY]

    stored = await state.get_data()
    run_id = stored.get(RUN_ID_KEY)
    reason = event.text
    if not isinstance(run_id, int) or reason is None:
        # A photo, a sticker or a voice message where a line of text was asked for. TZ 8.2:
        # ask again in the same words rather than explain the mistake.
        await event.answer(texts.CHECKLIST_SKIP_COMMENT_PROMPT)
        return

    result = await services.checklists.complete(
        run_id=run_id,
        completed_by=actor.user_id,
        moment=utc_now(),
        skip_comment=reason,
    )
    if result.outcome is CompletionOutcome.NEEDS_REASON:
        # Spaces are not a reason (`_clean` in the service). Still waiting for one.
        await event.answer(texts.CHECKLIST_SKIP_COMMENT_PROMPT)
        return

    await state.clear()
    run = result.run
    if result.outcome is CompletionOutcome.NOT_FOUND or run is None:
        await event.answer(texts.ERROR_OUTDATED_SCREEN)
        return
    if result.outcome is CompletionOutcome.ALREADY_FINISHED:
        await event.answer(texts.CHECKLIST_ALREADY_FINISHED)
        return

    await _draw(
        bot=bot,
        checklists=services.checklists,
        run_id=run_id,
        screen=render_finished(
            run.type,
            skipped=result.outcome is CompletionOutcome.SKIPPED,
        ),
        chat_id=run.chat_id,
        message_id=run.message_id,
        here=event.chat.id,
    )


# --------------------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------------------


async def _draw(
    *,
    bot: Bot,
    checklists: ChecklistService,
    run_id: int,
    screen: Screen,
    chat_id: int | None,
    message_id: int | None,
    here: int | None,
    answer: Callable[..., Awaitable[object]] | None = None,
    notice: str | None = None,
) -> None:
    """Put a screen where this run's message is, and remember where that turned out to be.

    Three cases, and only the first is ordinary:

    * the run knows its message — edit it (TZ 8.2: one message, not forty);
    * the run has no message yet — the checklist was created but has not been delivered, and
      the employee got here through the shift screen first. Send it and write the id down, so
      the delivery that follows edits this message instead of opening a second checklist;
    * there is nowhere to draw at all (a callback with no message behind it). Close the
      spinner and stop — TZ 9 gives an action one second to answer, and an unanswered press
      reads as a broken bot.

    The spinner is closed in a `finally` for the same reason `safe_edit` does it: the answer
    is the consolation, not the work, and it is owed even when the work failed.
    """

    async def remember(new_chat_id: int, new_message_id: int) -> None:
        await checklists.remember_message(
            run_id=run_id,
            chat_id=new_chat_id,
            message_id=new_message_id,
        )

    spinner = CallbackAnswer(answer)
    try:
        if chat_id is not None and message_id is not None:
            await safe_edit(
                bot,
                chat_id=chat_id,
                message_id=message_id,
                text=screen.text,
                reply_markup=screen.markup,
                remember=remember,
            )
            return
        if here is None:
            return
        sent = await bot.send_message(
            chat_id=here,
            text=screen.text,
            reply_markup=screen.markup,
        )
        await remember(here, sent.message_id)
    finally:
        await spinner.close(notice)


def _chat_of(event: CallbackQuery) -> int | None:
    """The chat a press came from, used only when the run has no message of its own."""
    screen = event.message
    return None if screen is None else screen.chat.id


def router() -> Router:
    """The screens of TZ 5.4 (see the module docstring).

    Registration is by factory, never by a raw prefix: the resolver has already parsed the
    payload, fetched the run through this venue's repository and checked that the presser
    may touch it (TZ 9).
    """
    instance = Router(name=ROUTER_NAME)
    instance.callback_query.register(show, ChecklistShow.filter())
    instance.callback_query.register(switch_group, ChecklistGroup.filter())
    instance.callback_query.register(toggle, ChecklistToggle.filter())
    instance.callback_query.register(finish, ChecklistFinish.filter())
    instance.callback_query.register(accept_skip, ChecklistSkipAccept.filter())
    instance.message.register(skip_comment, StateFilter(ChecklistSkip.comment))
    return instance


__all__ = [
    "ROUTER_NAME",
    "RUN_ID_KEY",
    "accept_skip",
    "finish",
    "router",
    "show",
    "skip_comment",
    "switch_group",
    "toggle",
]
