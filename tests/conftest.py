"""Database test harness (plan, block A, task 5a).

Owner of this file is task 5a; other tasks only add factories to `tests/factories.py`.

Three guarantees the rest of the suite relies on:

* **`pytest` without a database is green.** Everything that needs PostgreSQL carries
  `@pytest.mark.db` and is skipped — not errored — when `DATABASE_URL` is missing or the
  server does not answer. The probe runs once per session.
* **One throwaway database per run.** `CREATE DATABASE barpoint_test_<random>`, then
  `alembic upgrade head` on it, then `DROP DATABASE` at the end. The developer's own
  database is never migrated or truncated by the suite.
* **One transaction per test, always rolled back.** The `session` fixture is bound to a
  connection with an open outer transaction; `session.commit()` inside a test lands on a
  savepoint, and the outer transaction is rolled back afterwards. Tests therefore never
  see each other's rows and never leave any behind.

Alembic is invoked in-process, but always on a separate thread: `alembic/env.py` calls
`asyncio.run()` for the async engine, which explodes if a loop is already running in the
same thread (and under pytest-asyncio one usually is).
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from src.config import settings

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Marker every test that needs PostgreSQL must carry; registered in pyproject.toml.
DB_MARKER = "db"

#: Set to any non-empty value to skip the database tests without even probing the server.
SKIP_DB_ENV_FLAG = "BARPOINT_SKIP_DB_TESTS"

#: Maintenance database used to issue CREATE/DROP DATABASE.
MAINTENANCE_DATABASE = "postgres"


# --------------------------------------------------------------------------------------
# Running async code from synchronous fixture code
# --------------------------------------------------------------------------------------


def _in_new_thread[T](work: Callable[[], T]) -> T:
    """Run `work` on a fresh thread and re-raise whatever it raised.

    Both `asyncio.run` and Alembic's `command.upgrade` need a thread with no running
    event loop; pytest-asyncio does not always give us one.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = work()
        except BaseException as error:
            box["error"] = error

    thread = threading.Thread(target=target, name="barpoint-test-db", daemon=True)
    thread.start()
    thread.join()

    if "error" in box:
        raise box["error"]
    return box["value"]  # type: ignore[no-any-return]


def _run_async[T](factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return _in_new_thread(lambda: asyncio.run(factory()))


# --------------------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------------------


def _configured_url() -> URL | None:
    """DATABASE_URL, normalised to the async driver; None when it is not configured."""
    raw = os.environ.get("DATABASE_URL", "").strip() or settings.database_url
    if not raw:
        return None
    url = make_url(raw)
    if url.get_backend_name() != "postgresql":
        return None
    # The harness always talks asyncpg, whatever driver the .env names.
    return url.set(drivername="postgresql+asyncpg")


def _render(url: URL) -> str:
    # URL.__str__ masks the password; the engine needs the real one.
    return url.render_as_string(hide_password=False)


# --------------------------------------------------------------------------------------
# Availability probe
# --------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def database_available() -> bool:
    """True when a PostgreSQL server actually answers. Probed once per session."""
    if os.environ.get(SKIP_DB_ENV_FLAG, "").strip():
        return False
    url = _configured_url()
    if url is None:
        return False

    async def probe() -> None:
        engine = create_async_engine(
            _render(url.set(database=MAINTENANCE_DATABASE)),
            poolclass=NullPool,
            connect_args={"timeout": 5},
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        _run_async(probe)
    except Exception:
        return False
    return True


def _skip_reason() -> str:
    if os.environ.get(SKIP_DB_ENV_FLAG, "").strip():
        return f"{SKIP_DB_ENV_FLAG} is set"
    if _configured_url() is None:
        return "DATABASE_URL is not set to a PostgreSQL URL"
    return "PostgreSQL at DATABASE_URL is not reachable"


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip every `@pytest.mark.db` test when there is no database to talk to.

    Matching is on the marker, not on `item.keywords`: keywords also contain the names of
    the enclosing directories, and `tests/db/` would drag the (database-free) metadata
    tests into the skip.
    """
    if database_available():
        return
    skip = pytest.mark.skip(reason=f"needs PostgreSQL: {_skip_reason()}")
    for item in items:
        if item.get_closest_marker(DB_MARKER) is not None:
            item.add_marker(skip)


# --------------------------------------------------------------------------------------
# Throwaway database + migrations
# --------------------------------------------------------------------------------------


def _alembic_config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    # Passed through `attributes`, not `sqlalchemy.url`: the latter goes through
    # ConfigParser interpolation and would break on a '%' inside a password.
    config.attributes["database_url"] = url
    config.attributes["configure_logging"] = False
    return config


def upgrade_to_head(url: str) -> None:
    """`alembic upgrade head` against `url`, safe to call from inside an event loop."""
    _in_new_thread(lambda: command.upgrade(_alembic_config(url), "head"))


def _create_scratch_database() -> tuple[str, Callable[[], None]]:
    """Create an empty database; return its URL and a callable that drops it."""
    base = _configured_url()
    assert base is not None, "database_available() must be checked first"
    name = f"barpoint_test_{uuid.uuid4().hex[:12]}"
    maintenance = _render(base.set(database=MAINTENANCE_DATABASE))

    async def execute(statement: str) -> None:
        engine = create_async_engine(maintenance, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                await connection.execute(text(statement))
        finally:
            await engine.dispose()

    _run_async(lambda: execute(f'CREATE DATABASE "{name}"'))

    def drop() -> None:
        # FORCE (PostgreSQL 13+) kicks out connections a failed test may have left open.
        _run_async(lambda: execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))

    return _render(base.set(database=name)), drop


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """One migrated throwaway database for the whole run."""
    if not database_available():
        pytest.skip(f"needs PostgreSQL: {_skip_reason()}")

    url, drop = _create_scratch_database()
    try:
        upgrade_to_head(url)
        yield url
    finally:
        drop()


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """Per-test engine.

    NullPool and function scope on purpose: pytest-asyncio gives every test its own event
    loop, and an asyncpg pool must not outlive the loop its connections were opened on.
    """
    created = create_async_engine(database_url, poolclass=NullPool)
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Connection with an outer transaction that is always rolled back."""
    async with engine.connect() as opened:
        transaction = await opened.begin()
        try:
            yield opened
        finally:
            if transaction.is_active:
                await transaction.rollback()


@pytest_asyncio.fixture
async def session(connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """Session whose writes never survive the test.

    `join_transaction_mode="create_savepoint"` lets the code under test call `commit()`
    normally — it lands on a savepoint inside the outer transaction, which is then rolled
    back. Service-layer code therefore needs no test-only branches.
    """
    factory = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    async with factory() as opened:
        yield opened


@pytest_asyncio.fixture
async def empty_migrated_database() -> AsyncIterator[AsyncEngine]:
    """A database with nothing but `alembic upgrade head` behind it (criterion 11.5).

    Deliberately separate from the shared `database_url`: the emptiness check must not
    depend on which tests ran before it, nor on their rollback discipline.
    """
    if not database_available():
        pytest.skip(f"needs PostgreSQL: {_skip_reason()}")

    url, drop = _create_scratch_database()
    try:
        await asyncio.to_thread(upgrade_to_head, url)
        created = create_async_engine(url, poolclass=NullPool)
        try:
            yield created
        finally:
            await created.dispose()
    finally:
        await asyncio.to_thread(drop)
