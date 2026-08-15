"""The rules every inline answer of this bot obeys (part IV of the stage 1 spec, TZ 5.5).

Inline mode is the one screen of this product that Telegram caches on its own servers and
draws without asking us again, so two of its arguments are not preferences. They are here,
in one function, because every place that answers an inline query — the handler and the
four middlewares that refuse one — has to set both, and a refusal that forgot them would be
the leak the handler is careful about.

**`is_personal=True`.** Without it Telegram caches the answer *by the text of the query, for
everybody at once*. A bartender of venue B typing a drink's name would then be handed venue
A's recipe out of the cache, without a single query reaching our database — a leak between
venues that goes around every repository predicate, and one that no test of the repository
layer can see (TZ 3.3, acceptance 11.3).

**`cache_time=0`.** TZ 5.5 promises that an edited recipe is immediately available to the
whole shift. Any other value is exactly the window in which it is not.

**A refusal is an answer.** An inline query that is never answered leaves the client
loading until it gives up — the bartender sees a bot that hangs, not a bot that said no. So
the gate, the venue context, the limiter and the error middleware all end here, with an
empty list, which is also all a stranger may learn (TZ 5.1: no hint of what this bot does).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from aiogram.types import InlineQuery, InlineQueryResultsButton
from aiogram.types.inline_query_result_union import InlineQueryResultUnion

#: Seconds Telegram may hold an answer. Zero, and see the module docstring for why.
INLINE_CACHE_SECONDS: Final = 0

#: What `next_offset` says when there is no next page: an empty string, which is Telegram's
#: own way of spelling "that was all" (a missing value would mean "no pagination at all",
#: and the client would keep asking).
NO_MORE_RESULTS: Final = ""


async def answer(
    query: InlineQuery,
    results: Sequence[InlineQueryResultUnion] = (),
    *,
    next_offset: str = NO_MORE_RESULTS,
    button: InlineQueryResultsButton | None = None,
) -> None:
    """Answer one inline query under both rules of the module docstring."""
    await query.answer(
        list(results),
        cache_time=INLINE_CACHE_SECONDS,
        is_personal=True,
        next_offset=next_offset,
        button=button,
    )


async def answer_nothing(
    query: InlineQuery,
    *,
    button: InlineQueryResultsButton | None = None,
) -> None:
    """Close a query this bot has nothing to say to (TZ 9: always answer something).

    Empty rather than an explanation: whoever is asking may be a stranger, and TZ 5.1 gives
    a stranger no hint of what the bot can do. Where there *is* something to say — a member
    typing in the wrong chat — the caller passes the button that says it.
    """
    await answer(query, (), button=button)


__all__ = ["INLINE_CACHE_SECONDS", "NO_MORE_RESULTS", "answer", "answer_nothing"]
