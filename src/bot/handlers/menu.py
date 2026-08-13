"""The main menu, the shift screen and the schedule (TZ 5.2, 5.3; plan task 23).

Owner: plan task 23. The router is declared empty so that the tree in
`src/bot/handlers/__init__.py` is complete from the first commit of wave 2 and the guard in
`tests/bot/test_handler_boundary.py` covers this module before it has anything to cover.
"""

from __future__ import annotations

from typing import Final

from aiogram import Router

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "menu"


def router() -> Router:
    """The screens of this block (see the module docstring)."""
    return Router(name=ROUTER_NAME)


__all__ = ["ROUTER_NAME", "router"]
