"""Static guard over `src/**` (plan, task 4; TZ 3.2, 3.3; acceptance 11.3).

Threat model — read this before adding a rule
---------------------------------------------

**What this file defends against is a mistake, not an adversary.** The author of the code
it reads is a developer or an agent who means well: someone types `!=` where `==` belongs,
widens a filter with `or_` to "also show the library", reaches for `session.get()` because
it is shorter, or overrides `venue_column` while refactoring. Every rule below is tuned to
that: it must fire on the plausible slip and stay silent on correct code, because a guard
that cries wolf is a guard someone deletes.

It is **not** a sandbox. Anyone who wants to get a query past it can: the code being
checked and the checker live in the same repository, and whoever edits one can edit the
other. Trying to win that race would mean matching on obfuscation, which buys nothing here
and costs false positives on ordinary code. The tricks below are therefore known and
deliberately **not** closed:

* a keyword split across literals — `"SELEC" "T * FROM products"`;
* SQL arriving base64-decoded, `chr()`-assembled or otherwise computed at runtime;
* the session bound to a name `SESSION_RECEIVER` does not recognise (`self.s`, `handle`);
* a statement constructor re-exported through a module of this project and imported from
  there, so that no `sqlalchemy` import appears in the file;
* a query reaching another venue through a chain of relationships from a globally scoped
  row, rather than through a `select()` this file can see;
* a statement built outside the repository layer and handed to a repository method as an
  argument.

**Where the real guarantee comes from.** Criterion 11.3 is proved on a live database, by
acceptance test 36 of the plan: two venues, a row of one addressed from the context of the
other, a forged `callback_data` refused by the server, a child row unreachable across
venues. It lands with the repository implementations (plan tasks 11-13, 28); the throwaway
database and the object factories it needs are already here (`tests/conftest.py`,
`tests/factories.py`), and CI runs `db`-marked tests against a real PostgreSQL and fails if
they skip. Underneath it, the join in `ChildRepository` and the composite foreign keys of
the schema hold at database level, where no static rule is involved at all.

This file is the cheap guard that runs everywhere and always: it catches the slip before it
reaches the live test, and it covers code that no runtime test exercises yet. Green here is
not proof of isolation, and no rule should be added on the assumption that it is.

Three rules, all decided on the syntax tree and none on the source text. The previous
version of this file matched substrings, and a substring match is decided by whatever the
author typed in a trailing comment — `select(ChecklistItem).where(ChecklistItem.id == pk)
# venue_id via template` passed it. Everything below therefore asks the tree what the code
*does*, not what it says about itself.

1. **No SQL outside `src/db/repositories/`** (TZ 3.2). "SQL" is not the word `select`: it is
   any name bound from a `sqlalchemy*` import (so `from sqlalchemy import select as sel`
   is the same thing), any attribute of a sqlalchemy module alias (`sa.select`), any query
   or write method called on a session-like receiver (`session.execute`, `session.add`,
   `session.exec_driver_sql`), dynamic access to the package (`getattr(sa, ...)`,
   `importlib.import_module("sqlalchemy")`) and any string literal shaped like a SQL
   statement.

2. **`session.get()`, `session.get_one()` and `session.merge()` are banned everywhere**,
   repositories included. They address a row by primary key and ignore every filter, so
   there is no such thing as a venue-scoped `get`: a forged `callback_data` would read
   another venue's row and the relationships loaded from it. Rows are fetched with
   `by_id()`, which keeps the venue predicate (and, for child rows, the join).

3. **Every query inside the repositories is venue scoped** (TZ 3.3, acceptance 11.3).
   Accepted evidence is structural, and the operator is part of the structure: an
   *equality* `venue_id == self.venue_id`, or a call to `self.for_venue()` /
   `self.library()` / `self.by_id()` / `self.for_parent()`. `!=`, `is`, `is_not()` and
   anything under a negation are not evidence — they are the mistake this rule exists for.
   A disjunction is evidence only in the library shape (`venue_id = :vid OR venue_id IS
   NULL`, on the same column): `or_` widens, so `or_(venue_id == self.venue_id, true())`
   reads the whole table with a venue filter written next to it. Evidence may arrive
   through a local name (`stmt = self.for_venue()` … `session.scalars(stmt)`) or through a
   private helper — and a helper only counts when *every* call site passes a venue
   predicate in the parameter the helper filters on, which is what makes
   `ChildRepository._through_parent` legal without an exemption.

The guard's own teeth are tested: `KNOWN_BYPASSES` below is the list of tricks that used to
work, each one asserted to be caught, and `CLEAN_SOURCES` asserts the rules stay quiet on
code that is actually correct.

Blind spots of these three rules specifically, on top of the threat model above:

* a query assembled at runtime — a statement handed in as an argument from outside the
  repository layer, or built by `eval`. Static analysis ends where the syntax tree does;
* the left-hand side of the venue equality is checked by name (`…venue_id`,
  `…venue_column`, or a bare local, which is how `library()` is written). `something ==
  self.venue_id` on an unrelated column therefore passes as evidence — a filter that is
  wrong but not leaky, and tightening it further would flag correct code;
* `session` reached under a name this file does not recognise as a session (see
  `SESSION_RECEIVER`). Adding a name there is a one-line change and belongs in review.

The other half of rule 3 does not live here at all: `venue_column` and `scope_model` are
sealed in `src/db/repositories/base.py` and re-checked on every query shape, because a
subclass that overrode the property kept `for_venue()`, `library()` and `by_id()` while
none of them filtered by venue any more. That is a runtime guarantee, not a static one.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.repo_scan import (
    REPO_ROOT,
    REPOSITORIES_DIR,
    SRC_DIR,
    comments,
    docstring_nodes,
    python_files,
    read,
    relative,
)

# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------

#: Statement constructors of SQLAlchemy. Names, not spellings: the set is compared against
#: what a `sqlalchemy*` import actually bound in the file being read.
SQL_CONSTRUCTORS = frozenset(
    {
        "select",
        "insert",
        "update",
        "delete",
        "text",
        "exists",
        "union",
        "union_all",
        "table",
        "literal_column",
    }
)

#: Methods that run a statement. Only counted on a session-like receiver.
SESSION_QUERY_METHODS = frozenset(
    {"execute", "scalar", "scalars", "stream", "stream_scalars", "exec_driver_sql"}
)

#: Methods that address a row by identity, ignoring every filter. Banned outright.
SESSION_IDENTITY_METHODS = frozenset({"get", "get_one", "merge"})

#: Unit-of-work methods. Allowed inside the repositories, nowhere else.
SESSION_WRITE_METHODS = frozenset({"add", "add_all", "refresh", "delete", "expunge"})

#: What counts as "a session" on the left of a dot. `payload.get(...)` and
#: `os.environ.get(...)` must not be caught by rule 2, `self.session.get(...)` must.
SESSION_RECEIVER = re.compile(r"(?:^|\.)(?:session|sess|db|dbsession|db_session|conn|connection)$")

#: Repository methods that carry the venue predicate by construction (see base.py).
VENUE_SCOPED_HELPERS = frozenset({"for_venue", "library", "by_id", "for_parent"})

#: Ways of spelling a disjunction. `or_()` is the SQLAlchemy function, `|` the operator it
#: overloads; the Python `or` cannot build a predicate but is listed so that writing it by
#: mistake is not read as evidence either. A disjunction *widens*, so it is the one
#: combinator that has to be inspected arm by arm.
OR_FUNCTIONS = frozenset({"or_"})
#: Negations. `~(Product.venue_id == self.venue_id)` is the venue predicate turned inside
#: out; nothing under one of these counts.
NEGATION_FUNCTIONS = frozenset({"not_"})
#: `column.is_(None)` — the second arm of the library shape, and the only non-equality arm
#: an `or_` may carry (see `_is_library_disjunction`).
NULL_CHECK_METHODS = frozenset({"is_"})

#: A string literal shaped like a statement. `UPDATE` needs a `SET` and `SELECT` a `FROM`
#: so that English prose ("update the row where the id matches") is not mistaken for SQL.
RAW_SQL = re.compile(
    r"""^\s*(
        select\b(?=[\s\S]*\bfrom\b)
      | insert\s+into\b
      | update\b(?=[\s\S]*\bset\b)
      | delete\s+from\b
      | copy\b(?=[\s\S]*\bfrom\b)
      | merge\s+into\b
      | truncate\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

EXEMPT_MARKER = "# venue-exempt:"

#: The only queries allowed to skip rule 3, spelled out so that a new one shows up in the
#: diff of *this* file. Key: (path relative to the repository, enclosing function).
#: Empty on purpose — `ChildRepository._through_parent` is accepted structurally, through
#: its call sites, not by being listed here.
ALLOWED_VENUE_EXEMPTIONS: dict[tuple[str, str], str] = {}


# --------------------------------------------------------------------------------------
# Tree helpers
# --------------------------------------------------------------------------------------


def _dotted(node: ast.expr) -> str:
    """`self.session` for an attribute chain, `""` for anything else."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return ""
    parts.append(current.id)
    return ".".join(reversed(parts))


def _root_name(node: ast.expr) -> str:
    dotted = _dotted(node)
    return dotted.split(".")[0] if dotted else ""


def _is_self_attribute(node: ast.AST, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _assigned_values(scope: ast.AST, name: str) -> list[ast.expr]:
    """Every expression bound to `name` inside `scope`."""
    values: list[ast.expr] = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            values.extend(
                node.value
                for target in node.targets
                if isinstance(target, ast.Name) and target.id == name
            )
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name and node.value is not None:
                values.append(node.value)
        elif (
            isinstance(node, ast.NamedExpr)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            values.append(node.value)
    return values


def _call_name(node: ast.Call) -> str:
    """Trailing name of the callee: `or_` for both `or_(...)` and `sa.or_(...)`."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _looks_like_a_venue_column(node: ast.expr) -> bool:
    """The other side of the comparison: `Product.venue_id`, `self.venue_column`, `column`.

    A bare name is accepted because that is how `library()` is written in base.py
    (`column = self.venue_column` … `column == self.venue_id`); the name is not traced back
    to its assignment, which is stated in the module docstring.
    """
    dotted = _dotted(node)
    if not dotted:
        return False
    if "." not in dotted:
        return True
    return dotted.rsplit(".", 1)[1] in {"venue_id", "venue_column"}


def _is_venue_equality(node: ast.AST) -> bool:
    """`<venue column> == self.venue_id`, and nothing else.

    One `ast.Eq`, nothing around it. The operator is the whole meaning of the predicate:
    `!=` returns every *other* venue, `is` / `is not` compare Python objects and collapse
    to a constant `True`/`False` before SQLAlchemy ever sees them. The rule used to ask
    only whether `self.venue_id` occurred somewhere inside an `ast.Compare`, so all three
    passed for the same reason a correct filter did.
    """
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    if not isinstance(node.ops[0], ast.Eq):
        return False
    left, right = node.left, node.comparators[0]
    if _is_self_attribute(left, "venue_id"):
        return _looks_like_a_venue_column(right)
    if _is_self_attribute(right, "venue_id"):
        return _looks_like_a_venue_column(left)
    return False


def _is_null_check(node: ast.AST) -> ast.expr | None:
    """`column.is_(None)` -> the column it was called on, otherwise `None`."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in NULL_CHECK_METHODS
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value is None
    ):
        return node.func.value
    return None


def _disjunction_arms(node: ast.AST) -> list[ast.expr] | None:
    """The arms of `or_(a, b)`, `a | b` or `a or b`; `None` when this is not a disjunction."""
    if isinstance(node, ast.Call) and _call_name(node) in OR_FUNCTIONS:
        return [*node.args, *(keyword.value for keyword in node.keywords)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        arms: list[ast.expr] = []
        for side in (node.left, node.right):
            nested = _disjunction_arms(side)
            arms.extend(nested if nested is not None else [side])
        return arms
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return list(node.values)
    return None


def _is_library_disjunction(arms: list[ast.expr]) -> bool:
    """True only for the library shape: `venue_id = :vid OR venue_id IS NULL`.

    A disjunction is the one combinator that *widens*: every arm that is not itself the
    venue predicate hands back rows the predicate had excluded, so
    `or_(Product.venue_id == self.venue_id, true())` is the whole table with a venue
    filter written next to it. Arms are therefore checked one by one, and only two kinds
    are accepted — the equality itself, and `IS NULL` on the very same column, which is
    the shared BarPoint library of TZ 3.3.
    """
    equalities = [arm for arm in arms if _is_venue_equality(arm)]
    if not equalities:
        return False
    columns: set[str] = set()
    for equality in equalities:
        assert isinstance(equality, ast.Compare)  # narrowed by _is_venue_equality
        left, right = equality.left, equality.comparators[0]
        columns.add(ast.unparse(right if _is_self_attribute(left, "venue_id") else left))
    for arm in arms:
        if _is_venue_equality(arm):
            continue
        column = _is_null_check(arm)
        if column is not None and ast.unparse(column) in columns:
            continue
        return False
    return True


def _direct_venue_evidence(node: ast.AST) -> bool:
    """True when `node` narrows what is read to `self.venue_id`.

    Structural, and deliberately not "does `self.venue_id` appear anywhere in here": the
    operator and the combinator carry the whole meaning. A comparison is evidence only as
    `==`, a disjunction only in the library shape, and nothing under a negation is
    evidence at all. Everything else is walked through, because `and_()`, `.where()`,
    `.join()` and friends can only narrow further.
    """
    arms = _disjunction_arms(node)
    if arms is not None:
        return _is_library_disjunction(arms)
    if isinstance(node, ast.Compare):
        return _is_venue_equality(node)
    if isinstance(node, ast.UnaryOp):
        return False  # `~(venue_id == self.venue_id)` is the predicate inverted
    if isinstance(node, ast.Call):
        if _call_name(node) in NEGATION_FUNCTIONS:
            return False
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in VENUE_SCOPED_HELPERS
            and _dotted(node.func.value) == "self"
        ):
            return True
    return any(_direct_venue_evidence(child) for child in ast.iter_child_nodes(node))


class Module:
    """One parsed file, plus the questions the three rules ask of it."""

    def __init__(self, source: str, filename: str) -> None:
        self.source = source
        self.filename = filename
        self.tree = ast.parse(source, filename=filename)
        self.parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
        self.docstrings = docstring_nodes(self.tree)
        self.sql_names, self.module_aliases = self._sqlalchemy_bindings()

    # -- imports ------------------------------------------------------------------------

    def _sqlalchemy_bindings(self) -> tuple[frozenset[str], frozenset[str]]:
        """Names this file bound from `sqlalchemy*`: (statement constructors, modules).

        Without this an alias switches the guard off: `from sqlalchemy import select as
        sel` used to make every `sel(...)` invisible to it.
        """
        constructors: set[str] = set()
        modules: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "sqlalchemy":
                        modules.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] != "sqlalchemy":
                    continue
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if alias.name in SQL_CONSTRUCTORS:
                        constructors.add(bound)
                    elif alias.name.islower():
                        # `from sqlalchemy.dialects import postgresql` -> postgresql.insert
                        modules.add(bound)
        # `q = select` (or `q = sa.select`) hands the same callable a second name; without
        # this the whole rule is switched off by one assignment.
        changed = True
        while changed:
            changed = False
            for node in ast.walk(self.tree):
                if not isinstance(node, ast.Assign):
                    continue
                value = node.value
                aliased = (isinstance(value, ast.Name) and value.id in constructors) or (
                    isinstance(value, ast.Attribute)
                    and value.attr in SQL_CONSTRUCTORS
                    and _root_name(value.value) in modules
                )
                if not aliased:
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in constructors:
                        constructors.add(target.id)
                        changed = True
        return frozenset(constructors), frozenset(modules)

    def sql_constructor_imports(self) -> list[tuple[int, str]]:
        found: list[tuple[int, str]] = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if (node.module or "").split(".")[0] != "sqlalchemy":
                continue
            found.extend(
                (node.lineno, alias.name) for alias in node.names if alias.name in SQL_CONSTRUCTORS
            )
        return found

    # -- navigation ---------------------------------------------------------------------

    def enclosing_statement(self, node: ast.AST) -> ast.stmt | None:
        current: ast.AST | None = node
        while current is not None:
            if isinstance(current, ast.stmt):
                return current
            current = self.parents.get(current)
        return None

    def enclosing_function(self, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        current: ast.AST | None = node
        while current is not None:
            if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
                return current
            current = self.parents.get(current)
        return None

    def function_name(self, node: ast.AST) -> str:
        return self._qualified(self.enclosing_function(node))

    def _qualified(self, function: ast.FunctionDef | ast.AsyncFunctionDef | None) -> str:
        if function is None:
            return "<module>"
        current: ast.AST | None = self.parents.get(function)
        while current is not None:
            if isinstance(current, ast.ClassDef):
                return f"{current.name}.{function.name}"
            current = self.parents.get(current)
        return function.name

    def function_at_line(self, lineno: int) -> str:
        """Innermost function containing `lineno`, qualified by its class."""
        best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end and (best is None or node.lineno > best.lineno):
                best = node
        return self._qualified(best)

    # -- classification -----------------------------------------------------------------

    def sql_calls(self) -> Iterator[tuple[ast.Call, str]]:
        """Every call that builds or runs SQL, with a human-readable kind."""
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            kind = self.classify(node)
            if kind is not None:
                yield node, kind

    def classify(self, node: ast.Call) -> str | None:
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in self.sql_names:
                return f"{func.id}()"
            return None
        if not isinstance(func, ast.Attribute):
            return None
        receiver = _dotted(func.value)
        if func.attr in SQL_CONSTRUCTORS and _root_name(func.value) in self.module_aliases:
            return f"{receiver}.{func.attr}()"
        if not SESSION_RECEIVER.search(receiver):
            return None
        if func.attr in SESSION_QUERY_METHODS:
            return f"{receiver}.{func.attr}()"
        if func.attr in SESSION_IDENTITY_METHODS:
            return f"{receiver}.{func.attr}()"
        if func.attr in SESSION_WRITE_METHODS:
            return f"{receiver}.{func.attr}()"
        return None

    def identity_calls(self) -> Iterator[tuple[ast.Call, str]]:
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                receiver = _dotted(func.value)
                if func.attr in SESSION_IDENTITY_METHODS and SESSION_RECEIVER.search(receiver):
                    yield node, f"{receiver}.{func.attr}()"
                continue
            # `getattr(self.session, "get")(Model, pk)` is the same call, spelled around
            # the rule; the name of the method is right there in the arguments.
            if _dotted(func).endswith("getattr") and len(node.args) >= 2:
                receiver = _dotted(node.args[0])
                wanted = node.args[1]
                if (
                    SESSION_RECEIVER.search(receiver)
                    and isinstance(wanted, ast.Constant)
                    and wanted.value in SESSION_IDENTITY_METHODS
                ):
                    yield node, f"getattr({receiver}, {wanted.value!r})"

    def dynamic_access(self) -> Iterator[tuple[int, str]]:
        """`getattr(sa, "select")`, `import_module("sqlalchemy")`, `__import__(...)`."""
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            name = _dotted(node.func)
            if name.endswith("getattr") and node.args:
                target = _dotted(node.args[0])
                if _root_name(node.args[0]) in self.module_aliases or SESSION_RECEIVER.search(
                    target
                ):
                    yield node.lineno, f"getattr({target}, ...)"
            if name.endswith("import_module") or name == "__import__":
                first = node.args[0] if node.args else None
                if (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and first.value.split(".")[0] == "sqlalchemy"
                ):
                    yield node.lineno, f"{name}({first.value!r})"

    def raw_sql_literals(self) -> Iterator[tuple[int, str]]:
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in self.docstrings:
                continue
            if RAW_SQL.match(node.value):
                yield node.lineno, node.value.strip()[:60]

    # -- rule 3: venue evidence ---------------------------------------------------------

    def _name_is_scoped(
        self,
        name: str,
        scope: ast.AST,
        seen: frozenset[str],
    ) -> bool:
        if name in seen:
            return False
        seen = seen | {name}
        values = _assigned_values(scope, name)
        if not values:
            return False
        scoped = False
        for value in values:
            if _direct_venue_evidence(value):
                scoped = True
                continue
            derived = {node.id for node in ast.walk(value) if isinstance(node, ast.Name)}
            if name in derived:
                # `stmt = stmt.order_by(...)`: a refinement of the same statement. Adding
                # clauses can only narrow it; a widening `union` would carry its own
                # `select()`, which is checked as a query in its own right.
                continue
            if any(self._name_is_scoped(other, scope, seen) for other in derived - seen):
                scoped = True
                continue
            # A rebinding with neither a venue predicate nor a scoped source: the name
            # cannot be trusted, whatever the other assignments look like.
            return False
        return scoped

    def _helper_call_sites_are_scoped(
        self,
        helper: ast.FunctionDef | ast.AsyncFunctionDef,
        statement: ast.stmt,
    ) -> bool:
        """A private helper is scoped when its callers hand it the venue predicate.

        The statement has to actually use one of the helper's parameters, and then *every*
        `self.<helper>(...)` in the file must pass a venue predicate in that position. One
        careless call site and the helper stops counting — which is the difference between
        this and a blanket exemption.
        """
        if not helper.name.startswith("_"):
            return False
        positional = [argument.arg for argument in helper.args.args][1:]
        keyword_only = [argument.arg for argument in helper.args.kwonlyargs]
        parameters = positional + keyword_only
        used = {
            node.id
            for node in ast.walk(statement)
            if isinstance(node, ast.Name) and node.id in parameters
        }
        if not used:
            return False

        call_sites = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == helper.name
            and _dotted(node.func.value) == "self"
        ]
        if not call_sites:
            return False
        for call in call_sites:
            for parameter in used:
                argument = self._argument_for(call, parameter, positional)
                if argument is None or not _direct_venue_evidence(argument):
                    return False
        return True

    @staticmethod
    def _argument_for(
        call: ast.Call,
        parameter: str,
        positional: list[str],
    ) -> ast.expr | None:
        for keyword in call.keywords:
            if keyword.arg == parameter:
                return keyword.value
        if parameter in positional:
            index = positional.index(parameter)
            if index < len(call.args):
                return call.args[index]
        return None

    def query_is_venue_scoped(self, call: ast.Call) -> bool:
        statement = self.enclosing_statement(call)
        if statement is not None and _direct_venue_evidence(statement):
            return True
        function = self.enclosing_function(call)
        if function is None or statement is None:
            return False
        arguments: list[ast.expr] = [*call.args, *(kw.value for kw in call.keywords)]
        for argument in arguments:
            for node in ast.walk(argument):
                if isinstance(node, ast.Name) and self._name_is_scoped(
                    node.id, function, frozenset()
                ):
                    return True
        return self._helper_call_sites_are_scoped(function, statement)


# --------------------------------------------------------------------------------------
# The three rules as functions, so the guard can be pointed at a string in its own tests
# --------------------------------------------------------------------------------------


def sql_outside_repositories(source: str, filename: str = "<probe>") -> list[str]:
    module = Module(source, filename)
    offenders = [f"line {node.lineno}: {kind}" for node, kind in module.sql_calls()]
    offenders += [
        f"line {lineno}: imports {name!r} from sqlalchemy"
        for lineno, name in module.sql_constructor_imports()
    ]
    offenders += [f"line {lineno}: {what}" for lineno, what in module.dynamic_access()]
    offenders += [
        f"line {lineno}: raw SQL literal {snippet!r}"
        for lineno, snippet in module.raw_sql_literals()
    ]
    return sorted(set(offenders))


def identity_lookups(source: str, filename: str = "<probe>") -> list[str]:
    module = Module(source, filename)
    return [f"line {node.lineno}: {kind}" for node, kind in module.identity_calls()]


def unscoped_queries(source: str, filename: str = "<probe>") -> list[str]:
    """Queries inside the repository layer that carry no venue predicate."""
    module = Module(source, filename)
    offenders: list[str] = []
    for node, kind in module.sql_calls():
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in SESSION_WRITE_METHODS:
            # `session.add(obj)` persists an object the repository built; the venue is a
            # column on that object, not a predicate on a statement.
            continue
        if module.query_is_venue_scoped(node):
            continue
        function = module.function_name(node)
        if (filename, function) in ALLOWED_VENUE_EXEMPTIONS:
            continue
        offenders.append(f"line {node.lineno}: {kind} in {function}")
    return sorted(offenders)


def analysed_query_count(source: str, filename: str = "<probe>") -> int:
    return sum(1 for _ in Module(source, filename).sql_calls())


# --------------------------------------------------------------------------------------
# Rule 1: no SQL outside src/db/repositories/
# --------------------------------------------------------------------------------------

OUTSIDE_REPOSITORIES = [
    path for path in python_files(SRC_DIR) if REPOSITORIES_DIR not in path.parents
]
INSIDE_REPOSITORIES = python_files(REPOSITORIES_DIR)


@pytest.mark.parametrize("path", OUTSIDE_REPOSITORIES, ids=relative)
def test_no_sql_outside_repositories(path: Path) -> None:
    offenders = sql_outside_repositories(read(path), relative(path))
    assert not offenders, (
        f"{relative(path)}: SQL belongs to src/db/repositories/ only (TZ 3.2). "
        "An alias, a module attribute or a session method is still SQL: " + "; ".join(offenders)
    )


# --------------------------------------------------------------------------------------
# Rule 2: identity lookups are banned everywhere
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", python_files(SRC_DIR), ids=relative)
def test_session_identity_lookups_are_banned(path: Path) -> None:
    offenders = identity_lookups(read(path), relative(path))
    assert not offenders, (
        f"{relative(path)}: session.get()/get_one()/merge() ignore every filter, so a "
        "forged id reaches another venue's row (TZ 3.3, acceptance 11.3). Use by_id(), "
        "which keeps the venue predicate: " + "; ".join(offenders)
    )


# --------------------------------------------------------------------------------------
# Rule 3: every query in the repositories is venue scoped
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", INSIDE_REPOSITORIES, ids=relative)
def test_queries_inside_repositories_are_venue_scoped(path: Path) -> None:
    offenders = unscoped_queries(read(path), relative(path))
    assert not offenders, (
        f"{relative(path)}: a query without a venue predicate (TZ 3.3, acceptance 11.3). "
        "Build it from for_venue()/library()/by_id()/for_parent(), or compare against "
        "self.venue_id: " + "; ".join(offenders)
    )


@pytest.mark.parametrize("path", INSIDE_REPOSITORIES, ids=relative)
def test_venue_exempt_comments_are_registered(path: Path) -> None:
    """The marker documents an exemption; it never grants one.

    A `# venue-exempt:` comment is legal only where this file already lists the function,
    and it must carry a reason. The comment is found with `tokenize`, so the same text
    inside a string or in the middle of an expression proves nothing.
    """
    source = read(path)
    module = Module(source, relative(path))
    for lineno, comment in comments(source):
        if EXEMPT_MARKER not in comment:
            continue
        reason = comment.split(EXEMPT_MARKER, 1)[1].strip()
        assert reason, f"{relative(path)}:{lineno}: '{EXEMPT_MARKER}' without a reason"
        function = module.function_at_line(lineno)
        assert (relative(path), function) in ALLOWED_VENUE_EXEMPTIONS, (
            f"{relative(path)}:{lineno}: an exemption for {function} is not registered in "
            "ALLOWED_VENUE_EXEMPTIONS of tests/test_static_repos.py; a comment alone "
            "grants nothing"
        )


def test_exemption_list_has_no_stale_entries() -> None:
    """An exemption that is no longer needed must be deleted, not left to rot."""
    for (filename, function), reason in ALLOWED_VENUE_EXEMPTIONS.items():
        assert reason.strip(), f"{filename}:{function} is exempt without a reason"
        path = REPO_ROOT / filename
        assert path.exists(), f"{filename} no longer exists; drop the exemption"
        module = Module(read(path), filename)
        functions = {module.function_name(node) for node, _ in module.sql_calls()}
        assert function in functions, (
            f"{filename}:{function} holds no query any more; drop the exemption"
        )


def test_the_guard_is_not_vacuous() -> None:
    """Rules that scan nothing pass for the wrong reason."""
    assert INSIDE_REPOSITORIES, "src/db/repositories/ has no modules to check"
    assert OUTSIDE_REPOSITORIES, "src/ outside the repositories has no modules to check"
    analysed = sum(analysed_query_count(read(path), relative(path)) for path in INSIDE_REPOSITORIES)
    assert analysed >= 3, f"only {analysed} queries were analysed; rule 3 is not exercising"


# --------------------------------------------------------------------------------------
# The guard's own teeth: every trick that used to work is asserted to be caught
# --------------------------------------------------------------------------------------

KNOWN_BYPASSES: list[tuple[str, str, str]] = [
    (
        "comment claiming a venue filter",
        "venue",
        """
from sqlalchemy import select
from src.db.models import ChecklistItem

class Repo:
    def get(self, item_id):
        return select(ChecklistItem).where(ChecklistItem.id == item_id)  # venue_id via template
""",
    ),
    (
        "comment claiming a library lookup",
        "venue",
        """
from sqlalchemy import select
from src.db.models import Recipe

class Repo:
    def get(self, recipe_id):
        return select(Recipe).where(Recipe.id == recipe_id)  # library lookup
""",
    ),
    (
        "venue filter with the operator inverted",
        "venue",
        """
from sqlalchemy import select
from src.db.models import Product

class Repo:
    def others(self):
        return select(Product).where(Product.venue_id != self.venue_id)
""",
    ),
    (
        "venue filter written with is_not",
        "venue",
        """
from sqlalchemy import select
from src.db.models import Product

class Repo:
    def others(self):
        return select(Product).where(Product.venue_id.is_not(self.venue_id))
""",
    ),
    (
        "venue filter compared with `is`",
        "venue",
        """
from sqlalchemy import select
from src.db.models import Product

class Repo:
    def all(self):
        return select(Product).where(Product.venue_id is self.venue_id)
""",
    ),
    (
        "venue filter negated",
        "venue",
        """
from sqlalchemy import select
from src.db.models import Product

class Repo:
    def others(self):
        return select(Product).where(~(Product.venue_id == self.venue_id))
""",
    ),
    (
        "venue filter widened with or_(..., true())",
        "venue",
        """
from sqlalchemy import or_, select, true
from src.db.models import Product

class Repo:
    def all(self):
        return select(Product).where(or_(Product.venue_id == self.venue_id, true()))
""",
    ),
    (
        "venue filter widened with a second predicate",
        "venue",
        """
from sqlalchemy import or_, select
from src.db.models import Product

class Repo:
    def all(self, name):
        return select(Product).where(
            or_(Product.venue_id == self.venue_id, Product.name == name)
        )
""",
    ),
    (
        "venue filter widened with the | operator",
        "venue",
        """
from sqlalchemy import select, text
from src.db.models import Product

class Repo:
    def all(self):
        return select(Product).where((Product.venue_id == self.venue_id) | text("true"))
""",
    ),
    (
        "IS NULL taken on a column that is not the one filtered",
        "venue",
        """
from sqlalchemy import or_, select
from src.db.models import Recipe

class Repo:
    def all(self):
        return select(Recipe).where(
            or_(Recipe.venue_id == self.venue_id, Recipe.deleted_at.is_(None))
        )
""",
    ),
    (
        "venue_id mentioned only in order_by",
        "venue",
        """
from sqlalchemy import select
from src.db.models import Product

class Repo:
    def get(self, pid):
        return select(Product).where(Product.id == pid).order_by(Product.venue_id)
""",
    ),
    (
        "forged venue-exempt marker",
        "venue",
        """
from sqlalchemy import select
from src.db.models import Order

class Repo:
    def get(self, oid):
        return select(Order).where(Order.id == oid)  # venue-exempt: trust me
""",
    ),
    (
        "import alias instead of select",
        "venue",
        """
from sqlalchemy import select as sel
from src.db.models import OrderItem

class Repo:
    def get(self, oid):
        return sel(OrderItem).where(OrderItem.id == oid)
""",
    ),
    (
        "update() without a venue predicate",
        "venue",
        """
from sqlalchemy import update
from src.db.models import Product

class Repo:
    def rename(self, pid, name):
        return update(Product).where(Product.id == pid).values(name=name)
""",
    ),
    (
        "delete() without a venue predicate",
        "venue",
        """
from sqlalchemy import delete
from src.db.models import Order

class Repo:
    def drop(self, oid):
        return delete(Order).where(Order.id == oid)
""",
    ),
    (
        "statement built in a local, then executed",
        "venue",
        """
from sqlalchemy import select
from src.db.models import Product

class Repo:
    async def all(self):
        stmt = select(Product)
        return await self.session.scalars(stmt)
""",
    ),
    (
        "scoped statement rebound to an unscoped one",
        "venue",
        """
from sqlalchemy import select
from src.db.models import Product

class Repo:
    async def all(self, pid):
        stmt = self.for_venue()
        stmt = select(Product).where(Product.id == pid)
        return await self.session.scalars(stmt)
""",
    ),
    (
        "private helper called with a non-venue filter",
        "venue",
        """
from sqlalchemy import select

class Repo:
    def _filtered(self, condition):
        return select(self.model).where(condition)

    def for_venue(self):
        return self._filtered(self.venue_column == self.venue_id)

    def by_name(self, name):
        return self._filtered(self.model.name == name)
""",
    ),
    (
        "constructor renamed at runtime",
        "venue",
        """
from sqlalchemy import select
from src.db.models import Product

query = select

class Repo:
    def get(self, pid):
        return query(Product).where(Product.id == pid)
""",
    ),
    (
        "identity lookup spelled through getattr",
        "outside",
        """
class Repo:
    async def load(self, oid):
        return await getattr(self.session, "get")(oid)
""",
    ),
    (
        "session.get in a service",
        "outside",
        """
from src.db.models import ChecklistItem

async def load(session, item_id):
    return await session.get(ChecklistItem, item_id)
""",
    ),
    (
        "session.merge in a service",
        "outside",
        """
async def save(session, obj):
    return await session.merge(obj)
""",
    ),
    (
        "driver-level SQL with an f-string",
        "outside",
        """
async def find(session, name):
    return await session.exec_driver_sql(f"SELECT * FROM products WHERE name = '{name}'")
""",
    ),
    (
        "select aliased on import, outside the repositories",
        "outside",
        """
from sqlalchemy import select as sel
from src.db.models import OrderItem

def query(oid):
    return sel(OrderItem).where(OrderItem.id == oid)
""",
    ),
    (
        "module alias instead of a bare name",
        "outside",
        """
import sqlalchemy as sa
from src.db.models import Order

def query(oid):
    return sa.select(Order).where(Order.id == oid)
""",
    ),
    (
        "postgresql dialect insert",
        "outside",
        """
from sqlalchemy.dialects import postgresql
from src.db.models import Notification

def upsert(values):
    return postgresql.insert(Notification).values(**values)
""",
    ),
    (
        "sqlalchemy reached through getattr",
        "outside",
        """
import sqlalchemy as sa

def query(model):
    build = getattr(sa, "select")
    return build(model)
""",
    ),
    (
        "sqlalchemy imported dynamically",
        "outside",
        """
import importlib

def query(model):
    return importlib.import_module("sqlalchemy").select(model)
""",
    ),
    (
        "raw SQL handed to the session",
        "outside",
        """
async def find(session):
    return await session.execute("SELECT id FROM orders WHERE venue_id = 1")
""",
    ),
    (
        "unit-of-work write outside the repositories",
        "outside",
        """
async def save(session, obj):
    session.add(obj)
""",
    ),
]

CLEAN_SOURCES: list[tuple[str, str, str]] = [
    (
        "venue predicate on the statement",
        "venue",
        """
from sqlalchemy import select

class Repo:
    def for_venue(self):
        return select(self.model).where(self.venue_column == self.venue_id)
""",
    ),
    (
        "statement built from for_venue() through a local",
        "venue",
        """
class Repo:
    async def listing(self):
        stmt = self.for_venue()
        stmt = stmt.order_by(self.model.name)
        return await self.session.scalars(stmt)
""",
    ),
    (
        "private helper whose call sites all pass a venue predicate",
        "venue",
        """
from sqlalchemy import or_, select

class Repo:
    def _through_parent(self, venue_id_filter):
        return select(self.model).join(self.parent).where(venue_id_filter)

    def for_venue(self):
        return self._through_parent(self.venue_column == self.venue_id)

    def library(self):
        column = self.venue_column
        return self._through_parent(or_(column == self.venue_id, column.is_(None)))
""",
    ),
    (
        "the library shape, spelled with the | operator",
        "venue",
        """
from sqlalchemy import select

class Repo:
    def library(self):
        column = self.venue_column
        return select(self.model).where((column == self.venue_id) | column.is_(None))
""",
    ),
    (
        "a second, narrowing predicate next to the venue filter",
        "venue",
        """
from sqlalchemy import and_, select

class Repo:
    def active(self):
        return select(self.model).where(
            and_(self.venue_column == self.venue_id, self.model.is_active)
        )
""",
    ),
    (
        "dictionary and environment lookups are not identity lookups",
        "outside",
        """
import os

def read(payload):
    return payload.get("type"), os.environ.get("APP_ENV"), {}.get("x")
""",
    ),
    (
        "prose that starts with a SQL word",
        "outside",
        '''
def rename():
    """Update the row where the identifier matches."""
    return None
''',
    ),
]


@pytest.mark.parametrize(
    ("name", "rule", "source"),
    KNOWN_BYPASSES,
    ids=[name for name, _, _ in KNOWN_BYPASSES],
)
def test_known_bypasses_are_caught(name: str, rule: str, source: str) -> None:
    offenders = (
        unscoped_queries(source, "src/db/repositories/probe.py")
        if rule == "venue"
        else sql_outside_repositories(source, "src/services/probe.py")
        + identity_lookups(source, "src/services/probe.py")
    )
    assert offenders, f"the guard still lets {name!r} through"


@pytest.mark.parametrize(
    ("name", "rule", "source"),
    CLEAN_SOURCES,
    ids=[name for name, _, _ in CLEAN_SOURCES],
)
def test_correct_code_is_not_flagged(name: str, rule: str, source: str) -> None:
    offenders = (
        unscoped_queries(source, "src/db/repositories/probe.py")
        if rule == "venue"
        else sql_outside_repositories(source, "src/services/probe.py")
        + identity_lookups(source, "src/services/probe.py")
    )
    assert not offenders, f"{name!r} is correct code but was flagged: {offenders}"


# --------------------------------------------------------------------------------------
# Runtime shape of the repository layer (no database involved)
# --------------------------------------------------------------------------------------


def test_library_scope_is_whitelisted() -> None:
    from src.db.repositories import LIBRARY_TABLES

    # Decision D9: preps are deliberately absent until question C4 is answered.
    assert set(LIBRARY_TABLES) == {"recipes", "knowledge_articles"}


def test_for_venue_and_library_produce_the_two_allowed_shapes() -> None:
    from src.db.models import Recipe
    from src.db.repositories import BaseRepository

    class RecipeRepo(BaseRepository[Recipe]):
        model = Recipe

    repo = RecipeRepo(session=None, venue_id=1)  # type: ignore[arg-type]

    assert "recipes.venue_id = " in str(repo.for_venue())
    library_sql = str(repo.library())
    assert "recipes.venue_id = " in library_sql
    assert "recipes.venue_id IS NULL" in library_sql
    # A lookup by id keeps the scope: a foreign id returns nothing, not a row.
    assert "recipes.venue_id = " in str(repo.by_id(7))


def test_library_is_refused_outside_the_whitelist() -> None:
    from src.db.models import Prep
    from src.db.repositories import BaseRepository, LibraryScopeError

    class PrepRepo(BaseRepository[Prep]):
        model = Prep

    repo = PrepRepo(session=None, venue_id=1)  # type: ignore[arg-type]

    with pytest.raises(LibraryScopeError):
        repo.library()


def test_a_repository_may_not_choose_its_own_venue_column() -> None:
    """The property used to be an override point, and overriding it broke every shape.

    `Product.__table__.c.id` returned from `venue_column` left `for_venue()`, `library()`
    and `by_id()` in place and filtering on the primary key: `WHERE products.id = 1`
    instead of `WHERE products.venue_id = 1`, which reads every venue in the database.
    """
    from typing import Any

    from sqlalchemy import ColumnElement
    from src.db.models import Base, Product
    from src.db.repositories import BaseRepository, RepositoryScopeError

    with pytest.raises(RepositoryScopeError):

        class Wrong(BaseRepository[Product]):
            model = Product

            @property
            def venue_column(self) -> ColumnElement[Any]:
                return Product.__table__.c.id

    with pytest.raises(RepositoryScopeError):

        class AlsoWrong(BaseRepository[Product]):
            model = Product

            @classmethod
            def scope_model(cls) -> type[Base]:
                return Product


def test_a_venue_column_patched_after_class_creation_is_still_refused() -> None:
    """`__init_subclass__` cannot see an assignment made later; the query shapes can."""
    from src.db.models import Product
    from src.db.repositories import BaseRepository, VenueColumnError

    class ProductRepo(BaseRepository[Product]):
        model = Product

    ProductRepo.venue_column = property(  # type: ignore[assignment]
        lambda self: Product.__table__.c.id
    )
    repo = ProductRepo(session=None, venue_id=1)  # type: ignore[arg-type]

    for shape in (repo.for_venue, lambda: repo.by_id(7)):
        with pytest.raises(VenueColumnError):
            shape()


@pytest.mark.parametrize("venue_id", [None, 0, -1, True, "1", 1.0])
def test_a_repository_refuses_to_exist_without_a_venue(venue_id: object) -> None:
    """`venue_id=None` used to render `venue_id IS NULL` — the whole shared library."""
    from src.db.models import Recipe
    from src.db.repositories import BaseRepository, VenueScopeError

    class RecipeRepo(BaseRepository[Recipe]):
        model = Recipe

    with pytest.raises(VenueScopeError):
        RecipeRepo(session=None, venue_id=venue_id)  # type: ignore[arg-type]


def test_child_rows_are_reachable_only_through_their_parent() -> None:
    from src.db.models import ChecklistItem, ChecklistTemplate
    from src.db.repositories import ChildRepository

    class ItemRepo(ChildRepository[ChecklistItem, ChecklistTemplate]):
        model = ChecklistItem
        parent = ChecklistTemplate
        parent_fk = "template_id"

    repo = ItemRepo(session=None, venue_id=1)  # type: ignore[arg-type]

    for statement in (repo.for_venue(), repo.by_id(5), repo.for_parent(3)):
        sql = str(statement)
        assert "JOIN checklist_templates" in sql
        assert "checklist_templates.venue_id = " in sql


def test_a_child_repository_is_refused_for_a_venue_scoped_model() -> None:
    from src.db.models import ChecklistTemplate, Product
    from src.db.repositories import ChildRepository, RepositoryScopeError

    with pytest.raises(RepositoryScopeError):

        class Wrong(ChildRepository[Product, ChecklistTemplate]):
            model = Product
            parent = ChecklistTemplate
            parent_fk = "template_id"


def test_a_base_repository_is_refused_for_a_child_model() -> None:
    from src.db.models import ChecklistItem
    from src.db.repositories import BaseRepository, RepositoryScopeError

    with pytest.raises(RepositoryScopeError):

        class Wrong(BaseRepository[ChecklistItem]):
            model = ChecklistItem


def test_a_child_repository_must_match_the_schema_map() -> None:
    from src.db.models import ChecklistItem, Order
    from src.db.repositories import ChildRepository, RepositoryScopeError

    with pytest.raises(RepositoryScopeError):

        class Wrong(ChildRepository[ChecklistItem, Order]):
            model = ChecklistItem
            parent = Order
            parent_fk = "template_id"
