"""Synthetic Notion fixtures: a fake clock, a recording transport, one page.

Nothing here reaches a network, a real token or a live Notion workspace. The
page object is the shape the API documents, carrying deliberate canaries -- a
title and a ``last_edited_time`` -- so a test can prove they never become an
observation. This module holds no tests of its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

NOTION_REF = "od-page-7f3ba1d34f50c884600112ab"
PAGE_UUID = "1f2e3d4c-5b6a-7081-9243-a5b6c7d8e9f0"
PAGE_URL = (
    "https://www.notion.so/Quarterly-Handbook-1f2e3d4c5b6a70819243a5b6c7d8e9f0"
)

#: Synthetic. Not a Notion credential, and never sent anywhere.
TOKEN_CANARY = "ntn_r24synthetic9f3a7c1b5e2d4086"
TITLE_CANARY = "Board-Comp-Secret-Title-r24-canary"


@dataclass(frozen=True)
class Wire:
    """One canned HTTP answer for the injected transport."""

    status: int
    body: bytes


@dataclass(frozen=True)
class Call:
    """What the transport was asked for, and when."""

    page_uuid: str
    token: str
    timeout: float
    at: float


class FakeClock:
    """A monotonic clock that only moves when a test says it does."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def read(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingTransport:
    """Answers from a queue, records every call, and costs wall time.

    A queued entry that is an exception is raised instead of returned, which is
    how a timeout, a TLS failure or a reset is expressed. An empty queue means
    the test expected no further call and gets a loud failure if one arrives.
    """

    def __init__(self, wires: list, *, clock: FakeClock = None, cost: float = 0.0):
        self._wires = list(wires)
        self._clock = FakeClock() if clock is None else clock
        self._cost = cost
        self.calls: list[Call] = []

    def __call__(self, page_uuid: str, token: str, timeout: float):
        self.calls.append(
            Call(
                page_uuid=page_uuid,
                token=token,
                timeout=timeout,
                at=self._clock.read(),
            )
        )
        if not self._wires:
            raise AssertionError("unexpected extra Notion request")
        answer = self._wires.pop(0)
        self._clock.advance(self._cost)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def page_object(**overrides: object) -> dict:
    """The documented ``GET /v1/pages/{id}`` shape, plus test canaries."""

    document: dict = {
        "object": "page",
        "id": PAGE_UUID,
        "created_time": "2026-01-02T03:04:00.000Z",
        "last_edited_time": "2026-05-06T07:08:00.000Z",
        "archived": False,
        "in_trash": False,
        "url": PAGE_URL,
        "properties": {
            "title": {
                "id": "title",
                "type": "title",
                "title": [{"type": "text", "plain_text": TITLE_CANARY}],
            }
        },
    }
    document.update(overrides)
    return document


def encode_document(document: object) -> bytes:
    return json.dumps(
        document, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def encoded_page(**overrides: object) -> bytes:
    return encode_document(page_object(**overrides))
