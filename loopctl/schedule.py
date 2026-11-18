"""The scheduling order (phase2-design §4.3: code, not config).

0. the post-merge check of main's sha (`check-…`) and, when main is red, its revert PR (`revert-…`) (§10.6);
1. units in `gating` or `approved` (finish what is nearly merged): a gate;
2. own PRs in `changes` (fix rounds), and `building` units whose worker died;
3. `in_review` re-reviews;
4. new issues waiting for triage (a P0 can't be seen as P0 before it is triaged; triage is the cheapest unit), and
   the daily reconcile unit;
5. accepted P0 issues;
6. the owner's explicit order (`work.order`);
7. other accepted issues by priority, then age (unit key number as the age proxy: lower = older),
   only when `work.scope = all`, and never below `work.min_priority`.

Skipped: units holding a lease, in `needs_owner`, `closed` or `merged`, out of attempts, and units whose last worker
ended without progress at this same step (`stalled`, no hot loop).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loopctl.leases import MAX_ATTEMPTS
from loopctl.risk import _get

KIND_FOR_STATE = {"gating": "gate", "approved": "gate", "changes": "fix", "building": "fix", "in_review": "review",
                  "new": "triage", "queued": "fix"}  # fmt: skip
PRIORITY = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, None: 4}


@dataclass(frozen=True)
class Candidate:
    unit: str
    kind: str
    rank: int  # 1–7 as in the module docstring
    why: str


def _number(key: str) -> int:
    tail = key.split("-")[1]
    return int(tail) if tail.isdigit() else 0


def candidates(state: Mapping[str, Any], cfg: Any) -> list[Candidate]:
    order = list(_get(cfg, "work.order"))
    listed = _get(cfg, "work.scope") == "listed"
    floor = PRIORITY[_get(cfg, "work.min_priority")]
    out: list[tuple[tuple[Any, ...], Candidate]] = []
    for key, u in state["units"].items():
        st = u["state"]
        if u.get("lease") or st not in KIND_FOR_STATE or u["attempt"] >= MAX_ATTEMPTS or u.get("stalled") == st:
            continue
        kind = KIND_FOR_STATE[st]
        if u["kind"] in ("check", "revert") and st in ("queued", "building"):
            kind, rank, why = u["kind"], 0, "post-merge check of main" if u["kind"] == "check" else "main is red: revert"
        elif u["kind"] == "check":
            continue
        elif u["kind"] == "reconcile":  # the daily store-vs-GitHub check (G6): cheap, ranked with triage
            if st not in ("queued", "building"):
                continue
            kind, rank, why = "reconcile", 4, "daily reconcile"
        elif st in ("gating", "approved"):
            rank, why = 1, "nearly merged"
        elif st in ("changes", "building"):
            rank, why = 2, "fix round of an own PR"
        elif st == "in_review":
            rank, why = 3, "review"
        elif st == "new":
            if u["kind"] != "fix":
                continue
            rank, why = 4, "triage"
        else:  # queued
            n = _number(key)
            if u.get("priority") == "P0":
                rank, why = 5, "P0"
            elif key.startswith("issue-") and n in order:
                rank, why = 6, f"work.order position {order.index(n) + 1}"
            elif listed:
                continue  # work.scope = listed: only work.order (and P0) is taken
            elif PRIORITY[u.get("priority")] > floor:
                continue
            else:
                rank, why = 7, f"accepted {u.get('priority') or 'unprioritised'}"
        sort_key: tuple[Any, ...] = (rank,)
        if rank == 6:
            sort_key += (order.index(_number(key)),)
        elif rank == 7:
            sort_key += (PRIORITY[u.get("priority")], _number(key))
        else:
            sort_key += (_number(key),)
        out.append((sort_key + (key,), Candidate(key, kind, rank, why)))
    return [c for _, c in sorted(out, key=lambda x: x[0])]


def idle_listed(state: Mapping[str, Any], cfg: Any) -> list[str]:
    """Accepted issues that wait only because `work.scope = listed` and `work.order` doesn't name them (Phase 1 idle
    windows I1, I2): the tick raises a question instead of sitting idle."""
    if _get(cfg, "work.scope") != "listed":
        return []
    order = set(_get(cfg, "work.order"))
    return sorted((k for k, u in state["units"].items() if u["state"] == "queued" and not u.get("lease")
                   and u.get("priority") != "P0" and _number(k) not in order), key=_number)  # fmt: skip
