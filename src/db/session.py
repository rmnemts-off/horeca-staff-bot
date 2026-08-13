"""Async engine and session factory.

Both are lazy on purpose: importing the data layer must not require a live DATABASE_URL,
otherwise static tooling and unit tests cannot import the package.

The engine is kept in a module-level variable rather than behind `lru_cache`, because
shutdown must be able to tell "never created" from "created": a shutdown hook that builds
an engine in order to close it would ask for DATABASE_URL at the worst possible moment and
mask the original failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Process-wide async engine (postgresql+asyncpg), created on first use."""
    global _engine
    if _engine is None:
        settings.require("database_url")
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=10,
            echo=False,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Session factory; `expire_on_commit=False` keeps objects usable after commit."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One transaction per unit of work: commit on success, rollback on error."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


async def dispose_engine() -> None:
    """Close the pool on shutdown; a no-op when no engine was ever created."""
    global _engine, _sessionmaker
    engine, _engine, _sessionmaker = _engine, None, None
    if engine is not None:
        await engine.dispose()
