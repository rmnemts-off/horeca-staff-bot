"""The root of the management section (TZ 5.2, 5.8; plan task 26).

Owner: plan task 26. One screen — the five blocks of TZ 5.8 as buttons — plus the caption
of the management button on the reply keyboard. Each block itself lives in its own `admin_*`
module and answers its own `OpenAdmin` payload; this module only draws the board.

Empty until then; see `src/bot/handlers/__init__.py` for the order of the tree.
"""

from __future__ import annotations

from typing import Final

from aiogram import Router

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "admin"


def router() -> Router:
    """The screens of this block (see the module docstring)."""
    return Router(name=ROUTER_NAME)


__all__ = ["ROUTER_NAME", "router"]
