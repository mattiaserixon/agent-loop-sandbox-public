"""The per-tick decision log (M2's exit: "every decision that differs from v4's explained", proof-plan K-RECON).

Each tick that holds the controller lease records, in its `tick.done` event (one writer, the controller), what it
decided for every unit it looked at: per candidate of the §4.3 order what it started (or, in shadow and off, would
have started) or refused and the rule that decided it, then the open units that weren't candidates and why (held).
The log is compact and bounded: at most `MAX_ENTRIES` entries per tick (candidates first, in pick order, then held
units by key), each `why` at most `WHY_MAX` characters; what doesn't fit is counted in `decided_omitted`. It is an
event, so its retention is the history's (phase2-design §5.4: raw events 90 days, then the monthly archive).

Rule codes (`RULES`): the admission rule that refused (admission.py's order), `shadow` for "this mode decides only"
(the unit passed every other rule: it would have started), `admitted` for a start, and for held units the reason the
§4.3 order skipped them. `loopctl status --json` shows the last ticks' entries (`decisions`), and
`loopctl compare-v4` pairs them with v4's actions.

Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from loopctl.leases import MAX_ATTEMPTS
from loopctl.risk import _get
from loopctl.schedule import KIND_FOR_STATE, PRIORITY, candidates

MAX_ENTRIES = 60
WHY_MAX = 160
RECENT_TICKS = 3  # ticks shown by `loopctl status --json`
KINDS = ("fix", "review", "gate", "triage", "scan", "build", "reconcile", "check", "revert")
DECISIONS = ("start", "would_start", "refused", "held")
RULES = (
    # a start
    "admitted",
    # admission's rules, in its order (admission.py)
    "paused", "stop", "quiet_hours", "kind", "platform_limit", "main_red", "scope", "capacity", "budget_window",
    "budget_week", "round_cap", "refused", "brief", "shadow",
    # held: open units the §4.3 order skips
    "needs_owner", "attempts", "stalled", "not_listed", "below_min_priority", "not_eligible",
)  # fmt: skip
# The setting that would change a held unit's decision.
HELD_SETTING = {"not_listed": "work.order", "below_min_priority": "work.min_priority"}


def code_of(rule: str, key: str | None) -> str:
    """The rule code of an admission.Decision's text and setting (admission.plan writes both)."""
    if key == "run.mode":
        return "shadow"
    if key == "run.paused" or key == "run.paused_kinds":
        return "paused"
    if key == "STOP":
        return "stop"
    if key == "schedule.night":
        return "quiet_hours"
    if key == "work.kinds":
        return "kind"
    if key in ("scope.excluded_labels", "scope.excluded_paths"):
        return "scope"
    if key == "capacity.max_sessions":
        return "capacity"
    if key == "budget.window":
        return "budget_window"
    if key == "budget.week":
        return "budget_week"
    if rule.startswith("main is red"):
        return "main_red"
    if rule.startswith("refused:"):
        return "round_cap" if "review rounds, the cap is" in rule else "refused"
    return "platform_limit"


def _entry(unit: str, kind: str, decision: str, rule: str, why: str | None, setting: str | None,
           pr: Any) -> dict[str, Any]:  # fmt: skip
    e: dict[str, Any] = {"unit": unit, "kind": kind, "decision": decision, "rule": rule}
    if why:
        e["why"] = why[:WHY_MAX]
    if setting:
        e["setting"] = setting
    if isinstance(pr, int):
        e["pr"] = pr
    return e


def held_reason(unit: Mapping[str, Any], cfg: Any, questions: Mapping[str, Any]) -> tuple[str, str | None]:
    """(rule code, why) for an open unit without a lease that the §4.3 order doesn't offer."""
    st = unit["state"]
    if st == "needs_owner":
        kinds = sorted({q["kind"] for q in questions.values() if q.get("unit") == unit["key"]})
        return "needs_owner", ("waiting for the owner: " + ", ".join(kinds)) if kinds else "waiting for the owner"
    if unit["attempt"] >= MAX_ATTEMPTS:
        return "attempts", f"used its {MAX_ATTEMPTS} attempts"
    if unit.get("stalled") == st:
        return "stalled", f"its last worker ended without progress in {st}"
    if st == "queued" and unit["key"].startswith("issue-"):
        if _get(cfg, "work.scope") == "listed" and unit.get("priority") != "P0":
            return "not_listed", "work.scope = listed and work.order doesn't name it"
        if PRIORITY[unit.get("priority")] > PRIORITY[_get(cfg, "work.min_priority")]:
            return "below_min_priority", f"{unit.get('priority') or 'unprioritised'} is below work.min_priority"
    return "not_eligible", f"{unit['kind']} unit in {st}"


def entries(plan: Any, state: Mapping[str, Any], cfg: Any, *, starts: Iterable[str] = ()) -> tuple[list[dict[str, Any]], int]:  # fmt: skip  # noqa: E501
    """The tick's log entries and how many didn't fit. `plan`: the tick's admission.Plan (None: nothing was decided,
    e.g. GitHub unreadable); `state`: the state after the tick; `starts`: units given a start action (a granted unit
    whose brief needed the owner got none, so its grant is shown refused with rule `brief`)."""
    if plan is None:
        return [], 0
    started = set(starts)
    units = state["units"]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in plan.decisions:
        seen.add(d.unit)
        pr = units.get(d.unit, {}).get("pr")
        if d.admitted and d.unit not in started:
            q = units.get(d.unit, {}).get("state")
            out.append(_entry(d.unit, d.kind, "refused", "brief", f"granted, but not started (unit is {q})", None, pr))
        elif d.admitted:
            out.append(_entry(d.unit, d.kind, "start", "admitted", d.rule, None, pr))
        else:
            code = code_of(d.rule, d.key)
            decision = "would_start" if code == "shadow" else "refused"
            setting = d.key if d.key not in (None, "run.mode") else None
            out.append(_entry(d.unit, d.kind, decision, code, None if code == "shadow" else d.rule, setting, pr))
    offered = {c.unit for c in candidates(plan.state, cfg)} | seen
    for key, u in sorted(units.items()):
        if key in offered or u.get("lease") or u["state"] in ("merged", "closed"):
            continue
        code, why = held_reason(u, cfg, state.get("questions", {}))
        kind = KIND_FOR_STATE.get(u.get("prior_state") or u["state"], u["kind"])
        out.append(_entry(key, kind if kind in KINDS else u["kind"], "held", code, why, HELD_SETTING.get(code),
                          u.get("pr")))  # fmt: skip
    return out[:MAX_ENTRIES], max(len(out) - MAX_ENTRIES, 0)


def ticks(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every tick.done that carries a decision log, oldest first: {tick_id, at, mode, decided, omitted}."""
    out = []
    for e in sorted(events, key=lambda x: str(x.get("id", ""))):
        if e.get("invalid") or e.get("type") != "tick.done" or "decided" not in e.get("payload", {}):
            continue
        p = e["payload"]
        out.append({"tick_id": p["tick_id"], "at": e["at"], "mode": p.get("mode"), "decided": list(p["decided"]),
                    "omitted": int(p.get("decided_omitted", 0))})  # fmt: skip
    return out


def recent(events: Sequence[Mapping[str, Any]], n: int = RECENT_TICKS) -> list[dict[str, Any]]:
    """The last `n` recorded ticks, newest first (the status's `decisions`)."""
    return list(reversed(ticks(events)[-n:]))
