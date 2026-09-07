"""Durable Task display-ID high-water authority.

Task display IDs are monotonic workspace identities and are never reused after
permanent deletion. The durable authority is explicit SSOT metadata:

- v3: ``task_display_id_high_water`` on canonical workspace metadata
- v4: the same field on canonical ``store.json`` metadata

It is not a Task tombstone, idempotency record, runtime-only counter, manifest
generation, UUID ordering, or maximum-live-ID surrogate.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


FIELD = "task_display_id_high_water"
DISPLAY_PREFIX = "T-"
MIN_DISPLAY_DIGITS = 4
MAX_SAFE_INTEGER = 9_007_199_254_740_991


class TaskDisplayIdError(ValueError):
    """Content-free refusal of Task display-ID high-water admission."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _digits_of(value: str) -> str:
    return value[len(DISPLAY_PREFIX) :]


def is_canonical_display_id(value: object) -> bool:
    if type(value) is not str or not value.startswith(DISPLAY_PREFIX):
        return False
    digits = _digits_of(value)
    return (
        len(digits) >= MIN_DISPLAY_DIGITS
        and digits.isascii()
        and digits.isdigit()
    )


def parse_display_number(value: object) -> int:
    if not is_canonical_display_id(value):
        raise TaskDisplayIdError("malformed_task_id")
    number = int(_digits_of(str(value)))
    if number > MAX_SAFE_INTEGER:
        raise TaskDisplayIdError("malformed_task_id")
    return number


def format_display_id(number: int) -> str:
    if type(number) is not int or not 1 <= number <= MAX_SAFE_INTEGER:
        raise TaskDisplayIdError("high_water_overflow")
    return "{}{:04d}".format(DISPLAY_PREFIX, number)


def read_optional_high_water(container: Mapping[str, Any] | None) -> int | None:
    if container is None or FIELD not in container:
        return None
    value = container[FIELD]
    if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
        raise TaskDisplayIdError("high_water_invalid")
    return value


def roster_numbers(display_ids: Iterable[object]) -> tuple[int, ...]:
    seen: set[int] = set()
    numbers: list[int] = []
    for item in display_ids:
        number = parse_display_number(item)
        if number in seen:
            raise TaskDisplayIdError("duplicate_numeric_id")
        seen.add(number)
        numbers.append(number)
    return tuple(numbers)


def roster_high_water(display_ids: Iterable[object]) -> int:
    numbers = roster_numbers(display_ids)
    if not numbers:
        return 0
    return max(numbers)


def admitted_high_water(stored: int | None, display_ids: Iterable[object]) -> int:
    """Return the durable authority, seeding from the live roster when absent."""

    live = roster_high_water(display_ids)
    if stored is None:
        return live
    if stored < live:
        raise TaskDisplayIdError("high_water_below_live")
    return stored


def allocate_create(stored: int | None, display_ids: Iterable[object]) -> tuple[str, int]:
    current = admitted_high_water(stored, display_ids)
    if current >= MAX_SAFE_INTEGER:
        raise TaskDisplayIdError("high_water_overflow")
    nxt = current + 1
    return format_display_id(nxt), nxt


def persist_field(container: dict[str, Any], high_water: int) -> dict[str, Any]:
    if type(high_water) is not int or not 0 <= high_water <= MAX_SAFE_INTEGER:
        raise TaskDisplayIdError("high_water_invalid")
    container[FIELD] = high_water
    return container


def task_ids_from_records(tasks: Iterable[Mapping[str, Any]]) -> tuple[object, ...]:
    return tuple(item.get("id") for item in tasks)
