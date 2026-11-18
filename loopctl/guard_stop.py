"""The guard's `STOP` gate (phase2-design §4.5, decision record 0026, proof test T-GUARD).

A worker's guard reads `STOP` from `main` of the state repo with its own fetch (never a tool call), and obeys it:

- before every **irreversible step** the last read must be at most `FRESH_S` (60 s) old, else it reads again; if the
  read doesn't finish within `READ_TIMEOUT_S` (15 s) the step is refused (fail closed);
- on **any tool call** when the last read is older than `IDLE_S` (120 s), it reads again;
- a line that matches the worker (its kind or unit) makes the session **stopped**: from then on every tool call is
  refused except `loopctl finish … --stopped`. Three failed reads in a row count as stopped. Stopped is sticky for the
  session: the worker records a handoff and ends.

A session whose `loopctl` call was refused because another session claimed the unit's lease (a duplicate start,
`loopctl/claim.py`) is stopped the same way: loopctl leaves a marker in the container, `claim_refused` reads it.

The worker's kind and unit come from `loopctl guard identify <kind> <unit>` (the first command of every brief; the
guard records it, first one wins). Until then every `STOP` line matches (fail closed): a general `pause` stops the
session; a line for some kind or unit refuses each call except `identify` until the session says what it is.

Parsing and matching are `loopctl/stop.py`'s (the same reader the controller uses): blank lines are nothing;
`pause`, `pause budget`, `pause <kind> …`, `stop <unit>`; any other line counts as `pause`. The kinds and units
`identify` accepts are the ones the reducer knows, `reconcile` included.

Everything here is pure: the clock, the reader and the session store are passed in, so tests use a fake clock and a
fake fetch.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from loopctl import stop

FRESH_S = 60  # an irreversible step needs a read at most this old
IDLE_S = 120  # any tool call re-reads when the last read is older than this
READ_TIMEOUT_S = 15  # a read that takes longer fails (fail closed)
MAX_FAILURES = 3  # this many failed reads in a row count as stopped

KINDS = stop.STOP_KINDS  # every unit kind the reducer knows, reconcile included
UNIT = stop.UNIT
STOPPED_HINT = "the loop is paused: run `loopctl finish --stopped` (it records your handoff), then end"


class StopUnreadable(Exception):
    """The state repo's `STOP` couldn't be read in time."""


StopLine = stop.Line


def parse_stop(text: str) -> list[StopLine]:
    """Every non-blank line. A line that doesn't parse counts as `pause` (fail closed)."""
    return stop.parse(text)


def matching(text: str, kind: str | None, unit: str | None) -> list[StopLine]:
    """The lines that stop this session; an unidentified session is matched by every line (fail closed)."""
    return stop.matching_closed(stop.parse(text), kind, unit)


@dataclass
class Session:
    """What the guard remembers about one session between hook calls (hooks are separate processes)."""

    kind: str | None = None
    unit: str | None = None
    last_read_ns: int | None = None  # time of the last successful read
    stop_text: str = ""  # STOP as last read
    failures: int = 0  # failed reads in a row
    stopped: str | None = None  # why the session is stopped (sticky), or None

    @classmethod
    def load(cls, data: dict[str, Any] | None) -> Session:
        if not isinstance(data, dict):
            return cls()
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        try:
            return cls(**known)
        except TypeError:
            return cls()

    def dump(self) -> dict[str, Any]:
        return asdict(self)


class Store(Protocol):
    def load(self, session_id: str) -> dict[str, Any] | None: ...

    def save(self, session_id: str, data: dict[str, Any]) -> None: ...


class MemoryStore:
    """A session store in memory (tests)."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}

    def load(self, session_id: str) -> dict[str, Any] | None:
        return self.data.get(session_id)

    def save(self, session_id: str, data: dict[str, Any]) -> None:
        self.data[session_id] = dict(data)


Reader = Callable[[float], str]  # (timeout in seconds) -> STOP's text ("" when there is no STOP); raises on failure


@dataclass
class Gate:
    """The STOP gate for one hook call. `check()` returns None to let the call through, else the refusal."""

    reader: Reader
    store: Store
    now_ns: Callable[[], int]
    session_id: str = "unknown"
    env_kind: str | None = None  # AGENT_LOOP_KIND / AGENT_LOOP_UNIT, when the environment pins them
    env_unit: str | None = None
    claim_refused: Callable[[], str | None] | None = None  # why loopctl refused this session's lease claim, or None
    reads: list[int] = field(default_factory=list)  # times of reads attempted in this call (tests)

    def session(self) -> Session:
        session = Session.load(self.store.load(self.session_id))
        if self.env_kind:
            session.kind = self.env_kind
        if self.env_unit:
            session.unit = self.env_unit
        return session

    def identify(self, kind: str, unit: str) -> str | None:
        """Record the worker's kind and unit (first one wins). None when recorded or unchanged, else why not."""
        if kind not in KINDS or not UNIT.fullmatch(unit):
            return f"`loopctl guard identify` takes a kind ({', '.join(KINDS)}) and a unit such as issue-12"
        session = self.session()
        if session.kind is not None or session.unit is not None:
            if (session.kind, session.unit) == (kind, unit):
                return None
            return (f"this session is already identified as {session.kind} {session.unit}; "
                    "a session works on one unit")  # fmt: skip
        session.kind, session.unit = kind, unit
        self.store.save(self.session_id, session.dump())
        return None

    def _read(self, session: Session) -> bool:
        """One read. Updates the session; True when it succeeded."""
        self.reads.append(self.now_ns())
        start = self.now_ns()
        try:
            text = self.reader(READ_TIMEOUT_S)
            if (self.now_ns() - start) / 1e9 > READ_TIMEOUT_S:
                raise StopUnreadable(f"reading STOP took more than {READ_TIMEOUT_S} s")
        except Exception:  # noqa: BLE001  any failure is a failed read (fail closed)
            session.failures += 1
            if session.failures >= MAX_FAILURES and session.stopped is None:
                session.stopped = f"STOP couldn't be read {MAX_FAILURES} times in a row"
            return False
        session.failures = 0
        session.last_read_ns = self.now_ns()
        session.stop_text = text
        return True

    def _age_s(self, session: Session) -> float:
        if session.last_read_ns is None:
            return float("inf")
        return (self.now_ns() - session.last_read_ns) / 1e9

    def check(self, irreversible: bool, finish_stopped: bool) -> str | None:
        session = self.session()
        try:
            if session.stopped is None and self.claim_refused is not None:
                lost = self.claim_refused()
                if lost:
                    session.stopped = f"lease claim refused: {lost}"
            if session.stopped is None:
                if irreversible and self._age_s(session) > FRESH_S:
                    if not self._read(session) and session.stopped is None:
                        return (f"the guard couldn't read the loop's STOP file within {READ_TIMEOUT_S} s, so this "
                                "irreversible step is refused (fail closed); try again in a moment")  # fmt: skip
                elif self._age_s(session) > IDLE_S:
                    self._read(session)
            if session.stopped is None:
                hits = matching(session.stop_text, session.kind, session.unit)
                general = [h for h in hits if h.everything]
                if general or (hits and session.kind is not None and session.unit is not None):
                    session.stopped = f"STOP says `{(general or hits)[0].line}`"
                elif hits and not finish_stopped:
                    # Lines for particular kinds or units, and this session hasn't said which it is: refuse this call
                    # (fail closed) without stopping the session, so it can identify itself.
                    return (f"STOP holds `{hits[0].line}` and this session hasn't identified itself: run "
                            "`loopctl guard identify <kind> <unit>` (the first command in your brief) first")  # fmt: skip
            if session.stopped is not None and not finish_stopped:
                return f"{STOPPED_HINT} ({session.stopped})"
            return None
        finally:
            self.store.save(self.session_id, session.dump())
