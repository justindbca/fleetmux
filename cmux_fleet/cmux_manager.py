"""Forwarding module: the cmux backend now lives in ``cmux_fleet.backends.cmux``.

Kept so every existing import keeps working unchanged:
``from cmux_fleet import cmux_manager as cm``, ``from cmux_fleet.cmux_manager import
CmuxSessionManager, cmux_available``, and ``monkeypatch.setattr(cmux_manager, '_run', fake)``.

This module object *is* the moved module's namespace: ``sys.modules[__name__] = _cmux``. Reads and
writes then land in one dict, so a test stubbing ``cmux_manager._run`` here is seen by the cmux code
(which lives in either module's namespace) and the cmux code calling ``_run`` itself keeps working —
a plain re-export would break the second half and a write-through ``__setattr__`` would be bypassed
by pytest's ``monkeypatch`` (it calls ``setattr()`` on the dict). ``cmux_fleet.backends.cmux`` stays
importable and monkeypatchable as well; both names are the same module.

Nothing here but that aliasing; behaviour changes belong in ``backends/cmux.py``.
"""
from __future__ import annotations

import sys

from cmux_fleet.backends import cmux as _cmux

# One namespace for both names: everything defined in backends/cmux.py is reachable as
# cmux_fleet.cmux_manager.<name>, and a rebind through either name is seen by the code.
sys.modules[__name__] = _cmux
