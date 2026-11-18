"""The only source of time in loopctl (phase2-design §11, decision record 0013, invariant I5, proof test T-TIME).

Every stored time is written here, by code, in UTC, at the moment of writing. Nothing a model runs passes a time in.
Tests replace the clock with `use(fake)`; production never does.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import UTC, datetime

_now_ns: Callable[[], int] = time.time_ns

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_PATTERN = r"^[0-9A-HJKMNP-TV-Z]{26}$"


def use(now_ns: Callable[[], int]) -> None:
    """Replace the clock (tests only)."""
    global _now_ns
    _now_ns = now_ns


def reset() -> None:
    use(time.time_ns)


def now_ns() -> int:
    return _now_ns()


def now() -> datetime:
    return datetime.fromtimestamp(_now_ns() / 1e9, tz=UTC)


def stamp() -> str:
    """The current time as stored: ISO 8601, UTC, milliseconds, `Z` (e.g. 2026-11-18T13:40:30.060Z)."""
    ns = _now_ns()
    whole = datetime.fromtimestamp(ns // 1_000_000_000, tz=UTC)
    return whole.strftime("%Y-%m-%dT%H:%M:%S") + f".{(ns // 1_000_000) % 1000:03d}Z"


def stamp_at(moment: datetime) -> str:
    """A computed time (e.g. now + a lease's duration) in the stored format. Callers derive `moment` from `now()`."""
    ms = round(moment.timestamp() * 1000)
    whole = datetime.fromtimestamp(ms // 1000, tz=UTC)
    return whole.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


def parse(text: str) -> datetime:
    """Read a stamp back (accepts the `Z` form `stamp()` writes)."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


_last_ulid = 0


def ulid() -> str:
    """A ULID: 48 bits of milliseconds since the epoch, then 80 random bits, in Crockford base32. Sorts by time, and
    within one process strictly increases (monotonic ULIDs: a later call in the same millisecond adds one)."""
    global _last_ulid
    ms = _now_ns() // 1_000_000
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    if value >> 80 <= _last_ulid >> 80:  # same (or an earlier) millisecond: monotonic, so one writer's events keep
        value = _last_ulid + 1  # their order when the reducer sorts by id
    _last_ulid = value
    chars = []
    for _ in range(26):
        chars.append(CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))
