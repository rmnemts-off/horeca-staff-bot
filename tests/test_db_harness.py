"""The database harness must degrade to SKIP, never to an error (plan, task 5a).

`pytest` on a laptop without PostgreSQL has to be green, otherwise the guards that run
everywhere drown in red from the two tests that cannot run anywhere. Two checks:

* **Behavioural.** Every collected `@pytest.mark.db` item really carries a skip marker when
  the server does not answer. This inspects the live session, so it verifies the hook in
  `tests/conftest.py`, not a copy of its logic.
* **Structural.** A test that takes a database fixture but forgets `pytest.mark.db` is not
  merely untidy: the CI step that insists db tests really run selects them with
  `pytest -m db`, so an unmarked one silently never runs there, and the half of criterion
  11.5 it was written for goes unchecked. The marker is therefore required from anything
  that asks for a harness fixture — enforced by reading the test files, so the rule holds
  for tests that this run does not even collect.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.conftest import DB_MARKER, database_available

TESTS_DIR = Path(__file__).resolve().parent

#: Fixtures of `tests/conftest.py` that need a live server.
DATABASE_FIXTURES = frozenset({"database_url", "engine", "connection", "session"})
#: `empty_migrated_database` skips by itself, but the marker keeps the reporting uniform.
DATABASE_FIXTURES_SELF_SKIPPING = frozenset({"empty_migrated_database"})


def test_db_marked_tests_are_skipped_when_there_is_no_database(
    request: pytest.FixtureRequest,
) -> None:
    items = list(getattr(request.session, "items", []))
    marked = [item for item in items if item.get_closest_marker(DB_MARKER) is not None]
    if not marked:
        pytest.skip("no db-marked test was collected in this run")
    if database_available():
        pytest.skip("PostgreSQL answers, so the skip path is not the one under test")

    unskipped = [item.nodeid for item in marked if not list(item.iter_markers("skip"))]
    assert not unskipped, (
        "these tests need PostgreSQL and would have failed instead of skipping: "
        + ", ".join(unskipped)
    )
    reasons = {
        str(marker.kwargs.get("reason", ""))
        for item in marked
        for marker in item.iter_markers("skip")
    }
    assert any("needs PostgreSQL" in reason for reason in reasons), (
        f"the skip is not the harness's own; reasons seen: {sorted(reasons)}"
    )


def _test_modules() -> list[Path]:
    return sorted(
        path
        for path in TESTS_DIR.rglob("test_*.py")
        if "__pycache__" not in path.parts and path != Path(__file__)
    )


def _module_is_marked(tree: ast.Module) -> bool:
    """`pytestmark = pytest.mark.db` at module level, in any of its usual spellings."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets
        ):
            continue
        if any(
            isinstance(sub, ast.Attribute) and sub.attr == DB_MARKER for sub in ast.walk(node.value)
        ):
            return True
    return False


def _function_is_marked(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(sub, ast.Attribute) and sub.attr == DB_MARKER
        for decorator in node.decorator_list
        for sub in ast.walk(decorator)
    )


@pytest.mark.parametrize("path", _test_modules(), ids=lambda path: path.name)
def test_database_fixtures_are_only_used_under_the_db_marker(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    marked_module = _module_is_marked(tree)
    needed = DATABASE_FIXTURES | DATABASE_FIXTURES_SELF_SKIPPING

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        parameters = {argument.arg for argument in node.args.args}
        used = parameters & needed
        if used and not (marked_module or _function_is_marked(node)):
            offenders.append(f"{node.name} takes {sorted(used)}")

    assert not offenders, (
        f"{path.name}: a test that takes a database fixture must carry "
        f"@pytest.mark.{DB_MARKER}; without it `pytest -m {DB_MARKER}` in CI does not "
        "select the test and nobody notices it never ran: " + "; ".join(offenders)
    )


def test_the_marker_rule_has_teeth() -> None:
    """The structural rule must actually reject the mistake it exists for."""
    unmarked = ast.parse("async def test_x(session) -> None:\n    pass\n")
    marked = ast.parse("import pytest\npytestmark = pytest.mark.db\n")
    assert not _module_is_marked(unmarked)
    assert _module_is_marked(marked)
    function = next(node for node in ast.walk(unmarked) if isinstance(node, ast.AsyncFunctionDef))
    assert not _function_is_marked(function)
    assert {argument.arg for argument in function.args.args} & DATABASE_FIXTURES
