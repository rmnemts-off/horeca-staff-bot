"""Scheduler entry point.

Stage 0, wave A: a stub that only reads the configuration and reports startup.
The polling loop over the notifications table is added by the scheduler task (plan, task 31).
"""

from __future__ import annotations

import asyncio
import logging

from src.config import settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
logger = logging.getLogger("barpoint.scheduler")


async def main() -> None:
    logging.basicConfig(level=settings.log_level, format=LOG_FORMAT)
    settings.require("bot_token", "database_url")
    logger.info("scheduler process started", extra={"app_env": settings.app_env})


if __name__ == "__main__":
    asyncio.run(main())
