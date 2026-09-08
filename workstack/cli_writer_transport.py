"""The one owner/preflight/revalidate/post/replay sequence for a CLI write.

Lifted unchanged out of the admitted note route so each command reuses it
instead of growing a parallel transport. Only the path, the body, the
response projection and the three diagnostics differ; every command keeps
the exact wording it had. CSRF and Origin headers, the idempotency key on
retry, and the no-local-fallback rule stay here.
"""

from __future__ import annotations

import uuid
from typing import Callable, Mapping

from .cli_writer_owner import (
    AMBIGUOUS_TRANSPORT,
    OWNER_INVALID,
    OWNER_PRESENT,
    CommitUnknownError,
    CoordinatesReader,
    RequestJson,
    WriterTransportError,
    _origin,
    _preflight,
    _resolve_coordinates,
    expected_workspace_uid,
    read_owner_binding,
)


def new_idempotency_key() -> str:
    """One key per CLI invocation.

    Deliberately random rather than content-derived: repeating the same
    ``work-stack note`` arguments in a new invocation is a new intent and must
    create a second note, not replay the first.
    """

    return "cli-note-{}".format(uuid.uuid4().hex)


def _validate_keyless_post(method, keyless_post, idempotency_key, replay, extra_headers):
    if keyless_post and (
        method != "POST" or idempotency_key is not None or replay or extra_headers
    ):
        raise ValueError("keyless POST requires one attempt, no key and no extra headers")


def _write_headers(host, port, csrf, method, idempotency_key, extra_headers, keyless_post):
    headers = {"Origin": _origin(host, port), "X-WorkStack-CSRF": csrf}
    if method == "POST" and not keyless_post:
        headers["Idempotency-Key"] = idempotency_key or new_idempotency_key()
        if extra_headers:
            headers.update(extra_headers)
    return headers


def _forward_write(
    store: object,
    owner_state: str,
    *,
    path: str,
    body: dict[str, object],
    coordinates_reader: CoordinatesReader,
    request_json: RequestJson,
    idempotency_key: str | None,
    method: str = "POST",
    extra_headers: Mapping[str, str] | None = None,
    replay: bool = True,
    keyless_post: bool = False,
    project_result: Callable[[int, Mapping[str, object]], dict[str, object]] | None = None,
    project: Callable[[Mapping[str, object]], dict[str, object]],
    changed_message: str,
    unknown_message: str,
    refused_message: str,
    prepare: Callable[..., tuple[str, dict[str, object], Callable[..., dict[str, object]]]]
    | None = None,
) -> dict[str, object]:
    """The one owner/preflight/revalidate/post/replay sequence for a CLI write.

    Lifted unchanged out of the admitted note route so each command reuses it
    instead of growing a parallel transport. Only the path, the body, the
    response projection and the three diagnostics differ; every command keeps
    the exact wording it had.

    ``prepare`` is for a command that must read from the same owner before it
    writes. It runs after preflight and BEFORE the final same-advertisement
    revalidation, so its reads cannot widen the window between the last check
    and the first mutation, and it returns the final path, body and projection.
    Its reads are part of preflight and are never retried.
    """

    _validate_keyless_post(method, keyless_post, idempotency_key, replay, extra_headers)
    if owner_state == OWNER_INVALID:
        raise WriterTransportError(
            "Work Stack server runtime metadata is not a readable regular file"
        )
    if owner_state != OWNER_PRESENT:
        raise WriterTransportError("Work Stack server runtime metadata is not available")

    host, port, binding = _resolve_coordinates(store, coordinates_reader)
    expected_uid = expected_workspace_uid(store)
    csrf = _preflight(request_json, host, port, expected_uid)

    if prepare is not None:
        path, body, project = prepare(request_json, host, port)

    # Preflight takes several round trips, and the owner can stop, be replaced
    # or have its advertisement removed during them. Re-observe the same
    # binding immediately before the first mutation: a vanished, unreadable,
    # oversized or replaced advertisement refuses here instead of posting to an
    # owner that no longer exists. This never redirects to a different owner
    # and never repairs or removes the metadata.
    revalidated_host, revalidated_port, revalidated_binding = read_owner_binding(store)
    if (revalidated_host, revalidated_port, revalidated_binding) != (host, port, binding):
        raise WriterTransportError(changed_message)

    headers = _write_headers(
        host, port, csrf, method, idempotency_key, extra_headers, keyless_post
    )

    try:
        status, payload = request_json(
            host, port, method, path, body=body, headers=headers
        )
    except AMBIGUOUS_TRANSPORT as error:
        if method != "POST" or not replay:
            # No idempotency record exists on this route, so a second attempt
            # could not be recognised as a replay: it would either be refused
            # as stale or, for a no-op, silently applied again. One attempt
            # only, and the outcome stays unknown rather than being guessed.
            raise CommitUnknownError(unknown_message) from error
        # The request went out and the outcome is unknown: a lost connection, a
        # truncated read, or an unparseable body all leave the record possibly
        # created. Replay the identical bytes under the identical key exactly
        # once and let the server's idempotency record decide.
        try:
            status, payload = request_json(
                host, port, "POST", path, body=body, headers=headers
            )
        except AMBIGUOUS_TRANSPORT as error:
            raise CommitUnknownError(unknown_message) from error

    if 200 <= status < 300:
        if project_result is not None:
            # A caller that must judge the status as well as the body, such as
            # a route whose only success pairings are 201/false and 200/true.
            # Its refusal is raised as CommitUnknownError by the caller itself,
            # because the request DID reach the owner: reporting a determinate
            # refusal after a possible commit would be a false claim. Older
            # callers keep the existing determinate error policy.
            return project_result(status, payload)
        return project(payload)
    # A determinate HTTP status is an answer, not an ambiguity. Do not retry,
    # and do not surface the server's raw error text.
    raise WriterTransportError(refused_message.format(status))
