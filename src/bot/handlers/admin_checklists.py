"""The checklist template editor (TZ 5.8; plan task 28).

Owner: plan task 28. Empty until then; see `src/bot/handlers/__init__.py`.
"""

from __future__ import annotations

from typing import Final

from aiogram import Router

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "admin_checklists"


def router() -> Router:
    """The screens of this block (see the module docstring)."""
    return Router(name=ROUTER_NAME)


__all__ = ["ROUTER_NAME", "router"]
