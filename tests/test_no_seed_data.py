"""Static half of acceptance criterion 11.5: the system ships empty (TZ 1.4#6).

Threat model — read this before adding a rule
---------------------------------------------

**What this file defends against is a mistake, not an adversary.** Nobody smuggles a
catalogue into this product on purpose; what happens is that someone needs the twelve
write-off reasons every bar has, and puts them somewhere reasonable-looking — a JSON next
to the models, a `server_default` on a column, a tuple of ORM objects at the top of a
module, an extra service in compose that runs a one-off script. Each rule below exists for
one such reasonable-looking place, and each is tuned to stay silent on the configuration
and the schema vocabulary that legitimately ship.

It is **not** a sandbox. The code being checked and the checker live in the same
repository; whoever can edit one can edit the other, and matching on obfuscation would buy
nothing while costing false positives on ordinary code. Known and deliberately **not**
closed:

* a keyword split across literals — `op.execute("INSER" "T INTO writeoff_reasons …")`;
* content arriving base64-decoded, `chr()`-assembled or joined at runtime out of pieces;
* wording in `src/bot/texts/` spelled as separately named constants or class attributes —
  three named phrases there are indistinguishable from three interface messages;
* content written as identifiers — `["bar_top", "coffee_machine", "ice_bin"]` reads as
  machinery to rule 3 and passes. It also reads as machinery to a bartender, which is why
  the trade-off is accepted: the rule is tuned not to fire on parameter names and
  naming-convention keys, which have exactly that shape;
* a value in `.env.example` or another file the delivery does not treat as data.

**Where the real guarantee comes from.** Criterion 11.5 is proved on a live database by
`tests/test_empty_delivery.py`: a fresh database, `alembic upgrade head` and nothing else,
then every table discovered from `information_schema` — never a hard-coded list — must
hold zero rows. That test is marked `db`; CI runs it against a real PostgreSQL and fails
the build if it skips. This file is the cheap guard that runs everywhere and always,
including on a laptop with no database: it catches the slip before it reaches the live
test, and it covers the places the live test cannot see — a default that only fires on the
first insert, a script nothing runs yet, a compose service nobody started. Green here is
not proof that the delivery is empty.

Runs without a database, so it is the guard that always fires. The previous version fired
at three words in one directory and could be walked around in half a dozen ways; every one
of those ways is now a rule, and every rule is asserted against the trick it exists for
(the `*_BYPASSES` lists at the bottom — the guard tests its own teeth).

Scope is the whole repository minus `tests/` and `docs/` (see `tests/repo_scan.py`).
`src/` and `alembic/versions/` were not enough: a content list in `scripts/seed.py` shipped
green, and `docker/entrypoint.sh` runs whatever CMD says.

The rules:

1. **A migration creates structure, never rows.** No `INSERT`/`bulk_insert`/`COPY … FROM`/
   `MERGE INTO`/`executemany`; no reading of external files; nothing but `.py` in
   `alembic/versions/`; every `server_default` a number, a boolean, `{}`, `now()` or a
   value of an enum the models declare; every enum type created by a migration present in
   the models with exactly the same values. A default is not a harmless default: it stamps
   its content onto every row the venue will ever create.

   "A migration" is `alembic/versions/*.py` **plus `alembic/env.py`**, which executes on
   every single `alembic` invocation, **plus `alembic/script.py.mako`**, which is the file
   the next migration is copied from — a line added there is a line in every future
   migration. The template is read as Python with its `${…}` placeholders neutralised.
   The `server_default` rule additionally reads `src/db/models/**`: the models are where
   the schema is written by hand and the migration only carries out what they say.

2. **No Russian string literal outside `src/bot/texts/`** (TZ 8.2) — now including
   migrations, scripts and anything else that ships.

3. **No content constant anywhere, in any language.** Two independent tests: a name that
   announces a catalogue (`DEFAULT_*`, `*_ITEMS`, `*_REASONS`, `*_CATEGORIES`, …) holding
   string data, and a collection of three or more strings that are not schema vocabulary.
   Inside `src/bot/texts/` the second test is stricter still: interface wording is a set of
   named constants, and a list of three phrases there is a checklist, not a message.

   And no **ORM object is built while a module is imported** anywhere in `src/`:
   `_PRELOADED = (WriteoffReason(name="…"), …)` is a reference book whose elements are
   calls rather than string literals, which made it invisible to both tests above.

4. **A structured file in the delivery is registered configuration, or it is content.**
   `.json`, `.yaml`, `.toml`, `.ini`, `.txt` are configuration in one file and a reference
   book in the next, and nothing about the file itself tells the two apart — so the answer
   is a closed list of names (`CONFIGURATION_FILES`), and what is on it is scanned for
   content anyway. `.sql`, `.csv`, `.xlsx` and their kin are refused outright.

5. **The commands that run in the image are a closed list**, `command:` and `entrypoint:`
   alike — Docker gives the second priority over the image's CMD. An entry point that can
   be pointed at `python scripts/seed.py` makes every rule above decorative.

6. **`src/` never reaches into `tests/`** (decision D13), and `tests/` stays out of the
   image.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from collections.abc import Iterable
from fnmatch import fnmatch
from functools import lru_cache
from pathlib import Path

import pytest

from tests.repo_scan import (
    REPO_ROOT,
    SRC_DIR,
    TEXTS_DIR,
    VERSIONS_DIR,
    docstring_nodes,
    read,
    relative,
    scanned_files,
    scanned_python_files,
)

# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------

#: Row-writing statements. `\binsert\b` does not match inside `bulk_insert` (underscore is
#: a word character), so both spellings are listed; `COPY … FROM` and `MERGE INTO` are the
#: two ways of loading rows that carry no INSERT at all.
DATA_STATEMENTS = re.compile(
    r"\binsert\b|bulk_insert|\bcopy\b[\s\S]*?\bfrom\b|\bmerge\s+into\b|\bexecutemany\b"
    r"|\bupdate\b[\s\S]*?\bset\b",
    re.IGNORECASE,
)

CYRILLIC = re.compile(r"[Ѐ-ӿ]")

#: Names that announce a catalogue. Matched on UPPER_SNAKE constants only: a lowercase
#: local `items` is a variable, an uppercase `DEFAULT_ITEMS` is a shipped list.
CONTENT_NAME = re.compile(
    r"(?:^|_)(?:DEFAULT|DEFAULTS|SEED|SEEDS|SAMPLE|SAMPLES|PRESET|PRESETS|FIXTURE|FIXTURES"
    r"|ITEM|ITEMS|REASON|REASONS|CATEGORY|CATEGORIES|RECIPE|RECIPES|PRODUCT|PRODUCTS"
    r"|SUPPLIER|SUPPLIERS|INGREDIENT|INGREDIENTS|NOMENCLATURE)(?:_|$)"
)
CONSTANT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: A string that is machinery rather than something a bartender could read: a parameter
#: name, a naming-convention key, a format template. Venue content is human-readable —
#: "Включить кофемашину", "Whiskey", "staff drink" — and never looks like this.
IDENTIFIER_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TEMPLATE_TOKEN = re.compile(r"[%{}]")

#: A `server_default` that is technique rather than content.
TECHNICAL_DEFAULT = re.compile(r"^(?:-?\d+(?:\.\d+)?|true|false|\{\}|\[\]|now\(\))$", re.IGNORECASE)

#: The same thing spelled as a call: `server_default=func.now()`. A clock is not a word.
TECHNICAL_DEFAULT_CALLS = frozenset(
    {"now", "current_timestamp", "clock_timestamp", "statement_timestamp", "localtimestamp"}
)

#: `${...}` in a Mako template. `alembic/script.py.mako` is the file every future migration
#: is copied from, and with the placeholders replaced it is ordinary Python, which is what
#: lets the migration rules read it at all.
MAKO_PLACEHOLDER = re.compile(r"\$\{[^}]*\}")

#: A catalogue joined into one literal: `"Whiskey,Gin,Vermouth".split(",")`. Comma
#: separated, no whitespace — SQL and prose have spaces, a packed list does not.
PACKED_LIST = re.compile(r"^[^\s,]+(?:,[^\s,]+){2,}$")

#: `CREATE TYPE x AS ENUM ('a', 'b')` written by hand in a migration.
CREATE_ENUM = re.compile(r"create\s+type\s+([\w.\"]+)\s+as\s+enum\s*\(([^)]*)\)", re.IGNORECASE)
QUOTED = re.compile(r"'([^']*)'")

#: Files that are data by their very extension. Fixtures live in `tests/`, which is not
#: scanned; anything of this kind inside the delivery is content, whatever it is called.
DATA_SUFFIXES = frozenset({".sql", ".csv", ".tsv", ".xlsx", ".xls", ".dump", ".sqlite", ".db"})

#: Structured formats that are configuration in one file and a reference book in the next.
#: `src/db/reference.json` with write-off reasons and checklist items, read by a loader in
#: `src/db/reference.py`, shipped green while the content rules read `.py` only — and
#: "let's put the defaults in a JSON" is the most natural way to arrive there.
STRUCTURED_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".txt"})

#: The configuration of this project, by name. Which of the two a structured file is
#: cannot be decided by looking at it — `{"reasons": [...]}` and a settings block have the
#: same shape — so it is decided by registration: these files are configuration, and a
#: structured file that is not one of them is a reference book until someone adds it here,
#: in the diff of this test. Adding a genuine configuration file is a one-line change;
#: shipping a catalogue then has to be argued for in review, which is the point.
CONFIGURATION_FILES = frozenset(
    {
        "pyproject.toml",
        "alembic.ini",
        "docker-compose.yml",
        "docker-compose.override.yml",
    }
)
CONFIGURATION_PATTERNS = (".github/workflows/*.yml", ".github/workflows/*.yaml")

#: A string that is machinery in a configuration file: a path, a flag, a version pin, an
#: image tag, a service name. Content is human-readable and has neither of these shapes —
#: "Бой бутылки", "Whiskey", "Espresso machine".
CONFIG_TOKEN = re.compile(r"^\S*[-./:=<>@\\]\S*$")

#: Reading anything from disk inside a migration means the rows live in a second file.
EXTERNAL_READ_CALLS = frozenset({"open", "read_text", "read_bytes", "read", "load", "loads"})
EXTERNAL_READ_IMPORTS = frozenset({"csv", "json", "pathlib", "openpyxl", "pickle", "yaml", "io"})

#: The complete list of commands the delivery may run (TZ 11.5: nothing seeds the base).
ALLOWED_SERVICE_COMMANDS = frozenset(
    {
        '["python", "-m", "src.bot"]',
        '["python", "-m", "src.scheduler"]',
        '["alembic", "current"]',
        '["redis-server", "--appendonly", "yes"]',
    }
)
ALLOWED_IMAGE_COMMANDS = frozenset(
    {
        'CMD ["python", "-m", "src.bot"]',
        'ENTRYPOINT ["/app/docker/entrypoint.sh"]',
    }
)
#: Programs the entry point may start. `exec "$@"` hands over to the CMD above, which is
#: itself on the closed list; anything else would be a second, unchecked way in.
ENTRYPOINT_PROGRAMS = frozenset({"alembic", "exec", "echo", "export", "eval", "set", "exit"})
#: Shell syntax, not programs.
SHELL_KEYWORDS = frozenset(
    {"if", "then", "else", "elif", "fi", "for", "in", "do", "done", "while", "case", "esac"}
)
COMMAND_NAME = re.compile(r"^[A-Za-z_./][\w.\-/]*$")
ENTRYPOINT_ALEMBIC_COMMAND = "alembic upgrade head"


# --------------------------------------------------------------------------------------
# Schema vocabulary: the words the code is allowed to say
# --------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def schema_vocabulary() -> frozenset[str]:
    """Table, column, index, constraint, enum-type and enum-value names.

    This is what tells a technical list from a catalogue: `["venue_id", "type"]` is the
    schema talking about itself, `["Whiskey", "Gin", "Vermouth"]` is a bar's nomenclature.
    Importing the models needs no database.
    """
    from src.db.models import Base

    words: set[str] = set()
    for name, table in Base.metadata.tables.items():
        words.add(name)
        for column in table.c:
            words.add(column.name)
            words.add(getattr(column.type, "name", "") or "")
            words.update(getattr(column.type, "enums", None) or ())
        words.update(index.name for index in table.indexes if isinstance(index.name, str))
        words.update(
            constraint.name for constraint in table.constraints if isinstance(constraint.name, str)
        )
    words.discard("")
    return frozenset(words)


@lru_cache(maxsize=1)
def schema_enums() -> dict[str, tuple[str, ...]]:
    """Enum type name -> its values, as the models declare them."""
    from src.db.models import Base

    found: dict[str, tuple[str, ...]] = {}
    for table in Base.metadata.tables.values():
        for column in table.c:
            name = getattr(column.type, "name", None)
            values = getattr(column.type, "enums", None)
            if isinstance(name, str) and values:
                found[name] = tuple(values)
    return found


def _local_names(tree: ast.Module) -> set[str]:
    """Everything this module itself names: a string naming one of them is a reference."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


# --------------------------------------------------------------------------------------
# Rule 1: migrations carry structure, never data
# --------------------------------------------------------------------------------------


def data_statements(source: str) -> list[str]:
    return [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(source.splitlines(), start=1)
        if DATA_STATEMENTS.search(line)
    ]


def external_reads(source: str, filename: str = "<probe>") -> list[str]:
    tree = ast.parse(source, filename=filename)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "__file__":
            offenders.append(f"line {node.lineno}: __file__")
        elif isinstance(node, ast.Import):
            offenders.extend(
                f"line {node.lineno}: imports {alias.name}"
                for alias in node.names
                if alias.name.split(".")[0] in EXTERNAL_READ_IMPORTS
            )
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in EXTERNAL_READ_IMPORTS:
                offenders.append(f"line {node.lineno}: imports from {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in EXTERNAL_READ_CALLS:
                offenders.append(f"line {node.lineno}: {name}()")
    return sorted(set(offenders))


def python_source(path: Path) -> str:
    """The file as Python, with Mako placeholders neutralised."""
    source = read(path)
    return MAKO_PLACEHOLDER.sub("None", source) if path.suffix == ".mako" else source


def _called_name(node: ast.Call) -> str:
    """Trailing name of the callee: `now` for both `now()` and `sa.func.now()`."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


def _server_default_values(tree: ast.Module) -> list[tuple[int, ast.expr]]:
    found: list[tuple[int, ast.expr]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        found.extend(
            (node.lineno, keyword.value)
            for keyword in node.keywords
            if keyword.arg in {"server_default", "server_onupdate"}
        )
    return found


def content_server_defaults(source: str, filename: str = "<probe>") -> list[str]:
    """A `server_default` is content when it is neither a number, a flag nor an enum value.

    Read in the models as well as in the migrations: the models are where the schema is
    written by hand, and `mapped_column(server_default="Bottle breakage")` puts the phrase
    into every row the venue will ever create just as surely as a migration would.
    """
    tree = ast.parse(source, filename=filename)
    known_values = {value for values in schema_enums().values() for value in values}
    offenders: list[str] = []
    for lineno, value in _server_default_values(tree):
        if isinstance(value, ast.Constant) and not isinstance(value.value, str):
            continue  # a number or a boolean
        if isinstance(value, ast.Call):
            inner = value.args[0] if value.args else None
            if isinstance(inner, ast.Constant):
                value = inner  # text("now()"), text("'{}'::text[]")
            elif _called_name(value) in TECHNICAL_DEFAULT_CALLS:
                continue  # func.now(): a clock, not a word
        if isinstance(value, ast.Attribute | ast.Name):
            continue  # NotificationStatus.SCHEDULED.value and friends
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            offenders.append(f"line {lineno}: unreadable server_default {ast.dump(value)[:40]}")
            continue
        literal = value.value.strip()
        if TECHNICAL_DEFAULT.match(literal) or literal in known_values:
            continue
        offenders.append(f"line {lineno}: server_default={literal!r}")
    return offenders


def _literal_enum_declarations(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    """`(("bp_unit", ("ml", "l")), …)` written as a literal, whatever it is called."""
    found: dict[str, tuple[str, ...]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.List | ast.Tuple | ast.Set):
            continue
        try:
            value = ast.literal_eval(node)
        except (ValueError, TypeError, SyntaxError):
            continue
        if not isinstance(value, list | tuple | set):
            continue
        for item in value:
            if (
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], list | tuple)
                and all(isinstance(part, str) for part in item[1])
            ):
                found[item[0]] = tuple(item[1])
    return found


def foreign_enum_types(source: str, filename: str = "<probe>") -> list[str]:
    """Every enum a migration creates must be one the models declare, values included.

    An enum is a fine place to hide a catalogue — `CREATE TYPE writeoff_preset AS ENUM
    ('breakage', 'spillage', 'expired')` is a reference table with a different syntax.
    """
    tree = ast.parse(source, filename=filename)
    declared = dict(_literal_enum_declarations(tree))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for name, body in CREATE_ENUM.findall(node.value):
                declared[name.strip('"')] = tuple(QUOTED.findall(body))
        elif isinstance(node, ast.Call):
            # `postgresql.ENUM("staff", "manager", name="bp_member_role")` declares the
            # very same thing without a single line of SQL.
            func = node.func
            label = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if label not in {"Enum", "ENUM"}:
                continue
            values = tuple(
                argument.value
                for argument in node.args
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            )
            named = next(
                (
                    keyword.value.value
                    for keyword in node.keywords
                    if keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ),
                None,
            )
            if named and values:
                declared[named] = values
    known = schema_enums()
    offenders: list[str] = []
    for name, values in sorted(declared.items()):
        if name not in known:
            offenders.append(f"enum type {name!r} is created but no model declares it")
        elif tuple(values) != known[name]:
            offenders.append(
                f"enum type {name!r} is created with {list(values)}, "
                f"the models declare {list(known[name])}"
            )
    return offenders


# --------------------------------------------------------------------------------------
# Rule 2: Russian belongs to src/bot/texts/
# --------------------------------------------------------------------------------------


def _string_constants(tree: ast.Module) -> list[tuple[int, str]]:
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def cyrillic_literals(source: str, filename: str = "<probe>") -> list[str]:
    tree = ast.parse(source, filename=filename)
    return [
        f"line {lineno}: {value.strip()[:60]!r}"
        for lineno, value in _string_constants(tree)
        if CYRILLIC.search(value)
    ]


# --------------------------------------------------------------------------------------
# Rule 3: no content constants, in any language
# --------------------------------------------------------------------------------------


def catalogue_names(source: str, filename: str = "<probe>") -> list[str]:
    """Constants whose name announces a catalogue and whose value holds strings."""
    tree = ast.parse(source, filename=filename)
    vocabulary = schema_vocabulary() | _local_names(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        value = node.value
        if not targets or value is None:
            continue
        literals = [
            sub.value
            for sub in ast.walk(value)
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
        ]
        # `INGREDIENT = "ingredient"` inside an enum is the schema naming itself; only
        # strings that are *not* schema vocabulary make the name suspicious.
        if not literals or all(_is_technical(word, vocabulary) for word in literals):
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id
            if CONSTANT_NAME.match(name) and CONTENT_NAME.search(name):
                offenders.append(f"line {node.lineno}: {name}")
    return offenders


def _is_technical(word: str, vocabulary: frozenset[str] | set[str]) -> bool:
    """True when the string is machinery: schema vocabulary, a parameter name, a template."""
    if word in vocabulary:
        return True
    stripped = word.strip()
    if len(stripped) <= 3:
        return True
    if TEMPLATE_TOKEN.search(stripped):
        return True
    return bool(IDENTIFIER_TOKEN.match(stripped) and "_" in stripped)


def _is_typing_literal(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """`Literal["dev", "test", "prod"]` is a type, not a collection of data."""
    current: ast.AST | None = parents.get(node)
    while current is not None:
        if isinstance(current, ast.Subscript):
            name = current.value
            label = name.attr if isinstance(name, ast.Attribute) else getattr(name, "id", "")
            if label in {"Literal", "Annotated"}:
                return True
        current = parents.get(current)
    return False


def content_collections(
    source: str, filename: str = "<probe>", *, texts: bool = False
) -> list[str]:
    """Collections of three or more strings that are not the schema talking about itself.

    In `src/bot/texts/` the vocabulary exemption does not apply: interface wording is a
    named constant per message, and a list of three phrases is a checklist (TZ 1.4#6).
    """
    tree = ast.parse(source, filename=filename)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    exported: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            exported.add(id(node.value))

    vocabulary = schema_vocabulary() | _local_names(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List | ast.Tuple | ast.Set | ast.Dict):
            continue
        if id(node) in exported or _is_typing_literal(node, parents):
            continue
        elements = [*node.keys, *node.values] if isinstance(node, ast.Dict) else list(node.elts)
        strings = [
            element.value
            for element in elements
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        if len(strings) < 3:
            continue
        suspicious = (
            strings if texts else [word for word in strings if not _is_technical(word, vocabulary)]
        )
        if len(suspicious) >= 3:
            offenders.append(f"line {node.lineno}: {suspicious[:4]}")

    docstrings = docstring_nodes(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings or not PACKED_LIST.match(node.value.strip()):
            continue
        parts = node.value.strip().split(",")
        packed = parts if texts else [word for word in parts if not _is_technical(word, vocabulary)]
        if len(packed) >= 3:
            offenders.append(f"line {node.lineno}: packed list {packed[:4]}")
    return offenders


# --------------------------------------------------------------------------------------
# Rule 3b: no ORM object is built while a module is being imported
# --------------------------------------------------------------------------------------

MODELS_PACKAGE = "src.db.models"


def _dotted_name(node: ast.expr) -> str:
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return ""
    parts.append(current.id)
    return ".".join(reversed(parts))


def _model_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """(names that stand for a model in this file, aliases of the models package)."""
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == MODELS_PACKAGE or module.startswith(f"{MODELS_PACKAGE}."):
                names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if (alias.asname or alias.name)[:1].isupper()
                )
            elif module == "src.db":
                modules.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "models"
                )
        elif isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == MODELS_PACKAGE or alias.name.startswith(f"{MODELS_PACKAGE}.")
            )
    # Models declared in this very file: that is how the models package itself looks.
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    changed = True
    while changed:
        changed = False
        for node in classes:
            if node.name in names:
                continue
            bases = {_dotted_name(base).rsplit(".", 1)[-1] for base in node.bases}
            if "Base" in bases or bases & names:
                names.add(node.name)
                changed = True
    return names, modules


def _import_time_nodes(node: ast.AST) -> Iterable[ast.AST]:
    """Everything that runs when the module is imported: class bodies included, bodies of
    functions excluded — a function is only a promise to run later."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        yield child
        yield from _import_time_nodes(child)


def module_level_model_instances(source: str, filename: str = "<probe>") -> list[str]:
    """ORM objects built at import time — a fixture list with the label filed off.

    `_PRELOADED = (WriteoffReason(name="Бой бутылки"), …)` at the top of a models module
    is content the same way a migration would be, and it was invisible to every rule
    above: the elements are calls rather than string literals, and the variable is named
    neutrally. Nothing legitimate constructs a row while a module is being imported —
    rows are made by the running application, from what a venue entered.
    """
    tree = ast.parse(source, filename=filename)
    names, modules = _model_names(tree)
    offenders: list[str] = []
    for node in _import_time_nodes(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in names:
            offenders.append(f"line {node.lineno}: {func.id}(...)")
        elif isinstance(func, ast.Attribute) and func.attr[:1].isupper():
            dotted = _dotted_name(func)
            if dotted and dotted.rsplit(".", 1)[0] in modules:
                offenders.append(f"line {node.lineno}: {dotted}(...)")
    return sorted(set(offenders))


# --------------------------------------------------------------------------------------
# Rule 4: a structured file is either registered configuration or a reference book
# --------------------------------------------------------------------------------------


def is_registered_configuration(name: str) -> bool:
    return name in CONFIGURATION_FILES or any(
        fnmatch(name, pattern) for pattern in CONFIGURATION_PATTERNS
    )


def _is_content_word(word: str, vocabulary: frozenset[str]) -> bool:
    """True when a bartender could read this string; false for machinery."""
    stripped = word.strip()
    if not stripped or _is_technical(stripped, vocabulary):
        return False
    return CONFIG_TOKEN.match(stripped) is None


def _content_words(words: Iterable[str]) -> list[str]:
    vocabulary = schema_vocabulary()
    return [word.strip() for word in words if _is_content_word(word, vocabulary)]


def _strings_under(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return [found for item in value for found in _strings_under(item)]
    if isinstance(value, dict):
        return [found for item in value.values() for found in _strings_under(item)]
    return []


def _scan_parsed(value: object, where: str, offenders: list[str]) -> None:
    """The three content rules of this file, applied to parsed JSON/TOML."""
    if isinstance(value, str):
        if CYRILLIC.search(value):
            offenders.append(f"{where or '<root>'}: Russian text {value.strip()[:40]!r}")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            spot = f"{where}.{name}" if where else name
            # A collection, not a scalar: `default_loop_scope = "function"` is a setting,
            # `default_reasons = ["…", "…"]` is a reference book.
            catalogue = isinstance(item, list | tuple | dict) and CONTENT_NAME.search(name.upper())
            if catalogue and _content_words(_strings_under(item)):
                offenders.append(f"{spot}: a key that announces a catalogue, holding text")
            _scan_parsed(item, spot, offenders)
        return
    if isinstance(value, list | tuple):
        content = _content_words(item for item in value if isinstance(item, str))
        if len(content) >= 3:
            offenders.append(f"{where or '<root>'}: a collection of phrases {content[:4]}")
        for index, item in enumerate(value):
            _scan_parsed(item, f"{where}[{index}]", offenders)


def _uncommented(line: str) -> str:
    """The line without its `#` comment. Quotes are respected, crudely but enough."""
    quote = ""
    for index, character in enumerate(line):
        if quote:
            if character == quote:
                quote = ""
        elif character in "\"'":
            quote = character
        elif character == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


#: `key: [a, b, c]`, `key:`, `- scalar item`. A sequence item that is itself a mapping
#: (`- name: Install`) is not a scalar and carries no list of values.
#: The optional quotes matter: `DEFAULT_REASONS: '["…", "…", "…"]'` is an environment
#: variable holding a catalogue, and it is a string as far as YAML is concerned.
INLINE_ARRAY = re.compile(r"""^\s*(?:-\s+)?[\w."'-]+\s*:\s*["']?(\[.*\])["']?\s*$""")
YAML_KEY = re.compile(r"^\s*(?:-\s+)?([A-Za-z_][\w.-]*)\s*:(?:\s|$)")
YAML_SCALAR_ITEM = re.compile(r"^\s*-\s+(?![A-Za-z_][\w.-]*\s*:(?:\s|$))(.+?)\s*$")


def _array_items(text: str) -> list[str]:
    return [part.strip().strip("\"'") for part in text.strip()[1:-1].split(",") if part.strip()]


def _scan_lines(text: str) -> list[str]:
    """YAML and INI, read line by line.

    No YAML parser is in the dependency list, and pulling one in so that a guard can read
    a guard's input is not worth a dependency. The shapes that carry a catalogue are few:
    an inline array, a block sequence of scalars, and the key above either of them.
    """
    offenders: list[str] = []
    sequence: list[str] = []
    sequence_line = 0
    sequence_key = ""
    last_key = ""

    def flush() -> None:
        nonlocal sequence
        if sequence:
            content = _content_words(sequence)
            if len(content) >= 3:
                offenders.append(f"line {sequence_line}: a list of phrases {content[:4]}")
            elif content and CONTENT_NAME.search(sequence_key.upper()):
                offenders.append(
                    f"line {sequence_line}: {sequence_key!r} announces a catalogue "
                    f"and holds text {content[:4]}"
                )
        sequence = []

    for number, raw in enumerate(text.splitlines(), start=1):
        line = _uncommented(raw)
        if not line.strip():
            continue
        if CYRILLIC.search(line):
            offenders.append(f"line {number}: Russian text {line.strip()[:40]!r}")
        item = YAML_SCALAR_ITEM.match(line)
        if item:
            if not sequence:
                sequence_line, sequence_key = number, last_key
            sequence.append(item.group(1))
            continue
        flush()
        array = INLINE_ARRAY.match(line)
        key = YAML_KEY.match(line)
        last_key = key.group(1) if key else last_key
        if array:
            values = _array_items(array.group(1))
            content = _content_words(values)
            if len(content) >= 3:
                offenders.append(f"line {number}: a collection of phrases {content[:4]}")
            elif content and key and CONTENT_NAME.search(key.group(1).upper()):
                offenders.append(
                    f"line {number}: {key.group(1)!r} announces a catalogue and holds "
                    f"text {content[:4]}"
                )
    flush()
    return offenders


def configuration_content(text: str, filename: str = "<probe>.yml") -> list[str]:
    """Content hiding inside a file that was registered as configuration."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".toml":
        return _scan_parsed_root(tomllib.loads(text))
    if suffix == ".json":
        return _scan_parsed_root(json.loads(text))
    return _scan_lines(text)


def _scan_parsed_root(value: object) -> list[str]:
    offenders: list[str] = []
    _scan_parsed(value, "", offenders)
    return offenders


# --------------------------------------------------------------------------------------
# Tests over the repository
# --------------------------------------------------------------------------------------

MIGRATIONS = sorted(path for path in VERSIONS_DIR.rglob("*.py") if "__pycache__" not in path.parts)
SCANNED = list(scanned_python_files())
#: Everything Alembic runs or copies. `env.py` executes on *every* migration and used to
#: be outside these rules entirely — a `_bootstrap()` there, reading `bootstrap.json` and
#: replaying it through `exec_driver_sql`, shipped green. `script.py.mako` is the file the
#: next migration is generated from, so a line added there is a line in every future
#: migration.
ENV_PY = REPO_ROOT / "alembic" / "env.py"
SCRIPT_TEMPLATE = REPO_ROOT / "alembic" / "script.py.mako"
MIGRATION_SOURCES = [*MIGRATIONS, ENV_PY, SCRIPT_TEMPLATE]
#: The models are the source of the schema; the migrations only carry out what they say.
MODELS_DIR = SRC_DIR / "db" / "models"
MODEL_FILES = [path for path in SCANNED if MODELS_DIR in path.parents]
#: Everything that ships but is not the application itself: migrations, the entry point,
#: any script in the root. The bot inserts rows at runtime — that is its job; nothing in
#: the tooling around it has any business writing a row.
TOOLING = [path for path in SCANNED if SRC_DIR not in path.parents] + [SCRIPT_TEMPLATE]
#: The application itself.
SRC_FILES = [path for path in SCANNED if SRC_DIR in path.parents]


def test_the_guard_is_not_vacuous() -> None:
    """Every scope this file relies on must actually contain something."""
    assert VERSIONS_DIR.is_dir(), "alembic/versions is missing"
    assert MIGRATIONS, "no migration found — 0001_initial is mandatory"
    assert set(MIGRATIONS) <= set(TOOLING), "the migrations fell out of the scanned scope"
    assert ENV_PY.is_file(), "alembic/env.py is missing; it runs on every migration"
    assert SCRIPT_TEMPLATE.is_file(), "alembic/script.py.mako is missing"
    assert len(MODEL_FILES) > 5, f"only {len(MODEL_FILES)} model modules; the scope collapsed"
    assert TEXTS_DIR.is_dir(), (
        "src/bot/texts/ does not exist, so the rules that single it out check nothing"
    )
    assert len(SCANNED) > 20, f"only {len(SCANNED)} files are scanned; the scope collapsed"
    assert schema_vocabulary(), "the schema vocabulary is empty; the models did not import"
    assert STRUCTURED_FILES, "no structured data file is scanned; rule 4 would check nothing"
    for name in sorted(CONFIGURATION_FILES):
        assert (REPO_ROOT / name).is_file(), (
            f"{name} is registered as configuration but does not exist; a registration that "
            "outlives its file is a hole waiting for a data file of the same name"
        )


@pytest.mark.parametrize("path", TOOLING, ids=relative)
def test_delivery_tooling_writes_no_rows(path: Path) -> None:
    """Migrations create structure; scripts and the entry point create nothing at all."""
    offenders = data_statements(python_source(path))
    assert not offenders, (
        f"{relative(path)} writes rows; only the running application may do that "
        "(TZ 1.4#6, criterion 11.5): " + "; ".join(offenders)
    )


@pytest.mark.parametrize("path", MIGRATION_SOURCES, ids=relative)
def test_migration_reads_no_external_file(path: Path) -> None:
    offenders = external_reads(python_source(path), relative(path))
    assert not offenders, (
        f"{relative(path)} reads from outside itself; rows loaded from a side file are "
        "still rows (criterion 11.5): " + "; ".join(offenders)
    )


@pytest.mark.parametrize("path", [*MIGRATION_SOURCES, *MODEL_FILES], ids=relative)
def test_schema_defaults_are_technical(path: Path) -> None:
    offenders = content_server_defaults(python_source(path), relative(path))
    assert not offenders, (
        f"{relative(path)} carries content as a column default, which stamps it onto every "
        "row the venue will ever create (TZ 1.4#6): " + "; ".join(offenders)
    )


@pytest.mark.parametrize("path", MIGRATION_SOURCES, ids=relative)
def test_migration_creates_only_declared_enum_types(path: Path) -> None:
    offenders = foreign_enum_types(python_source(path), relative(path))
    assert not offenders, (
        f"{relative(path)}: an enum type is a reference table with another syntax; only the "
        "types the models declare may be created: " + "; ".join(offenders)
    )


def test_versions_directory_holds_python_only() -> None:
    """A `seed.sql` next to the migrations is invisible to every rule above."""
    intruders = [
        relative(path)
        for path in VERSIONS_DIR.rglob("*")
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    ]
    assert not intruders, f"alembic/versions/ must hold migrations only: {intruders}"


@pytest.mark.parametrize(
    "path", [path for path in SCANNED if TEXTS_DIR not in path.parents], ids=relative
)
def test_no_cyrillic_literals_outside_texts(path: Path) -> None:
    offenders = cyrillic_literals(read(path), relative(path))
    assert not offenders, (
        f"{relative(path)} contains a Russian string literal; wording belongs to "
        "src/bot/texts/ and venue content to the database (TZ 8.2, 1.4#6): " + "; ".join(offenders)
    )


@pytest.mark.parametrize("path", SCANNED, ids=relative)
def test_no_catalogue_constants(path: Path) -> None:
    offenders = catalogue_names(read(path), relative(path))
    assert not offenders, (
        f"{relative(path)} declares a catalogue as a constant. Checklist items, write-off "
        "reasons and categories are entered by the venue and live in the database; the "
        "system ships empty (TZ 1.4#6, criterion 11.5): " + "; ".join(offenders)
    )


@pytest.mark.parametrize("path", SRC_FILES, ids=relative)
def test_no_orm_objects_are_built_at_import_time(path: Path) -> None:
    offenders = module_level_model_instances(read(path), relative(path))
    assert not offenders, (
        f"{relative(path)} builds ORM objects while it is imported. A list of rows is a "
        "reference book whether it is written as strings or as constructor calls; rows "
        "are created by the running application from what a venue entered (TZ 1.4#6, "
        "criterion 11.5): " + "; ".join(offenders)
    )


@pytest.mark.parametrize("path", SCANNED, ids=relative)
def test_no_content_collections(path: Path) -> None:
    texts = TEXTS_DIR in path.parents
    offenders = content_collections(read(path), relative(path), texts=texts)
    hint = (
        "in src/bot/texts/ a message is one named constant; a list of phrases is a "
        "checklist, not wording"
        if texts
        else "strings outside the schema vocabulary in a list of three or more are content"
    )
    assert not offenders, f"{relative(path)}: {hint} (TZ 1.4#6): " + "; ".join(offenders)


def test_delivery_carries_no_data_files() -> None:
    intruders = [relative(path) for path in scanned_files() if path.suffix.lower() in DATA_SUFFIXES]
    assert not intruders, (
        "data files ship with the delivery; fixtures belong to tests/ and content to a "
        f"venue's own import (TZ 1.4#6, 7): {intruders}"
    )


STRUCTURED_FILES = [path for path in scanned_files() if path.suffix.lower() in STRUCTURED_SUFFIXES]


def test_structured_files_are_registered_configuration() -> None:
    """`.json`, `.yaml`, `.toml` in the delivery are configuration or they are content.

    The content rules above read `.py`, so a reference book in another format used to be
    invisible to all of them: `src/db/reference.json` with write-off reasons and checklist
    items plus a loader next to it shipped green, and so did a `bootstrap.yaml` in the
    root. Configuration is told from content by name (`CONFIGURATION_FILES`), because the
    two have the same shape and no rule can tell them apart by looking.
    """
    intruders = [
        relative(path)
        for path in STRUCTURED_FILES
        if not is_registered_configuration(relative(path))
    ]
    assert not intruders, (
        "a structured data file that is not registered configuration: reference data is "
        "entered by the venue and lives in the database, the system ships empty "
        "(TZ 1.4#6, criterion 11.5). If this really is configuration, add it to "
        f"CONFIGURATION_FILES in tests/test_no_seed_data.py: {intruders}"
    )


@pytest.mark.parametrize(
    "path",
    [path for path in STRUCTURED_FILES if is_registered_configuration(relative(path))],
    ids=relative,
)
def test_configuration_files_carry_no_content(path: Path) -> None:
    """Being registered as configuration is not a licence to carry a catalogue."""
    offenders = configuration_content(read(path), relative(path))
    assert not offenders, (
        f"{relative(path)} is configuration, but this is content: checklist items, "
        "write-off reasons and categories are entered by the venue and live in the "
        "database (TZ 1.4#6, criterion 11.5): " + "; ".join(offenders)
    )


# --------------------------------------------------------------------------------------
# Rule 5: the commands the delivery may run are a closed list
# --------------------------------------------------------------------------------------


#: Both keys, because Docker gives `entrypoint` priority over the image's CMD: a service
#: with `entrypoint: ["python", "scripts/seed.py"]` runs that and nothing else, and the
#: rule used to read `command:` alone.
COMPOSE_COMMAND = re.compile(r"^\s*(command|entrypoint):\s*(\[.*\]|\S.*)$", re.MULTILINE)


def _command_lines(text: str) -> list[tuple[str, str]]:
    return [
        (match.group(1), " ".join(match.group(2).split()))
        for match in COMPOSE_COMMAND.finditer(text)
    ]


def test_compose_services_run_a_closed_list_of_commands() -> None:
    composes = sorted(REPO_ROOT.glob("docker-compose*.y*ml"))
    assert composes, "no compose file found; the rule would check nothing"
    for path in composes:
        name = relative(path)
        for key, command in _command_lines(read(path)):
            assert command in ALLOWED_SERVICE_COMMANDS, (
                f"{name} runs {command!r} as {key}:. The entry point executes whatever CMD "
                "says and `entrypoint:` replaces it outright, so either key is a way to "
                "seed the database past every static rule; add it to "
                "ALLOWED_SERVICE_COMMANDS here if it is really wanted"
            )


@pytest.mark.parametrize("key", ["command", "entrypoint"])
def test_the_command_rule_has_teeth(key: str) -> None:
    """A service pointed at a seeding script must not read as an ordinary command."""
    seeding = f'services:\n  bot:\n    {key}: ["python", "scripts/seed.py"]\n'
    found = _command_lines(seeding)
    assert found == [(key, '["python", "scripts/seed.py"]')]
    assert found[0][1] not in ALLOWED_SERVICE_COMMANDS


def test_image_entry_command_is_the_expected_one() -> None:
    lines = [
        " ".join(line.split())
        for line in read(REPO_ROOT / "Dockerfile").splitlines()
        if line.strip().startswith(("CMD", "ENTRYPOINT"))
    ]
    assert lines, "the Dockerfile declares no CMD/ENTRYPOINT"
    for line in lines:
        assert line in ALLOWED_IMAGE_COMMANDS, f"Dockerfile runs {line!r}"


def entrypoint_programs(source: str) -> list[str]:
    """Programs started by a shell script, with shell syntax and redirections removed."""
    started: list[str] = []
    for number, raw in enumerate(source.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for fragment in re.split(r"[;|]+|&&", line):
            words = fragment.split()
            while words and (
                words[0] in SHELL_KEYWORDS
                or "=" in words[0]
                or words[0].startswith((">", "<", "!", "$", "["))
            ):
                if words[0] == "for":
                    # `for name in A B C` names a loop variable, not a program.
                    words = []
                    break
                words = words[1:]
            if not words:
                continue
            program = words[0].strip("\"'")
            if not COMMAND_NAME.match(program):
                continue
            started.append(f"line {number}: {' '.join(words)}")
    return started


def test_entrypoint_starts_nothing_but_migrations_and_the_command() -> None:
    """`docker/entrypoint.sh` may migrate and hand over — it may not load anything."""
    offenders: list[str] = []
    for started in entrypoint_programs(read(REPO_ROOT / "docker" / "entrypoint.sh")):
        command = started.split(": ", 1)[1]
        program = command.split()[0]
        if program == "alembic":
            if command != ENTRYPOINT_ALEMBIC_COMMAND:
                offenders.append(started)
        elif program not in ENTRYPOINT_PROGRAMS:
            offenders.append(started)
    assert not offenders, (
        "docker/entrypoint.sh runs something other than the migration and the command it "
        "was handed; a second program is a second way to seed the database: " + "; ".join(offenders)
    )


def test_the_entrypoint_rule_has_teeth() -> None:
    seeding = 'set -eu\nalembic upgrade head\npython scripts/seed.py\nexec "$@"\n'
    programs = [line.split(": ", 1)[1].split()[0] for line in entrypoint_programs(seeding)]
    assert "python" in programs, "a seeding step in the entry point would go unnoticed"
    assert "python" not in ENTRYPOINT_PROGRAMS


# --------------------------------------------------------------------------------------
# Rule 6: src/ never reaches into tests/
# --------------------------------------------------------------------------------------


def reaches_into_tests(source: str, filename: str = "<probe>") -> list[str]:
    tree = ast.parse(source, filename=filename)
    offenders: list[str] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        else:
            continue
        offenders.extend(
            f"line {node.lineno}: imports {module}"
            for module in modules
            if module == "tests" or module.startswith("tests.")
        )
    # importlib and __import__ would slip past the tree walk above.
    offenders.extend(
        f"mentions {marker!r}" for marker in ("tests.tools", "tests/tools") if marker in source
    )
    return offenders


@pytest.mark.parametrize("path", SRC_FILES, ids=relative)
def test_src_does_not_import_tests(path: Path) -> None:
    offenders = reaches_into_tests(read(path), relative(path))
    assert not offenders, (
        f"{relative(path)} depends on tests/; production code must not reach the reference "
        "loader (decision D13): " + "; ".join(offenders)
    )


def test_tests_are_excluded_from_the_image() -> None:
    """The other half of D13: unreachable by import *and* absent from the image."""
    ignored = {line.strip() for line in read(REPO_ROOT / ".dockerignore").splitlines()}
    assert "tests" in ignored, (
        ".dockerignore must exclude tests/: the reference workbook is a real bar's data "
        "(CLAUDE.md) and has no business in a production image"
    )


# --------------------------------------------------------------------------------------
# The guard's own teeth
# --------------------------------------------------------------------------------------

MIGRATION_BYPASSES: list[tuple[str, str]] = [
    (
        "COPY … FROM STDIN",
        """
def upgrade() -> None:
    op.execute("COPY writeoff_reasons (name) FROM STDIN")
""",
    ),
    (
        "op.bulk_insert",
        """
def upgrade() -> None:
    op.bulk_insert(reasons, [{"name": "breakage"}])
""",
    ),
    (
        "INSERT INTO",
        """
def upgrade() -> None:
    op.execute("INSERT INTO writeoff_reasons (name) VALUES ('breakage')")
""",
    ),
    (
        "UPDATE … SET",
        """
def upgrade() -> None:
    op.execute("UPDATE writeoff_reasons SET name = 'breakage'")
""",
    ),
    (
        "MERGE INTO",
        """
def upgrade() -> None:
    op.execute("MERGE INTO writeoff_reasons USING x ON true WHEN NOT MATCHED THEN DO NOTHING")
""",
    ),
]

MIGRATION_CONTENT_BYPASSES: list[tuple[str, str]] = [
    (
        "content as a column default",
        """
def upgrade() -> None:
    op.add_column(
        "writeoffs",
        sa.Column("preset", sa.Text(), server_default="Bottle breakage", nullable=False),
    )
""",
    ),
]

MIGRATION_ENUM_BYPASSES: list[tuple[str, str]] = [
    (
        "content as an enum type",
        """
def upgrade() -> None:
    op.execute("CREATE TYPE writeoff_preset AS ENUM ('breakage', 'spillage', 'expired')")
""",
    ),
    (
        "a column typed with an undeclared enum",
        """
def upgrade() -> None:
    op.add_column(
        "writeoffs",
        sa.Column("preset", postgresql.ENUM("breakage", "spillage", name="writeoff_preset")),
    )
""",
    ),
    (
        "an extra value on a declared type",
        """
ENUM_TYPES = (("bp_unit", ("ml", "l", "g", "kg", "pcs", "bottle", "case")),)
""",
    ),
]

MIGRATION_READ_BYPASSES: list[tuple[str, str]] = [
    (
        "rows loaded from a side file",
        """
from pathlib import Path

def upgrade() -> None:
    op.execute(Path(__file__).with_name("seed.sql").read_text())
""",
    ),
]

CONTENT_BYPASSES: list[tuple[str, str, bool]] = [
    (
        "checklist items hidden in texts/",
        'DEFAULT_ITEMS = ["Включить кофемашину", "Проверить лёд", "Протереть стойку"]',
        True,
    ),
    (
        "Latin nomenclature in src/db/",
        'DEFAULT_PRODUCT_CATEGORIES = ["Whiskey", "Gin", "Vermouth", "Syrups"]',
        False,
    ),
    (
        "write-off reasons in a script",
        'WRITEOFF_REASONS = ["breakage", "spillage", "staff drink", "expired"]',
        False,
    ),
    (
        "an unnamed list of phrases in texts/",
        'MENU = ["Моя смена", "График смен", "Рецепты и техкарты"]',
        True,
    ),
    (
        "a catalogue packed into one literal",
        'CATALOGUE = "Whiskey,Gin,Vermouth,Syrups".split(",")',
        False,
    ),
    (
        "checklist items as numbered constants",
        'FIRST_ITEM = "Включить кофемашину"\nSECOND_ITEM = "Проверить лёд"',
        True,
    ),
    (
        "a catalogue behind an innocent name",
        'ITEMS = ("Espresso machine", "Ice", "Bar top")',
        False,
    ),
]

CLEAN_CONTENT: list[tuple[str, str, bool]] = [
    (
        "schema vocabulary",
        'COLUMNS = ["venue_id", "template_id", "order_index"]',
        False,
    ),
    (
        "enum values of the models",
        'VALUES = ("opening", "closing", "stock")',
        False,
    ),
    (
        "an export list",
        '__all__ = ["Alpha", "Beta", "Gamma", "Delta"]\n\nAlpha = Beta = Gamma = Delta = 1',
        False,
    ),
    (
        "a typing Literal",
        'from typing import Literal\n\nAppEnv = Literal["dev", "test", "prod"]',
        False,
    ),
    (
        "enum members naming the schema",
        'class Kind:\n    INGREDIENT = "ingredient"\n    SUPPLIER = "supplier"',
        False,
    ),
    (
        "one interface message per constant",
        'GREETING = "Привет"\nBUTTON_SHIFT = "Моя смена"',
        True,
    ),
]


@pytest.mark.parametrize(
    ("name", "source"), MIGRATION_BYPASSES, ids=[name for name, _ in MIGRATION_BYPASSES]
)
def test_row_writing_bypasses_are_caught(name: str, source: str) -> None:
    assert data_statements(source), f"a migration can still ship rows through {name!r}"


@pytest.mark.parametrize(
    ("name", "source"),
    MIGRATION_CONTENT_BYPASSES,
    ids=[name for name, _ in MIGRATION_CONTENT_BYPASSES],
)
def test_default_content_bypasses_are_caught(name: str, source: str) -> None:
    assert content_server_defaults(source), f"content still ships through {name!r}"


@pytest.mark.parametrize(
    ("name", "source"),
    MIGRATION_ENUM_BYPASSES,
    ids=[name for name, _ in MIGRATION_ENUM_BYPASSES],
)
def test_enum_content_bypasses_are_caught(name: str, source: str) -> None:
    assert foreign_enum_types(source), f"content still ships through {name!r}"


@pytest.mark.parametrize(
    ("name", "source"),
    MIGRATION_READ_BYPASSES,
    ids=[name for name, _ in MIGRATION_READ_BYPASSES],
)
def test_external_read_bypasses_are_caught(name: str, source: str) -> None:
    assert external_reads(source), f"a migration can still load rows through {name!r}"


@pytest.mark.parametrize(
    ("name", "source", "texts"), CONTENT_BYPASSES, ids=[name for name, _, _ in CONTENT_BYPASSES]
)
def test_content_constant_bypasses_are_caught(name: str, source: str, texts: bool) -> None:
    offenders = (
        catalogue_names(source)
        + content_collections(source, texts=texts)
        + (cyrillic_literals(source) if not texts else [])
    )
    assert offenders, f"content still ships through {name!r}"


@pytest.mark.parametrize(
    ("name", "source", "texts"), CLEAN_CONTENT, ids=[name for name, _, _ in CLEAN_CONTENT]
)
def test_legitimate_constants_are_not_flagged(name: str, source: str, texts: bool) -> None:
    offenders = catalogue_names(source) + content_collections(source, texts=texts)
    assert not offenders, f"{name!r} is legitimate but was flagged: {offenders}"


def test_the_import_rule_has_teeth() -> None:
    """The rule used to pass because `tests/tools/` did not exist yet."""
    assert reaches_into_tests("from tests.tools import loader")
    assert reaches_into_tests("import importlib\nloader = 'tests.tools'")
    assert not reaches_into_tests("from src.db.models import Base")


IMPORT_TIME_BYPASSES: list[tuple[str, str]] = [
    (
        "ORM objects in a module-level tuple",
        """
from src.db.models import WriteoffReason

_PRELOADED = (
    WriteoffReason(name="Bottle breakage"),
    WriteoffReason(name="Spillage"),
)
""",
    ),
    (
        "the same thing through the package",
        """
from src.db import models

ROWS = [models.WriteoffReason(name="Bottle breakage")]
""",
    ),
    (
        "a class attribute, which also runs on import",
        """
from src.db.models import ChecklistItem

class Defaults:
    first = ChecklistItem(text="Turn on the coffee machine")
""",
    ),
    (
        "a model declared and instantiated in the same module",
        """
from src.db.models.base import Base

class WriteoffReason(Base):
    __tablename__ = "writeoff_reasons"

SAMPLE = WriteoffReason(name="Spillage")
""",
    ),
]

CLEAN_IMPORT_TIME: list[tuple[str, str]] = [
    (
        "a model built inside a function is the running application",
        """
from src.db.models import WriteoffReason

def create(name):
    return WriteoffReason(name=name)
""",
    ),
    (
        "declaring models is not building rows",
        """
from src.db.models.base import Base, IdMixin

class WriteoffReason(IdMixin, Base):
    __tablename__ = "writeoff_reasons"
""",
    ),
    (
        "sqlalchemy constructs at module level are schema, not data",
        """
from sqlalchemy import Column, Text
from src.db.models import Product

COLUMN = Column("name", Text(), nullable=False)
""",
    ),
]


@pytest.mark.parametrize(
    ("name", "source"), IMPORT_TIME_BYPASSES, ids=[name for name, _ in IMPORT_TIME_BYPASSES]
)
def test_import_time_row_bypasses_are_caught(name: str, source: str) -> None:
    assert module_level_model_instances(source), f"rows still ship through {name!r}"


@pytest.mark.parametrize(
    ("name", "source"), CLEAN_IMPORT_TIME, ids=[name for name, _ in CLEAN_IMPORT_TIME]
)
def test_legitimate_module_level_code_is_not_flagged(name: str, source: str) -> None:
    offenders = module_level_model_instances(source)
    assert not offenders, f"{name!r} is legitimate but was flagged: {offenders}"


DATA_FILE_BYPASSES: list[tuple[str, str, str]] = [
    (
        "a reference book as JSON",
        "src/db/reference.json",
        '{"writeoff_reasons": ["Бой бутылки", "Пролив", "Просрочка"]}',
    ),
    (
        "a reference book as YAML",
        "bootstrap.yaml",
        "checklist_items:\n"
        "  - Turn on the coffee machine\n"
        "  - Check the ice\n"
        "  - Wipe the bar top\n",
    ),
    (
        "a catalogue inside a registered configuration file",
        "docker-compose.yml",
        "services:\n  bot:\n    environment:\n"
        '      X: \'["Bottle breakage", "Spillage", "Expired stock"]\'\n',
    ),
    (
        "a catalogue inside pyproject",
        "pyproject.toml",
        '[tool.barpoint]\ndefault_reasons = ["Bottle breakage", "Spillage", "Expired stock"]\n',
    ),
]

CLEAN_DATA_FILES: list[tuple[str, str, str]] = [
    ("the real pyproject", "pyproject.toml", read(REPO_ROOT / "pyproject.toml")),
    ("the real compose file", "docker-compose.yml", read(REPO_ROOT / "docker-compose.yml")),
    ("the real alembic.ini", "alembic.ini", read(REPO_ROOT / "alembic.ini")),
]


@pytest.mark.parametrize(
    ("name", "filename", "source"),
    DATA_FILE_BYPASSES,
    ids=[name for name, _, _ in DATA_FILE_BYPASSES],
)
def test_data_file_bypasses_are_caught(name: str, filename: str, source: str) -> None:
    caught = configuration_content(source, filename) or not is_registered_configuration(filename)
    assert caught, f"content still ships through {name!r}"


@pytest.mark.parametrize(
    ("name", "filename", "source"),
    CLEAN_DATA_FILES,
    ids=[name for name, _, _ in CLEAN_DATA_FILES],
)
def test_real_configuration_is_not_flagged(name: str, filename: str, source: str) -> None:
    assert is_registered_configuration(filename), f"{filename} fell out of the registry"
    offenders = configuration_content(source, filename)
    assert not offenders, f"{name!r} is configuration but was flagged: {offenders}"


def test_the_registry_does_not_cover_the_whole_repository() -> None:
    """A pattern wide enough to match anything would switch rule 4 off."""
    for name in ("src/db/reference.json", "bootstrap.yaml", "alembic/bootstrap.json", "seed.toml"):
        assert not is_registered_configuration(name), f"{name} counts as configuration"


def test_the_template_is_read_as_python() -> None:
    """`script.py.mako` only reaches the migration rules once `${…}` is neutralised."""
    source = python_source(SCRIPT_TEMPLATE)
    assert "${" not in source
    ast.parse(source, filename="script.py.mako")
    assert content_server_defaults(
        python_source(SCRIPT_TEMPLATE).replace(
            "def upgrade() -> None:",
            'def upgrade() -> None:\n    op.add_column("x", sa.Column("y", server_default="Gin"))',
        )
    ), "content added to the template would go unnoticed"


def test_model_defaults_are_in_scope() -> None:
    """Rule 1 reads the models too; `func.now()` there is technique, a phrase is not."""
    assert MODEL_FILES, "no model module is scanned"
    assert content_server_defaults('x = mapped_column(server_default="Bottle breakage")')
    assert not content_server_defaults("x = mapped_column(server_default=func.now())")
    assert not content_server_defaults('x = mapped_column(server_default="false")')
