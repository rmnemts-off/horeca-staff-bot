"""The opening checklist as the employee sees it (TZ 5.4; plan task 24).

Owner: plan task 24. Empty until then; see `src/bot/handlers/__init__.py`.
"""

from __future__ import annotations

from typing import Final

from aiogram import Router

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "checklist"


def router() -> Router:
    """The screens of this block (see the module docstring)."""
    return Router(name=ROUTER_NAME)


__all__ = ["ROUTER_NAME", "router"]
