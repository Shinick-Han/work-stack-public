"""Compatibility alias for the shared bounded exchange, now in ``workstack``.

The implementation moved to ``workstack/bounded_process_exchange.py`` so a
backend process can use it without putting the desktop shell directory on
``sys.path``.  The desktop shell keeps importing ``bounded_process_exchange``
by its bare name, and this file exists only so that spelling keeps working.

This is an alias, not a re-export.  Binding ``sys.modules[__name__]`` to the
implementation module means ``import bounded_process_exchange`` and
``from workstack.bounded_process_exchange import ...`` hand back *the same
module object*: there is one ``SETTLE_GRACE_SECONDS``, one set of code
constants, and one ``BoundedExchange`` class.  A caller or test that assigns
``bounded_process_exchange.SETTLE_GRACE_SECONDS`` therefore still changes the
bound every exchange actually reads, which a ``from ... import *`` copy would
silently stop doing.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SHELL_DIRECTORY = Path(__file__).resolve().parent
_APPLICATION_ROOT = _SHELL_DIRECTORY.parents[1]
for _import_root in (_SHELL_DIRECTORY, _APPLICATION_ROOT):
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

from workstack import bounded_process_exchange as _implementation  # noqa: E402

sys.modules[__name__] = _implementation
