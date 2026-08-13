"""Creating a venue and its settings (TZ 5.8; plan tasks 26 and 30).

Owner: plan tasks 26 and 30. Empty until then; see `src/bot/handlers/__init__.py`.
"""

from __future__ import annotations

from typing import Final

from aiogram import Router

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "admin_venue"


def router() -> Router:
    """The screens of this block (see the module docstring)."""
    return Router(name=ROUTER_NAME)


__all__ = ["ROUTER_NAME", "router"]
