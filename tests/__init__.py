"""Test package.

The `__init__.py` files under `tests/` are not decoration. With pytest's default
`--import-mode=prepend` a test module is imported under its *basename*, so
`tests/db/test_metadata.py` and a future `tests/services/test_metadata.py` would both
become the module `test_metadata` and the run would die with "import file mismatch".
Packages give every module a full dotted name and, as a side effect, make the shared
helper `tests.repo_scan` importable from the static guards.
"""
