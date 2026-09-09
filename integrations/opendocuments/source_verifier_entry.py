"""Trusted source-checkout bootstrap for the OpenDocuments source verifier.

Invoke as ``[absolute_python, absolute_this_file]``, not ``python -m``.
Checkout root is derived from this file's location and inserted first on
``sys.path`` before ``source_verifier_main.main`` is imported. ``PYTHONPATH``
is not required. This is not a packaged executable, a sandbox, or a bundle
install. ``WORKSTACK_OD_VERIFIER_CONFIG`` stays the child's explicit
configuration. stdin/stdout/stderr/exit are ``source_verifier_main.main``
unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from integrations.opendocuments.source_verifier_main import main

    sys.exit(main())
