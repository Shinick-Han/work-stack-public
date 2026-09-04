"""Portable, local-first work-stack."""

__version__ = "1.0.7"

# Increment only when the desktop-to-remote HTTP contract becomes incompatible.
# This value is reported by the running server; desktop clients must read it from
# /api/v1/storage rather than assuming their bundled value describes a remote host.
REMOTE_PROTOCOL_VERSION = 1
