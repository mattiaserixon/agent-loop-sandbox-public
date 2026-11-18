"""Admission: which queued units start now (phase2-design §3.2 `lease.granted` guard, §4.1 step 5, §7.2; I2, I9).

`plan()` walks the §4.3 order (schedule.py) and admits a unit only if every rule lets it, granting its lease (one
CAS commit with the reduced state, store.py). Every refusal names the rule and the setting that would change it
(§8.3 provenance; status need (3): "for every idle slot, the rule that holds it and the setting that would change
it"). Budget, pauses and capacity never change a unit's state, only whether it starts now (§3.1).

Rules, in the order they are checked:
1. the mode starts sessions (`run.mode`; shadow decides and records but grants nothing);
2. `run.paused`; a `STOP` line for the kind or unit (decision record 0026; the predicate comes from stop.py, M0.7);
3. quiet hours (`schedule.quiet_hours`, `schedule.night`): `stop` starts nothing, `drain` only in-flight PRs;
4. the kind is in `work.kinds` and not in `run.paused_kinds`; a platform limit doesn't block it (budget signals);
5. a gate needs main not known red (`state.main.green`), except the gate of the revert PR that makes it green;
6. scope: no excluded label, not only excluded paths (I9, through `leases.grant_in_scope`);
7. capacity: active leases < `capacity.max_sessions` (I2);
8. budget: the projected cost fits the window room and the week room (I2).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from loopctl import budget, clock, leases
from loopctl.config import STARTS_SESSIONS
from loopctl.reducer import Policy, reduce
from loopctl.risk import _get
from loopctl.schedule import candidates, idle_listed

LOOP_KINDS = ("reconcile", "check", "revert")  # the loop's own checks and repairs, not work kinds (§10.6, G6)
IN_FLIGHT = ("gating", "approved", "changes", "building", "in_review")
IDLE_OPTIONS = [
    {"id": "extend_order", "label": "Add issues to work.order", "effect": "the named issues are taken in order",
     "widens": False},
    {"id": "scope_all", "label": "Take all accepted issues", "effect": "sets work.scope = all", "widens": True},
    {"id": "wait", "label": "Keep waiting", "effect": "the queue stays idle until work.order changes", "widens": False},
]  # fmt: skip


@dataclass(frozen=True)
class Decision:
    unit: str
    kind: str
    admitted: bool
    rule: str  # what decided it
    key: str | None = None  # the setting that would change it
    value: Any = None
    cost: float | None = None


@dataclass
class Plan:
    state: dict[str, Any]
    grants: list[tuple[leases.Grant, str]] = field(default_factory=list)  # (grant, unit)
    decisions: list[Decision] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)  # question.opened payloads to raise
    rooms: budget.Rooms | None = None
    starts: bool = True  # False in off/shadow: decisions are what it *would* do


def in_quiet_hours(cfg: Any, now: datetime) -> bool:
    hours = _get(cfg, "schedule.quiet_hours")
    if not hours:
        return False
    hhmm = now.astimezone(ZoneInfo(hours["tz"])).strftime("%H:%M")
    start, end = hours["from"], hours["to"]
    return start <= hhmm < end if start <= end else (hhmm >= start or hhmm < end)


def plan(state: Mapping[str, Any], cfg: Any, *, epoch: int, snapshot: Mapping[str, Mapping[str, Any]] | None = None,
         stopped: Callable[[str, str], str | None] | None = None, now: datetime | None = None) -> Plan:  # fmt: skip
    """Admit units in order. `snapshot`: unit key -> {"labels": [...], "paths": [...]} from the GitHub read;
    `stopped(kind, unit)` -> the matching `STOP` line or None."""
    now = now or clock.now()
    snapshot = snapshot or {}
    policy = Policy(cfg=cfg)
    s = reduce(state, [], policy).state
    mode = _get(cfg, "run.mode")
    out = Plan(s, starts=STARTS_SESSIONS[mode])
    quiet = in_quiet_hours(cfg, now)
    night = _get(cfg, "schedule.night")
    kinds, paused_kinds = set(_get(cfg, "work.kinds")), set(_get(cfg, "run.paused_kinds"))
    cap = int(_get(cfg, "capacity.max_sessions"))
    for c in candidates(s, cfg):
        u = s["units"][c.unit]
        rooms = budget.rooms(s, cfg, now)
        out.rooms = rooms
        cost = budget.projected(s, c.kind, u.get("tier"))

        def refuse(rule: str, key: str | None = None, value: Any = None, c=c, cost=cost) -> None:
            out.decisions.append(Decision(c.unit, c.kind, False, rule, key, value, cost))

        line = stopped(c.kind, c.unit) if stopped else None
        info = snapshot.get(c.unit, {})
        if _get(cfg, "run.paused"):
            refuse("paused", "run.paused", True)
        elif line:
            refuse(f"STOP line {line!r}", "STOP", line)
        elif quiet and night == "stop":
            refuse("quiet hours: night = stop", "schedule.night", night)
        elif quiet and night == "drain" and u["state"] not in IN_FLIGHT:
            refuse("quiet hours: night = drain (only in-flight PRs)", "schedule.night", night)
        elif c.kind not in kinds and c.kind not in LOOP_KINDS:
            refuse(f"{c.kind} is not in work.kinds", "work.kinds", sorted(kinds))
        elif c.kind in paused_kinds:
            refuse(f"{c.kind} is paused", "run.paused_kinds", sorted(paused_kinds))
        elif c.kind in rooms.blocked:
            refuse("; ".join(rooms.reasons) or "platform limit", None, None)
        elif c.kind == "gate" and s["main"]["green"] is False and u["kind"] != "revert":
            refuse("main is red: no merges until it is green", None, None)
        elif (why := _excluded(info, cfg)) is not None:
            refuse(f"out of scope: {why}", "scope.excluded_labels" if "label" in why else "scope.excluded_paths", None)
        elif len(leases.active(s)) >= cap:
            refuse(f"capacity: {len(leases.active(s))} of {cap} sessions busy", "capacity.max_sessions", cap)
        elif not rooms.fits(cost):
            short = "budget.window" if cost > rooms.window else "budget.week"
            room = rooms.window if short == "budget.window" else rooms.week
            refuse(f"budget: projected ${cost:.2f} > ${max(room, 0):.2f} left in the {short.split('.')[1]}", short,
                   _get(cfg, short))  # fmt: skip
        elif not out.starts:
            out.decisions.append(Decision(c.unit, c.kind, False, f"run.mode = {mode}: decides only, starts nothing",
                                          "run.mode", mode, cost))  # fmt: skip
        else:
            g = leases.grant_in_scope(s, c.unit, c.kind, epoch=epoch, labels=list(info.get("labels", [])),
                                      paths=list(info.get("paths", [])), cfg=cfg)  # fmt: skip
            result = reduce(s, [g.event], policy)
            if result.rejected:  # e.g. the round cap (I7): the reducer has the last word
                refuse(f"refused: {result.rejected[0][1]}", None, None)
                continue
            s = result.state
            out.grants.append((g, c.unit))
            out.decisions.append(Decision(c.unit, c.kind, True, c.why, None, None, cost))
    out.state = s
    out.rooms = budget.rooms(s, cfg, now)
    idle = idle_listed(s, cfg)
    asked = any(any(o["id"] == "scope_all" for o in q["options"]) for q in s["questions"].values())
    if idle and not asked and len(leases.active(s)) < cap and out.starts:  # a slot idles only because of the list
        out.questions.append({"question_id": "q-" + clock.ulid(), "kind": "other", "options": IDLE_OPTIONS,
                              "default_option": "wait", "due_at": clock.stamp_at(now)})  # fmt: skip
    return out


def _excluded(info: Mapping[str, Any], cfg: Any) -> str | None:
    from loopctl.risk import excluded

    return excluded(info.get("labels", []), info.get("paths", []), cfg)
