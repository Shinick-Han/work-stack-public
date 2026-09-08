"""Which storage composition a call is admitted for, and where it dispatches.

Two related concerns live together because they answer the same question at the
same moment: is THIS facade the released v3 composition, and does a configured
command backend replace the released implementation for this call? The
composition predicates are identity checks through the existing adapter seams -
protocol method presence is never treated as capability - and the decorators
below are the only place a backend is chosen.

``stack``/``self`` is the ``WorkStack`` facade. It is annotated ``Any`` rather
than imported: the facade imports this module, so a real import would be a
cycle.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from .service_errors import (
    _CAPTURE_REPLY_ERROR_TYPES,
    _OPTIONAL_COMMAND_ERROR_TYPES,
    DomainError,
)
from .store import Store
from .storage.document_repository import StoreDocumentRepository


def _released_v3_document_composition(stack: Any) -> bool:
    """Recognize only the released v3 document composition, by exact adapter type.

    ``StoreDocumentRepository`` is the released adapter that alone knows how the
    released physical store represents these documents, and it wraps the released
    ``Store``.  Any other object satisfying the ``DocumentRepository`` protocol is
    an unadmitted composition: protocol method presence is not capability, and the
    protocol exposes no capability predicate to ask.
    """

    documents = stack.documents
    return (
        type(documents) is StoreDocumentRepository
        and type(getattr(documents, "_store", None)) is Store
    )


def _attributed_released_composition(stack: Any) -> bool:
    """The released composition AND the Store that actually transacts and publishes.

    Exact adapter and Store types are not enough. ``WorkStack.__init__`` accepts
    an independently supplied repository, so a released adapter can legitimately
    wrap a DIFFERENT Store: the documents would then be written in one Store
    while the outer transaction, the workspace identity behind the recorded fact
    and the typed publication all belong to another. Attribution requires those
    to be the same object, checked by identity through the existing seams rather
    than inferred from any capability surface.
    """

    return (
        _released_v3_document_composition(stack)
        and getattr(stack.documents, "_store", None) is stack.store
    )


def _released_v3_attributed_composition_owner(stack: Any) -> bool:
    """The narrow released composition bound to the transacting Store itself.

    Every new D5 write uses this, INCLUDING a null-origin browser write: a
    transition is durable evidence, so it may only be produced by the admitted
    composition writing through the same Store that transacts and publishes.
    Unrelated ordinary commands keep their existing behaviour.
    """

    documents = stack.documents
    return (
        type(documents) is StoreDocumentRepository
        and type(getattr(documents, "_store", None)) is Store
        and documents._store is stack.store
    )


def _require_released_composition_for_refs(
    stack: Any, patch: dict[str, Any]
) -> None:
    """Refuse a scoped-ref mutation, including [], on an unadmitted composition."""

    if "key_result_refs" in patch and not _released_v3_document_composition(stack):
        raise DomainError(
            "key result references are not supported by this storage composition"
        )


def _capture_reply_command(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return action()
    except ValueError as error:
        if getattr(error, "command_boundary", None) != "capture-reply":
            raise
        repository_code = str(getattr(error, "code", "invalid_request"))
        error_type = _CAPTURE_REPLY_ERROR_TYPES.get(repository_code, DomainError)
        raise error_type(
            "capture/reply command was refused",
            {"repository_code": repository_code},
        ) from error


def _capture_reply_backend(method):
    @wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any):
        commands = self.capture_reply_commands
        if commands is not None:
            command = getattr(commands, method.__name__)
            return _capture_reply_command(lambda: command(*args, **kwargs))
        return method(self, *args, **kwargs)

    return wrapped


def _optional_command(
    action: Callable[[], Any], boundary: str
) -> Any:
    try:
        return action()
    except (ValueError, RuntimeError) as error:
        if getattr(error, "command_boundary", None) != boundary:
            raise
        repository_code = str(getattr(error, "code", "invalid_request"))
        error_type = _OPTIONAL_COMMAND_ERROR_TYPES.get(repository_code, DomainError)
        raise error_type(
            "{} command was refused".format(boundary),
            {"repository_code": repository_code},
        ) from error


def _optional_command_backend(attribute: str, backend_method: str, boundary: str):
    def decorate(method):
        @wraps(method)
        def wrapped(self: Any, *args: Any, **kwargs: Any):
            commands = getattr(self, attribute)
            if commands is not None:
                command = getattr(commands, backend_method)
                return _optional_command(
                    lambda: command(*args, **kwargs), boundary
                )
            return method(self, *args, **kwargs)

        return wrapped

    return decorate


def _query_search_backend(method):
    @wraps(method)
    def wrapped(self: Any, query: str, limit: int = 30):
        if self.query_commands is None:
            return method(self, query, limit)
        result = _optional_command(
            lambda: self.query_commands.search(query, limit=limit), "query"
        )
        return result.to_released_projection()

    return wrapped


def _query_graph_backend(method):
    @wraps(method)
    def wrapped(self: Any):
        projection = method(self)
        if self.query_commands is None:
            return projection
        result = _optional_command(lambda: self.query_commands.graph(), "query")
        projection["edges"] = [
            {"kind": kind, "source": source, "target": target}
            for kind, source, target in result.edges
        ]
        return projection

    return wrapped


def _attributed_released_v3_only(attribute: str, backend_method: str, boundary: str):
    """Dispatch like ``_optional_command_backend``, but refuse an ATTRIBUTED call.

    ``_optional_command_backend`` runs before ``_transactional``, so a configured
    backend replaces the released-v3 implementation entirely. That backend's
    ``add_worklog`` accepts no provenance, so forwarding an attributed write
    there would silently drop the attribution and publish nothing while still
    reporting success.

    Method presence is never treated as evidence of capability: ANY configured
    backend refuses an attributed call, before the backend call and before any
    document write. Unattributed calls keep ordinary backend behaviour exactly,
    with the released-only keyword removed so the backend signature is unchanged.
    """

    def decorate(method):
        @wraps(method)
        def wrapped(self: Any, *args: Any, **kwargs: Any):
            # The document composition is checked FIRST, before any backend
            # dispatch and before any document write. An injected repository
            # that merely satisfies the DocumentRepository protocol is not the
            # admitted released composition: protocol method presence is not
            # capability, and the existing exact-adapter seam is what decides.
            if kwargs.get("origin") is not None and not _attributed_released_composition(self):
                raise DomainError(
                    "attributed review entries are not supported by this storage composition"
                )
            commands = getattr(self, attribute)
            if commands is not None:
                if kwargs.get("origin") is not None:
                    raise DomainError(
                        "attributed review entries are not supported by this backend"
                    )
                forwarded = {key: value for key, value in kwargs.items() if key != "origin"}
                command = getattr(commands, backend_method)
                return _optional_command(
                    lambda: command(*args, **forwarded), boundary
                )
            return method(self, *args, **kwargs)

        return wrapped

    return decorate


def _transactional(method):
    @wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any):
        with self.store.transaction():
            return method(self, *args, **kwargs)

    return wrapped
