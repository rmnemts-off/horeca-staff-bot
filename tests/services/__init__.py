"""Service-layer tests: business logic against fake repositories, no database at all.

See `tests/__init__.py` for why the `__init__.py` files under `tests/` exist. Nothing in
this package carries `@pytest.mark.db`: the services of TZ section 5 are pure logic over
the repository contract (`src/db/repositories/protocols.py`), and testing them through
PostgreSQL would test the repositories instead.
"""
