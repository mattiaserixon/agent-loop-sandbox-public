"""Review findings are records; their route is computed (phase2-design §9.2, decision record 0010).

| Finding | Route |
|---|---|
| `introduced` with severity ≥ `review.blocking_min_severity` (medium), or any `regression` | **blocking** |
| severity ≥ high anywhere in a file the PR changed | **blocking** |
| severity low, or in `scope.excluded_paths` below high | **note** (§9.5) |
| `preexisting`, severity ≥ medium, path not excluded | **issue** (for triage; not blocking) |

`review_submit` validates a review worker's submission (the `loopctl review submit` payload) and returns the routes,
so the worker knows which findings to file as issues. The verdict is never the reviewer's: see verdict.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from loopctl import schema
from loopctl.risk import _get, any_match

SEVERITY = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def at_least(severity: str, floor: str) -> bool:
    return SEVERITY[severity] >= SEVERITY[floor]


def classify(finding: Mapping[str, Any], changed: Sequence[str], cfg: Any) -> str:
    sev, kind, path = finding["severity"], finding["introduced"], finding["path"]
    if kind == "regression":
        return "blocking"
    if kind == "introduced" and at_least(sev, _get(cfg, "review.blocking_min_severity")):
        return "blocking"
    if at_least(sev, "high") and path in set(changed):
        return "blocking"
    excluded = any_match(path, _get(cfg, "scope.excluded_paths"))
    if sev == "low" or (excluded and not at_least(sev, "high")):
        return "note"
    if kind == "preexisting" and at_least(sev, "medium"):
        return "issue"
    return "note"  # introduced below the blocking floor (when the floor is raised)


def open_blocking(unit: Mapping[str, Any]) -> list[str]:
    """Ids of the unit's blocking findings that are still open (not fixed, verified or waived)."""
    return sorted(fid for fid, f in unit.get("findings", {}).items() if f["route"] == "blocking" and f["status"] == "open")


@dataclass(frozen=True)
class Submission:
    payload: dict[str, Any]  # the review.submitted payload
    routes: dict[str, str]  # finding id -> route


def review_submit(doc: Mapping[str, Any], unit: Mapping[str, Any], cfg: Any) -> Submission:
    """Validate a review submission `{pr, head, round, findings}` against the unit; raise ValueError if it can't be
    accepted (wrong head or round, duplicate ids, a finding that fails the schema)."""
    payload = {"pr": doc.get("pr"), "head": doc.get("head"), "round": doc.get("round"),
               "findings": list(doc.get("findings", []))}  # fmt: skip
    extra = set(doc) - {"pr", "head", "round", "findings"}
    if extra:
        raise ValueError(f"unknown field(s) in the review: {sorted(extra)}")
    for f in payload["findings"]:
        try:
            schema.validate(f, "finding")
        except schema.SchemaError as exc:
            raise ValueError(str(exc)) from exc
    ids = [f["id"] for f in payload["findings"]]
    if len(ids) != len(set(ids)) or set(ids) & set(unit.get("findings", {})):
        raise ValueError("finding ids must be new and unique")
    if payload["pr"] != unit["pr"] or payload["head"] != unit["head"]:
        raise ValueError(f"the review is of PR {payload['pr']} at {payload['head']}; the unit is PR {unit['pr']} at "
                         f"{unit['head']}")  # fmt: skip
    if payload["round"] != unit["round"] + 1:
        raise ValueError(f"this is round {unit['round'] + 1}, not {payload['round']}")
    routes = {f["id"]: classify(f, unit.get("changed", []), cfg) for f in payload["findings"]}
    return Submission(payload, routes)
