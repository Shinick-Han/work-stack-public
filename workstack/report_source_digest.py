"""The canonical digest of one bounded day of report source.

A report is durable evidence about a day, so every surface that compares a
report against its source has to agree, byte for byte, on what "the source"
was. The preview adapter answers `source_digest` for a day nobody has stored
yet; the composer decides whether a stored report is stale; the read
projection reports that staleness back. Three callers, one meaning — and the
meaning cannot live in any one of them.

It lives here, on a leaf that imports nothing but the canonical encoder, for
the reason the layer rules already state. The transport adapter is above the
application, and an application that reached up into it to borrow a digest
would invert the graph for the sake of a single expression. Moving the
expression down instead leaves the HTTP path importing the same function it
used to define, so the preview and the stored comparison keep answering with
the identical bytes.

The digest covers the bounded day projection and the date it was taken for,
never the generated markdown. Two reports written from the same day are
compared by what the day said, not by what a template made of it.
"""

from __future__ import annotations

from typing import Any

from .capture import canonical_digest


__all__ = ["day_source_digest"]


def day_source_digest(*, date: str, day: Any) -> str:
    """Canonical SHA-256 of the bounded single-day source, not the generated preview."""

    return canonical_digest({"date": date, "day": day})
