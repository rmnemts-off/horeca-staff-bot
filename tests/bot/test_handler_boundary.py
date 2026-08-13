"""What a handler may not do (TZ 3.2, 9; plan tasks 20 and 35).

`src/bot/handlers/` is empty today and will hold nine modules by the end of stage 1. This
guard is written **before** them on purpose: the framework of plan task 20 exists so that
nobody writes the venue check, the callback parsing and the SQL a tenth time, and a rule
introduced after the code it constrains is a rule somebody has to go back and enforce.

**A handler is what a router registers, not what sits in a directory.** Scanning
`src/bot/handlers/` alone made this file green and empty at the same time: the directory
holds nothing but `__init__.py`, while `src/bot/routers.py` has been registering a live
`/start` handler the whole time — outside the directory, so outside every rule below. The
scope is therefore computed from the code: every `router.<observer>.register(fn, …)` and
every `@router.<observer>(…)` in `src/` is followed to the module that *defines* `fn`, and
that module is a handler module wherever it lies (:func:`handler_modules`). The directory
stays in scope on top of that, so the nine modules of stage 1 are covered from their first
empty commit rather than from their first registration.

The rules read whole modules and not just the function bodies on purpose: two of the three
are about imports, and an import that a handler uses is written at the top of its file.

Three rules, all decided on the syntax tree.

1. **A handler never takes a raw `callback_data` apart.** No `.data` off a callback query,
   no `parse()`/`factory_for()`/`unpack()`. The resolver
   (`src/bot/middlewares/resolver.py`) has already parsed the payload, fetched the object
   through the venue's repository and checked the presser's rights; a handler that reads
   the string again is a handler that has skipped all three (TZ 9).
2. **A handler never imports a repository.** Data access is the service layer's
   (TZ 3.2): the handler takes an update, calls a service, renders the reply. This is what
   keeps a web admin panel possible on the same logic later, and it is also what keeps the
   venue predicate in one place (TZ 3.3).
3. **A handler never touches a database session.** No `AsyncSession`, no `session_scope`,
   no parameter named `session`, and no `session.execute()`-shaped call. The session of an
   update is opened and committed by `src/bot/middlewares/services.py`; a handler that
   reaches for it owns a transaction it did not open. The same rule bans a parameter named
   `repositories` and the attribute `.repositories`, which are the two ways the container
   could be picked out of the handler context by name.

**What this file is not.** It is not a sandbox: the code being checked and the checker live
in the same repository, and whoever can edit one can edit the other (the same threat model
as `tests/test_static_repos.py`, which additionally bans SQL everywhere outside
`src/db/repositories/`, handlers included). It defends against the ordinary slip — a
handler that needs one more field and reaches for the repository because it is right there.

Every rule is asserted against the code it exists to catch (`BYPASSES`) and against a
handler written the way the framework intends (`LEGITIMATE`), so a rule cannot rot into one
that fires on nothing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

import pytest

from tests.repo_scan import REPO_ROOT, SRC_DIR, python_files, read, relative

HANDLERS_DIR = SRC_DIR / "bot" / "handlers"

#: The aiogram observers a router exposes. A handler is attached to one of them, either by
#: `router.<observer>.register(fn, …)` or by decorating `fn` with `@router.<observer>(…)`.
#: Named explicitly so that `dispatcher.update.outer_middleware(…)` — which is a call of
#: the same shape on a neighbouring attribute — is not mistaken for a handler.
OBSERVERS: Final = frozenset(
    {
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "business_connection",
        "business_message",
        "edited_business_message",
        "deleted_business_messages",
        "message_reaction",
        "message_reaction_count",
        "inline_query",
        "chosen_inline_result",
        "callback_query",
        "shipping_query",
        "pre_checkout_query",
        "purchased_paid_media",
        "poll",
        "poll_answer",
        "my_chat_member",
        "chat_member",
        "chat_join_request",
        "chat_boost",
        "removed_chat_boost",
        "error",
        "errors",
    }
)

#: Reading a payload back after the resolver has already turned it into an object.
RAW_PAYLOAD_ATTRIBUTE: Final = "data"

#: Ways of taking a payload apart. `parse` and `factory_for` live in `src/bot/callbacks.py`
#: and are the resolver's; `unpack` is aiogram's own on a callback factory.
PARSING_CALLS: Final = frozenset({"parse", "factory_for", "unpack"})

#: Modules a handler may not import. The repository layer is the service layer's business.
FORBIDDEN_MODULES: Final = frozenset({"src.db.repositories", "src.db.session"})

#: Names that are a database session however they are spelled.
SESSION_NAMES: Final = frozenset(
    {"AsyncSession", "async_sessionmaker", "sessionmaker", "session_scope", "create_async_engine"}
)

#: Parameters through which aiogram would inject something a handler must not have: the
#: context keys of `src/bot/middlewares/`.
FORBIDDEN_PARAMETERS: Final = frozenset({"session", "repositories", "container"})

#: The same two, reached as attributes of something the handler *is* given.
FORBIDDEN_ATTRIBUTES: Final = frozenset({"repositories"})

#: Methods that run or stage a statement. Counted on a session-like receiver only, so that
#: `payload.get(...)` and `services.recipes.search(...)` are not mistaken for one.
SESSION_METHODS: Final = frozenset(
    {
        "execute",
        "scalar",
        "scalars",
        "stream",
        "exec_driver_sql",
        "add",
        "add_all",
        "flush",
        "commit",
        "rollback",
        "refresh",
        "merge",
        "get_one",
    }
)

#: What counts as a session on the left of a dot (same shape as `tests/test_static_repos.py`).
SESSION_RECEIVER: Final = re.compile(
    r"(?:^|\.)(?:session|sess|db|dbsession|db_session|conn|connection)$"
)


def _root_name(node: ast.AST) -> str:
    return ast.unparse(node) if isinstance(node, ast.Name | ast.Attribute) else ""


# --------------------------------------------------------------------------------------
# The three rules, as functions so the guard can be pointed at a string of its own
# --------------------------------------------------------------------------------------


def raw_callback_reads(source: str, filename: str = "<probe>") -> list[str]:
    """Rule 1: the payload is parsed by the resolver, and by nobody else."""
    tree = ast.parse(source, filename=filename)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == RAW_PAYLOAD_ATTRIBUTE:
            offenders.append(f"line {node.lineno}: reads {ast.unparse(node)}")
        elif isinstance(node, ast.Call):
            called = ast.unparse(node.func).split(".")[-1]
            if called in PARSING_CALLS:
                offenders.append(f"line {node.lineno}: calls {ast.unparse(node.func)}()")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "src.bot.callbacks"
        ):
            offenders.extend(
                f"line {node.lineno}: imports the parser {alias.name!r}"
                for alias in node.names
                if alias.name in PARSING_CALLS
            )
    return sorted(set(offenders))


def repository_imports(source: str, filename: str = "<probe>") -> list[str]:
    """Rule 2: data access belongs to `src/services/` (TZ 3.2)."""
    tree = ast.parse(source, filename=filename)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(
                f"line {node.lineno}: imports {alias.name!r}"
                for alias in node.names
                if _is_forbidden_module(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_forbidden_module(module):
                offenders.append(f"line {node.lineno}: imports from {module!r}")
                continue
            offenders.extend(
                f"line {node.lineno}: imports {alias.name!r} from {module!r}"
                for alias in node.names
                if _is_forbidden_module(f"{module}.{alias.name}")
            )
    return sorted(set(offenders))


def _is_forbidden_module(dotted: str) -> bool:
    return any(dotted == module or dotted.startswith(f"{module}.") for module in FORBIDDEN_MODULES)


def session_use(source: str, filename: str = "<probe>") -> list[str]:
    """Rule 3: the session of an update is the middleware's, not the handler's."""
    tree = ast.parse(source, filename=filename)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in SESSION_NAMES:
            offenders.append(f"line {node.lineno}: names {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in SESSION_NAMES:
                offenders.append(f"line {node.lineno}: names {ast.unparse(node)}")
            elif node.attr in FORBIDDEN_ATTRIBUTES:
                offenders.append(f"line {node.lineno}: reaches for {ast.unparse(node)}")
        elif isinstance(node, ast.arg) and node.arg in FORBIDDEN_PARAMETERS:
            offenders.append(f"line {node.lineno}: takes a {node.arg!r} parameter")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = _root_name(node.func.value)
            if node.func.attr in SESSION_METHODS and SESSION_RECEIVER.search(receiver):
                offenders.append(f"line {node.lineno}: calls {ast.unparse(node.func)}()")
    return sorted(set(offenders))


# --------------------------------------------------------------------------------------
# What counts as a handler: whatever a router registers, wherever it lives
# --------------------------------------------------------------------------------------


def _is_observer(node: ast.AST) -> bool:
    """`router.message`, `dispatcher.callback_query` — the thing a handler attaches to."""
    return isinstance(node, ast.Attribute) and node.attr in OBSERVERS


def _is_observer_register(node: ast.AST) -> bool:
    """`router.message.register`, and not `router.message.outer_middleware.register`."""
    return isinstance(node, ast.Attribute) and node.attr == "register" and _is_observer(node.value)


#: A registration whose target the guard cannot name: `register(SCREENS["toggle"])`,
#: `register(partial(handle, mode=1))`, `register(build_handler())`. Dropping it as "no name
#: found" reads as "no handler here" and quietly takes the registering module out of scope —
#: which is the whole bypass. It is kept as a reference that resolves to nothing, so
#: `handler_modules()` falls back to its documented rule and keeps the module in scope.
UNRESOLVED: Final = "<unresolved registration>"


def _registered_names(tree: ast.Module) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_observer_register(node.func) and node.args:
            found.append(_root_name(node.args[0]) or UNRESOLVED)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.extend(
                node.name
                for decorator in node.decorator_list
                if _is_observer(decorator.func if isinstance(decorator, ast.Call) else decorator)
            )
    return sorted({name for name in found if name})


def registered_names(source: str, filename: str = "<probe>") -> list[str]:
    """Every callable this module attaches to a router, spelled as it is written there.

    Both aiogram forms: `router.message.register(start, CommandStart())` and the decorator
    `@router.callback_query(RecipeShow.filter())`. A middleware is registered on a
    neighbouring attribute (`router.message.outer_middleware`) and is deliberately not a
    handler — the rules below would flag every middleware in the project, which is the one
    place the session legitimately lives.
    """
    return _registered_names(ast.parse(source, filename=filename))


def _dotted(path: Path) -> str:
    parts = path.relative_to(REPO_ROOT).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _absolute(node: ast.ImportFrom, path: Path) -> str:
    """The module a `from … import …` names, relative imports included."""
    if not node.level:
        return node.module or ""
    package = _dotted(path).split(".")
    package = package[: len(package) - node.level + 1]
    return ".".join([*package, node.module] if node.module else package)


def _module_file(dotted: str) -> Path | None:
    if not dotted.startswith("src."):
        return None
    base = REPO_ROOT.joinpath(*dotted.split("."))
    candidates = (base.with_suffix(".py"), base / "__init__.py")
    return next((path for path in candidates if path.is_file()), None)


def _defining_module(reference: str, tree: ast.Module, path: Path) -> Path | None:
    """The file that defines `reference` as it is written inside `path`.

    A handler registered where it is written resolves to that file; one imported from
    elsewhere (`from src.bot.handlers.shifts import card`, or `shifts.card`) resolves to
    the module it came from, because that is the file the rules have to read.
    """
    head, _, tail = reference.partition(".")
    if not tail and any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == head
        for node in ast.walk(tree)
    ):
        return path
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = _absolute(node, path)
            for alias in node.names:
                if (alias.asname or alias.name) == head:
                    return _module_file(f"{module}.{alias.name}") or _module_file(module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == head:
                    return _module_file(alias.name)
    return None


def handler_modules() -> list[Path]:
    """Every module of `src/` that defines something a router registers."""
    found: set[Path] = set()
    for path in python_files(SRC_DIR):
        tree = ast.parse(read(path), filename=relative(path))
        for reference in _registered_names(tree):
            # An unresolvable reference leaves the registering module in scope: a guard
            # that cannot tell where a handler came from should read more, not less.
            found.add(_defining_module(reference, tree, path) or path)
    return sorted(found)


# --------------------------------------------------------------------------------------
# The rules, over every handler there is
# --------------------------------------------------------------------------------------

REGISTERED_MODULES = handler_modules()
HANDLER_FILES = sorted(set(python_files(HANDLERS_DIR)) | set(REGISTERED_MODULES))


def test_the_handlers_directory_is_where_the_guard_thinks_it_is() -> None:
    """A path that stopped existing would make every rule below vacuous."""
    assert HANDLERS_DIR.is_dir(), f"{relative(HANDLERS_DIR)} is gone; this guard scans nothing"
    assert HANDLER_FILES, "not even __init__.py: the guard would have no file to read"


def test_the_guard_reads_the_handlers_that_actually_run() -> None:
    """The failure this scope was widened for: `src/bot/handlers/` holds `__init__.py` and
    nothing else, so scanning it alone was a green rule over zero handlers while
    `src/bot/routers.py` was registering `/start` a directory away."""
    assert REGISTERED_MODULES, (
        "no router in src/ registers a handler; either the framework of plan task 20 was "
        "taken apart or the discovery above stopped recognising a registration, and every "
        "rule in this file is now checking an empty directory"
    )
    assert set(REGISTERED_MODULES) <= set(HANDLER_FILES)


@pytest.mark.parametrize("path", HANDLER_FILES, ids=relative)
def test_no_handler_reads_a_raw_callback(path: Path) -> None:
    offenders = raw_callback_reads(read(path), relative(path))
    assert not offenders, (
        f"{relative(path)}: the resolver has already parsed the payload, fetched the object "
        "through the venue's repository and checked the rights (TZ 9). Take "
        "`callback_payload` and `subject` from the handler context instead: " + "; ".join(offenders)
    )


@pytest.mark.parametrize("path", HANDLER_FILES, ids=relative)
def test_no_handler_imports_a_repository(path: Path) -> None:
    offenders = repository_imports(read(path), relative(path))
    assert not offenders, (
        f"{relative(path)}: a handler takes an update, calls a service and renders the reply "
        "(TZ 3.2). Data access lives in src/services/ so that a web admin panel can sit on "
        "the same logic: " + "; ".join(offenders)
    )


@pytest.mark.parametrize("path", HANDLER_FILES, ids=relative)
def test_no_handler_touches_the_session(path: Path) -> None:
    offenders = session_use(read(path), relative(path))
    assert not offenders, (
        f"{relative(path)}: the session and the transaction of an update belong to "
        "src/bot/middlewares/services.py; a handler that reaches for them owns a "
        "transaction it did not open: " + "; ".join(offenders)
    )


# --------------------------------------------------------------------------------------
# The guard's own teeth
# --------------------------------------------------------------------------------------

RULES = {
    "raw callback": raw_callback_reads,
    "repository import": repository_imports,
    "session use": session_use,
}

BYPASSES: list[tuple[str, str, str]] = [
    (
        "splitting the payload by hand",
        "raw callback",
        """
async def show(callback):
    run_id = int(callback.data.split(":")[1])
    return run_id
""",
    ),
    (
        "parsing it with the project's own parser",
        "raw callback",
        """
from src.bot.callbacks import parse

async def show(callback, raw):
    return parse(raw)
""",
    ),
    (
        "unpacking a factory directly",
        "raw callback",
        """
from src.bot.callbacks import ChecklistToggle

async def toggle(raw):
    return ChecklistToggle.unpack(raw)
""",
    ),
    (
        "importing a repository",
        "repository import",
        """
from src.db.repositories.checklists import ChecklistRunRepo

async def show(session):
    return ChecklistRunRepo(session, 1)
""",
    ),
    (
        "importing the repository package",
        "repository import",
        """
import src.db.repositories.recipes

async def card():
    return src.db.repositories.recipes
""",
    ),
    (
        "importing the session factory",
        "session use",
        """
from src.db.session import session_scope

async def show():
    async with session_scope() as opened:
        return opened
""",
    ),
    (
        "taking the session as a parameter",
        "session use",
        """
async def show(message, session):
    return session
""",
    ),
    (
        "running a statement on it",
        "session use",
        """
async def show(message, db):
    return await db.execute("...")
""",
    ),
    (
        "picking the repositories out of the services",
        "session use",
        """
async def show(message, services):
    return await services.repositories.runs.get(1)
""",
    ),
]

LEGITIMATE: list[tuple[str, str]] = [
    (
        "a checklist handler written the way the framework intends",
        """
from aiogram.types import CallbackQuery

from src.bot import texts
from src.bot.callbacks import ChecklistToggle
from src.bot.safe_edit import safe_edit

async def toggle(callback, callback_payload: ChecklistToggle, subject, services, actor):
    run = await services.checklists.toggle(actor, run=subject, item_id=callback_payload.item_id)
    await safe_edit(
        callback.bot,
        chat_id=run.chat_id,
        message_id=run.message_id,
        text=texts.CHECKLIST_PROGRESS_TEMPLATE.format(done=run.done_items, total=run.total_items),
        answer=callback.answer,
    )
""",
    ),
    (
        "a search handler that reads the FSM state and calls a service",
        """
async def search(message, state, services, actor):
    payload = await state.get_data()
    page = await services.recipes.search(payload["query"])
    await message.answer(str(page.total))
""",
    ),
    (
        "a filter on a callback factory, which is routing and not parsing",
        """
from src.bot.callbacks import RecipeShow

def register(router, show):
    router.callback_query.register(show, RecipeShow.filter())
""",
    ),
]


@pytest.mark.parametrize(
    ("name", "rule", "source"),
    BYPASSES,
    ids=[name for name, _, _ in BYPASSES],
)
def test_known_bypasses_are_caught(name: str, rule: str, source: str) -> None:
    assert RULES[rule](source), f"a handler could still get away with {name!r}"


@pytest.mark.parametrize(
    ("name", "source"),
    LEGITIMATE,
    ids=[name for name, _ in LEGITIMATE],
)
def test_correct_handlers_are_not_flagged(name: str, source: str) -> None:
    offenders = {rule: check(source) for rule, check in RULES.items()}
    assert not any(offenders.values()), (
        f"{name!r} is how a handler is meant to be written, and the guard flagged it: {offenders}"
    )


# --------------------------------------------------------------------------------------
# The teeth of the scope itself
# --------------------------------------------------------------------------------------

REGISTRATIONS: list[tuple[str, str, list[str]]] = [
    (
        "registered on a router, the way stage 0 does it",
        """
def system_router():
    router = Router(name="system")
    router.message.register(start, CommandStart())
    return router
""",
        ["start"],
    ),
    (
        "registered by decorator",
        """
@router.callback_query(ChecklistToggle.filter())
async def toggle(callback):
    return callback
""",
        ["toggle"],
    ),
    (
        "registered from another module",
        """
from src.bot.handlers import shifts

def register(router):
    router.message.register(shifts.card)
""",
        ["shifts.card"],
    ),
    (
        "a decorator without arguments",
        """
@router.message
async def anything(message):
    return message
""",
        ["anything"],
    ),
]

NOT_REGISTRATIONS: list[tuple[str, str]] = [
    (
        "a middleware, which is where the session legitimately lives",
        """
def setup(dispatcher):
    dispatcher.update.outer_middleware(middleware)
    dispatcher.message.middleware.register(middleware)
""",
    ),
    (
        "including a router is not registering a handler",
        """
def build(dispatcher):
    dispatcher.include_router(routers.system_router())
""",
    ),
    (
        "a register() of our own on something that is not an observer",
        """
class Registry:
    def add(self, spec):
        self.register(spec)
""",
    ),
]


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    REGISTRATIONS,
    ids=[name for name, _, _ in REGISTRATIONS],
)
def test_every_way_of_registering_a_handler_is_seen(
    name: str, source: str, expected: list[str]
) -> None:
    """The scope is computed from these two shapes; a shape it does not know is a handler
    the three rules above never read."""
    assert registered_names(source) == expected, f"a handler {name!r} would stay unscanned"


@pytest.mark.parametrize(
    ("name", "source"), NOT_REGISTRATIONS, ids=[name for name, _ in NOT_REGISTRATIONS]
)
def test_what_is_not_a_handler_is_not_pulled_in(name: str, source: str) -> None:
    """A scope that swallowed the middlewares would fail on the session they are built to
    own, and the honest fix would then be to weaken a rule."""
    assert not registered_names(source), f"{name!r} was taken for a handler registration"


def test_a_handler_outside_the_directory_is_in_scope() -> None:
    """The concrete hole: a handler defined in `src/bot/`, registered in a router next to
    it, and never once read because it is not under `src/bot/handlers/`."""
    outsider = SRC_DIR / "bot" / "routers.py"
    tree = ast.parse("async def start(message):\n    return message\n")
    assert HANDLERS_DIR not in outsider.parents
    assert _defining_module("start", tree, outsider) == outsider
