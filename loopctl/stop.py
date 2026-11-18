"""The `STOP` file on the state repo's `main` (phase2-design §4.5, decision record 0026).

One line per stop:

    pause                    everything
    pause <kind> [<kind>…]   these kinds (fix, review, gate, triage, scan, build, reconcile, check, revert)
    pause budget             everything (written by the controller at the budget's hard cap)
    stop <unit>              one unit (issue-12, pr-40, …)

Blank lines say nothing. **Any other line counts as `pause`** (fail closed), comments included: a typo can stop too
much, never too little. Writers only ever add lines; removing one widens authority and needs a confirmed `resume`;
a tick that finds a line removed otherwise puts it back and raises a needs-you item (loopctl/commands.py,
`removed_lines`; proof test T-STOP).

Two ways to match a worker: `matching` (the controller, which knows each unit's kind) and `matching_closed` (the
guard, where a session that hasn't identified itself is matched by every line: fail closed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from loopctl.config import KINDS

STOP_KINDS = (*KINDS, "reconcile", "check", "revert")  # check: post-merge check of main; revert: its revert PR
UNIT = re.compile(r"^(issue|pr|scan|build|reconcile)-[0-9]{1,8}$|^(check|revert)-[0-9a-f]{12}$")
FILE = "STOP"


@dataclass(frozen=True)
class Line:
    line: str  # as written (stripped)
    kinds: tuple[str, ...] = ()  # empty with unit None: everything
    unit: str | None = None
    valid: bool = True

    @property
    def everything(self) -> bool:
        return not self.kinds and self.unit is None

    def matches(self, kind: str | None, unit: str | None) -> bool:
        if self.everything:
            return True
        if self.unit is not None:
            return unit == self.unit
        return kind in self.kinds


def parse_line(raw: str) -> Line | None:
    text = raw.strip()
    if not text:
        return None
    words = text.split()
    if words == ["pause"] or words == ["pause", "budget"]:
        return Line(text)
    if words[0] == "pause" and all(w in STOP_KINDS for w in words[1:]) and len(set(words[1:])) == len(words) - 1:
        return Line(text, kinds=tuple(k for k in STOP_KINDS if k in words[1:]))
    if words[0] == "stop" and len(words) == 2 and UNIT.match(words[1]):
        return Line(text, unit=words[1])
    return Line(text, valid=False)  # fail closed: counts as `pause`


def parse(text: str | None) -> list[Line]:
    """Every stop line in `text` (None: no STOP file). Undecodable or unparseable content fails closed."""
    if text is None:
        return []
    return [line for raw in text.splitlines() if (line := parse_line(raw)) is not None]


def matching(lines: list[Line], kind: str | None, unit: str | None) -> str | None:
    """The first line that stops `kind`/`unit`, or None."""
    for line in lines:
        if line.matches(kind, unit):
            return line.line
    return None


def matches_closed(line: Line, kind: str | None, unit: str | None) -> bool:
    """`line.matches`, except an unknown kind or unit is matched by any kind or unit line (the guard's rule)."""
    if line.everything:
        return True
    if line.unit is not None:
        return unit is None or unit == line.unit
    return kind is None or kind in line.kinds


def matching_closed(lines: list[Line], kind: str | None, unit: str | None) -> list[Line]:
    return [line for line in lines if matches_closed(line, kind, unit)]


def paused_everything(lines: list[Line]) -> bool:
    return any(line.everything for line in lines)
