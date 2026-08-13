"""What "the delivery" is, for the two static guards that police it.

`tests/test_no_seed_data.py` and `tests/test_static_repos.py` both have to answer the same
question first: which files ship? Answering it in one place keeps the two guards from
drifting apart, and keeps the answer honest — the scope is *the whole repository* minus a
short, explicit list of directories that are provably not part of a delivery:

* `tests/` — fixtures live here by design (CLAUDE.md), and the guards themselves live here;
* `docs/` — prose, including the TZ, quoted in Russian;
* `.git/`, `.venv/`, tool caches, build artefacts — not source at all.

Everything else is in scope, including `alembic/`, `docker/`, `.github/` and any script
someone drops in the root. A file that is not scanned is a file where content can hide,
which is exactly how the previous version of these guards was fooled.
"""

from __future__ import annotations

import ast
import tokenize
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Directory names skipped at any depth: not source, or generated.
NON_SOURCE_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        "build",
        "dist",
        "htmlcov",
        ".idea",
        ".vscode",
    }
)

#: Top-level directories deliberately outside the guards' reach, with the reason.
EXCLUDED_TOP_LEVEL: dict[str, str] = {
    "tests": "fixtures and the guards themselves; never executed in production",
    "docs": "prose, including the Russian text of the TZ",
}

SRC_DIR = REPO_ROOT / "src"
TEXTS_DIR = SRC_DIR / "bot" / "texts"
VERSIONS_DIR = REPO_ROOT / "alembic" / "versions"
REPOSITORIES_DIR = SRC_DIR / "db" / "repositories"


def relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def is_scanned(path: Path) -> bool:
    """True when `path` is part of what the project ships."""
    try:
        parts = path.relative_to(REPO_ROOT).parts
    except ValueError:
        return False
    if not parts:
        return False
    if parts[0] in EXCLUDED_TOP_LEVEL:
        return False
    if any(part in NON_SOURCE_DIRS for part in parts):
        return False
    return not any(part.endswith(".egg-info") for part in parts[:-1])


@lru_cache(maxsize=1)
def scanned_files() -> tuple[Path, ...]:
    """Every shipped file of the repository, in a stable order."""
    return tuple(
        sorted(path for path in REPO_ROOT.rglob("*") if path.is_file() and is_scanned(path))
    )


@lru_cache(maxsize=1)
def scanned_python_files() -> tuple[Path, ...]:
    return tuple(path for path in scanned_files() if path.suffix == ".py")


def python_files(root: Path) -> list[Path]:
    """Python files under `root` (which may itself be outside the scanned scope)."""
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse(path: Path) -> ast.Module:
    return ast.parse(read(path), filename=str(path))


def comments(source: str) -> list[tuple[int, str]]:
    """Real comments only, via `tokenize`.

    A marker looked for with `"# marker" in source_text` is not a comment check: the same
    characters inside a string literal, or inside the source line of an unrelated
    statement, would satisfy it. That is exactly how the venue-exemption marker used to be
    forged, so the guards ask the tokenizer instead.
    """
    found: list[tuple[int, str]] = []
    lines = source.splitlines(keepends=True)
    position = iter(lines)
    for token in tokenize.generate_tokens(lambda: next(position, "")):
        if token.type == tokenize.COMMENT:
            found.append((token.start[0], token.string))
    return found


def docstring_nodes(tree: ast.Module) -> set[int]:
    """`id()` of every string constant that is a docstring.

    Docstrings are prose about the code; the guards that look for SQL or for content
    exclude them, so that a sentence in English cannot fail a rule about statements.
    """
    marked: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            marked.add(id(first.value))
    return marked
