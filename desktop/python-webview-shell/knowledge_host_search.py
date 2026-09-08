"""Wire bounds for the host bridge's ``search-references`` operation.

``knowledge_host`` decodes every bounded knowledge request and shapes every
response. Search is the only operation that accepts free text from the UI and
the only one that answers with a corpus scope, so the bounds those two shapes
need live here instead of widening the dispatcher that owns the rest.

Refusals leave this module as :class:`SearchContractError`, carrying a closed
code and nothing else -- no query echo, no source text, no filesystem root. The
host maps a decode-time refusal onto its own closed error exactly as it maps the
reference layer's, and a response-time refusal reaches the encoder through the
same closed-code path a registry refusal already takes.

The shaper for one ``KnowledgeReadReference`` is injected rather than imported:
that schema belongs to the read operation this module has no part in, and
passing it in keeps the dependency pointing one way -- the host imports the
search bounds, never the reverse.
"""

from __future__ import annotations

from collections.abc import Callable
import re

MAX_QUERY_CHARS = 1000
MAX_SEARCH_MATCHES = 5
MAX_CORPUS_LABEL_CHARS = 120
MAX_DOCUMENT_COUNT = 100_000_000

_INDEXED_AT = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d{1,6})?([Zz]|[+-]\d{2}:\d{2})"
)


class SearchContractError(Exception):
    """Closed refusal from the search contract; the code is all it carries."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def parse_query(value: object) -> str:
    """Accept exactly one trimmed, bounded, control-free search query.

    The UI supplies nothing else: no command, executable, root or URL reaches
    the search provider, and the trimmed text is what the response echoes back.
    """

    if not isinstance(value, str):
        raise SearchContractError("invalid_query")
    query = value.strip()
    if not query or len(query) > MAX_QUERY_CHARS:
        raise SearchContractError("invalid_query")
    if any(_control_character(character) for character in query):
        raise SearchContractError("invalid_query")
    return query


def public_search(
    binding: dict[str, str | int],
    query: str,
    value: object,
    *,
    read_reference: Callable[[object], dict[str, object]],
) -> dict[str, object]:
    """Shape exactly the documented search payload, or refuse to answer."""

    if not isinstance(value, dict) or set(value) != {
        "corpus",
        "matches",
        "omitted_count",
    }:
        raise SearchContractError("operation_failed")
    matches = value["matches"]
    omitted = value["omitted_count"]
    if not isinstance(matches, list) or len(matches) > MAX_SEARCH_MATCHES:
        raise SearchContractError("operation_failed")
    if type(omitted) is not int or omitted < 0:
        raise SearchContractError("operation_failed")
    return {
        "binding": dict(binding),
        "query": query,
        "corpus": public_corpus(value["corpus"]),
        "matches": [read_reference(match) for match in matches],
        "omitted_count": omitted,
    }


def public_corpus(value: object) -> dict[str, object]:
    """Bound the scope the UI displays; a corpus label carries no local root."""

    if not isinstance(value, dict) or set(value) != {
        "label",
        "document_count",
        "indexed_at",
    }:
        raise SearchContractError("operation_failed")
    label = value["label"]
    if not isinstance(label, str) or not label or len(label) > MAX_CORPUS_LABEL_CHARS:
        raise SearchContractError("operation_failed")
    if any(separator in label for separator in "/\\") or any(
        _display_control(character) for character in label
    ):
        raise SearchContractError("operation_failed")
    count = value["document_count"]
    if type(count) is not int or not 0 <= count <= MAX_DOCUMENT_COUNT:
        raise SearchContractError("operation_failed")
    indexed_at = value["indexed_at"]
    if not isinstance(indexed_at, str) or not _INDEXED_AT.fullmatch(indexed_at):
        raise SearchContractError("operation_failed")
    return {"label": label, "document_count": count, "indexed_at": indexed_at}


def _display_control(character: str) -> bool:
    """Report every control character; a one-line label has no whitespace layout."""

    code = ord(character)
    return code < 32 or code == 127 or 0x80 <= code <= 0x9F


def _control_character(character: str) -> bool:
    """Report C0, DEL and C1 characters; normal whitespace is not a control."""

    code = ord(character)
    return (
        (code < 32 and character not in "\t\n\r")
        or code == 127
        or 0x80 <= code <= 0x9F
    )
