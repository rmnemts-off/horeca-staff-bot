"""Bot entry point: configuration, storage, dispatcher, long polling.

Nothing is decided here. The secrets come from the environment and are checked before
anything connects (TZ 9); the assembly is `src/bot/dispatcher.py`; what happens to an
update is `src/bot/middlewares/` and the routers. This file is the order of the four, plus
the shutdown that closes what it opened.

The screens themselves are `src/bot/handlers/`, one module per functional block, and the
order they are consulted in is decided there.

**The notification worker is not started here.** It is a second process
(`python -m src.scheduler`), and that is a requirement rather than a deployment taste:
acceptance criterion 11.4 asks for the scheduler to be restarted on its own and for the
notification to arrive exactly once anyway, which cannot be shown if restarting it means
restarting the bot.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot

from src.bot.dispatcher import build_bot, build_dispatcher, build_storage
from src.config import settings
from src.db.session import dispose_engine
from src.logging import configure_logging, get_logger

logger = get_logger("bot")


async def check_inline_mode(bot: Bot) -> bool:
    """Say so in the log when inline mode is off at @BotFather (part IV of the stage 1 spec).

    Inline mode is the one feature of this bot that **cannot be turned on from the code**:
    until `/setinline` is completed for this token, Telegram never sends an `inline_query`
    at all. The symptom is a dropdown that stays empty, which is indistinguishable from a
    search that finds nothing and from a handler that was never wired up — and it cost an
    evening of looking for a bug in a search that was working the whole time.

    `getMe` answers it outright, so the process says which of the two it is on every start.
    A refusal to answer is not a reason to stay down: the bot is perfectly usable without
    inline, so a failure here is logged and polling begins anyway.
    """
    try:
        me = await bot.get_me()
    except Exception:
        logger.warning("could not ask Telegram whether inline mode is on", exc_info=True)
        return False
    if not me.supports_inline_queries:
        logger.warning(
            "inline mode is OFF for this token: Telegram will not send inline_query at all. "
            "Enable it once in BotFather: /setinline, pick the bot, send the placeholder text"
        )
    return bool(me.supports_inline_queries)


async def run() -> None:
    """Start polling and keep polling until the process is asked to stop."""
    settings.require("bot_token", "database_url", "redis_url")

    bot = build_bot(settings.bot_token)
    storage = build_storage(settings.redis_url)
    dispatcher = build_dispatcher(
        storage=storage,
        # Decision A3: a one-off trigger for the "create a venue" wizard, never a right.
        bootstrap_owner_ids=frozenset(settings.owner_telegram_ids),
    )

    logger.info(
        "bot process started",
        extra={"app_env": settings.app_env, "item_count": len(settings.owner_telegram_ids)},
    )
    await check_inline_mode(bot)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await storage.close()
        await bot.session.close()
        await dispose_engine()
        logger.info("bot process stopped")


async def main() -> None:
    configure_logging()
    await run()


if __name__ == "__main__":
    asyncio.run(main())
