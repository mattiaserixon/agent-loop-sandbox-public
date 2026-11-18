"""The computed verdict, round caps, and the merge preconditions as one pure function (phase2-design §9.1, §9.2,
§9.4; decision records 0009, 0010; invariants I3, I7).

- `compute(unit)`: `approved` exactly when no blocking finding is open on the unit's head; else `changes`.
- `round_cap(unit, cfg)`: the tier's cap plus rounds the owner granted (`extra_rounds`, a GitHub-confirmed answer).
- `cap_question(unit, cfg)`: at the cap with a blocking finding open, the fixed-option question (§9.1).
- `check_merge(...)`: preconditions 1–6 of §9.4 on the exact head. The reducer (at `merge.done`), `loopctl gate`
  (M0.10) and the verify workflow's script (M0.11) all call this one function.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from loopctl import clock
from loopctl.findings import open_blocking
from loopctl.risk import cap

CAP_OPTIONS = [
    {"id": "one_more_round", "label": "One more review round", "effect": "allows one more fix and review round",
     "widens": True},
    {"id": "split", "label": "Split the PR", "effect": "closes this unit; the PR is split by hand", "widens": False},
    {"id": "close", "label": "Close it", "effect": "closes the unit and stops work on it", "widens": False},
    {"id": "waive", "label": "Merge with a waiver of named findings",
     "effect": "the named findings are waived after a GitHub confirmation; then the unit can be gated", "widens": True},
]  # fmt: skip
ALL_PRECONDITIONS = (1, 2, 3, 4, 5, 6)
VERIFY_PRECONDITIONS = (1, 2, 4, 6)  # what the verify workflow re-checks from the store (§9.4, §10.2)


def compute(unit: Mapping[str, Any]) -> str:
    return "changes" if open_blocking(unit) else "approved"


def round_cap(unit: Mapping[str, Any], cfg: Any) -> int:
    return cap(unit.get("tier") or "normal", cfg) + int(unit.get("extra_rounds", 0))


def cap_question(unit: Mapping[str, Any], cfg: Any) -> dict[str, Any] | None:
    """The `question.opened` payload when the unit is at its cap with a blocking finding open, else None."""
    if unit["state"] != "changes" or unit["round"] < round_cap(unit, cfg) or not open_blocking(unit):
        return None
    return {"question_id": "q-" + clock.ulid(), "kind": "cap_reached", "options": CAP_OPTIONS, "default_option": None,
            "due_at": clock.stamp()}  # fmt: skip


@dataclass(frozen=True)
class MergeCheck:
    failures: list[tuple[int, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def reason(self) -> str:
        return "; ".join(f"({n}) {r}" for n, r in self.failures) or "all merge preconditions hold"


def check_merge(unit: Mapping[str, Any], *, pr_head: str, cfg: Any, main_sha: str | None = None,
                tested: str | None = None, main_green: bool | None = None, gate_command_sha256: str | None = None,
                approvals: Iterable[str] | None = None,
                checks: Iterable[int] = ALL_PRECONDITIONS) -> MergeCheck:  # fmt: skip
    """§9.4 on head `pr_head` (the PR's head now). `main_sha`: main now (the gate's base must equal it: main hasn't
    moved since T was built); `tested`: the T being merged; `gate_command_sha256`: main's gate command hash (a result
    from another command is refused); `approvals`: heads the owner approved app-free (default: the unit's)."""
    want = set(checks)
    out: list[tuple[int, str]] = []
    head = unit.get("head")
    if 1 in want:
        if head != pr_head:
            out.append((1, f"the PR's head is {pr_head[:12]}, the store's is {str(head)[:12]}"))
        elif unit.get("approved_head") != pr_head:
            out.append((1, f"no approving verdict on {pr_head[:12]}"))
        elif unit.get("state") not in ("approved", "gating"):
            out.append((1, f"the unit is {unit.get('state')}, not approved"))
    if 2 in want:
        blocking = open_blocking(unit)
        if blocking:
            out.append((2, f"{len(blocking)} open blocking finding(s): {', '.join(blocking[:3])}"))
    if 3 in want and unit.get("round", 0) > round_cap(unit, cfg):
        out.append((3, f"round {unit['round']} is over the cap {round_cap(unit, cfg)}"))
    if 4 in want:
        gate = unit.get("gate")
        if not gate or gate["head"] != pr_head:
            out.append((4, f"no gate result for {pr_head[:12]}"))
        elif gate["result"] != "pass":
            out.append((4, "the gate failed"))
        elif main_sha is not None and gate["base"] != main_sha:
            out.append((4, f"main moved: the gate tested on {gate['base'][:12]}, main is {main_sha[:12]}"))
        elif tested is not None and gate["tested"] != tested:
            out.append((4, f"the merge is of {tested[:12]}, the gate tested {gate['tested'][:12]}"))
        elif gate_command_sha256 is not None and gate["gate_command_sha256"] != gate_command_sha256:
            out.append((4, "the gate result was made by a different gate command than main's"))
    if 5 in want and main_green is not True:
        out.append((5, "main is not known to be green"))
    if 6 in want and (unit.get("gate_files") or unit.get("critical")):
        approved = set(unit.get("approvals", []) if approvals is None else approvals)
        if pr_head not in approved:
            what = "touches gate files or .github/" if unit.get("gate_files") else "is a critical change"
            out.append((6, f"the PR {what} and has no owner approval of {pr_head[:12]}"))
    return MergeCheck(out)
