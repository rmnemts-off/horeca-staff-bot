"""Bot entry point.

Stage 0, wave A: a stub that only reads the configuration and reports startup.
Dispatcher, routers and middlewares are added by the bot framework task (plan, task 20).
"""

from __future__ import annotations

import asyncio
import logging

from src.config import settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
logger = logging.getLogger("barpoint.bot")


async def main() -> None:
    logging.basicConfig(level=settings.log_level, format=LOG_FORMAT)
    settings.require("bot_token", "database_url", "redis_url")
    logger.info(
        "bot process started",
        extra={"app_env": settings.app_env, "owners": len(settings.owner_telegram_ids)},
    )


if __name__ == "__main__":
    asyncio.run(main())
